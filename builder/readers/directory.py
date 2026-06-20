"""Input reader for directories — scans files and seeds CrateState."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from builder.state import CrateState

logger = logging.getLogger(__name__)


def read_directory(path: str) -> CrateState:
    """Read a directory and return a CrateState seeded with scanned files.

    1. Scan files with scanner.scan_files()
    2. Create a CrateState with metadata filled from directory name
    3. Add scan results as FileClassifications in scanned_files
    4. Return the seeded CrateState

    If path doesn't exist or is empty, return a CrateState with default metadata.

    Args:
        path: Path to the directory to read.

    Returns:
        A CrateState seeded with scanned file information.
    """
    from builder.tools.scanner import scan_files

    state = CrateState()

    dir_path = Path(path)
    state.metadata.input_path = str(dir_path.resolve())
    state.metadata.title = dir_path.name

    scanned = scan_files(path)
    state.scanned_files = scanned

    state.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    state.created_at = datetime.now(timezone.utc).isoformat()
    state.updated_at = state.created_at

    logger.info("Read directory %s — found %d files", path, len(scanned))
    return state