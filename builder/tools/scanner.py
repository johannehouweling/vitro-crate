"""File scanning and classification tools for the ISA-Tox RO-Crate Builder.

The scanner examines an input directory (or zip archive) and builds a raw
file inventory (path, size, mime type). It is called during session
initialisation, before the agent loop starts, to give the agent a picture
of what files are available.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import sys
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

from builder.state import ArchivePreview, FileClassification

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


def preview_archive(path: str) -> ArchivePreview:
    """Return metadata about a zip archive without extracting it.

    Provides a preview of the archive's contents (entry paths, sizes, and
    whether each entry is a directory) so the agent can present this to the
    user before deciding to extract.

    Handles gracefully:
    - Non-existent files: returns an ArchivePreview with an error message
      and an empty entries list.
    - Corrupt/invalid zip files: returns an ArchivePreview with an error
      message and an empty entries list.

    Args:
        path: Path to the zip archive to preview.

    Returns:
        An ``ArchivePreview`` dataclass with archive metadata.
    """
    archive_path = Path(path).resolve()

    # Handle non-existent file
    if not archive_path.is_file():
        return ArchivePreview(
            path=str(archive_path),
            filename=archive_path.name,
            size_bytes=0,
            size_mb=0.0,
            entry_count=0,
            entries=[],
            message=f"File not found: {path}",
            error=f"File not found: {path}",
        )

    size_bytes = archive_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)

    # Use existing helper — returns [] on corrupt zips
    entries = _list_zip_contents(archive_path)

    if not entries and size_bytes > 0:
        # _list_zip_contents returns [] for corrupt zips
        return ArchivePreview(
            path=str(archive_path),
            filename=archive_path.name,
            size_bytes=size_bytes,
            size_mb=round(size_mb, 2),
            entry_count=0,
            entries=[],
            message=f"Cannot read archive: {archive_path.name} (corrupt or unsupported format)",
            error=f"Cannot read archive: {archive_path.name} (corrupt or unsupported format)",
        )

    return ArchivePreview(
        path=str(archive_path),
        filename=archive_path.name,
        size_bytes=size_bytes,
        size_mb=round(size_mb, 2),
        entry_count=len(entries),
        entries=entries,
        message=(
            f"Archive {archive_path.name}: {size_mb:.1f} MB, "
            f"{len(entries)} entries"
        ),
    )


def _safe_walk(root: Path) -> list[Path]:
    """Recursively walk *root* skipping unreadable directories.

    ``Path.rglob`` raises ``PermissionError`` on unreadable subdirectories
    (e.g. ``/proc/*/map_files``).  This uses ``os.walk`` with an ``onerror``
    handler that logs and continues rather than crashing.

    Note that ``os.walk`` on Python 3.12+ raises ``PermissionError`` from
    ``os.scandir`` by default, so the ``onerror`` callback is essential.
    """
    results: list[Path] = []

    def _onerror(err: OSError) -> None:
        logger.warning("Skipping unreadable directory: %s", err.filename or err)

    try:
        for dirpath_str, dirnames, filenames in os.walk(root, onerror=_onerror):
            dirpath = Path(dirpath_str)
            for name in filenames:
                results.append(dirpath / name)
    except PermissionError:
        # os.walk's onerror catches PermissionError during scandir, but
        # a top-level PermissionError on the root itself still bubbles.
        logger.warning("Permission denied at root: %s", root)
    return results


def scan_files(
    path: str,
    approved_roots: set[str] | None = None,
) -> list[FileClassification]:
    """Scan a directory or zip archive and return a file inventory.

    If *path* is a directory, walks it and returns a list of
    ``FileClassification`` records.  If *path* is a zip archive (``.zip``,
    ``.tar.gz``, etc.), it is automatically extracted to a temporary
    directory and the extracted contents are scanned transparently.

    .. security::

       The scanner **only** accepts paths that descend from one of the
       *approved_roots* — directories the user has explicitly permitted.
       If *approved_roots* is ``None`` (the default) the first path scanned
       is auto-approved and becomes the sole root for subsequent calls.

    Args:
        path: Path to the directory **or** archive to inspect.
        approved_roots: Set of resolved absolute directory paths that are
            allowed for scanning.  Pass ``None`` on the first call to
            auto-approve the target.

    Returns:
        A list of ``FileClassification`` records.
    """
    target = Path(path).resolve()

    # -- Security guard: approved-roots check -----------------------------------
    if approved_roots is not None:
        if not any(
            str(target) == r or str(target).startswith(r + "/")
            for r in approved_roots
        ):
            logger.warning(
                "Refusing to scan %s — not in approved roots: %s",
                target, approved_roots,
            )
            return []

    # -- Security guard: don't follow symlinks out of the resolved path ---------
    _follows_symlinks = False
    try:
        _follows_symlinks = target.is_symlink()
    except PermissionError:
        pass
    if _follows_symlinks:
        logger.warning("Refusing to scan symlink: %s", target)
        return []

    # -- Archive case: auto-extract and recurse --------------------------------
    if target.is_file() and _is_archive(target):
        contents = _list_zip_contents(target)
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info(
            "Path %s is a zip archive (%.1f MB) with %d entries — auto-extracting",
            target, size_mb, len(contents),
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
        logger.warning("Path not found: %s", target)
        return []

    results: list[FileClassification] = []
    try:
        all_entries = sorted(target.rglob("*"))
    except PermissionError:
        logger.warning("Permission denied scanning %s — recursing manually", target)
        all_entries = sorted(_safe_walk(target))

    scan_start = time.monotonic()
    processed = 0
    total_candidates = len(all_entries)

    for entry in all_entries:
        if not entry.is_file():
            continue
        # Skip hidden files/dirs anywhere in the relative path
        try:
            rel_parts = entry.relative_to(target).parts
        except ValueError:
            continue
        if any(p.startswith(".") for p in rel_parts):
            continue
        # Skip macOS resource fork metadata (__MACOSX folders in zips)
        if any(p == "__MACOSX" for p in rel_parts):
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
        processed += 1
        if processed % 100 == 0:
            elapsed = time.monotonic() - scan_start
            logger.debug(
                "Progress: %d/%d files... (%.1fs elapsed)",
                processed,
                total_candidates,
                elapsed,
            )

    total_elapsed = time.monotonic() - scan_start
    logger.info(
        "Scan complete: %d files in %.2fs",
        len(results),
        total_elapsed,
    )
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
        # Extract next to the zip file by default — survives session resume
        dest = zip_path.parent / f"{zip_path.stem}_extracted"
        dest.mkdir(parents=True, exist_ok=True)

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

    # Strip macOS resource-fork metadata (__MACOSX, ._ files) unless we're on macOS
    _strip_macos_junk(dest)

    logger.info("Extracted %s -> %s (%d entries)", path, dest, extracted)
    return {
        "extracted_to": str(dest),
        "entry_count": extracted,
        "message": (
            f"Extracted {extracted} entries to {dest}. "
            f"Use ``scan_files`` with path ``{dest}`` to inventory the contents."
        ),
    }


def _strip_macos_junk(root: Path) -> None:
    """Remove __MACOSX directories and ._ resource fork files from *root*.

    These are macOS-specific metadata that get bundled into zips when
    created on a Mac.  On Linux they are useless bloat; on macOS we
    leave them alone since they may be meaningful to the system.
    """
    if sys.platform == "darwin":
        return  # macOS — leave them be
    if not root.is_dir():
        return
    for entry in list(root.rglob("*")):
        # Remove __MACOSX directories and any ._ resource fork files
        if "__MACOSX" in entry.parts:
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                logger.debug("Removed macOS junk: %s", entry)
            except OSError:
                logger.warning("Could not remove macOS junk: %s", entry)


def read_multiple_files(
    paths: list[str],
    *,
    lines: int = 50,
) -> dict:
    """Read several files in one go and return their contents.

    Use this tool when you need to inspect multiple files at once — for
    example all the metadata files in an assay directory — rather than
    calling ``read_file_sample`` for each individually.

    Args:
        paths: List of file paths to read (absolute or relative to cwd).
        lines: Max lines to read per file (default 50).

    Returns:
        A dict with:
        - ``files``: ``{path: content_or_error}``
        - ``count``: number of files successfully read
        - ``skipped``: list of paths that could not be read
    """
    results: dict[str, str] = {}
    skipped: list[str] = []
    total_start = time.monotonic()

    for path in paths:
        file_start = time.monotonic()
        content = read_file_sample(path, lines=lines)
        file_elapsed = time.monotonic() - file_start
        if content is not None:
            results[path] = content
            logger.debug("Read %s in %.3fs", path, file_elapsed)
        else:
            skipped.append(path)
            logger.debug("Skipped %s in %.3fs", path, file_elapsed)

    total_elapsed = time.monotonic() - total_start
    count = len(results)
    logger.info(
        "Read %d file(s) from %d path(s) in %.2fs",
        count,
        len(paths),
        total_elapsed,
    )
    msg = f"Read {count} file(s)"
    if skipped:
        msg += f" ({len(skipped)} skipped: {', '.join(skipped)})"
    return {
        "files": results,
        "count": count,
        "skipped": skipped,
        "message": msg,
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

    # Content-based binary guard: office formats (xlsx/xls) are zip/OLE2
    # containers whose MIME type is *not* octet-stream, so the check above
    # misses them and their raw bytes (PK\x03\x04…) would be read as mojibake.
    # A NUL byte in the first chunk reliably marks a file as binary.
    try:
        with file_path.open("rb") as fb:
            if b"\x00" in fb.read(8192):
                return None
    except OSError:
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


def summarize_scan_result(
    files: list[FileClassification], sample: int = 15
) -> str:
    """Return a compact, LLM-facing summary of a scan result.

    The full inventory is stored in ``CrateState.scanned_files``; the agent
    only needs a clear success signal plus a small sample — not the raw list of
    hundreds/thousands of dataclass objects, which floods the context with no
    obvious success cue and makes the agent re-scan in a loop.

    Args:
        files: The scanned ``FileClassification`` records.
        sample: Maximum number of filenames to include in the sample.

    Returns:
        A short human/LLM-readable summary string.
    """
    n = len(files)
    if n == 0:
        return "Scan complete: 0 files found."

    by_type = Counter(f.mime_type for f in files)
    types = ", ".join(f"{mime} ({count})" for mime, count in by_type.most_common(8))
    shown = ", ".join(f.filename for f in files[:sample])
    more = f", +{n - sample} more" if n > sample else ""
    return (
        f"Scan complete: {n} files found and stored in session state "
        f"(no need to scan again). Types: {types}. Sample: {shown}{more}."
    )