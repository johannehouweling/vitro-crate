"""Output writer that projects CrateState onto the ARC directory structure.

For the initial implementation, this creates the basic ARC directory layout
and delegates to rocrate_writer for the metadata.
"""

from __future__ import annotations

import logging
from pathlib import Path

from builder.state import CrateState

logger = logging.getLogger(__name__)


def write_arc(state: CrateState, output_path: str) -> dict:
    """Write CrateState as an ARC directory structure.

    Creates the ARC directory layout (studies/, assays/) and writes
    ro-crate-metadata.json at the root.

    Args:
        state: The CrateState to write.
        output_path: Path where the ARC directory should be created.

    Returns:
        Dict with keys: success (bool), path (str), error (str | None).
    """
    from builder.writers.rocrate_writer import write_rocrate

    output_dir = Path(output_path)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create basic ARC directory structure based on entities
        for entity in state.list_entities():
            if entity.type == "Study":
                study_dir = output_dir / "studies" / entity.entity_id
                study_dir.mkdir(parents=True, exist_ok=True)
            elif entity.type == "Assay":
                assay_dir = output_dir / "assays" / entity.entity_id
                assay_dir.mkdir(parents=True, exist_ok=True)
                (assay_dir / "dataset" / "raw_data").mkdir(parents=True, exist_ok=True)
                (assay_dir / "dataset" / "processed_data").mkdir(parents=True, exist_ok=True)

        # Write ro-crate-metadata.json via the rocrate writer
        rocrate_result = write_rocrate(state, output_path)

        if rocrate_result.get("success"):
            logger.info("ARC directory created at %s", output_path)
            return {"success": True, "path": output_path, "error": None}

        return {
            "success": False,
            "path": output_path,
            "error": rocrate_result.get("error"),
        }

    except OSError as e:
        logger.error("Failed to create ARC at %s: %s", output_path, e)
        return {"success": False, "path": output_path, "error": str(e)}
