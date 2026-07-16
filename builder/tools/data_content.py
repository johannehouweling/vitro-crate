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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        base_dir = Path(out_path) if out_path else Path.cwd()

    rel = _condition_table_rel(_mint_id(proc))
    dest = base_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    header_cols = _CONDITION_TABLE_HEADER.strip("\n").split(",")

    if isinstance(rows_or_csv_path, str):
        src = Path(rows_or_csv_path)
        if not src.is_file():
            return {"ok": False, "error": f"Plate-map CSV not found: {rows_or_csv_path}"}
        with src.open(newline="", encoding="utf-8") as fh:
            reader = list(csv.DictReader(fh))
        rows: Sequence[Mapping[str, Any]] = reader
    else:
        rows = rows_or_csv_path

    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header_cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in header_cols})

    logger.debug("Wrote %d condition-table rows to %s", len(rows), dest)
    return {"ok": True, "path": str(dest), "rows": len(rows)}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate_table", validate_table, takes_state=False)
TOOL_REGISTRY.register("populate_condition_table", populate_condition_table, takes_state=True)
