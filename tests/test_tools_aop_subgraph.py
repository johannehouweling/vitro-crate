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

import copy
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
    """Serve the bundled AOP-610 fixture instead of hitting AOP-Wiki.

    ``/aops/611.json`` is served too: a second pathway whose only key event
    REPEATS AOP-610's "Mitochondrial dysfunction" under a different event id. Two
    pathways in one crate legitimately share an event NAME while owning distinct
    AOP-Wiki ids, which is the case ``link_assay_to_key_event`` must refuse rather
    than pick from (#382). It is derived from the same fixture, so the duplicate
    name is the fixture's own wording, not a string the test invents.
    """
    aop_data = _load("aopwiki_aop610.json")
    ambiguous = copy.deepcopy(aop_data)
    ambiguous["aop"]["aop_kes"] = [
        {
            "event_id": 9177,
            "event": aop_data["aop"]["aop_kes"][0]["event"],
            "event_type": "KeyEvent",
        }
    ]

    def fake_get_json(url, **kwargs):
        if url.endswith("/aops/610.json"):
            return aop_data
        if url.endswith("/aops/611.json"):
            return ambiguous
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

    def test_result_reports_materialized_event_iris(self):
        # (#382) The composite used to report COUNTS only, so a caller that had
        # just put four KeyEvents in the crate had nothing to pick one WITH — the
        # reason neither arm ever linked an Assay to the event it measures.
        state = CrateState()
        result = materialize_aop_subgraph(state, "610")

        # Every value comes off the fixture through `lookup_aop`; the test writes
        # none of them into state.
        assert {
            "@id": "https://aopwiki.org/events/888",
            "name": "Binding of inhibitor, NADH-ubiquinone oxidoreductase (complex I)",
            "eventType": "Molecular Initiating Event",
        } in result["events_detail"]
        # Detail and count describe the same materialization.
        assert len(result["events_detail"]) == result["events"]
        assert {d["@id"] for d in result["events_detail"]} == {
            e.entity_id for e in _by_type(state, "KeyEvent")
        }

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

        # Membership, not equality: each AOP node carries its AOP class AND
        # schema:DefinedTerm, because the AOP classes resolve to
        # aopwiki.org/ontology/… and the base profile asks a described contextual
        # entity for a schema.org type. The AOP class stays first.
        def _typed(node, wanted):
            types = node.get("@type")
            return wanted in (types if isinstance(types, list) else [types])

        aop = next(n for n in graph if _typed(n, "AdverseOutcomePathway"))
        kes = [n for n in graph if _typed(n, "KeyEvent")]
        kers = [n for n in graph if _typed(n, "KeyEventRelationship")]
        assert aop["@type"][0] == "AdverseOutcomePathway"
        assert "schema:DefinedTerm" in aop["@type"]
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


class TestLinkAssayToKeyEvent:
    """The Assay -> Key Event link (#382).

    ``keyEvent`` is a fully declared Assay reference field — in the draft schema,
    mapped by ``_ASSAY_MENTION_FIELDS``, consumed by the build — and it had ZERO
    writers anywhere in ``builder/``. No crate either arm produced had ever
    recorded which Key Event an assay measures, so the biological meaning of the
    measurement was absent from every crate while the field looked supported.
    """

    def _crate(self, events: list[tuple[str, str]]):
        from builder.state import CrateState, Entity
        from builder.tools.composites import scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "AOP link"
        scaffold = scaffold_isa_backbone(
            state,
            investigation={"name": "I"},
            study={"name": "S"},
            assay={"name": "Transport assay"},
        )
        for entity_id, name in events:
            state.add_entity(Entity(entity_id=entity_id, type="KeyEvent", fields={"name": name}))
        return state, scaffold["assay_id"]

    _EVENTS = [
        ("https://aopwiki.org/events/177", "Mitochondrial dysfunction"),
        ("https://aopwiki.org/events/279", "Thyroperoxidase, Inhibition"),
    ]

    def test_exact_name_links_the_assay(self) -> None:
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(self._EVENTS)
        out = link_assay_to_key_event(state, assay_id, "Mitochondrial dysfunction")
        assert out["ok"]
        assert out["key_event_id"] == "https://aopwiki.org/events/177"

    def test_case_and_punctuation_are_ignored(self) -> None:
        # A depositor writes "thyroperoxidase inhibition"; AOP-Wiki says
        # "Thyroperoxidase, Inhibition". Same event.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(self._EVENTS)
        out = link_assay_to_key_event(state, assay_id, "thyroperoxidase inhibition")
        assert out["key_event_id"] == "https://aopwiki.org/events/279"

    def test_a_near_miss_writes_nothing_and_offers_candidates(self) -> None:
        # "TPO inhibition" is the same thing to a human and NOT a token match.
        # Guessing which Key Event an assay measures is a scientific claim.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(self._EVENTS)
        out = link_assay_to_key_event(state, assay_id, "TPO inhibition")
        assert out["ok"] is False
        assert state.get_entity(assay_id).fields.get("keyEvent") is None
        assert {c["name"] for c in out["candidates"]} == {n for _i, n in self._EVENTS}

    def test_ambiguous_name_writes_nothing(self) -> None:
        from builder.tools.composites import link_assay_to_key_event

        duplicated = [
            ("https://aopwiki.org/events/1", "Mitochondrial dysfunction"),
            ("https://aopwiki.org/events/2", "mitochondrial  dysfunction"),
        ]
        state, assay_id = self._crate(duplicated)
        out = link_assay_to_key_event(state, assay_id, "Mitochondrial dysfunction")
        assert out["ok"] is False
        assert state.get_entity(assay_id).fields.get("keyEvent") is None

    def test_no_key_events_says_to_materialize_first(self) -> None:
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate([])
        out = link_assay_to_key_event(state, assay_id, "anything")
        assert out["ok"] is False
        assert "materialize_aop_subgraph" in out["error"]

    def test_the_link_reaches_the_exported_crate(self, tmp_path) -> None:
        # The point of the issue: the field existed and nothing ever populated
        # it, so no crate carried the link.
        import json

        from builder.tools.builder import export_crate
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(self._EVENTS)
        link_assay_to_key_event(state, assay_id, "Mitochondrial dysfunction")
        out = tmp_path / "crate"
        export_crate(state, str(out), validate=False)
        graph = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))["@graph"]
        assay = next(n for n in graph if n.get("additionalType") == "Assay")
        assert assay["keyEvent"] == [{"@id": "https://aopwiki.org/events/177"}]


class TestLinkAssayToMaterializedKeyEvent:
    """The same link, but over the KeyEvents AOP-Wiki really produced (#382).

    :class:`TestLinkAssayToKeyEvent` hand-builds its KeyEvents, which cannot show
    that the id committed onto the Assay is one the LOOKUP minted. These tests
    type only the event's lowercase NAME and let the AOP-610 fixture supply every
    id, so a name-derived or fabricated IRI fails them.
    """

    _MITOCHONDRIAL_DYSFUNCTION = "https://aopwiki.org/events/177"

    def _crate(self, aop_ids: tuple[str, ...] = ("610",)):
        from builder.tools.composites import scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "AOP link"
        scaffold = scaffold_isa_backbone(
            state,
            investigation={"name": "I"},
            study={"name": "S"},
            assay={"name": "Complex I activity assay"},
        )
        for aop_id in aop_ids:
            materialize_aop_subgraph(state, aop_id, study_id=scaffold["study_id"])
        return state, scaffold["assay_id"]

    def _assay_node(self, state: CrateState):
        from builder.tools.builder import assemble_crate

        graph = assemble_crate(
            state, materialize_payload=False, include_all_scanned=False
        ).metadata.generate()["@graph"]
        return next(n for n in graph if n.get("additionalType") == "Assay")

    def test_links_assay_to_uniquely_named_key_event(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        # Only the lowercase NAME is typed here; the IRI below is the fixture's.
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        assay_node = self._assay_node(state)
        assert assay_node["keyEvent"] == [{"@id": self._MITOCHONDRIAL_DYSFUNCTION}]
        # And the target really is a KeyEvent node in the SAME graph — the whole
        # point is an edge into the AOP subgraph, not a dangling reference.
        assert self._MITOCHONDRIAL_DYSFUNCTION in {e.entity_id for e in _by_type(state, "KeyEvent")}

    def test_unmatched_name_writes_nothing_and_returns_candidates(self):
        # Honesty control for the row above: "TPO inhibition" is a real assay
        # name that matches nothing in AOP-610, and the abbreviation gap it stands
        # for ("TPO" vs "Thyroperoxidase") is exactly what no matcher may bridge.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        result = link_assay_to_key_event(state, assay_id, "TPO inhibition")

        assert result["ok"] is False
        assert "keyEvent" not in state.get_entity(assay_id).fields
        assert "keyEvent" not in self._assay_node(state)
        # The candidates are the four fixture events, offered for a human choice.
        assert {c["name"] for c in result["candidates"]} == {
            e.fields.get("name") for e in _by_type(state, "KeyEvent")
        }
        assert len(result["candidates"]) == 4

    def test_ambiguous_name_across_two_pathways_writes_nothing(self):
        # Two materialized pathways share an event NAME under different AOP-Wiki
        # ids. Picking either would be a fabricated scientific assertion.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(("610", "611"))
        result = link_assay_to_key_event(state, assay_id, "Mitochondrial dysfunction")

        assert result["ok"] is False
        assert "keyEvent" not in state.get_entity(assay_id).fields
        duplicated = [c for c in result["candidates"] if c["name"] == "Mitochondrial dysfunction"]
        assert len(duplicated) == 2
        assert {c["@id"] for c in duplicated} == {
            self._MITOCHONDRIAL_DYSFUNCTION,
            "https://aopwiki.org/events/9177",
        }

    def test_never_fabricates_an_iri_from_the_name(self):
        # D5 control: the committed reference must be an id that is ALREADY in
        # state, so an id minted from the name (#KeyEvent_mitochondrial_...) or
        # any other guess fails regardless of how plausible it looks.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        committed = state.get_entity(assay_id).fields["keyEvent"]
        # `set_fields` canonicalises a reference to its bare id, so the stored
        # value is the IRI string rather than the `{"@id": …}` it was passed.
        ref_id = committed.get("@id") if isinstance(committed, dict) else committed
        assert ref_id in {e.entity_id for e in _by_type(state, "KeyEvent")}

    def test_field_status_source_is_lookup_on_the_camel_case_field(self):
        # The MIT slot is `Assay:keyEvent` and `_count_filled_fields` keys on the
        # RAW state field name, so writing `key_event` would produce a
        # correct-looking crate whose maturity row stays unfilled.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        assay = state.get_entity(assay_id)
        assert "key_event" not in assay.fields
        status = assay.get_field_status("keyEvent")
        assert status is not None
        # `lookup`, not `llm`/`user`: the id came from AOP-Wiki, not from prose.
        assert status.source == "lookup"
