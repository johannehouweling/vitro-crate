"""Tool that assembles a ROCrate directory from CrateState entity data.

For now, this is a scaffold that creates the output directory structure and
writes a minimal ro-crate-metadata.json from state data. Full ROCrate assembly
via rocrate-py comes later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState

logger = logging.getLogger(__name__)

ISA_TOX_CONTEXT = [
    {
        "@vocab": "http://schema.org/",
        "schema": "http://schema.org/",
    }
]


def build_crate(state: CrateState, output_path: str) -> dict[str, Any]:
    """Build (or scaffold) an RO-Crate from CrateState.

    Creates the output directory, writes a minimal ro-crate-metadata.json
    from state data, and returns a result dict.

    Args:
        state: The current CrateState to build from.
        output_path: Path where the crate directory should be created.

    Returns:
        A dict with keys:
            success (bool): Whether the crate was built successfully.
            crate_path (str): The output path used.
            error (str | None): Error message if success is False.
    """
    try:
        if not output_path:
            return {"success": False, "crate_path": output_path, "error": "Empty output path"}

        output_dir = Path(output_path)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build minimal ro-crate-metadata.json
        root_id = "./"

        # Root dataset entry
        root_dataset: dict[str, Any] = {
            "@id": root_id,
            "@type": "Dataset",
        }

        if state.metadata.title:
            root_dataset["name"] = state.metadata.title
        if state.metadata.description:
            root_dataset["description"] = state.metadata.description
        if state.metadata.accession:
            root_dataset["identifier"] = state.metadata.accession

        # Add entities from state as graph nodes
        graph_entries: list[dict[str, Any]] = [root_dataset]
        for entity in state.list_entities():
            entry: dict[str, Any] = {
                "@id": entity.entity_id,
                "@type": entity.type,
            }
            entry.update(entity.fields)
            graph_entries.append(entry)

        metadata: dict[str, Any] = {
            "@context": ISA_TOX_CONTEXT,
            "@graph": graph_entries,
        }

        metadata_path = output_dir / "ro-crate-metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2, default=str)

        logger.info("Crate scaffold created at %s", output_path)
        return {"success": True, "crate_path": output_path, "error": None}

    except OSError as e:
        logger.error("Failed to create crate at %s: %s", output_path, e)
        return {"success": False, "crate_path": output_path, "error": str(e)}
    except Exception as e:
        logger.error("Unexpected error building crate: %s", e)
        return {"success": False, "crate_path": output_path, "error": str(e)}