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
  With no joinable parameters at all (the YAML unreadable or uncurated),
  ``coverage`` is ``None`` — not assessed, never a fabricated zero.
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

**CSVW typing and referential integrity, scored row-level.**
:func:`condition_table_typing_score` gives half its score to "the condition table is
CSVW-typed *and populated*" and half to "its reference cells resolve
in-crate".

This used to be called ``csvw_air_score`` and was presented as the manuscript's
AI-readiness axis. It is a good measurement and a bad name: no Bridge2AI criterion
operates below file level, let alone on a table column, so what it measures is CSVW
typing and referential integrity. AI-readiness is now scored by the published
instrument (:mod:`builder.tools.air_assessment`), and this feeds it as the evidence
behind criterion 2.c ("a machine-readable data dictionary or schema"). Decisions:

- **Row-level, not schema-level**: a header-only table scores zero. Every
  crate this builder assembles carries the full typed schema regardless of
  rows (#473), so schema-level scoring would be tautologically maximal on a
  failed deposit — the axis must deflate, not silently inflate. When the
  payload is readable, *the file itself* is the authority on row count — a
  stale ``populated`` self-report over a header-only file still scores zero.
- **The #408 rule is never penalised**: a reference column that legitimately
  dropped its ``valueUrl`` because it is multivalued (checked with the same
  predicate the build uses) still earns its score; a *single*-valued
  reference column missing ``valueUrl`` is a genuine typing gap.
- Reference cells resolve by **exact match** (whitespace-stripped) against
  the shared :func:`builder.tools.data_content.reference_cell_allowlist` —
  the same allow-list and strictness as the build's own Frictionless
  foreign-key validation, so scorer and build can never disagree about the
  same cell. Every reference column counts in the denominator: a column with
  no cells at all has no resolving references and scores zero, uniformly.
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
from builder.tools._crate_mapping import CONDITION_TABLE_REFERENCE_COLUMNS
from builder.tools.data_content import (
    condition_table_multivalued_columns_from_rows,
    reference_cell_allowlist,
)
from builder.tools.mit_assessment import graph_nodes, load_mit_yaml, unique_module_params

# The emitters' own placeholder set (single source, no drift — the #377 rule).
from profiles.models.tox import _PLACEHOLDER_VALUES

logger = logging.getLogger(__name__)

# The 3901-line MIT YAML is immutable within a process — parse it once, not
# once per scored crate.
_MIT_DATA_CACHE: dict[str, Any] | None = None
_MIT_DATA_LOADED = False


def _load_mit_data() -> dict[str, Any] | None:
    global _MIT_DATA_CACHE, _MIT_DATA_LOADED
    if not _MIT_DATA_LOADED:
        _MIT_DATA_CACHE = load_mit_yaml()
        _MIT_DATA_LOADED = True
    return _MIT_DATA_CACHE


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
    zero never has to be taken on faith. ``coverage`` is ``None`` when there
    is nothing to join against (no loadable YAML / no IRI-bearing params) —
    not assessed, never a fabricated zero.
    """
    data = mit_data if mit_data is not None else _load_mit_data()
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
        "coverage": covered / len(per_param) if per_param else None,
        "covered": covered,
        "joinable": len(per_param),
        "unjoinable": total_params - len(per_param),
        "per_param": per_param,
    }


# ---------------------------------------------------------------------------
# CSVW / AI-readiness axis
# ---------------------------------------------------------------------------


def _find_condition_table(
    nodes: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    *,
    report_path: str = "",
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """The condition-table node and its schema columns keyed by ``titles``.

    The builder emits one table per Exposure but populates exactly one, so the
    report's ``path`` (when given) selects WHICH table is being scored — the
    generic filename-suffix match is only the fallback for a path-less report.
    A found table with an unresolvable schema is still returned (with empty
    columns) so the caller can name the *typing* gap instead of misreporting
    the table as absent.
    """

    def _is_table(node: dict[str, Any]) -> bool:
        return "csvw:Table" in _type_names(node)

    basename = Path(report_path).name if report_path else ""
    table = None
    if basename:
        table = next(
            (
                n
                for n in nodes
                if _is_table(n) and str(n.get("@id", "")).endswith(basename)
            ),
            None,
        )
    if table is None:
        table = next(
            (
                n
                for n in nodes
                if _is_table(n)
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
    return table, columns


def _read_rows(path: str) -> list[dict[str, str]] | None:
    # The same failure classes the build's own reader tolerates
    # (condition_table_multivalued_columns): a depositor cell with a NUL or
    # non-UTF-8 byte must degrade the axis, never abort the eval run.
    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.warning("Condition-table payload unreadable at %s: %s", path, exc)
        return None


def condition_table_typing_score(
    state: CrateState,
    graph: dict[str, Any] | list[dict[str, Any]] | None,
    condition_table: dict[str, Any] | None,
) -> dict[str, Any]:
    """CSVW typing and referential integrity: typed-and-populated half + reference half.

    Evidence for Bridge2AI criterion 2.c, not an axis of its own — see the module
    docstring for why the old ``csvw_air_score`` name overstated what this measures.

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
    path = str(condition_table.get("path") or "")
    table, columns = _find_condition_table(nodes, index, report_path=path)

    if table is None:
        return {
            "score": 0.0,
            "reason": "no CSVW-typed condition table in the crate graph",
            "columns": {},
        }
    if not columns:
        return {
            "score": 0.0,
            "reason": "condition table lacks a resolvable tableSchema",
            "columns": {},
        }

    rows = _read_rows(path) if path else None
    if rows is None:
        # No readable payload — the file, not the self-report, is the row
        # authority, so the reference half is unverifiable. The typed half
        # falls back to the build's own report.
        reported = condition_table.get("rows")
        reported_rows = reported if isinstance(reported, int) else 0
        if not (bool(condition_table.get("populated")) and reported_rows > 0):
            return {
                "score": 0.0,
                "reason": "header-only condition table — CSVW typing over zero rows is vacuous",
                "columns": {},
            }
        return {
            "score": 0.5,
            "reason": "condition-table payload unreadable — reference cells not verifiable",
            "columns": {},
        }
    if not rows:
        return {
            "score": 0.0,
            "reason": "header-only condition table — CSVW typing over zero rows is vacuous",
            "columns": {},
        }

    typed_half = 0.5
    multivalued = condition_table_multivalued_columns_from_rows(rows)
    column_verdicts: dict[str, dict[str, Any]] = {}
    scores: list[float] = []
    for title, entity_type in CONDITION_TABLE_REFERENCE_COLUMNS.items():
        cells = [str(row.get(title) or "").strip() for row in rows]
        filled = [c for c in cells if c]
        allowed = set(reference_cell_allowlist(state, entity_type))
        resolves = bool(filled) and all(c in allowed for c in filled)
        col = columns.get(title)
        has_value_url = bool(col and col.get("valueUrl"))
        typing_ok = has_value_url or title in multivalued
        scores.append(1.0 if (resolves and typing_ok) else 0.0)
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
