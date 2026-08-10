"""The manuscript's evaluation scorers (#474).

The paper's §Evaluation defines two operationalisations the rest of the
harness does not compute; this module implements both as pure functions over
artifacts the harness already has (an assembled ``@graph``, the build's
condition-table report, ``CrateState``). The design decisions the issue
demands are made explicit here:

**MIT coverage joined via ``schema:propertyID`` (= FAIR R1.3), per parameter.**
:func:`mit_propertyid_coverage` joins each MIT parameter's curated ontology
IRI (the ``iri`` key in ``mit/invitro_tox.yaml``) against the ``propertyID``
of the crate's PropertyValue nodes, counting a parameter as covered only when
a binding exists with a non-empty, non-placeholder ``value``. Decisions:

- The denominator is the **IRI-bearing parameters only**: a parameter with no
  curated term has no join key, so the per-parameter metric is undefined for
  it — reported as ``unjoinable`` rather than silently counted as failed.
- The join is **exact IRI equality**. Where the crate's emitters and the MIT
  YAML curate *different* terms for the same concept (e.g. MIT ``organ`` =
  UBERON_0000062 vs the crate's ``…/param/organ``), the parameter honestly
  reads unbound — the ``per_param`` detail exists precisely so those curation
  gaps are visible and fixable instead of papered over.
- ``propertyID`` is accepted both as ``{"@id": IRI}`` (every builder-emitted
  path) and as a bare string (LLM-drafted PropertyValue state entities pass
  it through verbatim).
- The placeholder vocabulary is **imported from the emitters' own constant**
  (`profiles.models.tox`), never duplicated, so scorer and build cannot
  drift on what "placeholder" means.
- Run-provenance PropertyValues (owned by the ``CreateAction``) are excluded,
  matching :func:`eval.metrics.crate_graph_hash`'s exclusion.

**The CSVW / AI-readiness axis, scored row-level.**
:func:`csvw_air_score` gives half the axis to "the condition table is
CSVW-typed *and populated*" and half to "its reference cells resolve
in-crate". Decisions:

- **Row-level, not schema-level**: a header-only table scores zero. Every
  crate this builder assembles carries the full typed schema regardless of
  rows (#473), so schema-level scoring would be tautologically maximal on a
  failed deposit — the axis must deflate, not silently inflate.
- **The #408 rule is never penalised**: a reference column that legitimately
  dropped its ``valueUrl`` because it is multivalued (checked with the same
  predicate the build uses, ``condition_table_multivalued_columns``) still
  earns its score; a *single*-valued reference column missing ``valueUrl`` is
  a genuine typing gap.
- Reference cells carry entity **names**, not ids, so resolution matches a
  cell against the in-state entity names/ids (mirroring the pipeline's
  ``_reference_names`` allow-list semantics).
- With no pipeline condition-table report (the ReAct arm, mocks) the axis is
  ``None`` — "not assessed", never a fabricated verdict (the ``meets_quota``
  precedent).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState
from builder.tools.data_content import condition_table_multivalued_columns
from builder.tools.mit_assessment import graph_nodes, load_mit_yaml, unique_module_params

# The emitters' own placeholder set (single source, no drift — the #377 rule).
from profiles.models.tox import _PLACEHOLDER_VALUES

logger = logging.getLogger(__name__)

_REFERENCE_COLUMNS: dict[str, str] = {
    "compound": "MolecularEntity",
    "cell_line": "CellLineSample",
}


# ---------------------------------------------------------------------------
# shared graph helpers
# ---------------------------------------------------------------------------


def _type_names(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    types = raw if isinstance(raw, list) else [raw]
    return {str(t) for t in types if t}


def _ref_id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("@id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# MIT propertyID-joined coverage
# ---------------------------------------------------------------------------


def _is_real_binding(value: Any) -> bool:
    """Non-empty and non-placeholder, per the emitters' own vocabulary."""
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and stripped.casefold() not in _PLACEHOLDER_VALUES
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _run_provenance_ids(nodes: list[dict[str, Any]]) -> set[str]:
    """@ids of PropertyValues owned by the run-provenance CreateAction."""
    excluded: set[str] = set()
    for node in nodes:
        if "CreateAction" not in _type_names(node):
            continue
        for ref in _as_list(node.get("additionalProperty")):
            ref_id = _ref_id(ref)
            if ref_id:
                excluded.add(ref_id)
    return excluded


def mit_propertyid_coverage(
    graph: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    mit_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-parameter MIT coverage joined via ``schema:propertyID``.

    Returns ``{coverage, covered, joinable, unjoinable, per_param}`` where
    ``per_param`` lists every IRI-bearing parameter with its verdict, so a
    zero never has to be taken on faith.
    """
    data = mit_data if mit_data is not None else load_mit_yaml()
    joinable: list[tuple[str, str, str]] = []  # (param id, name, iri)
    total_params = 0
    if data:
        for module in data.get("modules", []):
            for param in unique_module_params(module):
                total_params += 1
                iri = param.get("iri")
                if iri:
                    joinable.append(
                        (str(param.get("id")), str(param.get("name")), str(iri))
                    )

    nodes = graph_nodes(graph if graph is not None else [])
    excluded = _run_provenance_ids(nodes)
    bound_iris: set[str] = set()
    for node in nodes:
        if "PropertyValue" not in _type_names(node):
            continue
        if str(node.get("@id")) in excluded:
            continue
        iri = _ref_id(node.get("propertyID"))
        if iri and _is_real_binding(node.get("value")):
            bound_iris.add(iri)

    per_param = [
        {"id": pid, "name": name, "iri": iri, "bound": iri in bound_iris}
        for pid, name, iri in joinable
    ]
    covered = sum(1 for p in per_param if p["bound"])
    return {
        "coverage": covered / len(per_param) if per_param else 0.0,
        "covered": covered,
        "joinable": len(per_param),
        "unjoinable": total_params - len(per_param),
        "per_param": per_param,
    }


# ---------------------------------------------------------------------------
# CSVW / AI-readiness axis
# ---------------------------------------------------------------------------


def _find_condition_table(
    nodes: list[dict[str, Any]], index: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """The condition-table node and its schema columns keyed by ``titles``."""
    table = next(
        (
            n
            for n in nodes
            if "csvw:Table" in _type_names(n)
            and str(n.get("@id", "")).endswith("_condition_table.csv")
        ),
        None,
    )
    if table is None:
        return None, {}
    schema_id = _ref_id(table.get("tableSchema"))
    schema = index.get(schema_id) if schema_id else None
    columns: dict[str, dict[str, Any]] = {}
    if schema:
        for ref in _as_list(schema.get("columns")):
            col_id = _ref_id(ref)
            col = index.get(col_id) if col_id else None
            if col is not None:
                columns[str(col.get("titles", ""))] = col
    if not columns:
        return None, {}
    return table, columns


def _allowed_cell_values(state: CrateState, entity_type: str) -> set[str]:
    """Casefolded entity names + ids, mirroring the pipeline's allow-list."""
    allowed: set[str] = set()
    for entity in state.list_entities(entity_type):
        name = str(entity.fields.get("name") or "").strip()
        if name:
            allowed.add(name.casefold())
        allowed.add(entity.entity_id.strip().casefold())
    return allowed


def _read_rows(path: str) -> list[dict[str, str]] | None:
    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return None


def csvw_air_score(
    state: CrateState,
    graph: dict[str, Any] | list[dict[str, Any]] | None,
    condition_table: dict[str, Any] | None,
) -> dict[str, Any]:
    """The AI-readiness axis: typed-and-populated half + reference half.

    ``condition_table`` is the build's own report
    (``pipeline_result["materialized"]["condition_table"]``); ``None`` means
    the arm produced no such report and the axis is not assessed.
    """
    if not isinstance(condition_table, dict):
        return {
            "score": None,
            "reason": "not assessed (no pipeline condition-table report)",
            "columns": {},
        }

    nodes = graph_nodes(graph if graph is not None else [])
    index = {str(n["@id"]): n for n in nodes if "@id" in n}
    table, columns = _find_condition_table(nodes, index)

    rows_reported = condition_table.get("rows")
    populated = bool(condition_table.get("populated")) and isinstance(
        rows_reported, int
    ) and rows_reported > 0

    if table is None:
        return {
            "score": 0.0,
            "reason": "no CSVW-typed condition table in the crate graph",
            "columns": {},
        }
    if not populated:
        return {
            "score": 0.0,
            "reason": "header-only condition table — CSVW typing over zero rows is vacuous",
            "columns": {},
        }

    typed_half = 0.5

    path = str(condition_table.get("path") or "")
    rows = _read_rows(path) if path else None
    if rows is None:
        return {
            "score": typed_half,
            "reason": "condition-table payload unreadable — reference cells not verifiable",
            "columns": {},
        }

    multivalued = condition_table_multivalued_columns(path)
    column_verdicts: dict[str, dict[str, Any]] = {}
    scores: list[float] = []
    for title, entity_type in _REFERENCE_COLUMNS.items():
        cells = [str(row.get(title) or "").strip() for row in rows]
        filled = [c for c in cells if c]
        if not filled:
            continue  # a blank column has no reference cells to resolve
        allowed = _allowed_cell_values(state, entity_type)
        resolves = all(c.casefold() in allowed for c in filled)
        col = columns.get(title)
        has_value_url = bool(col and col.get("valueUrl"))
        typing_ok = has_value_url or title in multivalued
        verdict = 1.0 if (resolves and typing_ok) else 0.0
        scores.append(verdict)
        column_verdicts[title] = {
            "resolves_in_crate": resolves,
            "value_url": has_value_url,
            "multivalued": title in multivalued,
            "cells": len(filled),
        }

    reference_half = 0.5 * (sum(scores) / len(scores)) if scores else 0.0
    return {
        "score": typed_half + reference_half,
        "reason": "row-level: typed+populated half plus reference-resolution half",
        "columns": column_verdicts,
    }
