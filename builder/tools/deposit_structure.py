"""The deposit's structure, read from its folder tree (#852).

A deposit's folders are the depositor's own statement of what its studies and
assays are: S-VHPS22 files ``study_01…/assay_01…04/``, WP2.3 files
``<study>/Assays/<assay>/``, S-VHPS26 files one ``Assay_<x>/``. Folder names
vary per deposit, so no fixed layout and no name rule decides it. Instead the
tree is summarised here (:func:`tree_summary`), one bounded model call labels it
(:func:`builder.agents.pipeline.leaves.derive_deposit_structure`), and the
labelling is accepted only when :func:`check_structure` finds nothing wrong;
otherwise :func:`fallback_structure` — one study, one assay, at the root — is
what the build keeps, and :func:`describe_structure` says so in one line.

A structure is a plain dict, the shape the model returns and the session saves::

    {"studies": [{"folder": "Exposure scenarios", "name": "Exposure scenarios",
                  "assays": [{"folder": "Exposure scenarios/Assays/LDH analysis",
                              "name": "LDH analysis"}, ...]}],
     "source": "model" | "fallback"}

Folders are paths relative to the input root, ``"."`` for the root itself.
This module reads no files: the tree comes from the scan inventory and the
README excerpts from the ranked documents, both already in ``CrateState``.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from builder.state import FileClassification
from builder.tools.document_discovery import (
    CLASS_METADATA,
    CLASS_PROTOCOL,
    _class_tally,
    _relative,
)

ROOT = "."

# Folders deeper than this are not listed, only counted on their listed
# ancestor's line; the real deposits' assay folders all sit at depth 2–4.
MAX_DEPTH = 4
# Folder lines in a summary; S-VHPS22's 1468 files fit in a quarter of this.
MAX_LINES = 300
# Of a README, enough to say what the folder is.
README_CHARS = 160
_DOCS_SHOWN = 4
_DATA_SHOWN = 3


def _norm(folder: Any) -> str:
    """A folder as the structure spells it: ``"."`` for the root, no slashes at either end."""
    text = str(folder).replace("\\", "/").strip().strip("/")
    while text.startswith("./"):
        text = text[2:]
    return text or ROOT


def _contains(folder: str, path: str) -> bool:
    """Whether *path* (a folder or a file, normalised) lies at or below *folder*."""
    return folder == ROOT or path == folder or path.startswith(folder + "/")


def _depth(folder: str) -> int:
    return 0 if folder == ROOT else len(PurePosixPath(folder).parts)


def _ancestors(folder: str) -> list[str]:
    """The folder and every folder above it, root last."""
    out = []
    while folder != ROOT:
        out.append(folder)
        folder = _norm(PurePosixPath(folder).parent)
    out.append(ROOT)
    return out


def _relatives(files: Sequence[FileClassification], input_root: str) -> list[str]:
    return [_relative(f.path, input_root).replace("\\", "/") for f in files]


def _readme_excerpts(documents: Sequence[Mapping[str, Any]] | None) -> dict[str, str]:
    """``{folder: excerpt}`` from the ranked documents' bounded previews."""
    excerpts: dict[str, str] = {}
    for doc in documents or []:
        relative = _norm(doc.get("relative_path") or "")
        path = PurePosixPath(relative)
        if not path.stem.lower().startswith("readme"):
            continue
        text = re.sub(r"\s+", " ", str(doc.get("preview") or "")).strip()
        if not text:
            continue
        excerpts.setdefault(_norm(path.parent), text[:README_CHARS])
    return excerpts


def tree_summary(
    files: Sequence[FileClassification],
    *,
    input_root: str,
    documents: Sequence[Mapping[str, Any]] | None = None,
    max_depth: int = MAX_DEPTH,
    max_lines: int = MAX_LINES,
) -> str:
    """One line per folder, root first, bounded in depth and length.

    A line carries the folder, its own files counted by classification, the
    names of up to four documents and three data files it holds directly, how
    many files sit in folders below the depth cap, and the first
    :data:`README_CHARS` of its README when the ranked *documents* read one. A
    folder with no file of its own is still a line: the model has to be able to
    name ``Exposure scenarios/Assays`` as what lies between a study and its
    assays. Past *max_lines* the remaining folders are counted in a trailer.
    """
    relatives = _relatives(files, input_root)
    direct: dict[str, list[FileClassification]] = {}
    below: dict[str, Counter[str]] = {}
    for relative, file in zip(relatives, files):
        folder = _norm(PurePosixPath(relative).parent)
        for ancestor in _ancestors(folder):
            direct.setdefault(ancestor, [])
        if _depth(folder) <= max_depth:
            direct[folder].append(file)
        else:
            listed = _norm(PurePosixPath(*PurePosixPath(folder).parts[:max_depth]))
            below.setdefault(listed, Counter())[folder] += 1

    excerpts = _readme_excerpts(documents)
    lines: list[str] = []
    for folder in sorted(direct, key=lambda f: (f != ROOT, f.casefold())):
        if _depth(folder) > max_depth:
            continue
        own = direct[folder]
        parts = [
            f"{len(own)} file{'s' if len(own) != 1 else ''}"
            + (f": {_class_tally(own)}" if own else "")
        ]
        docs = [f.filename for f in own if f.classification in (CLASS_METADATA, CLASS_PROTOCOL)]
        data = [f.filename for f in own if f.classification not in (CLASS_METADATA, CLASS_PROTOCOL)]
        for label, names, shown in (("docs", docs, _DOCS_SHOWN), ("data", data, _DATA_SHOWN)):
            if names:
                rest = f" (+{len(names) - shown})" if len(names) > shown else ""
                parts.append(f"{label}: {', '.join(names[:shown])}{rest}")
        if folder in below:
            deeper = below[folder]
            parts.append(f"{sum(deeper.values())} files below in {len(deeper)} deeper folders")
        if folder in excerpts:
            parts.append(f'README: "{excerpts[folder]}"')
        label = "./" if folder == ROOT else folder + "/"
        lines.append(f"{label} — " + " | ".join(parts))
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"+ {hidden} more folders not shown"]
    return "\n".join(lines)


def normalise_structure(structure: Mapping[str, Any]) -> dict[str, Any]:
    """The labelling as the session stores it: folders normalised, nothing else kept.

    A study is its assays, so a folder labelled a study with none — WP2.3's
    ``AOP/`` and ``Genetic susceptibility/``, which hold papers — is dropped
    here; its files are the Investigation's. Raises :class:`ValueError` when
    the shape is not studies of assays with a folder each;
    :func:`check_structure` turns that into a problem.
    """
    studies_in = structure.get("studies") if isinstance(structure, Mapping) else None
    if not isinstance(studies_in, list):
        raise ValueError("a structure is a list of studies")
    studies = []
    for study in studies_in:
        if not isinstance(study, Mapping) or not isinstance(study.get("folder"), str):
            raise ValueError("a study names its folder")
        assays_in = study.get("assays")
        if not isinstance(assays_in, list):
            raise ValueError("a study lists its assays")
        assays = []
        for assay in assays_in:
            if not isinstance(assay, Mapping) or not isinstance(assay.get("folder"), str):
                raise ValueError("an assay names its folder")
            assays.append(
                {"folder": _norm(assay["folder"]), "name": str(assay.get("name") or "").strip()}
            )
        if not assays:
            continue
        studies.append(
            {
                "folder": _norm(study["folder"]),
                "name": str(study.get("name") or "").strip(),
                "assays": assays,
            }
        )
    return {"studies": studies, "source": "model"}


def check_structure(
    structure: Mapping[str, Any], files: Sequence[FileClassification], *, input_root: str
) -> list[str]:
    """Everything wrong with a labelling; an empty list accepts it.

    Accepted means: every study and assay folder holds at least one scanned file
    (so it exists and is not an empty organisational folder), no assay lies
    inside another, every assay lies inside its study's folder, no folder is an
    assay twice, and there is at least one assay. A folder holding documents
    only is still an assay — S-VHPS27 deposits its two assays as README, SOP
    and metadata workbook with the data to follow.
    """
    try:
        normalised = normalise_structure(structure)
    except ValueError as exc:
        return [str(exc)]
    folders = {ROOT}
    for relative in _relatives(files, input_root):
        folders.update(_ancestors(_norm(PurePosixPath(relative).parent)))

    problems: list[str] = []
    assays: list[str] = []
    for study in normalised["studies"]:
        if study["folder"] not in folders:
            problems.append(f"study folder '{study['folder']}' holds no scanned file")
        for assay in study["assays"]:
            folder = assay["folder"]
            if folder not in folders:
                problems.append(f"assay folder '{folder}' holds no scanned file")
            elif not _contains(study["folder"], folder):
                problems.append(f"assay '{folder}' lies outside its study '{study['folder']}'")
            if folder in assays:
                problems.append(f"assay folder '{folder}' is listed twice")
            assays.append(folder)
    for folder in assays:
        for other in assays:
            if other != folder and _contains(other, folder):
                problems.append(f"assay '{folder}' lies inside assay '{other}'")
    if not assays:
        problems.append("no assay")
    return problems


def fallback_structure() -> dict[str, Any]:
    """One study and one assay at the root: what every build made before #852."""
    return {
        "studies": [{"folder": ROOT, "name": "", "assays": [{"folder": ROOT, "name": ""}]}],
        "source": "fallback",
    }


def describe_structure(structure: Mapping[str, Any]) -> str:
    """The one progress line: what the deposit was read as, or that it was not."""
    if structure.get("source") == "fallback":
        return "Could not read the deposit's structure; built one study with one assay."
    studies = structure.get("studies") or []
    parts = []
    for index, study in enumerate(studies):
        label = study.get("name") or (
            PurePosixPath(study["folder"]).name if study["folder"] != ROOT else "deposit"
        )
        count = len(study.get("assays") or [])
        unit = f" assay{'s' if count != 1 else ''}" if index == 0 else ""
        parts.append(f"{label} ({count}{unit})")
    noun = "study" if len(studies) == 1 else "studies"
    return f"Deposit read as: {len(studies)} {noun} — {', '.join(parts)}"
