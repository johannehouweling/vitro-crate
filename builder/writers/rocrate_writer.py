"""Output writer that writes CrateState to RO-Crate directory format."""

from __future__ import annotations

import logging

from builder.state import CrateState

logger = logging.getLogger(__name__)


def write_rocrate(state: CrateState, output_path: str) -> dict:
    """Write CrateState to an RO-Crate at output_path.

    Delegates to builder.build_crate for the actual crate assembly.

    Args:
        state: The CrateState to write.
        output_path: Path where the RO-Crate directory should be created.

    Returns:
        Dict with keys: success (bool), path (str), error (str | None), entity_count (int).
    """
    from builder.tools.builder import build_crate

    result = build_crate(state, output_path)
    entity_count = len(state.list_entities())

    return {
        "success": result.get("success", False),
        "path": output_path,
        "error": result.get("error"),
        "entity_count": entity_count,
    }