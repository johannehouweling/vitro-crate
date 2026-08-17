"""LabProcess derivation-chain tools (Issue #88).

The paper's core value proposition is that a receiving lab can trace how an
output was produced:

    Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
           →[EndpointReadout]→ raw_measurements →[DataAnalysis]→ figures

The crate mapping resolves a process's ``object``/``result``/``input``/``output``
references, but those reference keys live behind the schema-less ``hints`` param,
invisible to a weak model — so the chain is never wired and the front half of the
provenance graph dangles. These tools give the agent explicit verbs:

- :func:`draft_file` — create a File data entity (the mapping renders File nodes
  but nothing created one before).
- :func:`link` — add a single provenance edge (``from --relation--> to``), with
  the relation drawn from :data:`PROVENANCE_RELATIONS`.
- :func:`check_provenance` — a **report-only** connectivity lint returning issues
  in #87's routable shape. It never auto-chains (branching assays make a fixed
  process order wrong); it only surfaces what the agent must wire.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import PROVENANCE_RELATIONS, _file_source
from builder.tools.drafters import _make_entity_id
from builder.tools.management import entity_not_found_message
from builder.tools.scanner import encoding_format_for_name

logger = logging.getLogger(__name__)

# Reference fields that carry a process's consumed (input) and produced (output)
# edges. Mirrors how _build_process reads them; kept here so the lint and the
# mapping agree on what an input/output edge is.
_INPUT_FIELDS: tuple[str, ...] = ("object", "input", "samples", "cell_line")
_OUTPUT_FIELDS: tuple[str, ...] = ("result", "output")

# Domain process types whose build mapping has NO synthesized output fallback
# (_build_process synthesizes a result for CellCulture and Exposure, but takes
# EndpointReadout/DataAnalysis results only from explicit fields). A missing
# output on these therefore leaves the derivation chain genuinely dangling.
_OUTPUT_REQUIRED_TYPES = frozenset({"EndpointReadout", "DataAnalysis"})

# Where the BUILD reads a domain entity from, keyed by (target type, process
# type). The bundled ISA shape allows only File/Sample/BioSample as a process
# object/input, so `_build_process` takes compounds from `chemicals` and the
# cell line from `cell_line` — a compound named as an Exposure's `input` is read
# by nothing and vanishes at assembly. Worse, `_build_process` reads
# `f.get("object") or f.get("input")`, so once `object` holds the sample the
# input field is never even consulted: every such link succeeds and every
# compound ends up orphaned in the exported crate.
#
# So the edge is written where the build will find it, and the caller is told.
# Refusing instead would be defensible, but the intent of
# `link(exposure, input, compound)` is unambiguous and correct — the experiment
# did expose those cells to that compound — and only the field is wrong.
# Mirrors composites._DOMAIN_WIRING.
_PROCESS_LINK_HOMES: dict[tuple[str, str], str] = {
    ("MolecularEntity", "Exposure"): "chemicals",
    ("CellLineSample", "CellCulture"): "cell_line",
}

# Relations that name "this process consumed that thing" and so can be rerouted
# to the field carrying it. Outputs are never rerouted: a compound as a process
# *result* is a different claim, and a wrong guess there would invent chemistry.
_REROUTABLE_RELATIONS = frozenset({"input", "object", "samples"})


def _build_honoured_field(state: CrateState, src: Entity, relation: str, to_id: str) -> str:
    """The field *relation* must be written to for the build to see the edge."""
    if relation not in _REROUTABLE_RELATIONS:
        return relation
    target = state.get_entity(to_id)
    if target is None:
        return relation
    process_type = str(src.fields.get("process_type") or src.fields.get("additionalType") or "")
    return _PROCESS_LINK_HOMES.get((str(target.type), process_type), relation)


def draft_file(
    state: CrateState,
    name: str,
    path: str | None = None,
    role: str | None = None,
    encoding_format: str | None = None,
    additional_types: list[str] | None = None,
    programming_language: str | None = None,
    entity_id: str | None = None,
) -> Entity:
    """Create a File data entity in the state.

    Args:
        state: The crate state to add the entity to.
        name: Human-readable file name (also used to mint the entity_id).
        path: Crate-relative destination path for the file (``dest_path``). When
            omitted the mapping derives ``data/<name>``.
        role: Optional role label for the file (e.g. "raw_data", "figure").
        encoding_format: Optional IANA media type (schema:encodingFormat). When
            omitted it is auto-derived from the file extension (``name`` first,
            then ``path``) via the scientific-format-aware registry (Issue #148),
            so e.g. ``run.mzML`` becomes ``application/x-mzml`` rather than being
            left blank or mislabeled text/plain. An explicit value always wins.
        additional_types: Optional extra ``@type`` term(s) to co-type the node
            alongside ``File`` (Issue #180). e.g. ``["SoftwareSourceCode"]`` makes
            an analysis script a ``@type:[File, SoftwareSourceCode]`` data entity
            (gold ``plot.py``). When omitted the node stays a plain ``File``.
        programming_language: Optional schema:programmingLanguage (e.g. "Python")
            — for a source-code File. Left unset when omitted.
        entity_id: Optional explicit id. Ids are normally minted from ``name``,
            which collides for two files sharing a basename in different
            directories; :func:`_unclaimed_file_id` passes a qualified id in
            exactly that case and ``None`` otherwise.

    Returns:
        The newly created File Entity.
    """
    fields: dict[str, Any] = {"name": name}
    if path:
        fields["dest_path"] = path
    if role:
        fields["role"] = role
    if not encoding_format:
        # Auto-derive from the extension (name, then dest path) when the caller
        # omits it. Returns None for extensionless/unknown names, leaving the
        # field unset rather than guessing.
        encoding_format = encoding_format_for_name(name) or (
            encoding_format_for_name(path) if path else None
        )
    if encoding_format:
        fields["encodingFormat"] = encoding_format
    # Drop blanks/dupes/the implicit File type so a clean term list reaches the
    # mapping (which co-types the node @type:[File, *additional_types]).
    if additional_types:
        extra = [t for t in additional_types if t and t != "File"]
        if extra:
            fields["additional_types"] = extra
    if programming_language:
        fields["programmingLanguage"] = programming_language
    entity = Entity(
        entity_id=_make_entity_id("file", name, {"entity_id": entity_id} if entity_id else {}),
        type="File",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(fields, source="llm")
    state.add_entity(entity)
    return entity


def link(state: CrateState, from_id: str, relation: str, to_id: str) -> dict[str, str]:
    """Add a single provenance edge ``from_id --relation--> to_id``.

    The ``relation`` MUST be one of :data:`PROVENANCE_RELATIONS`. Both endpoints
    MUST already exist in the state. If the relation already holds a value the
    new target is appended (the edge becomes a list), so a process can take
    several inputs/outputs.

    Args:
        state: The crate state to operate on.
        from_id: entity_id of the source entity (the process or sample).
        relation: One of :data:`PROVENANCE_RELATIONS` (object/result/input/...).
        to_id: entity_id of the target entity.

    Returns:
        A small confirmation dict ``{"from_id", "relation", "to_id"}``.

    Raises:
        ValueError: If ``relation`` is unknown or either endpoint is missing —
            with an actionable, model-readable message.
    """
    if relation not in PROVENANCE_RELATIONS:
        valid = ", ".join(sorted(PROVENANCE_RELATIONS))
        raise ValueError(f"Unknown provenance relation {relation!r}. Valid relations are: {valid}.")
    # Route a miss through the shared message so it names the ids the caller was
    # most likely reaching for. A flat "not found" tells the agent its guess was
    # wrong without telling it what is right, so it guesses again — one profiled
    # session lost four iterations to exactly that, on ids it had never been shown.
    src = state.get_entity(from_id)
    if src is None:
        raise ValueError(f"link source: {entity_not_found_message(state, from_id)}")
    if state.get_entity(to_id) is None:
        raise ValueError(f"link target: {entity_not_found_message(state, to_id)}")

    # Write where the build reads, not where the caller pointed — see
    # _PROCESS_LINK_HOMES. Silently storing an edge assembly discards is worse
    # than either honouring it or refusing it.
    stored_as = _build_honoured_field(state, src, relation, to_id)

    existing = src.fields.get(stored_as)
    if existing is None:
        src.fields[stored_as] = to_id
    elif isinstance(existing, list):
        if to_id not in existing:
            existing.append(to_id)
    elif existing != to_id:
        src.fields[stored_as] = [existing, to_id]
    src.set_field_status(stored_as, "filled", "llm")
    logger.debug("Linked %s --%s--> %s (stored as %r)", from_id, relation, to_id, stored_as)

    result = {"from_id": from_id, "relation": relation, "to_id": to_id}
    if stored_as != relation:
        result["stored_as"] = stored_as
        result["note"] = (
            f"Recorded as {stored_as!r}, not {relation!r}: the ISA profile allows only "
            f"File/Sample/BioSample as a process {relation}, so the crate carries this "
            f"link through {stored_as!r}. The edge is kept — use {stored_as!r} directly "
            "next time."
        )
        logger.info(
            "link(%s, %s, %s) rerouted to %r so the build keeps it",
            from_id,
            relation,
            to_id,
            stored_as,
        )
    return result


def _scanned_abspath(path_str: str, input_path: str | None) -> str:
    """Resolve a scanned file's path to an absolute string."""
    p = Path(path_str)
    if not p.is_absolute() and input_path:
        p = Path(input_path) / path_str
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _scanned_dest(path_str: str, filename: str, input_path: str | None) -> str:
    """Crate-relative dest mirroring the file's path under ``input_path``."""
    if input_path:
        try:
            return (
                Path(_scanned_abspath(path_str, input_path))
                .relative_to(Path(input_path).resolve())
                .as_posix()
            )
        except (ValueError, OSError):
            pass
    return f"data/{filename}"


def _append_haspart(entity: Entity, child_id: str) -> None:
    """Append ``child_id`` to ``entity``'s hasPart list (deduped)."""
    existing = entity.fields.get("hasPart")
    if existing is None:
        entity.fields["hasPart"] = [child_id]
    elif isinstance(existing, list):
        if child_id not in existing:
            existing.append(child_id)
    elif existing != child_id:
        entity.fields["hasPart"] = [existing, child_id]
    entity.set_field_status("hasPart", "filled", "llm")


def _unclaimed_file_id(state: CrateState, filename: str, dest: str) -> str | None:
    """An entity_id for *dest* that no OTHER file already holds.

    File ids are minted from the file's name, so two deposited files sharing a
    basename in different directories mint the same id and the second silently
    replaced the first — on svhps22, `characterisation/README.txt` and
    `processeddata/README.txt`, leaving one entity that two different steps then
    claimed as their output.

    Returns ``None`` when the name-derived id is free (the overwhelming case), so
    ids stay readable and unchanged; only an actual clash is qualified, by a
    digest of the destination path so the result is stable across runs.

    Args:
        state: The crate state to check for a claimant.
        filename: The file's basename, from which the id is normally minted.
        dest: The crate-relative destination that identifies this file.

    Returns:
        A distinct entity_id, or ``None`` to let the caller mint the usual one.
    """
    minted = _make_entity_id("file", filename, {})
    claimant = state.get_entity(minted)
    if claimant is None or str(claimant.fields.get("dest_path") or "") == dest:
        return None
    digest = hashlib.blake2s(dest.encode("utf-8"), digest_size=4).hexdigest()
    return f"{minted}_{digest}"


def file_index_by_source(state: CrateState) -> dict[str, Entity]:
    """Existing File entities keyed by resolved on-disk source AND by destination.

    The index behind find-or-create. Built once by a bulk caller and threaded
    through :func:`find_or_create_file`, so placing fifty files is one pass over
    the entities rather than fifty.

    Keyed both ways because neither key alone is enough. The resolved source
    catches one file reachable by two different scanned paths — but it resolves
    only for a file that exists at build time, so on its own it silently stopped
    deduping whenever the deposit was not mounted, and the entity a chain had
    already wired as a step's result was minted a second time by ``attach_files``
    (each copy then carrying half the metadata). ``dest_path`` is derived from the
    scan and always available, so it closes that gap.
    """
    input_path = state.metadata.input_path
    out: dict[str, Entity] = {}
    for fe in state.list_entities("File"):
        dest = str(fe.fields.get("dest_path") or "")
        if dest:
            out.setdefault(dest, fe)
        src = _file_source(fe, input_path)
        if src:
            out[str(Path(src).resolve())] = fe
    return out


def find_or_create_file(
    state: CrateState,
    fc: Any,
    *,
    role: str | None = None,
    index: dict[str, Entity] | None = None,
) -> Entity:
    """The File entity for a scanned file — created once, then reused (#177, #589).

    Deduped by resolved on-disk source and by destination (see
    :func:`file_index_by_source`), so one deposited file is never represented
    twice however it is reached: ``attach_files`` placing it under an Assay and
    ``draft_process_chain`` wiring it as a step's result converge on the same
    entity instead of minting rivals that would each carry half the metadata.

    Two *different* files sharing a basename are the opposite hazard — they mint
    one id and silently overwrite each other — which :func:`_unclaimed_file_id`
    resolves.

    Args:
        state: The crate state to find or create in.
        fc: The scanned-file record (``FileClassification``).
        role: Optional role to stamp — set on creation, refreshed on a hit.
        index: Optional prebuilt source→Entity index from
            :func:`file_index_by_source`; built on demand when omitted. A caller
            passing one must reuse it across the batch, since new entities are
            recorded there.

    Returns:
        The File entity representing *fc*.
    """
    input_path = state.metadata.input_path
    if index is None:
        index = file_index_by_source(state)
    key = _scanned_abspath(fc.path, input_path)
    dest = _scanned_dest(fc.path, fc.filename, input_path)
    fe = index.get(key) or index.get(dest)
    if fe is None:
        fe = draft_file(
            state,
            name=fc.filename,
            path=dest,
            role=role,
            encoding_format=fc.mime_type or None,
            entity_id=_unclaimed_file_id(state, fc.filename, dest),
        )
        index[key] = fe
        index[dest] = fe
    elif role:
        fe.fields["role"] = role
        fe.set_field_status("role", "filled", "llm")
    return fe


def attach_files(
    state: CrateState,
    to: str,
    name_contains: str | None = None,
    mime_contains: str | None = None,
    paths: list[str] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Bulk-place scanned files under a Study or Assay (#177).

    The inclusion fallback (#175) guarantees every file lands *somewhere*; this is
    the agent's *placement* verb — it associates a group of scanned files with the
    structural entity they belong to. For each matching file it finds-or-creates a
    ``File`` entity (deduped by on-disk source, so it is not duplicated and drops
    out of the root fallback) and appends it to ``to``'s ``hasPart``, so the build
    nests it under that dataset. Process inputs/outputs stay with ``link``.

    Args:
        state: The crate state to operate on.
        to: entity_id of the target Study or Assay.
        name_contains: Match files whose filename or path contains this substring.
        mime_contains: Match files whose mime_type contains this substring.
        paths: Explicit scanned paths/filenames to attach (with/instead of the
            substring filters).
        role: Optional role to stamp on each File (e.g. "raw_data", "processed").

    Returns:
        ``{"attached": int, "file_ids": [...], "to": to}``.

    Raises:
        ValueError: If ``to`` is missing or is not a Study/Assay.
    """
    target = state.get_entity(to)
    if target is None:
        raise ValueError(f"attach_files target: {entity_not_found_message(state, to)}")
    if target.type not in ("Study", "Assay"):
        raise ValueError(
            f"attach_files target must be a Study or Assay; {to!r} is a "
            f"{target.type}. Use draft_file + link for process inputs/outputs."
        )

    name_q = name_contains.lower() if name_contains else None
    mime_q = mime_contains.lower() if mime_contains else None
    explicit = {str(p) for p in paths} if paths else None

    def _matches(fc: Any) -> bool:
        if explicit is not None and fc.path not in explicit and fc.filename not in explicit:
            return False
        if name_q is not None and (
            name_q not in (fc.filename or "").lower() and name_q not in (fc.path or "").lower()
        ):
            return False
        if mime_q is not None and mime_q not in (fc.mime_type or "").lower():
            return False
        return True

    existing = file_index_by_source(state)

    file_ids: list[str] = []
    for fc in state.scanned_files:
        if not _matches(fc):
            continue
        fe = find_or_create_file(state, fc, role=role, index=existing)
        _append_haspart(target, fe.entity_id)
        if fe.entity_id not in file_ids:
            file_ids.append(fe.entity_id)

    logger.debug("attach_files: %d file(s) -> %s", len(file_ids), to)
    return {"attached": len(file_ids), "file_ids": file_ids, "to": to}


def _ref_ids(value: Any) -> set[str]:
    """Normalize a reference value (id, {@id}, or list thereof) to bare ids."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key:
            out.add(str(key).lstrip("#"))
    return out


def _process_type(proc: Entity) -> str:
    """The domain discriminator of a LabProcess (process_type or additionalType)."""
    return proc.fields.get("process_type") or proc.fields.get("additionalType") or ""


def _issue(entity_id: str, prop: str, message: str, fix: str) -> dict[str, Any]:
    """A routable issue in #87's shape (REQUIRED, ISA layer)."""
    return {
        "entity_id": entity_id,
        "property": prop,
        "message": message,
        "fix": fix,
        "severity": "required",
        "profile": "isa",
    }


def check_provenance(state: CrateState) -> dict[str, Any]:
    """Report-only connectivity lint over the derivation chain.

    Surfaces (without modifying state) three classes of break:

    1. A domain LabProcess that produces no output where the build has no
       fallback (EndpointReadout / DataAnalysis) — the chain dangles there.
    2. A File referenced by no process input/output and not part of any
       ``hasPart`` — an orphan data entity with no producer.
    3. *Continuity* (Issue #140): a process consumes a ``Sample`` that no
       process produces and that is not a CellCulture seed — the derivation
       chain is broken upstream of that process (its input does not trace back
       to a cultured/exposed material). Only applied when the crate actually
       models sample material-flow (some process produces a Sample output), and
       only to ``Sample`` inputs (``File`` inputs may be imported starting data;
       ``CellLineSample``/``MolecularEntity`` are external starting materials),
       so legitimate primary-cell, data-only, and multi-assay crates are not
       false-flagged.

    Args:
        state: The crate state to lint.

    Returns:
        ``{"ok": bool, "issues": [issue, ...]}`` where each issue is the #87
        routable shape ``{entity_id, property, message, fix, severity, profile}``
        keyed to the state ``entity_id`` (the id the agent passes to ``link`` /
        the management tools).
    """
    issues: list[dict[str, Any]] = []
    processes = state.list_entities("LabProcess")

    # Collect every entity_id consumed/produced by a process or held in a
    # hasPart, so a File can be checked for a producer / parent.
    referenced: set[str] = set()
    for proc in processes:
        for fld in (*_INPUT_FIELDS, *_OUTPUT_FIELDS):
            referenced |= _ref_ids(proc.fields.get(fld))
    for entity in state.list_entities():
        for fld in ("hasPart", "has_part"):
            referenced |= _ref_ids(entity.fields.get(fld))

    # Rule 1: dangling process output (no build-time fallback for these types).
    for proc in processes:
        ptype = _process_type(proc)
        if ptype in _OUTPUT_REQUIRED_TYPES and not any(proc.fields.get(f) for f in _OUTPUT_FIELDS):
            issues.append(
                _issue(
                    proc.entity_id,
                    "result",
                    f"{ptype} '{proc.entity_id}' has no output (result); the "
                    f"derivation chain dangles here.",
                    f"Produce an output and wire it, e.g. draft_file(...) then "
                    f"link('{proc.entity_id}', 'result', '<file_id>').",
                )
            )

    # Rule 2: orphan File (no producing process, not in any hasPart).
    for fe in state.list_entities("File"):
        if fe.entity_id not in referenced:
            issues.append(
                _issue(
                    fe.entity_id,
                    "hasPart",
                    f"File '{fe.entity_id}' is not produced by any process and "
                    f"not part of any dataset (orphan).",
                    f"Wire it as a process output "
                    f"(link('<process_id>', 'result', '{fe.entity_id}')) or add "
                    f"it to a dataset's hasPart.",
                )
            )

    # Rule 3: derivation-chain continuity. Build the set of entity_ids produced
    # by some process; if any produced entity is a Sample, the crate models
    # sample material-flow, so every consumed Sample must trace back to a
    # producer (or be a CellCulture seed). A consumed Sample that does neither
    # means the chain is broken upstream — exactly the mid-chain break a flat
    # presence lint cannot see.
    produced: set[str] = set()
    for proc in processes:
        for fld in _OUTPUT_FIELDS:
            produced |= _ref_ids(proc.fields.get(fld))
    models_sample_flow = any(
        (e := state.get_entity(pid)) is not None and e.type == "Sample" for pid in produced
    )
    if models_sample_flow:
        seeds: set[str] = set()
        for proc in processes:
            if _process_type(proc) == "CellCulture":
                for fld in _INPUT_FIELDS:
                    seeds |= _ref_ids(proc.fields.get(fld))
        for proc in processes:
            for fld in _INPUT_FIELDS:
                for tid in _ref_ids(proc.fields.get(fld)):
                    target = state.get_entity(tid)
                    if target is None or target.type != "Sample":
                        continue
                    if tid in produced or tid in seeds:
                        continue
                    issues.append(
                        _issue(
                            proc.entity_id,
                            fld,
                            f"{_process_type(proc) or 'LabProcess'} "
                            f"'{proc.entity_id}' consumes sample '{tid}', but no "
                            f"process produces it and it is not a culture seed — "
                            f"the derivation chain is broken upstream.",
                            f"Wire the producing step's output to this input, e.g. "
                            f"link('<upstream_process_id>', 'result', '{tid}'), or "
                            f"point this input at the correct cultured/exposed "
                            f"sample.",
                        )
                    )

    return {"ok": not issues, "issues": issues}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("draft_file", draft_file, takes_state=True)
TOOL_REGISTRY.register("link", link, takes_state=True)
TOOL_REGISTRY.register("attach_files", attach_files, takes_state=True)
TOOL_REGISTRY.register("check_provenance", check_provenance, takes_state=True)
