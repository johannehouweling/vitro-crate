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
from pathlib import Path, PurePosixPath
from typing import Any

from builder.state import FileClassification

_DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".html", ".json", ".md", ".pdf", ".rst",
    ".txt", ".xml", ".xls", ".xlsx", ".yaml", ".yml",
}
_TEXT_MIMES = ("text/", "application/json", "application/xml", "application/pdf")
_MAX_PREVIEW_CHARS = 3_000
_MAX_CONTEXT_CHARS = 12_000
_JOINER = "\n\n"
_TRUNCATED = " […]"

@dataclass
class DocumentationCandidate:
    """A compact, ranked documentation candidate."""

    path: str
    filename: str
    relative_path: str
    kind: str
    classification: str
    score: float
    reasons: list[str] = field(default_factory=list)
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "kind": self.kind,
            "classification": self.classification,
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


# --- one file classification (#591) ------------------------------------------
#
# What a file IS, in four values, answered once for every scanned file and read
# by everything downstream. It replaces four competing vocabularies — a
# discovery `role`, the pipeline plan's raw/processed/condition_table, the
# spine's raw_data/processed_data, and the process chain's raw/processed folder
# tier — which disagreed because two of them labelled FORM ("this is a table")
# and two labelled FUNCTION ("this is metadata"), so a tabular metadata workbook
# could only be one and lost the other.
#
# `metadata` and `protocol` are about what the file says; the two data classes
# are about which step produced it. A plate map is `metadata` — it states the
# design that was intended, not a value that was measured (owner's call on #591).
CLASS_METADATA = "metadata"
CLASS_PROTOCOL = "protocol"
CLASS_RAW_DATA = "raw_data_file"
CLASS_PROCESSED_DATA = "processed_data_file"
FILE_CLASSES: tuple[str, ...] = (
    CLASS_METADATA, CLASS_PROTOCOL, CLASS_RAW_DATA, CLASS_PROCESSED_DATA,
)

# CONTENT, then FILENAME, then PATH — the inverse of the folder-tier rule this
# replaces, and the order matters at every step. A first crude pass let the
# extension outrank the content and filed an instrument printout under `raw
# data/` as a protocol because it was a .pdf; letting the path outrank the
# filename filed `assay1_rawdata/README.txt` as a measurement.
#
# Within the filename step, WHAT A FILE IS outranks WHICH TIER it would be: a
# paper called "Normalization of Data for Viability…" is a publication, not
# processed data, even though `normali` is a derived-data word.

_NON_WORD = re.compile(r"[^0-9a-z]+")


def _normalise(text: str) -> str:
    """Casefold and reduce every separator to one space, so terms match tokens.

    ``assay1_processeddata`` becomes ``assay1 processeddata``, which the
    token-anchored terms below then read as the processed tier.
    """
    return _NON_WORD.sub(" ", text.casefold()).strip()


def _hits(text: str, terms: tuple[str, ...]) -> list[str]:
    """Which of *terms* start a word in *text* (already normalised).

    Anchored at the START of a word and open at the end, so one spelling covers
    a family — `normali` catches "normalised" and "normalization", `count`
    catches "counts" — while `raw` still refuses "drawings" and `process`
    refuses "unprocessed", which names the OPPOSITE tier.
    """
    return [t for t in terms if re.search(r"\b" + re.escape(t), text)]


# Content — a table's own column and sheet names.
_METADATA_TABLE = ("metadata", "data dictionary", "sample sheet", "controlled vocabular")
# The VHP4Safety assay-metadata workbooks: one column names a field, the next
# holds its value. That pairing is what a descriptive template looks like in any
# dialect, and no measurement table has it.
_METADATA_TABLE_KEY = ("field", "parameter")
_METADATA_TABLE_VALUE = ("value",)
_PROCESSED_TABLE = (
    "summar", "combin", "normali", "averag", "statistic", "mean", "median",
    "sd", "sem", "stdev", "auc", "ic50", "ec50", "conversion", "relative",
    "fold change", "tidy", "fitted",
)
_RAW_TABLE = (
    "protocol id", "protocol name", "measurement date", "completion status",
    "sample code", "plate readout", "microplate", "end point",
    "cpm", "count", "absorbance", "luminescence", "fluorescence",
)
# Content — prose. The two vocabularies compete on coverage, and a document
# covering neither falls through to its filename rather than being guessed at.
_PROTOCOL_PROSE = (
    "standard operating procedure", "sop", "protocol", "work instruction",
    "materials and methods", "procedure", "incubat", "pipett", "reagent",
    "buffer", "centrifug", "step 1",
)
_METADATA_PROSE = (
    "readme", "metadata", "data dictionary", "sample sheet", "plate map",
    "doi", "pmid", "journal", "abstract", "supplementary", "this folder",
    "file naming", "contents",
)
# GraphPad writes its fitted curves and analyses as this XML root, whatever the
# extension. Content rather than filename, because svhps26 files the project
# beside the instrument export it was fitted from, in one directory naming both
# tiers — the case no folder rule can reach.
_ANALYSIS_PROJECT_ROOT = "graphpadprismfile"

# Filename.
_SCRIPT_SUFFIXES = frozenset({".py", ".r", ".sh", ".ipynb", ".rmd", ".m", ".do", ".sql", ".jl"})
_ANALYSIS_PROJECT_SUFFIXES = frozenset({".prism", ".pzfx", ".pzf"})
_PUBLICATION_NAME = ("et al", "doi", "pmid", "preprint", "manuscript", "supplementary")
_METADATA_NAME = (
    "readme", "metadata", "sample sheet", "samplesheet", "plate map", "platemap",
    "data dictionary", "codebook", "manifest", "condition table",
)
_PROTOCOL_NAME = ("protocol", "sop", "standard operating", "procedure", "work instruction")
# Only for a document format. "Assay" names half the files in a deposit and says
# nothing about a table, but a .docx called "Deiodinase activity assay" is the
# protocol for it.
_PROTOCOL_DOCUMENT_NAME = ("assay", "method")
_PROCESSED_NAME = (
    "process", "combin", "tidy", "summar", "analy", "result", "normali",
    "averag", "mean", "statistic", "stats", "figure", "plot", "graph", "curve",
    "ic50", "ec50", "fitted",
)
_RAW_NAME = ("raw",)

# Path — the last resort, and still the depositor's clearest statement when the
# file itself is silent: an unreadable instrument printout with a timestamp for
# a name is raw because of the folder it was filed in, and nothing else.
_RAW_DIRECTORY = ("raw",)
_PROCESSED_DIRECTORY = ("process",)

# Formats that carry words rather than measurements. Their default is metadata:
# calling a document a measurement would put prose into the derivation chain as
# the instrument's output, which is the more damaging of the two wrong answers.
_DOCUMENT_FORMATS = frozenset(
    {".txt", ".md", ".rst", ".doc", ".docx", ".odt", ".pdf", ".html", ".htm",
     ".xml", ".json", ".yaml", ".yml"}
)
# Of those, the ones only a PERSON ever writes — so the folder they were filed in
# says nothing about them, and the metadata default stands even inside `raw
# data/`. The distinction earns its keep in both directions on svhps22: bench
# notes filed beside the measurements are not measurements, while four of the
# deposit's raw outputs ARE `.pdf`, printed by the counter with a timestamp for
# a filename and no extractable text — for those the folder is all there is.
_AUTHORED_FORMATS = frozenset({".doc", ".docx", ".odt", ".md", ".rst"})


def looks_like_publication(filename: str) -> bool:
    """Whether *filename* reads as a journal article.

    Publications classify as ``metadata`` — they describe the study rather than
    measure it — so the class alone cannot tell the ReAct gap engine that an
    article was deposited and never recorded. This names that signal once,
    beside the classification that consumes it.

    The NAME only, deliberately. Contents mention these words without being an
    article: the deposit record carries ``DOI`` as one of its field names, and an
    assay README template carries "et al" in a worked citation example — both
    fire on content across all three real deposits, and neither is a paper.
    Measured over the same three, the filename alone finds the one article any of
    them ships (``Krebs et al (2018) - Normalization … (ALTEX).pdf``) and nothing
    else.
    """
    return bool(_hits(_normalise(Path(filename).stem), _PUBLICATION_NAME))


def _tabular_class(text: str) -> tuple[str, str] | None:
    """A table's class from its own headers, or ``None`` when they say nothing."""
    if _hits(text, _METADATA_TABLE) or (
        _hits(text, _METADATA_TABLE_KEY) and _hits(text, _METADATA_TABLE_VALUE)
    ):
        return CLASS_METADATA, "content: descriptive columns, not measured values"
    # Processed is asked first because a processed workbook routinely EMBEDS its
    # raw tab — svhps22's own README says so ("Raw data files matching the
    # processed data … can be found in processsed data file: tab: 'Raw data
    # \"date\"'") — so a file naming both is the derived one.
    if hits := _hits(text, _PROCESSED_TABLE):
        return CLASS_PROCESSED_DATA, f"content: derived columns ({', '.join(hits[:3])})"
    if hits := _hits(text, _RAW_TABLE):
        return CLASS_RAW_DATA, f"content: instrument columns ({', '.join(hits[:3])})"
    return None


# How far ahead a vocabulary must be to have decided anything. The two lists are
# not the same length, so one hit each is 8.3% against 7.7% — a winner produced
# by the denominator rather than by the document. That margin handed the study
# README to `protocol` on the strength of the word "incubated"; below it, the
# prose is not decisive and the filename answers instead.
_DECISIVE_COVERAGE_RATIO = 1.5


def _prose_class(text: str) -> tuple[str, str] | None:
    """A document's class from the vocabulary its prose covers, or ``None``.

    Coverage, not presence: one incidental "assay" is not a protocol. A README
    describing what a folder holds is metadata; one describing how the assay was
    run is a protocol, and only the words decide which — when they say enough to
    decide at all.
    """
    (share, name), (runner_up, _) = sorted(
        (
            (len(_hits(text, terms)) / len(terms), name)
            for name, terms in (
                (CLASS_PROTOCOL, _PROTOCOL_PROSE),
                (CLASS_METADATA, _METADATA_PROSE),
            )
        ),
        reverse=True,
    )
    if share <= 0 or share < runner_up * _DECISIVE_COVERAGE_RATIO:
        return None
    return name, f"content: {name} vocabulary {share:.0%} covered"


def _filename_class(stem: str, suffix: str) -> tuple[str, str] | None:
    """A file's class from its own name, or ``None`` when the name says nothing."""
    if suffix in _SCRIPT_SUFFIXES:
        return CLASS_PROTOCOL, "an analysis script is how the work was done"
    if suffix in _ANALYSIS_PROJECT_SUFFIXES:
        return CLASS_PROCESSED_DATA, f"{suffix} is a fitted-curve analysis project"
    text = _normalise(stem)
    if _hits(text, _PUBLICATION_NAME):
        return CLASS_METADATA, "filename: reads as a publication"
    if hits := _hits(text, _METADATA_NAME):
        return CLASS_METADATA, f"filename: {hits[0]!r}"
    named_protocol = _hits(text, _PROTOCOL_NAME) or (
        _hits(text, _PROTOCOL_DOCUMENT_NAME) if suffix in _DOCUMENT_FORMATS else []
    )
    if named_protocol:
        return CLASS_PROTOCOL, f"filename: {named_protocol[0]!r}"
    if hits := _hits(text, _PROCESSED_NAME):
        return CLASS_PROCESSED_DATA, f"filename: {hits[0]!r}"
    if hits := _hits(text, _RAW_NAME):
        return CLASS_RAW_DATA, f"filename: {hits[0]!r}"
    return None


def _directory_class(relative_path: str) -> tuple[str, str] | None:
    """The tier the containing directories declare, or ``None``.

    ``None`` when they name BOTH tiers or neither, and the refusal is not a
    corner case: svhps21 and svhps26 file every run under one ``Raw data +
    individual processed data/``. Reading just the first match there would hand
    every processed file in both deposits to the EndpointReadout.
    """
    directories = _normalise(" ".join(PurePosixPath(relative_path.replace("\\", "/")).parts[:-1]))
    named = {
        cls
        for cls, terms in (
            (CLASS_RAW_DATA, _RAW_DIRECTORY),
            (CLASS_PROCESSED_DATA, _PROCESSED_DIRECTORY),
        )
        if _hits(directories, terms)
    }
    if len(named) != 1:
        return None
    cls = named.pop()
    return cls, "the containing directory declares the tier"


def classify_file(filename: str, preview: str, relative_path: str = "") -> tuple[str, str]:
    """``(class, reason)`` for one file — content first, then name, then folder.

    Args:
        filename: The file's base name; its suffix decides which content rules
            apply and what the fallback is.
        preview: A bounded read of the file, in whichever mode
            :func:`preview_mode_for` asks for. Empty is normal — every ``.docx``
            in these deposits previews as nothing.
        relative_path: The path within the deposit, used only for the folder
            tier. Defaults to *filename*, i.e. no containing directories.

    Returns:
        One of :data:`FILE_CLASSES`, and the signal that decided it.
    """
    suffix = Path(filename).suffix.lower()
    text = _normalise(preview)
    if text:
        if _ANALYSIS_PROJECT_ROOT in text:
            return CLASS_PROCESSED_DATA, "content: a GraphPad Prism analysis project"
        if suffix in _STRUCTURED_SUFFIXES and structured_field_count(preview) >= _MIN_RECORD_FIELDS:
            return CLASS_METADATA, "content: a structured record, many distinct field names"
        decided = (
            _tabular_class(text)
            if evidence_kind(filename, preview) == KIND_TABULAR
            else _prose_class(text)
        )
        if decided:
            return decided
    if decided := _filename_class(Path(filename).stem, suffix):
        return decided
    if suffix not in _AUTHORED_FORMATS:
        if decided := _directory_class(relative_path or filename):
            return decided
    if suffix in _DOCUMENT_FORMATS:
        return CLASS_METADATA, "nothing said otherwise, and a document is not a measurement"
    return CLASS_RAW_DATA, "nothing said otherwise; least-transformed is the safer default"


def classification_of(file: FileClassification, *, input_root: str = "") -> str:
    """The class of an inventory record, derived on the spot if not yet stamped.

    :func:`classify_scanned_files` stamps every file once, from a real preview.
    This answers for a record that never went through it — a resumed session
    saved before #591, or any caller holding the inventory without the deposit
    mounted — using the scanner's own ``first_rows`` as the content. Never
    touches the disk, because the callers that need it (the process chain) run
    with no approved roots and often no deposit at all.
    """
    if file.classification:
        return file.classification
    preview = "\n".join(file.first_rows or [])
    return classify_file(file.filename, preview, _relative(file.path, input_root))[0]


# How many files of a folder are opened to decide what the folder holds (#598).
# A deposit is a handful of homogeneous groups, not N distinct things: svhps22's
# 1468 scanned files fall into 149 `(directory, extension)` groups, the largest
# holding 84 gamma-counter printouts. Opening one of those says what the other
# 83 are; opening all 84 costs 84 PDF parses to learn nothing. Three leaves that
# deposit reading 324 of its 1468 files, and every file of the other two.
_EXEMPLARS_PER_GROUP = 3

# The one class a folder may be summarised into. Instrument output is what makes
# a deposit big and what makes its files interchangeable: 1358 of svhps22's 1468
# files, and a gamma-counter printout says nothing its 83 siblings did not. The
# other three tiers are read in full however large the folder, because a
# propagated file has no preview, and a workbook with no preview is ranked on
# its filename alone — the defect #587 fixed.
_SUMMARISABLE_CLASSES = frozenset({CLASS_RAW_DATA})

# How big a folder must be before summarising it is worth being blind to the
# rest of it — four times the sample. Below that the saving is a rounding error
# and the cost is real: svhps26 files six per-plate workbooks per run directory,
# and summarising those cost eight of the twenty ranked slots while saving 20
# reads of 91. Measured across 4..48, the deposits are flat between 4 and 32
# (svhps22: 292 to 392 reads of 1468) and collapse at 48, where its own
# printout folders start falling under the floor.
_MIN_SUMMARISABLE_GROUP = 4 * _EXEMPLARS_PER_GROUP


def _group_key(file: FileClassification) -> tuple[str, str]:
    """What makes two scanned files the same kind of thing: one folder, one format."""
    name = file.filename or Path(file.path).name
    return str(Path(file.path).parent), Path(name).suffix.lower()


def _exemplar_indexes(size: int, count: int) -> list[int]:
    """*count* indexes spread evenly across ``range(size)``, both ends included.

    Spread rather than the first *count*: a folder sorted by run date puts its
    exception at the end as often as at the front, and a sample that never
    reaches the last file cannot see it.
    """
    if size <= count:
        return list(range(size))
    if count <= 1:
        return [0]
    step = (size - 1) / (count - 1)
    return sorted({round(i * step) for i in range(count)})


def classify_scanned_files(
    files: list[FileClassification], *, input_root: str, approved_roots: set[str]
) -> dict[str, str]:
    """Stamp every scanned file with its class, and return the previews read.

    EVERY file, not the ranked subset: :func:`discover_documents` caps at 20
    candidates because its job is filling a bounded prompt, and what gets wired
    into the crate must not depend on what fits in a context window.

    Not every file is OPENED, though (#598). Files are grouped by containing
    directory and extension, :data:`_EXEMPLARS_PER_GROUP` files spread across
    each group are read, and a group whose sample agrees takes that verdict
    across the rest without touching them. A group is opened in full when it is
    under :data:`_MIN_SUMMARISABLE_GROUP`, when its sample DISAGREES — the
    folder is heterogeneous and cannot be summarised — or when the sample lands
    on anything but :data:`_SUMMARISABLE_CLASSES`.

    What this cannot see is a file whose CONTENT alone makes it an exception,
    sitting in the interior of a large uniform folder: nothing about its name,
    extension or directory sets it apart, so only opening it would tell. On the
    three real deposits it costs nothing — every one of 1622 files keeps the
    class a full read gives it, and every top-20 ranking is unchanged — while
    svhps22 drops from 1468 reads to 324, about ten times faster.

    Returns:
        ``{path: preview}`` for every file, so the ranking that follows reads
        none of them again. A file the sample spoke for maps to ``""``: it has
        no preview, and the empty string is what stops :func:`discover_documents`
        from opening the deposit a second time to find one.
    """
    previews: dict[str, str] = {}

    def read_and_stamp(file: FileClassification) -> str:
        preview = _safe_preview(
            file.path, approved_roots, _MAX_PREVIEW_CHARS, mode=preview_mode_for(file.filename)
        )
        previews[file.path] = preview
        decided = classify_file(file.filename, preview, _relative(file.path, input_root))[0]
        file.classification = decided
        return decided

    groups: dict[tuple[str, str], list[FileClassification]] = {}
    for file in files:
        groups.setdefault(_group_key(file), []).append(file)

    for group in groups.values():
        group.sort(key=lambda file: file.path)
        sampled = set(_exemplar_indexes(len(group), _EXEMPLARS_PER_GROUP))
        verdicts = {read_and_stamp(group[index]) for index in sampled}
        rest = [file for index, file in enumerate(group) if index not in sampled]
        if not rest:
            continue
        if (
            len(group) < _MIN_SUMMARISABLE_GROUP
            or len(verdicts) > 1
            or not verdicts <= _SUMMARISABLE_CLASSES
        ):
            for file in rest:
                read_and_stamp(file)
            continue
        agreed = next(iter(verdicts))
        for file in rest:
            previews[file.path] = ""
            file.classification = agreed
    return previews


# The prose analogue of :data:`_EXPERIMENTAL_HEADER_TERMS`, and a RANKING list,
# not a classifying one. Ranking asks how much a document would tell a model
# about the study; classification asks what the document is. Answering the second
# needs words that separate a procedure from a description, and those are
# deliberately narrow — which makes them a poor measure of the first, because a
# README covering the whole study earns one hit for "incubated". Two questions,
# two vocabularies; what #591 collapsed was four lists all answering the SAME
# question differently.
#
# The ranking does NOT yet use the classification to allocate its 20 slots, and
# should: a `.docx` previews as a paragraph count, so a protocol's score comes
# almost entirely from which of these words are in its FILENAME, and 7 of
# svhps22's 13 protocol documents miss the cut while 9 of its 14 metadata files
# are named. Tracked in #595.
_STUDY_PROSE_TERMS = (
    "assay", "endpoint", "readout", "cell", "compound", "substance",
    "concentration", "dose", "exposure", "incubat", "replicate", "protocol",
    "study", "experiment", "control", "measur",
)


def _narrative_signals(text: str) -> tuple[list[str], float]:
    """Score prose by how much of the study's vocabulary it actually covers.

    Coverage rather than presence: a count saturated at five distinct terms and
    treated one incidental mention of "assay" as evidence, which is how a
    top-level README came to be ranked an assay protocol (#587).
    """
    hits = _hits(text, _STUDY_PROSE_TERMS)
    if not hits:
        return ["no study vocabulary matched"], 0.10
    share = len(hits) / len(_STUDY_PROSE_TERMS)
    return [f"study vocabulary {share:.0%} covered"], 0.20 + min(0.45, share * 0.9)


def _kind_and_signals(
    filename: str, preview: str, relative_path: str = ""
) -> tuple[str, str, list[str], float]:
    """``(kind, classification, reasons, score)`` — the ranking's whole judgement.

    *kind* is the file's FORM and drives how it is rendered into the prompt;
    *classification* is what it IS and is what the crate is built from. Both are
    reported because they answer different questions about the same file.
    """
    kind = evidence_kind(filename, preview)
    classification, reason = classify_file(filename, preview, relative_path or filename)
    if kind == KIND_DESCRIPTOR:
        # A structured record: many distinct field names rather than many rows of
        # one shape. Density is the evidence — a three-field config is not a
        # deposit record, and nothing here needs to know which registry wrote it.
        fields = structured_field_count(preview)
        reasons = [f"structured record, {fields} distinct field(s)"]
        score = 0.60 + min(0.40, fields / 30)
    elif kind == KIND_TABULAR:
        reasons, score = _tabular_signals(preview)
    elif kind == KIND_OPAQUE:
        reasons, score = ["no readable text (binary or unsupported)"], 0.10
    else:
        reasons, score = _narrative_signals(_normalise(f"{filename} {preview}"))
    return kind, classification, [*reasons, reason], score


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
    previews: dict[str, str] | None = None,
) -> list[DocumentationCandidate]:
    """Screen and rank readable scientific documentation deterministically.

    Root and immediate-child files receive a location bonus, but deeper files
    remain eligible: assay SOPs and publications are often nested.  The result
    is stable for equal scores and contains only bounded previews.

    *previews* are what :func:`classify_scanned_files` already read, keyed by
    path; supplying them keeps the deposit read once rather than twice. An entry
    is present but empty for a file that classification summarised rather than
    opened (#598) — which is what stops this from opening it after all, and
    leaves it ranked and rendered like any other file with no readable preview.
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
        preview = (previews or {}).get(file.path)
        if preview is None:
            preview = _safe_preview(
                file.path,
                approved_roots,
                _MAX_PREVIEW_CHARS,
                mode=preview_mode_for(file.filename),
            )
        kind, classification, reasons, score = _kind_and_signals(
            file.filename, preview, relative
        )
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
                classification=classification,
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


def _fair_shares(needs: list[int], budget: int) -> list[int]:
    """Split *budget* over *needs* so no one entry can crowd out the others.

    Walking the list in rank order and spending until the budget ran out meant a
    long document silently deleted every cheaper one behind it: on svhps22 three
    READMEs at ~2 500 characters each consumed the whole context and all five
    protocols dropped out of the listing — not shortened, never named, and the
    agent cannot read a file it was not told exists.

    Max-min fair instead, which needs no per-file constant to tune: every entry is
    offered an equal share, anything asking for less than its share is met in
    full, and what it does not spend flows to the entries that want more. Small
    entries (a data table's one-line shape, a ``.docx`` whose preview is a
    paragraph count) are therefore always affordable, and the long documents
    divide what is left between them.
    """
    shares = [0] * len(needs)
    remaining, left = budget, len(needs)
    for index in sorted(range(len(needs)), key=lambda i: needs[i]):
        grant = min(needs[index], remaining // left)
        shares[index] = grant
        remaining -= grant
        left -= 1
    return shares


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
    headers = [f"[{c.kind}/{c.classification}] {c.relative_path}\n" for c in candidates]
    bodies = [_context_body(c) for c in candidates]
    blocks = [header + body for header, body in zip(headers, bodies)]
    # Each block's share has to cover the separator that follows it, or the
    # emitted total overruns the ceiling by two characters per candidate.
    needs = [len(block) + len(_JOINER) for block in blocks]
    parts: list[str] = []
    shares = _fair_shares(needs, max_chars)
    for header, body, block, allowed in zip(headers, bodies, blocks, shares):
        room = allowed - len(_JOINER)
        if room >= len(block):
            parts.append(block)
        # Only the BODY is truncated, and only once the header is paid for in
        # full. An equal share of a small enough budget otherwise lands inside
        # the header, and the listing becomes a column of `[nar […]` naming
        # nothing — twenty entries worse than the rank-order spend this replaced,
        # which at least bought one readable one. The agent cannot read a file it
        # was not told the name of, so a block that cannot carry its own header
        # is not written and its share goes to the candidates that can use it.
        elif room >= len(header) + len(_TRUNCATED):
            kept = body[: room - len(header) - len(_TRUNCATED)].rstrip()
            parts.append(header + kept + _TRUNCATED)
    context = _JOINER.join(parts)
    if total_scanned is not None and total_scanned > len(candidates):
        hidden = total_scanned - len(candidates)
        context += (
            f"\n\n({len(candidates)} of {total_scanned} scanned files shown; "
            f"{hidden} not surfaced — read any of them directly if this list "
            "does not cover what you need.)"
        )
    return context