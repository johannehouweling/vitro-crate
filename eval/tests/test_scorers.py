"""Tests for the manuscript's MIT coverage scorer (#474).

``mit_propertyid_coverage`` — per-parameter MIT coverage joined via
``schema:propertyID``, counting only non-empty non-placeholder bindings — is an
axis the manuscript's §Evaluation defines but the codebase lacked.

Pure-metric tests: hand-built graphs (the ``eval/tests`` metric convention), no
SHACL, no network, no LLM.
"""

from __future__ import annotations

from typing import Any

from builder.tools.mit_assessment import load_mit_yaml, unique_module_params
from eval.scorers import mit_propertyid_coverage

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iri_params() -> dict[str, str]:
    """Oracle from the spec: every MIT param id -> its curated ontology IRI."""
    data = load_mit_yaml()
    assert data is not None
    out: dict[str, str] = {}
    for module in data.get("modules", []):
        for param in unique_module_params(module):
            iri = param.get("iri")
            if iri:
                out[str(param.get("id"))] = str(iri)
    return out


def _pv(iri: str | dict | None, value: Any, *, node_id: str = "#pv1") -> dict:
    node: dict[str, Any] = {
        "@id": node_id,
        "@type": "PropertyValue",
        "name": "some parameter",
        "value": value,
    }
    if iri is not None:
        node["propertyID"] = iri
    return node


# ---------------------------------------------------------------------------
# MIT propertyID-joined coverage
# ---------------------------------------------------------------------------


class TestMitPropertyIdCoverage:
    """The manuscript's per-parameter MIT axis: joined via schema:propertyID."""

    def test_denominator_is_the_iri_bearing_params(self) -> None:
        result = mit_propertyid_coverage([])
        expected = _iri_params()
        assert result["joinable"] == len(expected)
        assert result["joinable"] > 0
        assert result["covered"] == 0
        assert result["coverage"] == 0.0
        reported_ids = {p["id"] for p in result["per_param"]}
        assert reported_ids == set(expected)

    def test_bound_parameter_counts(self) -> None:
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        graph = [_pv({"@id": iri}, "cell viability")]
        result = mit_propertyid_coverage(graph)
        assert result["covered"] == 1
        assert result["coverage"] == 1 / result["joinable"]
        bound = {p["id"] for p in result["per_param"] if p["bound"]}
        assert bound == {"endpoints_of_the_test_method"}

    def test_bare_string_propertyid_also_joins(self) -> None:
        # An LLM-drafted PropertyValue state entity passes propertyID through
        # verbatim, so the graph can carry it as a bare string, not {"@id": ...}.
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        result = mit_propertyid_coverage([_pv(iri, "cell viability")])
        assert result["covered"] == 1

    def test_placeholder_value_does_not_count(self) -> None:
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        for placeholder in ("unknown", "n/a", "  NONE  ", ""):
            result = mit_propertyid_coverage([_pv({"@id": iri}, placeholder)])
            assert result["covered"] == 0, placeholder

    def test_missing_value_does_not_count(self) -> None:
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        node = _pv({"@id": iri}, "x")
        del node["value"]
        assert mit_propertyid_coverage([node])["covered"] == 0

    def test_run_provenance_bindings_are_excluded(self) -> None:
        # A CreateAction-owned PropertyValue must never satisfy an MIT param,
        # even if it carries a matching IRI (same exclusion as crate_graph_hash).
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        pv = _pv({"@id": iri}, "cell viability", node_id="#run_pv")
        action = {
            "@id": "#run_provenance",
            "@type": "CreateAction",
            "additionalProperty": [{"@id": "#run_pv"}],
        }
        assert mit_propertyid_coverage([action, pv])["covered"] == 0

    def test_accepts_wrapped_graph_document(self) -> None:
        iris = _iri_params()
        iri = iris["endpoints_of_the_test_method"]
        doc = {"@graph": [_pv({"@id": iri}, "cell viability")]}
        assert mit_propertyid_coverage(doc)["covered"] == 1

    def test_unloadable_yaml_reads_not_assessed(self) -> None:
        # No join surface at all must be None — "not assessed" — never a
        # fabricated 0.0 indistinguishable from total curation failure.
        result = mit_propertyid_coverage([], mit_data={})
        assert result["coverage"] is None
        assert result["joinable"] == 0
