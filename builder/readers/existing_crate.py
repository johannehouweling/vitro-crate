"""Input reader for existing RO-Crates — reconstructs CrateState from metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance, EntityType

logger = logging.getLogger(__name__)

_VALID_TYPES = frozenset(
    {
        "Investigation",
        "Study",
        "Assay",
        "LabProcess",
        "LabProtocol",
        "Sample",
        "MolecularEntity",
        "CellLineSample",
        "Person",
        "Organization",
        "Publication",
        "DefinedTerm",
        "PropertyValue",
        "File",
    }
)
_DATASET_SUBTYPES = frozenset({"Investigation", "Study", "Assay"})


def _crate_type(node: dict) -> EntityType | None:
    """Map a graph node's @type/additionalType to a CrateState entity type.

    A valid ISA-Tox crate types Investigation/Study/Assay as ``Dataset`` +
    ``additionalType`` and the cell-based test system as ``Sample`` +
    ``additionalType "CellLine"``; LabProcess subtypes keep ``@type LabProcess``
    with the subtype carried in ``additionalType``. Returns None for nodes that
    are not reconstructable CrateState entities (e.g. Profile descriptors).
    """
    t = node.get("@type")
    primary = t[0] if isinstance(t, list) else t
    add = node.get("additionalType")
    if isinstance(add, list):
        add = add[0] if add else None

    if primary == "Dataset":
        return add if add in _DATASET_SUBTYPES else None
    if primary == "LabProcess":
        return "LabProcess"
    if primary == "Sample":
        return "CellLineSample" if add == "CellLine" else "Sample"
    if primary == "ScholarlyArticle":
        return "Publication"
    if primary == "MediaObject":
        return "File"
    if primary in _VALID_TYPES:
        return primary
    return None


def read_existing_crate(crate_dir: str) -> CrateState:
    """Read an existing RO-Crate and reconstruct CrateState from its metadata.

    1. Read ro-crate-metadata.json
    2. Parse the @graph for entities
    3. Create Entity objects from each graph node
    4. Return populated CrateState

    If crate doesn't exist or is invalid, return a default CrateState.

    Args:
        crate_dir: Path to the directory containing ro-crate-metadata.json.

    Returns:
        A CrateState reconstructed from the crate's metadata.
    """
    crate_path = Path(crate_dir)
    metadata_path = crate_path / "ro-crate-metadata.json"

    if not metadata_path.is_file():
        logger.warning("No ro-crate-metadata.json found in %s", crate_dir)
        return CrateState()

    try:
        with open(metadata_path) as f:
            data = json.load(f)

        state = CrateState()
        state.metadata.input_path = crate_dir

        graph = data.get("@graph", [])
        for node in graph:
            node_id = node.get("@id", "")

            # Only the Root Data Entity (@id "./") describes the crate as a whole;
            # the metadata descriptor is skipped. Study/Assay are real entities.
            if node_id == "./":
                md = state.metadata
                md.title = node.get("name", md.title)
                md.description = node.get("description", md.description)
                md.accession = node.get("identifier", md.accession)
                continue
            if node_id == "ro-crate-metadata.json":
                continue

            ctype = _crate_type(node)
            if ctype is None:
                continue

            # Recover the local entity_id by stripping a single leading '#'.
            entity_id = node_id[1:] if node_id.startswith("#") else node_id
            fields = {k: v for k, v in node.items() if not k.startswith("@")}
            entity = Entity(
                entity_id=entity_id,
                type=ctype,
                fields=fields,
                _provenance=EntityProvenance(created_by="scanner"),
            )
            state.add_entity(entity)

        logger.info(
            "Read existing crate from %s — found %d entities",
            crate_dir,
            len(state.list_entities()),
        )
        return state

    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Failed to read existing crate from %s: %s", crate_dir, e)
        return CrateState()
