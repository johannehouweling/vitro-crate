"""Frictionless payload / data-content validation layer (Issue #95).

This is a **separate layer** from the SHACL metadata validator
(``profiles/validator.py``). The split mirrors the BioHackEU25 report "Towards a
Robust Validation Service for Data and Metadata in ARC RO-Crates" (Chadwick et
al., biohackrxiv ``zah28``):

- **SHACL** checks the *metadata descriptor* — the structure and semantics of
  ``ro-crate-metadata.json`` (does the graph declare the right entities,
  properties, profiles).
- **Frictionless** checks the *payload* — do the rows in a referenced CSV
  actually match its declared CSVW / Frictionless ``tableSchema`` (column types,
  value constraints, and foreign keys to ``MolecularEntity`` / ``Sample`` ids)?

The obvious payload to validate here is the CSVW condition table emitted by
#94 (the per-well design table) and any raw-measurement tables: do the cells
match the declared datatypes, and do the compound / cell-line columns reference
ids that actually exist in the crate?

Issues come back in the **same routable shape as #87**
(``{entity_id, property, message, fix, severity, profile}``) so the agent routes
a data-content fix exactly as it routes a SHACL fix. The ``profile`` is the new
``"data"`` layer, keeping it cleanly distinct from ``base`` / ``isa`` / ``tox``.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from builder.state import CrateState

logger = logging.getLogger(__name__)

# Source-header synonyms -> the canonical condition-table title (#381). Real plate
# maps do not use the ten canonical names, and the writer below drops anything that
# does not match one exactly, so without this map a genuine plate map produces a
# table of blank cells — strictly worse than the honest header-only placeholder.
#
# ``concentration`` / ``unit`` / ``duration`` are the pre-#180 five-column names the
# ReAct tool description advertised long after they stopped existing; a model that
# obeyed that description had every one of its values silently discarded.
#
# The ``*_id`` / ``biosample_*`` / ``exposure_*`` block is the tidy per-well export
# vocabulary (#471), read off the header of the corpus's only genuine tidy design
# table (S-VHPS22 ``Combined uptake data EDCs_tidy.csv``, 1048 rows). Not one of its
# fifteen headers matched a canonical title or an alias, so the whole table hit the
# refusal gate and the study shipped a header-only placeholder.
#
# Six headers of that file are deliberately NOT aliased and keep surfacing in
# ``unmapped_source_columns``: ``measurement_*`` / ``notes`` are results and prose,
# not design, and ``biosample_id`` (blank in every row there) would collide with
# ``biosample_type`` on ``cell_line`` — see the collision rule in
# :func:`project_condition_rows`. Reporting them is the honest outcome; inventing a
# canonical home for a measurement column is exactly the fabrication D5 forbids.
_CONDITION_TABLE_ALIASES: dict[str, str] = {
    "well": "well_id",
    "well_position": "well_id",
    # A tidy export has no plate geometry: ``run_id`` is its per-row key, and
    # well_id is typed dcterms:identifier (an identifier, not a coordinate), so the
    # source's own key goes there verbatim. Sending run_id to ``experiment`` instead
    # reads better in isolation but leaves every row without a well key, and the
    # second refusal gate below then drops all 1048 rows on the floor.
    "run_id": "well_id",
    "concentration": "concentration_value",
    "conc": "concentration_value",
    "dose": "concentration_value",
    "exposure_concentration_value": "concentration_value",
    "unit": "concentration_unit",
    "units": "concentration_unit",
    "exposure_concentration_unit": "concentration_unit",
    "duration": "exposure_duration",
    "exposure_time": "exposure_duration",
    "cell": "cell_line",
    "cell line": "cell_line",
    "biosample_type": "cell_line",
    "chemical": "compound",
    "substance": "compound",
    "test_item": "compound",
    "test_substance_id": "compound",
    # The endpoint names the readout the row belongs to (T3 / T4 uptake); ``assay``
    # is the only canonical column that carries it, and it is a plain string column
    # — no entity resolution rides on it (CONDITION_TABLE_REFERENCE_COLUMNS covers
    # compound and cell_line only), so this cannot turn prose into a false id claim.
    "assay_endpoint": "assay",
    "replicate": "technical_replicate",
    "replicate_id": "technical_replicate",
}

# Tidy sources split a duration into value + unit columns where the canonical table
# has ONE string column (#471). Aliasing both to ``exposure_duration`` would make
# them collide, and the loser would be dropped — emitting a bare ``24`` with the
# ``h`` discarded is a claim we must never make, since h and d differ by 24x, the
# same magnitude trap the uM/mM suffix rule exists to avoid. So the pair is composed
# into one literal (``"24 h"``) instead. D5 as everywhere else here: the unit is
# carried verbatim and never lifted to a UO IRI.
#
# Keyed by canonical column -> (value header, unit header), both lower-cased.
_CONDITION_TABLE_UNIT_PAIRS: dict[str, tuple[str, str]] = {
    "exposure_duration": ("exposure_duration_value", "exposure_duration_unit"),
}

# A dose column that carries its unit in the header (``concentration_uM``,
# ``dose_mM``): the value feeds concentration_value and the suffix becomes the
# literal concentration_unit. D5 — the suffix is prose from a filename, so it is
# carried verbatim and never lifted to a UO IRI; that needs an authoritative lookup.
_UNIT_SUFFIX_RE = re.compile(r"^(?:concentration|conc|dose)_(?P<unit>[A-Za-zµ%/]+)$", re.IGNORECASE)

# CSVW datatype (from #94's _CONDITION_TABLE_COLUMNS) -> Frictionless field type.
# Anything unmapped falls back to "string" (Frictionless's permissive default).
_CSVW_TO_FRICTIONLESS_TYPE: dict[str, str] = {
    "string": "string",
    "double": "number",
    "decimal": "number",
    "float": "number",
    "integer": "integer",
    "int": "integer",
    "boolean": "boolean",
    "date": "date",
    "dateTime": "datetime",
}

# The routable-issue layer name for data-content issues — deliberately distinct
# from the SHACL layers ("base" | "isa" | "tox") so the two validation services
# never entangle (Issue #95).
DATA_CONTENT_PROFILE = "data"

# Frictionless 5 error types we map to a per-column fix hint. Other error types
# fall back to a generic hint built from the column name.
_FIX_TEMPLATES: dict[str, str] = {
    "type-error": "Fix the value in column `{column}` so it matches its declared type.",
    "constraint-error": "Fix the value in column `{column}` so it satisfies the schema constraint.",
    "foreign-key": (
        "Use a `{column}` value that resolves to an existing entity id "
        "(MolecularEntity / Sample), or add the missing entity."
    ),
}


def _fix_for(error_type: str, column: str | None) -> str:
    """Build a short, actionable fix hint for a Frictionless error."""
    col = column or "the affected column"
    template = _FIX_TEMPLATES.get(error_type)
    if template:
        return template.format(column=col)
    if column:
        return f"Correct the data in column `{col}` so it conforms to the table schema."
    return "Correct the table data so it conforms to the declared table schema."


def _routable(
    *,
    entity_id: str | None,
    column: str | None,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    """A data-content issue in #87's routable shape."""
    return {
        "entity_id": entity_id,
        "property": column,
        "message": message,
        "fix": _fix_for(error_type, column),
        "severity": "required",
        "profile": DATA_CONTENT_PROFILE,
    }


def validate_table(
    file: str,
    table_schema: dict[str, Any],
    foreign_keys: dict[str, list[str]] | None = None,
    *,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Validate a CSV's *content* against its CSVW / Frictionless table schema.

    This is the payload/data-content validation layer (Issue #95): it checks that
    the rows of ``file`` match the column types and constraints declared in
    ``table_schema`` and, optionally, that designated columns reference only
    known entity ids (``MolecularEntity`` / ``Sample``). It does **not** touch
    the SHACL metadata validator — the two are independent layers.

    Args:
        file: Path to the CSV file to validate (the condition table or a
            raw-measurement table).
        table_schema: A Frictionless table schema descriptor — a ``{"fields":
            [{"name", "type", "constraints"?}, ...]}`` dict. CSVW column entries
            (``datatype`` / ``propertyUrl``) emitted by #94 should be adapted to
            this shape before being passed in.
        foreign_keys: Optional mapping of ``column_name -> [allowed_id, ...]``.
            Each named column is checked so every cell value is one of the
            allowed ids (the in-crate ``MolecularEntity`` / ``Sample`` ids).
            Values not in the list are reported as foreign-key issues.
        entity_id: Optional id of the crate entity that owns the table (e.g. the
            condition-table ``File``), echoed back on each issue so the agent can
            route the fix to the right node.

    Returns:
        ``{"ok": bool, "issues": [issue, ...]}`` where each issue is the #87
        routable shape ``{entity_id, property, message, fix, severity, profile}``
        with ``profile == "data"``. On a setup error (missing file, malformed
        schema) ``ok`` is False and an ``"error"`` key carries the reason; the
        function never raises into the agent loop.
    """
    # Imported lazily so the rest of builder.tools imports without frictionless
    # being installed (it is only needed for this layer).
    try:
        from frictionless import Package, Resource, Schema, system
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        logger.warning("Frictionless not available: %s", e)
        return {
            "ok": False,
            "issues": [],
            "error": f"Frictionless data-content validation not available: {e}",
        }

    path = Path(file)
    if not path.is_file():
        return {
            "ok": False,
            "issues": [],
            "error": f"Table file not found: {file}",
        }

    try:
        schema = Schema.from_descriptor(dict(table_schema))
    except (TypeError, ValueError, AttributeError) as e:
        return {
            "ok": False,
            "issues": [],
            "error": f"Invalid table schema: {e}",
        }

    foreign_keys = foreign_keys or {}

    try:
        # Frictionless 5 refuses absolute/unsafe paths unless the running context
        # is marked trusted; this is a local, user-approved file, so opt in.
        with system.use_context(trusted=True):
            report = _run_validation(Package, Resource, schema, path, foreign_keys)
    except Exception as e:  # noqa: BLE001 - surface as a tool error, never crash the loop
        logger.error("validate_table failed: %s", e)
        return {"ok": False, "issues": [], "error": str(e)}

    issues = _issues_from_report(report, entity_id)
    return {"ok": not issues, "issues": issues}


def _run_validation(
    Package: Any,
    Resource: Any,
    schema: Any,
    path: Path,
    foreign_keys: dict[str, list[str]],
) -> Any:
    """Validate the table, wiring optional foreign-key reference resources.

    Foreign keys in Frictionless are checked against a reference *resource*. We
    synthesise an in-memory single-column resource of allowed ids per foreign-key
    column and add an inline ``foreignKeys`` rule to the schema, then validate the
    whole thing as a Package so the lookups resolve.
    """
    if not foreign_keys:
        return Resource(path=str(path), schema=schema).validate()

    ref_resources = []
    for column, allowed in foreign_keys.items():
        ref_name = f"_fk_{column}"
        rows = [{"id": value} for value in allowed]
        ref_resources.append(Resource(name=ref_name, data=rows))
        schema.foreign_keys.append(
            {
                "fields": [column],
                "reference": {"resource": ref_name, "fields": ["id"]},
            }
        )

    main = Resource(name="table", path=str(path), schema=schema)
    package = Package(resources=[main, *ref_resources])
    return package.validate()


def _issues_from_report(report: Any, entity_id: str | None) -> list[dict[str, Any]]:
    """Translate a Frictionless validation report into routable issues."""
    issues: list[dict[str, Any]] = []
    for task in report.tasks:
        for error in task.errors:
            column = getattr(error, "field_name", None) or None
            error_type = getattr(error, "type", "") or ""
            note = getattr(error, "note", None) or str(error)
            cell = getattr(error, "cell", None)
            row_number = getattr(error, "row_number", None)
            message = _compose_message(error_type, column, cell, row_number, note)
            issues.append(
                _routable(
                    entity_id=entity_id,
                    column=column,
                    error_type=error_type,
                    message=message,
                )
            )
    return issues


def _compose_message(
    error_type: str,
    column: str | None,
    cell: Any,
    row_number: int | None,
    note: str,
) -> str:
    """A human-readable, self-contained message that names the offending value."""
    parts: list[str] = []
    if row_number is not None:
        parts.append(f"row {row_number}")
    if column:
        parts.append(f"column '{column}'")
    location = ", ".join(parts)
    prefix = f"{location}: " if location else ""
    cell_str = f" (value: {cell!r})" if cell not in (None, "") else ""
    return f"{prefix}{note}{cell_str}"


def csvw_to_frictionless(columns: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Convert CSVW column descriptors into a Frictionless table schema.

    The condition table's typed columns are authored once as CSVW
    (``_crate_mapping._CONDITION_TABLE_COLUMNS``: ``titles`` + ``datatype`` +
    ``propertyUrl``; #94). This bridges them into the Frictionless
    ``{"fields": [{"name", "type"}, ...]}`` shape so :func:`validate_table` can
    check the populated CSV without a separately hand-authored schema — keeping
    the #94 CSVW typing the single source of truth for both layers (#144).

    Args:
        columns: An iterable of CSVW column dicts, each with at least a
            ``titles`` (the column name) and optionally a ``datatype``.

    Returns:
        A Frictionless table schema descriptor ``{"fields": [...]}``.
    """
    fields: list[dict[str, Any]] = []
    for col in columns:
        name = col.get("titles") or col.get("name")
        if not name:
            continue
        datatype = str(col.get("datatype", "string"))
        fields.append(
            {
                "name": str(name),
                "type": _CSVW_TO_FRICTIONLESS_TYPE.get(datatype, "string"),
            }
        )
    return {"fields": fields}


def project_condition_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project source rows onto the canonical condition-table columns (#381).

    Maps each source header to its canonical title via
    :data:`_CONDITION_TABLE_ALIASES` and the suffix-unit rule
    (:data:`_UNIT_SUFFIX_RE`), so a real plate map populates real columns instead
    of being dropped by the writer's ``extrasaction="ignore"``.

    Precedence is deliberate: a **canonical** source name always wins, and an alias
    never overwrites a canonical value already set on that row. So a row carrying
    both ``well_id`` and ``well`` keeps ``well_id``, and an explicit
    ``concentration_unit`` survives a ``dose_uM`` header.

    Two *aliases* can legitimately target the same canonical column (``chemical``
    and ``test_substance_id`` both mean ``compound``). When both carry a value on
    one row the first in the source's own column order wins and the second is
    dropped — a defined outcome rather than a coin flip, because a row mapping is
    ordered. The loser is NOT reported in ``unmapped_source_columns``: that list
    means "no canonical home", and a collision loser had one. Where a split source
    would collide by construction — a value column and its unit column, both meaning
    ``exposure_duration`` — :data:`_CONDITION_TABLE_UNIT_PAIRS` composes the two into
    one literal instead of letting either half be silently discarded.

    Args:
        rows: Source row mappings, keyed by whatever headers the source used.

    Returns:
        ``{"rows": [...], "mapped_columns": [...], "unmapped_source_columns": [...]}``
        — the projected rows, the canonical columns any row populated, and the
        source columns nothing could be done with (reported, never silently
        swallowed: a measurement column like ``tpo_activity_rfu`` has no canonical
        home and the caller deserves to know it was skipped).
    """
    from builder.tools._crate_mapping import _CONDITION_TABLE_HEADER

    canonical = _CONDITION_TABLE_HEADER.strip("\n").split(",")
    canonical_set = set(canonical)

    projected: list[dict[str, Any]] = []
    mapped: set[str] = set()
    unmapped: set[str] = set()

    for row in rows:
        out: dict[str, Any] = {}
        # Canonical headers first, so an alias can never displace them.
        for key, value in row.items():
            name = str(key).strip()
            if name in canonical_set and _has_value(value):
                out[name] = value
                mapped.add(name)
        # Value+unit pairs are composed BEFORE the alias pass, so both halves reach
        # the single canonical column they share instead of colliding there.
        lowered_row = {str(k).strip().lower(): v for k, v in row.items()}
        # Headers the pair rule CLAIMED on this row, i.e. suppressed from
        # `unmapped_source_columns`. A header is claimed only once it has actually
        # been consumed — or had nothing to give. Claiming the pair unconditionally
        # is the trap: a unit column whose value column is missing is genuinely
        # discarded, so claiming it would drop the `d` from a two-day exposure AND
        # hide that from the caller. That is strictly worse than the collision this
        # rule exists to prevent, because the report is the only thing left that
        # could notice.
        paired_headers: set[str] = set()
        for target, (value_header, unit_header) in _CONDITION_TABLE_UNIT_PAIRS.items():
            magnitude = lowered_row.get(value_header)
            unit = lowered_row.get(unit_header)
            # A canonical `exposure_duration` on the row outranks the split pair,
            # exactly as it outranks an alias. Both halves then lose a DEFINED
            # collision, and a collision loser is never reported unmapped — it had
            # a canonical home, it just did not win it.
            if target in out:
                paired_headers.update((value_header, unit_header))
                continue
            if not _has_value(magnitude):
                # No magnitude, nothing to compose. A blank unit is claimed anyway
                # (the header was understood, the row was empty — what the alias
                # pass does with a blank value too); a unit carrying a REAL value
                # is deliberately left unclaimed, so it surfaces in
                # `unmapped_source_columns` rather than vanishing.
                paired_headers.add(value_header)
                if not _has_value(unit):
                    paired_headers.add(unit_header)
                continue
            composed = str(magnitude).strip()
            if _has_value(unit):
                composed = f"{composed} {str(unit).strip()}"
            out[target] = composed
            mapped.add(target)
            paired_headers.update((value_header, unit_header))

        for key, value in row.items():
            name = str(key).strip()
            if name in canonical_set:
                continue
            lowered = name.lower()
            if lowered in paired_headers:
                continue
            target = _CONDITION_TABLE_ALIASES.get(lowered)
            if target is not None:
                if _has_value(value) and target not in out:
                    out[target] = value
                    mapped.add(target)
                continue
            # Match the ORIGINAL header, not the lower-cased one: the regex is
            # already case-insensitive, and the captured suffix must keep its case
            # because uM / mM / nM differ by three orders of magnitude each.
            suffix = _UNIT_SUFFIX_RE.match(name)
            if suffix is not None:
                if _has_value(value) and "concentration_value" not in out:
                    out["concentration_value"] = value
                    mapped.add("concentration_value")
                    if "concentration_unit" not in out:
                        out["concentration_unit"] = suffix.group("unit")
                        mapped.add("concentration_unit")
                continue
            unmapped.add(name)
        projected.append(out)

    return {
        "rows": projected,
        "mapped_columns": sorted(mapped),
        "unmapped_source_columns": sorted(unmapped),
    }


def _has_value(value: Any) -> bool:
    """True when *value* carries content (not ``None`` and not blank/whitespace)."""
    return value is not None and str(value).strip() != ""


def condition_table_multivalued_columns(csv_path: str) -> set[str]:
    """Canonical columns in *csv_path* carrying more than one distinct value (#408).

    The condition table's CSVW schema resolves the ``cell_line`` / ``compound``
    columns to a single in-crate entity via ``valueUrl`` — a claim about the WHOLE
    column. At zero rows that is vacuous; once rows exist it asserts that every
    value in the column resolves to that one entity. On a multi-compound plate that
    is false, so the build must drop the claim rather than inherit it (D5: never
    assert an entity mapping that was not verified). Per-value mapping is out of
    scope — this only reports which columns can no longer carry a column-wide
    claim.

    Blank cells are absence, not a second value, so a column of ``T4`` plus empties
    stays single-valued. A missing/unreadable file or a header-only table yields an
    empty set: the in-memory validate path has no CSV on disk, and no rows means
    nothing to contradict.

    Args:
        csv_path: Path to the condition-table CSV.

    Returns:
        The canonical column titles carrying ≥2 distinct non-empty values.
    """
    path = Path(csv_path)
    if not path.is_file():
        return set()

    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.warning("Could not read condition table %s: %s", path, exc)
        return set()

    return condition_table_multivalued_columns_from_rows(rows)


def condition_table_row_count(csv_path: str) -> int | None:
    """Number of DATA rows in the condition table at *csv_path* (#473).

    ``None`` when the file cannot be read at all — the in-memory validate path
    has no CSV on disk, and *we did not look* is a different claim from *there
    are no rows*. Callers stamp the "contains no rows" note only on a definite
    zero, so the crate never asserts emptiness it did not verify (D5).

    A row that is entirely blank does not count: a trailing newline, or the
    empty line a spreadsheet export leaves behind, is not a condition.

    Args:
        csv_path: Path to the condition-table CSV.

    Returns:
        The data-row count, or ``None`` if the file is missing or unreadable.
    """
    path = Path(csv_path)
    if not path.is_file():
        return None

    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.warning("Could not read condition table %s: %s", path, exc)
        return None

    return sum(
        1
        for row in rows
        if any((value or "").strip() for value in row.values())
    )


def condition_table_multivalued_columns_from_rows(
    rows: list[dict[str, Any]],
) -> set[str]:
    """The #408 predicate over already-parsed rows.

    The single definition of "this column can no longer carry a column-wide
    ``valueUrl`` claim"; :func:`condition_table_multivalued_columns` is the
    file-reading wrapper, and a caller that already holds the rows (the eval
    scorer, #474) consults this directly instead of re-reading the CSV.
    """
    from builder.tools._crate_mapping import _CONDITION_TABLE_HEADER

    # Only canonical columns are described by the CSVW schema, so only they can
    # carry (or lose) a valueUrl; a verbatim-copied plate map's extra headers are
    # not the build's to reason about.
    canonical = set(_CONDITION_TABLE_HEADER.strip("\n").split(","))
    seen: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row.items():
            name = str(key).strip() if key is not None else ""
            if name not in canonical or not _has_value(value):
                continue
            seen.setdefault(name, set()).add(str(value).strip())
    return {column for column, values in seen.items() if len(values) > 1}


def reference_cell_allowlist(state: CrateState, entity_type: str) -> list[str]:
    """Allowed cell values for a condition-table reference column.

    The table's ``compound`` / ``cell_line`` cells carry entity **names**, not
    entity ids — :func:`propose_condition_rows` writes ``name`` and only falls
    back to ``entity_id`` for an entity that has none, and a depositor's own
    plate map names compounds the way a bench scientist writes them. Allowing
    both is therefore the check working as intended, not a loosening: an
    id-only allow-list would flag every row of a correct table, and a check
    that always fires is a check nobody reads.

    Matching is **exact** (whitespace-stripped, case-preserved) — the same
    strictness the Frictionless foreign-key validation applies, so the build's
    payload verdict and the eval scorer (#474) can never disagree about the
    same cell.
    """
    allowed: list[str] = []
    for entity in state.list_entities(entity_type):
        name = str(entity.fields.get("name") or "").strip()
        if name:
            allowed.append(name)
        allowed.append(entity.entity_id)
    return allowed


# --- plate-map intake (#422) ------------------------------------------------
# `populate_condition_table` accepted any string path and opened it as UTF-8
# text. The pipeline classifies a plan file as `condition_table` by ROLE, not by
# extension, so the real deposit's `.xlsx` plate map hit `csv.DictReader` and
# raised UnicodeDecodeError on the first ZIP byte. Dispatch on the suffix so a
# binary never reaches a text decode, and refuse formats there is no reader for
# rather than failing in a way that reads like "the plate map was unusable".

_TEXT_TABLE_SUFFIXES: dict[str, str] = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
_EXCEL_SUFFIXES: frozenset[str] = frozenset({".xlsx", ".xlsm"})


def _rows_from_file(
    src: Path,
) -> tuple[list[Mapping[str, Any]], str, str | None, str | None]:
    """Read a plate map into rows. Returns ``(rows, reader, error, sheet)``."""
    suffix = src.suffix.lower()

    if suffix in _TEXT_TABLE_SUFFIXES:
        reader_name = "csv.DictReader"
        try:
            with src.open(newline="", encoding="utf-8") as fh:
                return (
                    list(csv.DictReader(fh, delimiter=_TEXT_TABLE_SUFFIXES[suffix])),
                    reader_name,
                    None,
                    None,
                )
        except (UnicodeDecodeError, OSError, csv.Error) as exc:
            return [], reader_name, str(exc), None

    if suffix in _EXCEL_SUFFIXES:
        from builder.tools.file_readers import read_excel_rows

        reader_name = "read_excel_rows (openpyxl)"
        sheets = read_excel_rows(str(src))
        if sheets is None:
            return [], reader_name, "workbook could not be opened", None
        # Pick the sheet DETERMINISTICALLY by scoring each against the very
        # projection that decides whether a table is usable — never "the first
        # sheet", which in a depositor workbook is usually a cover page.
        # `rows` is a list of dicts and `list` is invariant, so the Mapping
        # spelling never actually accepted the value assigned below.
        best: tuple[int, int, str, list[dict[str, Any]]] | None = None
        tried: list[str] = []
        for name, rows in sheets.items():
            rows = [r for r in rows if not r.get("__truncated__")]
            tried.append(f"{name} ({', '.join(list(rows[0])[:6]) if rows else 'empty'})")
            if not rows:
                continue
            projection = project_condition_rows(rows)
            wells = sum(1 for r in projection["rows"] if _has_value(r.get("well_id")))
            mapped = len(projection["mapped_columns"])
            if not mapped or not wells:
                continue
            score = (wells, mapped)
            if best is None or score > (best[0], best[1]):
                best = (wells, mapped, name, rows)
        if best is None:
            return (
                [],
                reader_name,
                "no sheet has both a mapped condition-table column and a well_id; "
                "tried " + "; ".join(tried),
                None,
            )
        return list(best[3]), reader_name, None, best[2]

    return (
        [],
        f"no reader for {suffix or 'a file with no suffix'}",
        "populate_condition_table reads .csv / .tsv / .xlsx plate maps",
        None,
    )


def populate_condition_table(
    state: Any,
    exposure_id: str,
    rows_or_csv_path: Sequence[Mapping[str, Any]] | str,
    *,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Write per-well rows into an Exposure's CSVW condition table.

    Replaces the header-only placeholder #94 materialises with the actual
    per-well design table. Two intake modes:

    - ``rows_or_csv_path`` is a list of row dicts (keys are the condition-table
      column titles): the rows are written under the canonical
      ``_CONDITION_TABLE_HEADER`` into the Exposure's condition-table CSV at
      ``<output_dir>/data/<exposure>_condition_table.csv``.
    - ``rows_or_csv_path`` is a path to a user-supplied plate-map CSV: it is
      copied verbatim into that destination (its columns are assumed to match
      the condition-table schema).

    The CSVW typing (#94) is preserved: the populated CSV lives at the same
    ``dest_path`` the build wires as a typed ``csvw:Table`` with its
    ``tableSchema`` attached, so populating rows does not strip the table's
    machine-readable schema.

    Args:
        state: The crate state (used to resolve the Exposure entity).
        exposure_id: ``entity_id`` of the Exposure LabProcess.
        rows_or_csv_path: Either a list of row dicts or a path to a CSV file.
        output_dir: Crate root directory to write the CSV under. Defaults to the
            state's configured output path when omitted.

    Returns:
        ``{"ok": bool, "path": str, "rows": int}`` on success, or
        ``{"ok": False, "error": str}`` when the Exposure cannot be resolved or
        the supplied CSV is missing.
    """
    # Imported here to avoid a circular import at module load (data_content is
    # imported by _crate_mapping's consumers, not the other way round).
    from builder.tools._crate_mapping import (
        _CONDITION_TABLE_HEADER,
        _condition_table_rel,
        _mint_id,
    )

    proc = state.get_entity(exposure_id)
    if proc is None:
        return {"ok": False, "error": f"Exposure entity not found: {exposure_id!r}"}
    ptype = proc.fields.get("process_type") or proc.fields.get("additionalType") or ""
    if ptype != "Exposure":
        return {
            "ok": False,
            "error": f"{exposure_id!r} is a {ptype or 'LabProcess'}, not an Exposure.",
        }

    base_dir = Path(output_dir) if output_dir else None
    if base_dir is None:
        out_path = getattr(getattr(state, "metadata", None), "output_path", None)
        if out_path:
            base_dir = Path(out_path)
        else:
            # Resolve exactly as export_crate does (#381). The old Path.cwd()
            # fallback let populate and export target different roots, so rows
            # written here vanished at export with nothing reported.
            from builder.tools.builder import _default_crate_path

            base_dir = Path(_default_crate_path(state))

    header_cols = _CONDITION_TABLE_HEADER.strip("\n").split(",")

    sheet_used: str | None = None
    if isinstance(rows_or_csv_path, str):
        src = Path(rows_or_csv_path)
        if not src.is_file():
            return {"ok": False, "error": f"Plate map not found: {rows_or_csv_path}"}
        read_rows, reader_name, read_error, sheet_used = _rows_from_file(src)
        if read_error is not None:
            # #422: this used to be an unguarded UTF-8 text decode, so a real
            # .xlsx plate map raised UnicodeDecodeError, the spine swallowed it
            # into a `reason:` string, and the crate shipped a header-only table
            # with nothing said about why. Name the file AND the reader.
            logger.warning(
                "condition table: %s could not be read by %s: %s",
                src.name,
                reader_name,
                read_error,
            )
            return {
                "ok": False,
                "error": f"{src.name} could not be read by {reader_name}: {read_error}",
                "read_failed": True,
                "reader": reader_name,
            }
        rows: Sequence[Mapping[str, Any]] = read_rows
    else:
        rows = rows_or_csv_path

    # Alias source headers onto the canonical titles BEFORE deciding to write, so
    # a real plate map is populated rather than dropped by extrasaction="ignore".
    projection = project_condition_rows(rows)
    projected = projection["rows"]
    unmapped = projection["unmapped_source_columns"]

    # Refusal contract (#381): a table of blank cells is worse than the honest
    # header-only placeholder, because the CSVW schema then advertises typed
    # columns backed by nothing. Refuse, write NOTHING, and say what was skipped.
    if not projection["mapped_columns"]:
        return {
            "ok": False,
            "error": (
                "No source column maps to a condition-table column; refusing to write "
                "a blank table. Expected one of: " + ", ".join(header_cols)
            ),
            "unmapped_source_columns": unmapped,
        }
    if not any(_has_value(row.get("well_id")) for row in projected):
        return {
            "ok": False,
            "error": (
                "No row resolves a well_id; a per-well design table without a well key "
                "is not a design table. Refusing to write."
            ),
            "unmapped_source_columns": unmapped,
        }

    # The workbook sheet the rows came from, so a wrong-sheet pick is visible
    # rather than silently baked into the table.
    rel = _condition_table_rel(_mint_id(proc))
    dest = base_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header_cols, extrasaction="ignore")
        writer.writeheader()
        for row in projected:
            writer.writerow({c: row.get(c, "") for c in header_cols})

    logger.debug("Wrote %d condition-table rows to %s", len(projected), dest)
    if unmapped:
        logger.info("Condition table: skipped unmapped source columns %s", unmapped)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(dest),
        "rows": len(projected),
        "unmapped_source_columns": unmapped,
    }
    if sheet_used is not None:
        # Which worksheet the rows came from — a depositor workbook has several,
        # and a wrong pick should be visible rather than silently baked in.
        result["sheet"] = sheet_used
    return result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate_table", validate_table, takes_state=False)
TOOL_REGISTRY.register("populate_condition_table", populate_condition_table, takes_state=True)


# ---------------------------------------------------------------------------
# Best-effort condition-table proposal (#438)
# ---------------------------------------------------------------------------
# `populate_condition_table` above writes rows the user SUPPLIED. When no plate
# map was supplied the table shipped header-only, and the compounds it should
# have named stayed orphaned — the crate described 22 substances and connected
# none of them to the experiment.
#
# This proposes the rows instead, under one rule: restate what the crate already
# knows, never invent what it does not. Compound identity, the cell line and the
# assay are facts already sitting in the graph as entities, so writing them into
# a design table asserts nothing new. A CONCENTRATION is different — if the crate
# never mentions a dose, generating a dilution series would be fabricating the
# experiment (D5), so the cell is left blank and ONE question is raised for the
# human at the screen rather than 22 invented numbers.
#
# The split is deliberate: fill what is obvious without asking, ask about what is
# genuinely ambiguous, and never do the third thing — guess quietly.

_DOSE_FIELD_HINTS: tuple[str, ...] = (
    "concentration",
    "concentration_value",
    "dose",
    "dose_value",
    "test_concentration",
)
_DOSE_UNIT_HINTS: tuple[str, ...] = (
    "concentration_unit",
    "dose_unit",
    "units",
    "unit",
)


def _first_field(entity: Any, names: Iterable[str]) -> str | None:
    """First non-empty value among *names* on *entity*'s fields."""
    fields = getattr(entity, "fields", {}) or {}
    for name in names:
        value = fields.get(name)
        if value not in (None, "", []):
            return str(value)
    return None


def _ref_id(value: Any) -> str | None:
    """The entity id a reference field points at (handles str / {"@id"} / list)."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("@id")
    return str(value).lstrip("#") if value else None


def propose_condition_rows(state: Any, exposure_id: str) -> dict[str, Any]:
    """Propose per-well design rows for an Exposure from what the crate knows.

    Deterministic and side-effect-free: it reads state and returns a proposal —
    the caller decides whether to write it (``populate_condition_table``) after
    the human has confirmed.

    Fills, without asking, what is already an entity in the crate: one row per
    compound wired to the Exposure, the cell line consumed by the CellCulture,
    the parent Assay's name, and the Exposure's duration when it carries one.
    Leaves a cell BLANK whenever the crate does not state its value.

    Raises no questions for a blank the crate simply never mentions in any form;
    raises a specific, answerable question when the crate states a value for SOME
    compounds and not others, because applying one compound's dose to another is
    extrapolation rather than capture.

    Args:
        state: The crate state to read.
        exposure_id: ``entity_id`` of the Exposure LabProcess.

    Returns:
        ``{"ok": bool, "rows": [...], "known": [...], "blank": [...],
        "questions": [...]}``. Each question is
        ``{"id", "column", "question", "options"}`` — specific enough to answer
        without re-reading the crate. ``{"ok": False, "error": ...}`` when the
        Exposure cannot be resolved.
    """
    exposure = state.get_entity(exposure_id)
    if exposure is None:
        return {"ok": False, "error": f"Exposure not found: {exposure_id!r}"}

    compound_ids = exposure.fields.get("chemicals")
    compound_ids = compound_ids if isinstance(compound_ids, list) else [compound_ids]
    compounds = [
        c for c in (state.get_entity(_ref_id(cid)) for cid in compound_ids if cid) if c
    ]
    if not compounds:
        return {
            "ok": False,
            "error": (
                "the Exposure references no compounds — wire them with "
                "`chemicals` before proposing a design table"
            ),
        }

    # Cell line: whatever the CellCulture consumed, else the single declared one.
    cell_line_name: str | None = None
    for proc in state.list_entities("LabProcess"):
        if str(proc.fields.get("process_type") or "") != "CellCulture":
            continue
        target = state.get_entity(_ref_id(proc.fields.get("cell_line")) or "")
        if target is not None:
            cell_line_name = _first_field(target, ("name",))
            break
    if cell_line_name is None:
        lines = state.list_entities("CellLineSample")
        if len(lines) == 1:
            cell_line_name = _first_field(lines[0], ("name",))

    assay_name: str | None = None
    assays = state.list_entities("Assay")
    if len(assays) == 1:
        assay_name = _first_field(assays[0], ("name",))

    duration = _first_field(exposure, ("duration", "exposure_duration"))

    # Doses, per compound — captured only where the crate states one.
    doses: dict[str, tuple[str, str | None]] = {}
    for compound in compounds:
        value = _first_field(compound, _DOSE_FIELD_HINTS)
        if value:
            doses[compound.entity_id] = (
                value,
                _first_field(compound, _DOSE_UNIT_HINTS)
                or _first_field(exposure, _DOSE_UNIT_HINTS),
            )

    rows: list[dict[str, str]] = []
    for index, compound in enumerate(compounds, start=1):
        dose_value, dose_unit = doses.get(compound.entity_id, ("", None))
        rows.append(
            {
                "well_id": str(index),
                "assay": assay_name or "",
                "cell_line": cell_line_name or "",
                "compound": _first_field(compound, ("name",)) or compound.entity_id,
                "concentration_value": dose_value,
                "concentration_unit": dose_unit or "",
                "exposure_duration": duration or "",
                "experiment": "",
                "technical_replicate": "",
                "control": "",
            }
        )

    known = [
        column
        for column in ("compound", "assay", "cell_line", "exposure_duration")
        if any(row[column] for row in rows)
    ]
    blank = [c for c in rows[0] if not any(row[c] for row in rows)] if rows else []

    questions: list[dict[str, Any]] = []
    if doses and len(doses) < len(compounds):
        # The sharp case: a dose is stated for some compounds and not others.
        # Copying it across would assert a concentration the crate never claimed.
        # Name the compounds that DO carry a dose — the question is about them,
        # and listing three arbitrary others makes it unanswerable.
        dosed = sorted(
            _first_field(c, ("name",)) or c.entity_id
            for c in compounds
            if c.entity_id in doses
        )
        named = ", ".join(dosed[:3]) + ("…" if len(dosed) > 3 else "")
        questions.append(
            {
                "id": "partial_dose",
                "column": "concentration_value",
                "question": (
                    f"The crate states a concentration for {len(doses)} of "
                    f"{len(compounds)} compounds ({named}). Apply the same "
                    "concentration to the rest, or leave theirs blank?"
                ),
                "options": ["apply to all", "leave the rest blank", "let me supply them"],
            }
        )
    elif not doses:
        questions.append(
            {
                "id": "no_dose",
                "column": "concentration_value",
                "question": (
                    f"No concentration appears anywhere in this crate, so the "
                    f"{len(compounds)} rows are proposed without one. Supply the "
                    "concentrations, or leave the column blank?"
                ),
                "options": ["leave blank", "let me supply them"],
            }
        )

    return {"ok": True, "rows": rows, "known": known, "blank": blank, "questions": questions}
