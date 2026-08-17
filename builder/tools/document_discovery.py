"""Deterministic, bounded discovery of scientific documentation.

Discovery is deliberately independent of either agent architecture.  It uses
the scanner inventory, reads only small previews inside approved roots, and
returns ranked evidence for ReAct and the deterministic pipeline to consume.
Filenames are signals, never requirements.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from builder.state import FileClassification

_DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".html", ".json", ".md", ".pdf", ".rst",
    ".txt", ".xml", ".xls", ".xlsx", ".yaml", ".yml",
}
_TEXT_MIMES = ("text/", "application/json", "application/xml", "application/pdf")
_MAX_PREVIEW_CHARS = 3_000
_MAX_CONTEXT_CHARS = 12_000
_ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "publication": ("doi", "pmid", "journal", "abstract", "references", "supplementary"),
    "assay_protocol": (
        "assay", "test method", "experimental procedure", "endpoint", "readout"
    ),
    "sop": (
        "sop", "standard operating procedure", "work instruction", "laboratory procedure"
    ),
    "study_plan": (
        "study design", "study plan", "objective", "investigation", "experimental design"
    ),
    "metadata": (
        "metadata", "data dictionary", "sample sheet", "plate map", "condition table"
    ),
    "analysis_method": (
        "data analysis", "analysis method", "statistics", "normalization", "pipeline"
    ),
}
_FILENAME_TERMS = {
    "publication": ("publication", "paper", "article", "doi", "pmid"),
    "assay_protocol": ("assay", "protocol", "method"),
    "sop": ("sop", "standard_operating", "work_instruction"),
    "study_plan": ("study", "investigation", "experiment", "design"),
    "metadata": ("metadata", "sample", "plate", "dictionary", "condition"),
    "analysis_method": ("analysis", "pipeline", "statistics"),
}


@dataclass
class DocumentationCandidate:
    """A compact, ranked documentation candidate."""

    path: str
    filename: str
    relative_path: str
    kind: str
    role: str
    score: float
    reasons: list[str] = field(default_factory=list)
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "kind": self.kind,
            "role": self.role,
            "score": self.score,
            "reasons": list(self.reasons),
            "preview": self.preview,
        }


def _relative(path: str, root: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return Path(path).name


def _is_candidate(file: FileClassification) -> bool:
    suffix = Path(file.filename).suffix.lower()
    return suffix in _DOCUMENT_SUFFIXES or file.mime_type.startswith(_TEXT_MIMES)


# --- evidence kind (#587) ---------------------------------------------------
# What a file IS, decided by shape rather than vocabulary. The old ranking asked
# one question — "how document-like is this?" — of two different populations, and
# narrative won by construction: a +0.12 "prose-like" bonus a spreadsheet cannot
# earn, plus up to +0.24 for sitting near the root. The deposit's 1048-row
# measurement table scored 0.44 against a top-level README's 0.65 while carrying
# twice the content evidence, and the assay-metadata workbooks were scored from
# their filenames alone, because `mode="content"` returns nothing for an .xlsx.
#
# Kind is decidable; role is a guess. Both are reported, and only kind is relied
# on downstream.
KIND_DESCRIPTOR = "descriptor"
KIND_TABULAR = "tabular"
KIND_NARRATIVE = "narrative"
KIND_OPAQUE = "opaque"

_TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls"}
_NARRATIVE_SUFFIXES = {".txt", ".md", ".rst", ".doc", ".docx", ".pdf", ".html", ".xml"}
# Formats with no readable first lines: ask the scanner for its type-aware digest
# (sheet names, column headers, sample paragraphs) instead of an empty string.
_SUMMARY_PREVIEW_SUFFIXES = {".xlsx", ".xls", ".doc", ".docx", ".pdf"}

# Header terms that mark a table as carrying the experiment's own variables. A
# table whose columns name substances, doses and replicates is the crate's raw
# material; one with three bookkeeping columns is not.
_EXPERIMENTAL_HEADER_TERMS = (
    "compound", "substance", "chemical", "cas",
    "concentration", "dose", "exposure",
    "cell", "sample", "well", "plate",
    "replicate", "endpoint", "measurement", "value", "unit", "time",
)

_ROWS_X_COLS = re.compile(r"(\d[\d,]*)\s*rows?\s*[x×]\s*(\d+)\s*cols?", re.IGNORECASE)


def preview_mode_for(filename: str) -> str:
    """Which preview a file needs: binaries have no first lines to read (#587)."""
    return "summary" if Path(filename).suffix.lower() in _SUMMARY_PREVIEW_SUFFIXES else "content"


_STRUCTURED_SUFFIXES = {".json", ".yaml", ".yml"}


# A field name in a structured record: `"key":` (JSON-ish) or a `key:` at the
# start of a line (YAML-ish). Deliberately matched rather than parsed — previews
# are bounded, so a real record arrives truncated and would fail json.loads().
_FIELD_NAME = re.compile(
    r"[\"']([A-Za-z_][\w .\-]{0,40})[\"']\s*:|^\s*([A-Za-z_][\w.\-]{0,40})\s*:", re.M
)
# Below this, a colon-bearing text file is prose, not a record. Measured: the
# fixtures' submission records show 6 distinct field names in their readable
# head; a README shows 0.
_MIN_RECORD_FIELDS = 4


def structured_field_count(text: str) -> int:
    """How many distinct field names *text* carries, from its readable head.

    Deliberately format-blind. An earlier version tested for BioStudies' own
    shape (``accno`` plus an attribute tree). It read every fixture correctly —
    and special-cased one repository's dialect, which is what §Input Formats
    forbids: *"treated as a generic metadata source, not a special-cased input
    type … regardless of the metadata file's format or schema"*. All three
    fixtures happen to be BioStudies, so that detector looked right while being
    unable to recognise an ISA-JSON, DataCite, Zenodo or in-house record.

    What separates a record from a data file needs no dialect: it carries many
    distinct field NAMES, where a data file carries many rows of one shape. This
    counts names rather than parsing, because previews are bounded and a real
    record arrives truncated mid-document.
    """
    return len({m.group(1) or m.group(2) for m in _FIELD_NAME.finditer(text)})


def evidence_kind(filename: str, preview: str) -> str:
    """Classify a file by what it is, from its shape (#587)."""
    suffix = Path(filename).suffix.lower()
    # Gated on the format family, not on any vendor's schema: only a structured
    # document can be a record. Without this, a workbook's *summary* preview
    # ("Sheet1: 148 rows x 3 cols; columns: …") matched the field-name pattern —
    # the scanner's own formatting read as the file's content.
    if suffix in _STRUCTURED_SUFFIXES and structured_field_count(preview) >= _MIN_RECORD_FIELDS:
        return KIND_DESCRIPTOR
    if suffix in _TABULAR_SUFFIXES:
        return KIND_TABULAR
    if suffix in _NARRATIVE_SUFFIXES or suffix in _STRUCTURED_SUFFIXES:
        return KIND_NARRATIVE
    return KIND_OPAQUE


def _table_shape(preview: str) -> tuple[int, int, str]:
    """``(rows, cols, header)`` for a tabular preview, from either preview mode."""
    if match := _ROWS_X_COLS.search(preview):
        rows = int(match.group(1).replace(",", ""))
        cols = int(match.group(2))
        header = preview.split("columns:", 1)[-1] if "columns:" in preview else ""
        return rows, cols, header
    lines = [line for line in preview.splitlines() if line.strip()]
    if not lines:
        return 0, 0, ""
    header = lines[0]
    delimiter = "\t" if header.count("\t") > header.count(",") else ","
    return max(0, len(lines) - 1), header.count(delimiter) + 1, header


def _tabular_signals(preview: str) -> tuple[list[str], float]:
    """Score a table by what it measurably holds, not by how it reads."""
    rows, cols, header = _table_shape(preview)
    reasons: list[str] = []
    if not rows and not cols:
        return ["tabular, but its shape could not be read"], 0.25
    score = 0.35
    reasons.append(f"{rows} row(s) x {cols} column(s)")
    # Rows are evidence, with diminishing returns: 10 rows is meaningfully more
    # than 1, 1000 is not meaningfully more than 500.
    score += min(0.30, math.log10(rows + 1) / 10)
    hits = sorted({t for t in _EXPERIMENTAL_HEADER_TERMS if t in header.casefold()})
    if hits:
        score += min(0.25, len(hits) * 0.05)
        reasons.append(f"experimental columns: {', '.join(hits[:6])}")
    return reasons, score


def _narrative_signals(name: str, preview: str) -> tuple[str, list[str], float]:
    """Score prose by how much of a role's vocabulary it actually covers.

    Coverage rather than presence: the old count saturated at five distinct
    terms and treated one incidental mention of "assay" as evidence — which is
    how a top-level README came to be labelled an assay protocol.
    """
    text = f"{name}\n{preview.casefold()}"
    coverage = {
        role: sum(term in text for term in terms) / len(terms)
        for role, terms in _ROLE_TERMS.items()
    }
    role, share = max(coverage.items(), key=lambda item: (item[1], item[0]))
    reasons: list[str] = []
    if share <= 0:
        return "other_document", ["no role vocabulary matched"], 0.10
    reasons.append(f"{role.replace('_', ' ')} vocabulary {share:.0%} covered")
    score = 0.20 + min(0.45, share * 0.9)
    if any(term in name for term in _FILENAME_TERMS[role]):
        reasons.append("filename supports role")
    return role, reasons, score


def _role_and_signals(filename: str, preview: str) -> tuple[str, list[str], float]:
    """Legacy entry point: the narrative scorer, kept for callers that ask for a role."""
    return _narrative_signals(filename.casefold().replace("-", "_"), preview)


def _kind_and_signals(filename: str, preview: str) -> tuple[str, str, list[str], float]:
    """``(kind, role, reasons, score)`` — the ranking's whole judgement of a file."""
    kind = evidence_kind(filename, preview)
    if kind == KIND_DESCRIPTOR:
        # A structured record: many distinct field names rather than many rows of
        # one shape. Density is the evidence — a three-field config is not a
        # deposit record, and nothing here needs to know which registry wrote it.
        fields = structured_field_count(preview)
        return (
            kind,
            "structured_record",
            [f"structured record, {fields} distinct field(s)"],
            0.60 + min(0.40, fields / 30),
        )
    if kind == KIND_TABULAR:
        reasons, score = _tabular_signals(preview)
        return kind, "data_table", reasons, score
    if kind == KIND_OPAQUE:
        return kind, "other_document", ["no readable text (binary or unsupported)"], 0.10
    role, reasons, score = _narrative_signals(
        filename.casefold().replace("-", "_"), preview
    )
    return kind, role, reasons, score


def _safe_preview(
    path: str, approved_roots: set[str], limit: int, *, mode: str = "content"
) -> str:
    """Read a small preview only after approved-root containment succeeds.

    *mode* is passed through to ``read_file_sample``. It defaults to ``"content"``
    — the first lines of the file — which is right for CSV and text and returns
    NOTHING for a binary format: an .xlsx or .docx has no first lines to read.
    ``"summary"`` gets the file-type-aware digest instead (sheet names, column
    headers, sample paragraphs), which is what a caller describing a workbook
    needs. Exposed rather than hard-coded so a caller can ask for the one it
    needs; the default keeps every existing caller's behaviour unchanged.
    """
    from builder.tools.scanner import _contain, read_file_sample

    contained = _contain(path, approved_roots)
    if contained is None or not contained.is_file():
        return ""
    try:
        value = read_file_sample(str(contained), lines=40, mode=mode)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not value:
        return ""
    return str(value)[:limit]


def discover_documents(
    files: list[FileClassification],
    *,
    input_root: str,
    approved_roots: set[str],
    max_candidates: int = 20,
    max_context_chars: int = _MAX_CONTEXT_CHARS,
) -> list[DocumentationCandidate]:
    """Screen and rank readable scientific documentation deterministically.

    Root and immediate-child files receive a location bonus, but deeper files
    remain eligible: assay SOPs and publications are often nested.  The result
    is stable for equal scores and contains only bounded previews.
    """
    ranked: list[DocumentationCandidate] = []
    for file in files:
        if not _is_candidate(file):
            continue
        relative = _relative(file.path, input_root)
        depth = len(Path(relative).parts) - 1
        # Ask each format for the preview it can actually give: `content` returns
        # nothing for a workbook, which is why the assay-metadata .xlsx files were
        # ranked on their filenames alone (#587).
        preview = _safe_preview(
            file.path,
            approved_roots,
            _MAX_PREVIEW_CHARS,
            mode=preview_mode_for(file.filename),
        )
        kind, role, reasons, score = _kind_and_signals(file.filename, preview)
        # Depth is a tiebreak, not a third of the score. Where a file sits says
        # something about how central it is, and nothing about what it holds.
        score += max(0.0, 0.05 - depth * 0.01)
        reasons.append(f"directory depth {depth}")
        ranked.append(
            DocumentationCandidate(
                path=file.path,
                filename=file.filename,
                relative_path=relative,
                kind=kind,
                role=role,
                score=round(score, 4),
                reasons=reasons,
                preview=preview,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.relative_path.casefold()))
    return _interleave_kinds(ranked, max_candidates)


# Narrative explains what the study IS; tabular carries the values the crate is
# built from. A ranking dominated by either is the same defect — the original
# surfaced seven READMEs and one data file, and scoring tables on their real
# contents simply inverts it, because a deposit holds far more instrument output
# than prose. So the two are interleaved by their own rank rather than competing
# on one scale, and balance follows from the construction rather than from a
# threshold someone has to keep tuning.
_INTERLEAVED_KINDS = (KIND_NARRATIVE, KIND_TABULAR)


def _interleave_kinds(
    ranked: list[DocumentationCandidate], limit: int
) -> list[DocumentationCandidate]:
    """Fill *limit* slots alternating between narrative and tabular, best first.

    The descriptor leads if there is one — a submission record outranks anything
    else in the deposit, being the only file that states the study's own identity.
    Whatever remains (opaque formats, and the tail of either kind) fills the slots
    the alternation does not use, so nothing is excluded merely by its kind.
    """
    by_kind: dict[str, list[DocumentationCandidate]] = {}
    for candidate in ranked:
        by_kind.setdefault(candidate.kind, []).append(candidate)

    chosen = list(by_kind.get(KIND_DESCRIPTOR, []))[:limit]
    queues = [list(by_kind.get(kind, [])) for kind in _INTERLEAVED_KINDS]
    while len(chosen) < limit and any(queues):
        for queue in queues:
            if not queue or len(chosen) >= limit:
                continue
            chosen.append(queue.pop(0))
    if len(chosen) < limit:
        taken = {id(c) for c in chosen}
        chosen.extend(c for c in ranked if id(c) not in taken)
    chosen = chosen[:limit]
    chosen.sort(key=lambda item: (-item.score, item.relative_path.casefold()))
    return chosen


def _context_body(candidate: DocumentationCandidate) -> str:
    """What a file contributes to the model's context.

    Descriptive files contribute their text: the model is deciding what the
    STUDY is, and prose is where that is written. A data table contributes only
    its shape — the header and how many rows sit under it — because its rows are
    for the deterministic readers, not for a language model. Measured on the
    S-VHPS22 fixture, one CSV preview took 3080 characters, a quarter of the
    whole budget, to say what its header says in one line; the protocols that
    describe how the experiment was run were then cut for lack of room.

    The file is still ranked, still named, and still says what it holds, so the
    agent can read it deliberately when it needs the values.
    """
    if candidate.kind != KIND_TABULAR:
        return candidate.preview.strip() or "(preview unavailable; read this file if relevant)"
    rows, cols, header = _table_shape(candidate.preview)
    columns = " ".join(header.split())[:300]
    shape = f"{rows} row(s) x {cols} column(s)" if rows or cols else "shape unread"
    return f"(data table — {shape}" + (f"; columns: {columns})" if columns else ")")


def format_document_context(
    candidates: list[DocumentationCandidate],
    *,
    max_chars: int = _MAX_CONTEXT_CHARS,
    total_scanned: int | None = None,
) -> str:
    """Format ranked candidates as bounded, kind-labelled agent context.

    When *total_scanned* is given and the ranking showed fewer files than were
    scanned, the context says so. A silent cap reads as "this is everything" —
    the same reason the maturity report names the findings it hid rather than
    trailing off (#587).
    """
    parts: list[str] = []
    used = 0
    for candidate in candidates:
        header = f"[{candidate.kind}/{candidate.role}] {candidate.relative_path}\n"
        body = _context_body(candidate)
        block = header + body
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip() + " […]"
        parts.append(block)
        used += len(block) + 2
    context = "\n\n".join(parts)
    if total_scanned is not None and total_scanned > len(candidates):
        hidden = total_scanned - len(candidates)
        context += (
            f"\n\n({len(candidates)} of {total_scanned} scanned files shown; "
            f"{hidden} not surfaced — read any of them directly if this list "
            "does not cover what you need.)"
        )
    return context