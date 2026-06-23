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

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
            report = _run_validation(
                Package, Resource, schema, path, foreign_keys
            )
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate_table", validate_table, takes_state=False)
