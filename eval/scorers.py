"""The manuscript's MIT coverage scorer (#474).

The paper's §Evaluation defines a per-parameter MIT coverage the rest of the
harness does not compute; this module implements it as a pure function over an
artifact the harness already has (an assembled ``@graph``). The design decisions
the issue demands are made explicit here:

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
  YAML curate *different* terms for the same concept, the parameter honestly
  reads unbound — the ``per_param`` detail exists precisely so those curation
  gaps are visible and fixable instead of papered over. That is how the
  ``organ``/``tissue`` gap was found and closed (#856).
- ``propertyID`` is accepted both as ``{"@id": IRI}`` (every builder-emitted
  path) and as a bare string (LLM-drafted PropertyValue state entities pass
  it through verbatim).
- The placeholder vocabulary is **imported from the emitters' own constant**
  (`profiles.models.tox`), never duplicated, so scorer and build cannot
  drift on what "placeholder" means.
- Run-provenance PropertyValues (owned by the ``CreateAction``) are excluded,
  matching :func:`eval.metrics.crate_graph_hash`'s exclusion.
"""

from __future__ import annotations

from typing import Any

from builder.tools.mit_assessment import graph_nodes, load_mit_yaml, unique_module_params

# The emitters' own placeholder set (single source, no drift — the #377 rule).
from profiles.models.tox import _PLACEHOLDER_VALUES

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
