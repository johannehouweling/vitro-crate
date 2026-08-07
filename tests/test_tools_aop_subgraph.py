"""Tests for the ``materialize_aop_subgraph`` composite tool (Issue #180).

``materialize_aop_subgraph`` turns ONE AOP-Wiki id into the full crate subgraph
and wires it deterministically: an ``AdverseOutcomePathway`` node with its
MIE/KE/AO and KeyEventRelationship link arrays, one ``KeyEvent`` node per event
(discriminated only by ``eventType``), one ``KeyEventRelationship`` per relation,
and — when a ``study_id`` is supplied — the AOP wired onto that Study via the
``aop`` / ``mentions`` reference.

The only model-supplied input is the numeric ``aop_id``; everything else is the
deterministic AOP-Wiki graph (D5: never fabricate ids). Tests never hit the
network — the AOP-Wiki HTTP is mocked from the bundled AOP-610 fixture exactly
as the existing lookup tests do (per project policy, lookups stay offline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools.composites import materialize_aop_subgraph
from builder.tools.drafters import draft_investigation, draft_study
from builder.tools.validation import build_and_validate
from lookups import _http, aopwiki


# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _offline_aop(monkeypatch):
    """Serve the bundled AOP-610 fixture instead of hitting AOP-Wiki."""
    aop_data = _load("aopwiki_aop610.json")

    def fake_get_json(url, **kwargs):
        if url.endswith("/aops/610.json"):
            return aop_data
        # Per-event detail endpoint: .../events/<id>.json
        eid = url.rsplit("/", 1)[1].removesuffix(".json")
        return {"short_name": f"Event {eid}", "biological_organization": "Cellular"}

    aopwiki.lookup_aop.cache_clear()
    aopwiki._event_details.cache_clear()
    _http.reset_host_throttle()
    monkeypatch.setattr(aopwiki, "http_get_json", fake_get_json)
    monkeypatch.setattr(_http, "_HOST_MIN_INTERVAL", 0.0)
    # The tools-layer lookup_aop is independently lru_cached.
    from builder.tools import lookups as tool_lookups

    tool_lookups.lookup_aop.cache_clear()
    yield
    aopwiki.lookup_aop.cache_clear()
    aopwiki._event_details.cache_clear()
    # A test may monkeypatch the tools-layer lookup_aop with a plain function
    # (no lru_cache); guard the cache_clear so teardown never raises.
    if hasattr(tool_lookups.lookup_aop, "cache_clear"):
        tool_lookups.lookup_aop.cache_clear()
    _http.reset_host_throttle()


def _by_type(state: CrateState, type_name: str) -> list:
    return [e for e in state.list_entities() if e.type == type_name]


class TestMaterializeState:
    def test_materializes_typed_entities(self):
        state = CrateState()
        result = materialize_aop_subgraph(state, "610")

        # The AOP-610 fixture is 1 MIE + 2 KE + 1 AO + 3 relationships.
        aops = _by_type(state, "AdverseOutcomePathway")
        events = _by_type(state, "KeyEvent")
        kers = _by_type(state, "KeyEventRelationship")
        assert len(aops) == 1
        assert len(events) == 4  # MIE + 2 KE + AO all share @type KeyEvent
        assert len(kers) == 3

        # Result reports the materialized ids/counts.
        assert result["aop_id"] == "610"
        assert result["events"] == 4
        assert result["relationships"] == 3
        assert result["aop_entity_id"] == aops[0].entity_id

    def test_event_types_discriminated_only_by_event_type_field(self):
        state = CrateState()
        materialize_aop_subgraph(state, "610")
        events = _by_type(state, "KeyEvent")
        # Every event node is @type KeyEvent; the only discriminator is eventType.
        types = {e.fields.get("eventType") for e in events}
        assert "Molecular Initiating Event" in types
        assert "Key Event" in types
        assert "Adverse Outcome" in types

    def test_aop_carries_link_arrays(self):
        state = CrateState()
        materialize_aop_subgraph(state, "610")
        aop = _by_type(state, "AdverseOutcomePathway")[0]
        assert aop.fields["has_molecular_initiating_event"]
        assert aop.fields["has_key_event"]
        assert aop.fields["has_adverse_outcome"]
        assert aop.fields["has_key_event_relationship"]
        assert aop.fields["identifier"] == "610"
        assert aop.fields["url"] == "https://aopwiki.org/aops/610"

    def test_relationships_link_upstream_downstream(self):
        state = CrateState()
        materialize_aop_subgraph(state, "610")
        ker = _by_type(state, "KeyEventRelationship")[0]
        assert "@id" in ker.fields["upstream_event"]
        assert "@id" in ker.fields["downstream_event"]

    def test_idempotent_no_duplicates(self):
        state = CrateState()
        materialize_aop_subgraph(state, "610")
        materialize_aop_subgraph(state, "610")
        assert len(_by_type(state, "AdverseOutcomePathway")) == 1
        assert len(_by_type(state, "KeyEvent")) == 4
        assert len(_by_type(state, "KeyEventRelationship")) == 3

    def test_unknown_aop_returns_error(self, monkeypatch):
        from builder.tools import lookups as tool_lookups

        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "nope"},
        )
        state = CrateState()
        result = materialize_aop_subgraph(state, "999999")
        assert result.get("ok") is False
        assert _by_type(state, "AdverseOutcomePathway") == []


class TestMaterializeBuild:
    def test_graph_contains_cross_linked_subgraph(self):
        state = CrateState()
        materialize_aop_subgraph(state, "610")
        report = build_and_validate(state)
        # The build itself succeeds (no exception → report has conformance).
        assert "conformance" in report

        from builder.tools.builder import assemble_crate

        graph = assemble_crate(
            state, materialize_payload=False, include_all_scanned=False
        ).metadata.generate()["@graph"]

        aop = next(n for n in graph if n.get("@type") == "AdverseOutcomePathway")
        kes = [n for n in graph if n.get("@type") == "KeyEvent"]
        kers = [n for n in graph if n.get("@type") == "KeyEventRelationship"]
        assert len(kes) == 4
        assert len(kers) == 3

        # The AOP @id is the resolvable AOP-Wiki IRI and its has_* arrays point at
        # the KeyEvent / KER node @ids (cross-linked, not fabricated).
        assert aop["@id"] == "https://aopwiki.org/aops/610"
        ke_ids = {n["@id"] for n in kes}
        ker_ids = {n["@id"] for n in kers}
        mie_targets = {r["@id"] for r in aop["has_molecular_initiating_event"]}
        assert mie_targets <= ke_ids
        ker_targets = {r["@id"] for r in aop["has_key_event_relationship"]}
        assert ker_targets == ker_ids

        # Each KER links upstream/downstream KeyEvent @ids.
        ker = kers[0]
        assert ker["upstream_event"]["@id"] in ke_ids
        assert ker["downstream_event"]["@id"] in ke_ids


class TestMaterializeStudyWiring:
    def test_wires_aop_onto_study(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        materialize_aop_subgraph(state, "610", study_id=study.entity_id)

        # The AOP id is recorded on the Study's `aop` reference field.
        refs = study.fields.get("aop")
        assert refs is not None
        ids = [r.get("@id") if isinstance(r, dict) else r for r in refs]
        assert "https://aopwiki.org/aops/610" in ids

    def test_study_wiring_round_trips_into_graph(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        materialize_aop_subgraph(state, "610", study_id=study.entity_id)

        from builder.tools.builder import assemble_crate

        graph = assemble_crate(
            state, materialize_payload=False, include_all_scanned=False
        ).metadata.generate()["@graph"]
        study_node = next(n for n in graph if n.get("additionalType") == "Study")
        # schema:mentions alias `aop` connects the Study to the AOP IRI.
        mention_ids = {
            r.get("@id")
            for key in ("aop", "mentions")
            for r in (
                study_node.get(key, [])
                if isinstance(study_node.get(key), list)
                else [study_node.get(key)]
            )
            if isinstance(r, dict)
        }
        assert "https://aopwiki.org/aops/610" in mention_ids

    def test_missing_study_id_is_ignored(self):
        state = CrateState()
        # No study exists; passing a bogus id must not raise.
        result = materialize_aop_subgraph(state, "610", study_id="nonexistent")
        assert result["aop_id"] == "610"


class TestMaterializeRoundTrip:
    def test_subgraph_survives_build_read_build(self, tmp_path):
        from builder.readers.existing_crate import read_existing_crate
        from builder.tools.builder import export_crate

        state = CrateState()
        materialize_aop_subgraph(state, "610")
        export_crate(state, str(tmp_path / "crate"))

        reread = read_existing_crate(str(tmp_path / "crate"))
        assert len(_by_type(reread, "AdverseOutcomePathway")) == 1
        assert len(_by_type(reread, "KeyEvent")) == 4
        assert len(_by_type(reread, "KeyEventRelationship")) == 3
        # The AOP node keeps its resolvable IRI as the entity_id (no '#' prefix).
        aop = _by_type(reread, "AdverseOutcomePathway")[0]
        assert aop.entity_id == "https://aopwiki.org/aops/610"
