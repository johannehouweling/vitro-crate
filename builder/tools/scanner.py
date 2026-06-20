"""File scanning and classification tools for the ISA-Tox RO-Crate Builder.

The scanner examines an input directory (or zip archive) and builds a raw
file inventory (path, size, mime type). It is called during session
initialisation, before the agent loop starts, to give the agent a picture
of what files are available.
"""

from __future__ import annotations

import logging
import mimetypes
import tempfile
import zipfile
from pathlib import Path

from builder.state import FileClassification

logger = logging.getLogger(__name__)

# Initialise mimetypes so .txt -> text/plain, .csv -> text/csv, etc.
mimetypes.init()

_ZIP_EXTENSIONS = frozenset({".zip", ".tar", ".gz", ".tgz", ".tar.gz"})


def _detect_mime_type(file_path: Path) -> str:
    """Detect the MIME type of a file using mimetypes and content sniffing.

    Args:
        file_path: Path to the file.

    Returns:
        A MIME type string.
    """
    # Fallback: guess from extension
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


_TABULAR_MIME_TYPES = {
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls"}


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def _is_archive(path: Path) -> bool:
    """Return True if *path* looks like a compressed archive."""
    return path.suffix.lower() in _ZIP_EXTENSIONS or path.name.endswith(".tar.gz")


def _list_zip_contents(zip_path: Path) -> list[dict]:
    """Peek inside a zip archive and return metadata about its contents.

    Returns a list of dicts with keys ``path``, ``size``, ``is_dir`` so the
    agent can present a preview to the user without extracting.
    """
    contents: list[dict] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                contents.append(
                    {
                        "path": info.filename,
                        "size": info.file_size,
                        "is_dir": info.is_dir(),
                    }
                )
    except zipfile.BadZipFile:
        logger.warning("Bad zip (corrupt?): %s", zip_path)
        return []
    except Exception:
        logger.exception("Error reading zip: %s", zip_path)
        return []
    contents.sort(key=lambda c: (not c["is_dir"], c["path"]))
    return contents


def scan_files(path: str) -> list[FileClassification]:
    """Scan a directory or zip archive and return a file inventory.

    If *path* is a directory, walks it and returns a list of
    ``FileClassification`` records.  If *path* is a zip archive (``.zip``,
    ``.tar.gz``, etc.), it is automatically extracted to a temporary
    directory and the extracted contents are scanned transparently.

    Args:
        path: Path to the directory **or** archive to inspect.

    Returns:
        A list of ``FileClassification`` records.
    """
    target = Path(path)

    # -- Archive case: auto-extract and recurse --------------------------------
    if target.is_file() and _is_archive(target):
        contents = _list_zip_contents(target)
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info(
            "Path %s is a zip archive (%.1f MB) with %d entries — auto-extracting",
            path, size_mb, len(contents),
        )
        result = unzip_file(str(target))
        if "error" in result:
            logger.warning("Could not extract archive: %s", result["error"])
            return []
        extracted_dir = result["extracted_to"]
        # Scan extracted directory recursively
        return scan_files(extracted_dir)

    # -- Directory case --------------------------------------------------------
    if not target.is_dir():
        logger.warning("Path not found: %s", path)
        return []

    results: list[FileClassification] = []
    for entry in sorted(target.rglob("*")):
        if not entry.is_file():
            continue
        # Skip hidden files/dirs anywhere in the relative path
        rel_parts = entry.relative_to(target).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        mime = _detect_mime_type(entry)
        first_rows: list[str] | None = None
        if mime in _TABULAR_MIME_TYPES or entry.suffix.lower() in _TABULAR_SUFFIXES:
            sample = read_file_sample(str(entry), lines=20)
            if sample is not None:
                first_rows = sample.splitlines()
        results.append(
            FileClassification(
                path=str(entry),
                filename=entry.name,
                size=entry.stat().st_size,
                mime_type=mime,
                first_rows=first_rows,
            )
        )
    logger.info("Scanned %s — found %d files", path, len(results))
    return results


def unzip_file(path: str, output_dir: str | None = None) -> dict:
    """Extract a zip archive and return the path to the extracted directory.

    The agent should call ``present_to_human`` before calling this tool for
    large archives so the user can confirm.

    Args:
        path: Path to the zip file to extract.
        output_dir: Optional output directory.  Defaults to a temporary dir.

    Returns:
        A dict with keys ``extracted_to``, ``entry_count``, and ``message``.
    """
    zip_path = Path(path)
    if not zip_path.is_file():
        return {
            "error": f"File not found: {path}",
            "message": "The file does not exist.",
        }

    if output_dir:
        dest = Path(output_dir)
        dest.mkdir(parents=True, exist_ok=True)
    else:
        dest = Path(tempfile.mkdtemp(prefix=f"{zip_path.stem}_extracted_"))

    extracted = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest)
            extracted = len(zf.infolist())
    except zipfile.BadZipFile:
        return {
            "error": f"Cannot open {path} as a zip archive (corrupt?)",
            "message": "The archive appears to be corrupt or not a valid zip.",
        }
    except Exception as exc:
        logger.exception("Error extracting zip")
        return {"error": str(exc), "message": f"Failed to extract: {exc}"}

    logger.info("Extracted %s -> %s (%d entries)", path, dest, extracted)
    return {
        "extracted_to": str(dest),
        "entry_count": extracted,
        "message": (
            f"Extracted {extracted} entries to {dest}. "
            f"Use ``scan_files`` with path ``{dest}`` to inventory the contents."
        ),
    }


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

    # Skip very large files
    try:
        if file_path.stat().st_size > 100 * 1024 * 1024:
            logger.info("Skipping file sample (>100MB): %s", path)
            return None
    except OSError:
        return None

    # Skip binary files
    if _detect_mime_type(file_path) == "application/octet-stream":
        return None

    try:
        sample_lines: list[str] = []
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
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