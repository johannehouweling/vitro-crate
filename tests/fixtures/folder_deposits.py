"""Deposits whose folders say what their studies and assays are.

A deposit is given as the list of its files. :func:`deposit_state` records them
as a scan would, with nothing on disk, for the tests that ask which folder an
entity takes; :func:`written_deposit` writes them and scans them with the engine,
for the tests that export a crate. Files are placed with the real
``attach_files``, never by writing a ``hasPart`` field by hand.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, FileClassification
from builder.tools.drafters import draft_assay, draft_investigation, draft_study
from builder.tools.provenance import attach_files

# A deposit root nothing is read from: these states hold paths only.
ROOT = "/deposits/folders"


def deposit_state(*files: str, root: str = ROOT) -> CrateState:
    """A state that has scanned *files*, relative to *root*, and drafted nothing."""
    state = CrateState()
    state.metadata.input_path = root
    state.approved_scan_roots = {root}
    for relative in files:
        state.scanned_files.append(
            FileClassification(
                path=f"{root}/{relative}",
                filename=PurePosixPath(relative).name,
                size=1,
                mime_type="text/csv",
            )
        )
    return state


def written_deposit(folder: Path, *files: str, state: CrateState | None = None) -> CrateState:
    """*files* written under *folder* and scanned by the engine, into *state* if given."""
    for relative in files:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("well,value\nA1,1\n", encoding="utf-8")
    engine = AgentEngine(state=state) if state is not None else AgentEngine()
    engine.initialize(str(folder))
    assert len(engine.state.scanned_files) == len(files), "the scan missed a file"
    return engine.state


def backbone(state: CrateState, *assays: str, study: str = "Study") -> tuple[Entity, list[Entity]]:
    """One Investigation, one Study and the named Assays, with no file placed."""
    investigation = draft_investigation(
        state, {"name": "Investigation", "description": "A deposit.", "identifier": "S-TEST1"}
    )
    study_entity = draft_study(state, investigation.entity_id, {"name": study})
    return study_entity, [
        draft_assay(state, study_entity.entity_id, {"name": name}) for name in assays
    ]


def place(state: CrateState, entity: Entity, folder: str) -> None:
    """Attach every scanned file under *folder* to *entity*, as an agent would."""
    result = attach_files(state, to=entity.entity_id, name_contains=f"{folder}/")
    assert result["attached"], f"no scanned file lies under {folder!r}"


def structure(study_folder: str, *assay_folders: str) -> dict[str, Any]:
    """An accepted labelling: one study at *study_folder* with the assays named."""
    return {
        "studies": [
            {
                "folder": study_folder,
                "name": "",
                "assays": [{"folder": folder, "name": ""} for folder in assay_folders],
            }
        ],
        "source": "model",
    }


def graph_of(state: CrateState) -> dict[str, dict[str, Any]]:
    """The graph the validation reads, by ``@id``: nothing written, no scanned leaves."""
    from builder.tools.builder import assemble_crate

    document = assemble_crate(
        state, output_dir=None, materialize_payload=False, include_all_scanned=False
    ).metadata.generate()
    return {node["@id"]: node for node in document["@graph"]}


def ids(value: Any) -> list[str]:
    """The ``@id`` of each reference in *value*, whether one or a list."""
    if value is None:
        return []
    return [item["@id"] for item in (value if isinstance(value, list) else [value])]
