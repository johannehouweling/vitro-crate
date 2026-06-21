"""Input reader for metadata files found in scanned directories.

Examines scanned files for metadata files (.json, .yaml, .yml) and
extracts structured metadata to populate entities in the state.
Currently a stub.
"""

from __future__ import annotations

import logging

from builder.state import CrateState

logger = logging.getLogger(__name__)


def read_metadata_files(state: CrateState) -> CrateState:
    """Examine scanned files for metadata files and extract entities.

    Currently a stub that returns the state unchanged.
    Full implementation will parse JSON/YAML metadata files and
    populate entities accordingly.

    Args:
        state: The current CrateState with scanned_files populated.

    Returns:
        The CrateState, potentially with new entities added.
    """
    logger.info("Metadata file reader: stub implementation — no files processed")
    return state
