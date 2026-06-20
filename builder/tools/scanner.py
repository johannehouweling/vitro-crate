"""File scanning and classification tools for the ISA-Tox RO-Crate Builder.

The scanner examines an input directory and builds a raw file inventory
(path, size, mime type). It is called during session initialization,
before the agent loop starts, to give the agent a picture of what files
are available.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from builder.state import FileClassification

logger = logging.getLogger(__name__)

# Initialise mimetypes so .txt → text/plain, .csv → text/csv, etc.
mimetypes.init()


def _detect_mime_type(file_path: Path) -> str:
    """Detect the MIME type of a file using mimetypes and content sniffing.

    Args:
        file_path: Path to the file.

    Returns:
        A MIME type string.
    """
    # First try mimetypes based on extension
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type:
        return mime_type

    # Fallback: try content sniffing for common text formats
    try:
        with file_path.open("rb") as f:
            header = f.read(512)

        # Check for CSV/TSV by looking for commas/tabs in the first line
        if header.startswith(b",") or (b"," in header.split(b"\n")[0][:256]):
            return "text/csv"
        if header.startswith(b"\t") or (b"\t" in header.split(b"\n")[0][:256]):
            return "text/tab-separated-values"

        # Check if it looks like plain text (printable ASCII or common UTF-8)
        try:
            header.decode("utf-8")
            return "text/plain"
        except (UnicodeDecodeError, UnicodeError):
            pass
    except PermissionError:
        logger.warning("Permission denied reading: %s", file_path)
    except OSError:
        pass

    return "application/octet-stream"


def scan_files(path: str) -> list[FileClassification]:
    """Scan a directory and return a raw file inventory.

    Examines input directory and builds a raw file inventory (path, size,
    mime type). Returns an empty list for empty or non-existent directories.
    Skips hidden files (names starting with '.').

    Args:
        path: Path to the directory to scan.

    Returns:
        A list of FileClassification records.
    """
    dir_path = Path(path)
    if not dir_path.is_dir():
        logger.warning("Directory not found: %s", path)
        return []

    results: list[FileClassification] = []
    for entry in sorted(dir_path.rglob("*")):
        if not entry.is_file():
            continue
        # Skip hidden files/dirs anywhere in the relative path
        rel_parts = entry.relative_to(dir_path).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        results.append(
            FileClassification(
                path=str(entry),
                filename=entry.name,
                size=entry.stat().st_size,
                mime_type=_detect_mime_type(entry),
            )
        )
    logger.info("Scanned %s — found %d files", path, len(results))
    return results


def read_file_sample(path: str, lines: int = 20) -> str | None:
    """Read first N lines of a file for context without loading the whole thing.

    Args:
        path: Path to the file to sample.
        lines: Number of lines to read (default 20).

    Returns:
        A string with the first N lines, or None if the file cannot be read.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            sample_lines: list[str] = []
            for _ in range(lines):
                line = f.readline()
                if not line:
                    break
                sample_lines.append(line.rstrip("\n"))
            return "\n".join(sample_lines)
    except PermissionError:
        logger.warning("Permission denied reading file: %s", path)
        return None
    except Exception:
        logger.exception("Error reading file sample: %s", path)
        return None