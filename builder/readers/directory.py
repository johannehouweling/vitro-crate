"""Input reader for directories — scans files and seeds CrateState."""

from __future__ import annotations

import logging
from pathlib import Path

import builder.config as _config
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
    from builder.tools.scanner import _is_forbidden_root, scan_files

    state = CrateState()

    dir_path = Path(path)
    resolved = dir_path.resolve()
    state.metadata.input_path = str(resolved)
    state.metadata.title = dir_path.name

    # A user-provided input directory is a legitimate scan root. Approve it and
    # pass the allowlist to the now fail-closed scanner (#197). A forbidden root
    # (filesystem root, home dir, system tree) is refused outright.
    if _is_forbidden_root(resolved):
        logger.warning("Refusing to read forbidden directory: %s", resolved)
        scanned: list = []
    else:
        approved = {str(resolved)}
        state.approved_scan_roots.add(str(resolved))
        scanned = scan_files(path, approved_roots=approved)
    state.scanned_files = scanned

    state.session_id = _config.now().strftime("%Y%m%d_%H%M%S")
    state.created_at = _config.now().isoformat()
    state.updated_at = state.created_at

    logger.info("Read directory %s — found %d files", path, len(scanned))
    return state
