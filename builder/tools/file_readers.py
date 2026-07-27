"""Format-aware file readers for structured and unstructured documents.

Provides dedicated readers for Excel (``.xlsx``), Word (``.docx``), and a
unified ``read_file`` dispatcher that routes to the right reader based on
file extension.  All readers enforce size and row limits so the LLM never
gets flooded with large files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Unified size ceiling (Issue #148): matches scanner.read_file_sample /
# extract_pdf_text (100 MB). The readers cap rows/lines independently, so memory
# stays bounded even for large files; the old 1 MB ceiling silently returned
# None for ordinary mid-size files (e.g. a 5 MB CSV), starving the agent.
_MAX_BYTES = 100 * 1024 * 1024  # 100 MB — skip files larger than this
_MAX_ROWS = 500  # max rows to return from structured formats

# Full-return byte budget for plain-text / JSON content (Issue #240). A 32 KB
# JSON is only ~8K tokens and must come back COMPLETE — the old 100-line cap
# dropped the tail, so a weak model never saw fields deep in the file and looped
# "let me read the rest". We return text in full up to this budget; only a file
# that genuinely exceeds it is truncated, and then with an explicit marker.
_TEXT_BUDGET_BYTES = 64 * 1024  # 64 KiB — generous full-return budget for text


def _format_kib(num_bytes: int) -> str:
    """Format a byte count as a compact ``N.N KiB`` string for messages."""
    return f"{num_bytes / 1024:.1f} KiB"


# How many concrete child-file paths to surface in a directory message. Enough
# for a weak model to pick a real file to read next, capped so the tool message
# stays compact on a large directory.
_DIR_LISTING_LIMIT = 25


def _directory_message(path: str) -> str:
    """Actionable message for a reader that was handed a directory (Issue #240).

    The LLM kept calling ``read_file``/``read_file_sample`` on a *directory* and
    got a silent ``None`` each time, then looped. The abstract "use
    list_scanned_files" hint alone still looped a weak model, so this also lists
    the directory's immediate **readable file children** as CONCRETE paths the
    model can read next — the most direct way to break the loop (follow-up to
    #240). Subdirectories are excluded (they would just loop the same way); an
    empty/unreadable directory falls back to the plain guidance.
    """
    base = (
        f"{path} is a directory, not a file — use list_scanned_files to browse "
        f"the inventory, then read a specific file by its path."
    )
    try:
        children = sorted(
            entry
            for entry in Path(path).iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )
    except OSError:
        return base
    if not children:
        return base

    shown = children[:_DIR_LISTING_LIMIT]
    listed = "\n".join(f"  - {child}" for child in shown)
    more = ""
    if len(children) > len(shown):
        more = f"\n  …and {len(children) - len(shown)} more (use list_scanned_files)."
    return (
        f"{base}\nReadable files in this directory — read one of these paths "
        f"directly:\n{listed}{more}"
    )


# ---------------------------------------------------------------------------
# Excel (.xlsx)
# ---------------------------------------------------------------------------


def read_excel(
    path: str,
    *,
    max_rows: int = _MAX_ROWS,
    max_bytes: int = _MAX_BYTES,
    compact: bool = False,
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
        compact: Run the result through :func:`compact_grid_text` (#378), which
            strips the repeated header row, the Comments column and empty cells.
            Defaults to **False** so existing callers see byte-identical output.

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
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True, keep_links=False)
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

    text = "\n".join(parts).rstrip("\n")
    return compact_grid_text(text) if compact else text


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
        doc = Document(str(file_path))
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
                parts.append("| " + " | ".join("---" for _ in table.rows[0].cells) + " |")
                parts.extend(table_rows[1:])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------


def _read_text_file(
    path: str,
    *,
    max_bytes: int = _MAX_BYTES,
    budget: int | None = None,
) -> str | None:
    """Read a plain-text file IN FULL, up to a generous byte *budget* (Issue #240).

    Returns the whole file when it fits in ``budget`` bytes (default
    :data:`_TEXT_BUDGET_BYTES`, 64 KiB). When it exceeds the budget the content
    shown is returned PLUS an explicit, unmistakable truncation marker stating
    how much was shown and that re-reading the same way will NOT return more — so
    a weak model stops looping instead of asking for "the rest".

    The hard ``max_bytes`` safety cap (default 100 MB) still skips genuinely huge
    files entirely (returns *None*) so we never load a 16 MB binary into memory.

    Returns *None* if the file is too large (> ``max_bytes``), binary, or
    unreadable.
    """
    # Resolve the budget at call time so tests can monkeypatch the module attr.
    if budget is None:
        budget = _TEXT_BUDGET_BYTES

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
        # Read up to budget+1 bytes so we can tell whether the file overflowed
        # the budget without slurping the whole (possibly large) file.
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            shown = f.read(budget)
            overflowed = f.read(1) != ""
    except PermissionError:
        logger.warning("Permission denied reading file: %s", path)
        return None
    except Exception:
        logger.exception("Error reading text file: %s", path)
        return None

    content = shown.rstrip("\n")
    if not overflowed:
        return content

    shown_bytes = len(shown.encode("utf-8"))
    marker = (
        f"\n[truncated: showing first {_format_kib(shown_bytes)} of "
        f"{_format_kib(size)}; this is the maximum for this tool — do not "
        f"re-read]"
    )
    return content + marker


def compact_grid_text(text: str) -> str:
    """Densify ``[Sheet: …]`` + pipe-row output, keeping every signal cell (#378).

    A depositor-filled metadata workbook is mostly boilerplate: a ``Parameter |
    Standard or ontology reference | Value | Comments`` header repeated per
    sheet, a Comments column of authoring instructions, and empty cells. On the
    real S-VHPS26 workbook that noise pushes the cell line, RRID, author and
    chemicals 2-5 past any affordable context slice.

    Three rules, in order: drop the repeated header row; drop the trailing
    Comments column **only when that sheet's own header names it** ``Comments``;
    drop empty cells, then drop rows left with fewer than two non-empty cells.

    **Rows are never dropped on the emptiness of one named column.** That rule
    looks right and destroys the General information sheet, because this
    depositor filled column 2 rather than the ``Value`` column — so ``Dr. Fabian
    Wagenaars``, the ORCID, the DOI and the assay name all sit on rows whose
    ``Value`` cell is blank. Text carrying no pipe rows is returned unchanged.
    """
    lines = text.split("\n")
    out: list[str] = []
    drop_last_column = False
    seen_header = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[Sheet:"):
            # A new sheet re-arms header detection; the Comments column is a
            # per-sheet property, not a workbook-wide one.
            seen_header = False
            drop_last_column = False
            out.append(line)
            continue
        if not stripped.startswith("|"):
            out.append(line)
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not seen_header:
            seen_header = True
            drop_last_column = bool(cells) and cells[-1].lower() == "comments"
            if drop_last_column or (cells and cells[0].lower() == "parameter"):
                continue

        if drop_last_column and len(cells) > 1:
            cells = cells[:-1]
        kept = [c for c in cells if c]
        if len(kept) < 2:
            continue
        out.append("| " + " | ".join(kept) + " |")

    return "\n".join(out).strip()


def _flatten_attributes(attrs: Any, out: list[str], indent: str = "") -> None:
    """Emit ``name=value [Qual=Val; …]`` lines for a BioStudies attribute list."""
    for attr in attrs or []:
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("name") or "").strip()
        value = str(attr.get("value") or "").strip()
        if not name and not value:
            continue
        line = f"{indent}{name}={value}" if name else f"{indent}{value}"
        quals: list[str] = []
        for qual in attr.get("valqual") or []:
            if not isinstance(qual, dict):
                continue
            qname = str(qual.get("name") or "").strip()
            qvalue = str(qual.get("value") or "").strip()
            if qname or qvalue:
                quals.append(f"{qname}={qvalue}" if qname else qvalue)
        if quals:
            line += " [" + "; ".join(quals) + "]"
        out.append(line)


def _flatten_section(node: Any, out: list[str]) -> None:
    """Walk a BioStudies section tree, emitting attributes, links and children."""
    if isinstance(node, list):
        for item in node:
            _flatten_section(item, out)
        return
    if not isinstance(node, dict):
        return

    stype = str(node.get("type") or "").strip()
    accno = str(node.get("accno") or "").strip()
    if stype or accno:
        out.append(f"[{stype or 'section'}{' ' + accno if accno else ''}]")

    _flatten_attributes(node.get("attributes"), out)

    for link in node.get("links") or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if url:
            out.append(f"link={url}")
        _flatten_attributes(link.get("attributes"), out, indent="  ")

    if node.get("section") is not None:
        _flatten_section(node["section"], out)
    for sub in node.get("subsections") or []:
        _flatten_section(sub, out)


def compact_attribute_json(text: str) -> str:
    """Flatten a BioStudies ``{name, value, valqual[]}`` tree to dense lines (#378).

    A pretty-printed submission descriptor spends most of its bytes on JSON
    punctuation and indentation, so a bounded slice of the raw file stops inside
    the first section — on the real S-VHPS26 descriptor the licence, the AOP URL
    and every author sit past 2,900 characters and never reach the leaf.

    ``valqual`` entries are preserved as a bracketed suffix. Dropping them is the
    tempting simplification and it loses exactly the qualified values worth
    having — the AOP wiki URL and the BAO ontology ids.

    Input that is not JSON, or is JSON without a BioStudies attribute tree, is
    returned **unchanged** so the caller can apply this blindly.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, dict) or not (
        "attributes" in parsed or "section" in parsed
    ):
        return text

    out: list[str] = []
    accno = str(parsed.get("accno") or "").strip()
    if accno:
        out.append(f"accno={accno}")
    _flatten_section(parsed, out)
    return "\n".join(out).strip()


def read_file(
    path: str,
    *,
    max_lines: int = 100,
    max_bytes: int = _MAX_BYTES,
    compact: bool = False,
) -> str | None:
    """Read a file, dispatching to the right reader based on its extension.

    Supported formats:

    - ``.txt``, ``.csv``, ``.tsv``, ``.json``, ``.yml``, ``.yaml``,
      ``.xml``, ``.md``, ``.log``, ``.ini``, ``.cfg``, ``.toml``,
      ``.py``, ``.r``, ``.sh`` — plain text, read as UTF-8
    - ``.xlsx`` — Excel via :func:`read_excel`
    - ``.docx`` — Word via :func:`read_docx`
    - ``.pdf`` — via :func:`~builder.tools.scanner.extract_pdf_text`

    Plain-text and JSON files are returned **in full** up to a generous byte
    budget (:data:`_TEXT_BUDGET_BYTES`, 64 KiB); a file that genuinely exceeds it
    comes back with an explicit truncation marker so a weak model does not loop
    "read the rest" (Issue #240). A *directory* path returns a clear, actionable
    message (browse the inventory, then read a specific file) instead of *None*.

    Unsupported or unreadable files return *None*.

    Args:
        path: Path to the file.
        max_lines: Max rows to return from structured formats (``.xlsx``); text
            and JSON are governed by the byte budget, not a line cap.
        max_bytes: Hard safety cap in bytes (default 100 MB); files larger than
            this are skipped (returns *None*).
        compact: Apply the shared boilerplate compactors (#378) —
            :func:`compact_grid_text` for ``.xlsx`` grids and
            :func:`compact_attribute_json` for BioStudies ``.json`` descriptors.
            Defaults to **False** so no existing caller's output changes; the
            deterministic pipeline opts in for its bounded context slice.

    Returns:
        File content as a string, a directory-guidance message, or *None*.
    """
    file_path = Path(path)
    if file_path.is_dir():
        return _directory_message(path)
    if not file_path.is_file():
        return None

    suffix = file_path.suffix.lower()

    if suffix == ".xlsx":
        return read_excel(path, max_rows=max_lines, max_bytes=max_bytes, compact=compact)

    if suffix == ".docx":
        return read_docx(path, max_bytes=max_bytes)

    if suffix == ".pdf":
        from builder.tools.scanner import extract_pdf_text

        return extract_pdf_text(path)

    # Everything else is read as plain text (full content up to the byte budget).
    text = _read_text_file(path, max_bytes=max_bytes)
    if compact and text and suffix == ".json":
        return compact_attribute_json(text)
    return text


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
    description="Read any supported file format by extension (txt, csv, json, xlsx, docx, md, pdf). Text/JSON come back in full up to 64 KiB; a larger file is truncated with an explicit marker, and a directory returns guidance to use list_scanned_files",  # noqa: E501
)
