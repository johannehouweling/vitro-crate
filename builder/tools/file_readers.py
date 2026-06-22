"""Format-aware file readers for structured and unstructured documents.

Provides dedicated readers for Excel (``.xlsx``), Word (``.docx``), and a
unified ``read_file`` dispatcher that routes to the right reader based on
file extension.  All readers enforce size and row limits so the LLM never
gets flooded with large files.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_BYTES = 1_000_000  # 1 MB — skip files larger than this
_MAX_ROWS = 500  # max rows to return from structured formats


# ---------------------------------------------------------------------------
# Excel (.xlsx)
# ---------------------------------------------------------------------------


def read_excel(
    path: str,
    *,
    max_rows: int = _MAX_ROWS,
    max_bytes: int = _MAX_BYTES,
) -> str | None:
    """Read an Excel ``.xlsx`` file and return its content as pipe-delimited text.

    Each worksheet is returned with a ``[Sheet: <name>]`` header followed by
    pipe-delimited rows.  Empty rows are skipped.  Returns *None* if the file
    cannot be read, is too large, or is not a valid Excel workbook.

    Args:
        path: Path to the ``.xlsx`` file.
        max_rows: Maximum number of data rows to return per sheet (default 500).
        max_bytes: Maximum file size in bytes (default 1 MB).  Larger files
            are skipped.

    Returns:
        Pipe-delimited text with sheet headers, or *None*.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None

    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            logger.info("Skipping large Excel file (>%d bytes): %s", max_bytes, path)
            return None
    except OSError:
        return None

    try:
        import openpyxl
    except ImportError:
        logger.error("openpyxl is not installed.")
        return None

    try:
        wb = openpyxl.load_workbook(
            file_path, read_only=True, data_only=True, keep_links=False
        )
    except Exception:
        logger.exception("Error opening Excel file: %s", path)
        return None

    parts: list[str] = []
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            parts.append(f"[Sheet: {sheet_name}]")
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                if all(cell is None for cell in row):
                    continue
                cleaned = [str(cell) if cell is not None else "" for cell in row]
                parts.append("| " + " | ".join(cleaned) + " |")
                row_count += 1
                if row_count >= max_rows:
                    parts.append(f"[... truncated at {max_rows} rows]")
                    break
            parts.append("")
    finally:
        wb.close()

    return "\n".join(parts).rstrip("\n")
# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------


def read_docx(
    path: str,
    *,
    max_bytes: int = _MAX_BYTES,
) -> str | None:
    """Read a Word ``.docx`` file and return its text content.

    Extracts paragraph text, table content (as pipe-delimited rows), and
    section headings where available.  Returns *None* if the file cannot be
    read, is too large, or is not a valid ``.docx``.

    Args:
        path: Path to the ``.docx`` file.
        max_bytes: Maximum file size in bytes (default 1 MB).

    Returns:
        Plain text with ``[Table N]`` markers, or *None*.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None

    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            logger.info("Skipping large DOCX file (>%d bytes): %s", max_bytes, path)
            return None
    except OSError:
        return None

    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        logger.error("python-docx is not installed.")
        return None

    try:
        doc = Document(file_path)
    except Exception:
        logger.exception("Error opening DOCX file: %s", path)
        return None

    parts: list[str] = []
    table_idx = 0

    body = doc.element.body
    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            from docx.text.paragraph import Paragraph

            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                parts.append(text)

        elif tag == "tbl":
            from docx.table import Table as DocxTable

            table = DocxTable(child, doc)
            table_idx += 1
            table_rows: list[str] = []

            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    table_rows.append("| " + " | ".join(cells) + " |")

            if table_rows:
                parts.append(f"[Table {table_idx} ({len(table_rows)} rows)]")
                parts.append(table_rows[0])
                parts.append(
                    "| " + " | ".join("---" for _ in table.rows[0].cells) + " |"
                )
                parts.extend(table_rows[1:])

    return "\n".join(parts)

# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------


def _read_text_file(
    path: str,
    *,
    max_lines: int = 100,
    max_bytes: int = _MAX_BYTES,
) -> str | None:
    """Read first *max_lines* lines of a plain-text file.

    Returns *None* if the file is too large, binary, or unreadable.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            logger.info("Skipping large text file (>%d bytes): %s", max_bytes, path)
            return None
    except OSError:
        return None

    try:
        with file_path.open("rb") as fb:
            if b"\x00" in fb.read(8192):
                return None
    except OSError:
        return None

    try:
        sample: list[str] = []
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                sample.append(line.rstrip("\n"))
            return "\n".join(sample)
    except PermissionError:
        logger.warning("Permission denied reading file: %s", path)
        return None
    except Exception:
        logger.exception("Error reading text file: %s", path)
        return None


def read_file(
    path: str,
    *,
    max_lines: int = 100,
    max_bytes: int = _MAX_BYTES,
) -> str | None:
    """Read a file, dispatching to the right reader based on its extension.

    Supported formats:

    - ``.txt``, ``.csv``, ``.tsv``, ``.json``, ``.yml``, ``.yaml``,
      ``.xml``, ``.md``, ``.log``, ``.ini``, ``.cfg``, ``.toml``,
      ``.py``, ``.r``, ``.sh`` — plain text, read as UTF-8
    - ``.xlsx`` — Excel via :func:`read_excel`
    - ``.docx`` — Word via :func:`read_docx`
    - ``.pdf`` — via :func:`~builder.tools.scanner.extract_pdf_text`

    Unsupported or unreadable files return *None*.

    Args:
        path: Path to the file.
        max_lines: Max lines/rows for text files and structured formats.
        max_bytes: Max file size in bytes (1 MB default).

    Returns:
        File content as a string, or *None*.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None

    suffix = file_path.suffix.lower()

    if suffix == ".xlsx":
        return read_excel(path, max_rows=max_lines, max_bytes=max_bytes)

    if suffix == ".docx":
        return read_docx(path, max_bytes=max_bytes)

    if suffix == ".pdf":
        from builder.tools.scanner import extract_pdf_text

        return extract_pdf_text(path)

    text_extensions = {
        ".txt", ".csv", ".tsv", ".json", ".yml", ".yaml",
        ".xml", ".md", ".log", ".ini", ".cfg", ".toml",
        ".py", ".r", ".sh", ".bat", ".ps1", ".env",
        ".html", ".htm", ".css", ".js", ".mjs",
    }

    if suffix in text_extensions:
        return _read_text_file(path, max_lines=max_lines, max_bytes=max_bytes)

    return _read_text_file(path, max_lines=max_lines, max_bytes=max_bytes)

# ---------------------------------------------------------------------------
# Explicit ToolRegistry registration
# ---------------------------------------------------------------------------

from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register(
    "read_excel",
    read_excel,
    description="Read an Excel .xlsx file and return its content as pipe-delimited text",
)
TOOL_REGISTRY.register(
    "read_docx",
    read_docx,
    description="Read a Word .docx file and return its text content",
)
TOOL_REGISTRY.register(
    "read_file",
    read_file,
    description="Read any supported file format by extension (txt, csv, json, xlsx, docx, md, pdf)",
)
