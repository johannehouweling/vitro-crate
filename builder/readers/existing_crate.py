"""Input reader for existing RO-Crates — reconstructs CrateState from metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from builder.state import CrateState, Entity, EntityProvenance

logger = logging.getLogger(__name__)

_VALID_TYPES = frozenset({
    "Investigation", "Study", "Assay", "LabProcess", "LabProtocol",
    "Sample", "MolecularEntity", "CellLineSample", "Person",
    "Organization", "Publication", "DefinedTerm", "PropertyValue", "File",
})


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
            node_type = node.get("@type", "")

            if node_id == "./" or node_type == "Dataset":
                state.metadata.title = node.get("name", state.metadata.title)
                state.metadata.description = node.get("description", state.metadata.description)
                state.metadata.accession = node.get("identifier", state.metadata.accession)
                continue

            if not node_type:
                continue

            primary_type = node_type[0] if isinstance(node_type, list) else node_type
            if primary_type in ("CreativeWork", "File", None):
                continue
            if primary_type not in _VALID_TYPES:
                continue

            fields = {k: v for k, v in node.items() if not k.startswith("@")}
            entity = Entity(
                entity_id=node_id,
                type=primary_type,
                fields=fields,
                _provenance=EntityProvenance(created_by="scanner"),
            )
            state.add_entity(entity)

        logger.info(
            "Read existing crate from %s — found %d entities",
            crate_dir, len(state.list_entities()),
        )
        return state

    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Failed to read existing crate from %s: %s", crate_dir, e)
        return CrateState()