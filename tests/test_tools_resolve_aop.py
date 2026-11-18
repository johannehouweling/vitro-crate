"""Tests for the ``resolve_aop`` composite tool (Issue #180).

``resolve_aop`` turns ONE AOP-Wiki id into the pathway a Study
addresses and wires it deterministically: one ``AdverseOutcomePathway`` entity at
its AOP-Wiki address, wired onto the Study named by ``study_id`` — or onto the
sole Study when none is named — via the ``aop`` / ``mentions`` reference.
``link_assay_to_key_event`` then stores the one ``KeyEvent`` an Assay measures,
matched against the events AOP-Wiki lists for that pathway. The rest of the
pathway stays on AOP-Wiki: no other key event, no ``KeyEventRelationship`` and
none of the properties that link them reaches the state or the crate.

The only model-supplied input is the numeric ``aop_id``; everything else is the
deterministic AOP-Wiki answer (D5: never fabricate ids). Tests never hit the
network — the AOP-Wiki HTTP is mocked from the bundled AOP-610 fixture exactly
as the existing lookup tests do (per project policy, lookups stay offline).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools.composites import resolve_aop
from builder.tools.drafters import draft_investigation, draft_study
from builder.tools.lookups import lookup_aop as _lookup_aop
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
    # The tools-layer lookup_aop is independently lru_cached, and the suite
    # defaults it to a miss (tests/conftest.py). These tests drive the real one
    # over the recorded page.
    from builder.tools import lookups as tool_lookups

    monkeypatch.setattr(tool_lookups, "lookup_aop", _lookup_aop)
    tool_lookups.lookup_aop.cache_clear()  # ty: ignore[unresolved-attribute]
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


# The properties that link a pathway to its events and one event to another.
_LINK_PROPERTIES = frozenset(
    {
        "has_molecular_initiating_event",
        "has_key_event",
        "has_adverse_outcome",
        "has_key_event_relationship",
        "upstream_event",
        "downstream_event",
    }
)


def _typed(node: dict, wanted: str) -> bool:
    """Whether *node* carries *wanted* among its types, prefixed or not."""
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return any(str(v).split(":")[-1] == wanted for v in values if v)


def _graph(state: CrateState) -> list[dict]:
    from builder.tools.builder import assemble_crate

    return assemble_crate(
        state, materialize_payload=False, include_all_scanned=False
    ).metadata.generate()["@graph"]


class TestResolveAopState:
    def test_stores_the_pathway_and_nothing_else_of_it(self):
        state = CrateState()
        result = resolve_aop(state, "610")

        # The AOP-610 fixture is 1 MIE + 2 KE + 1 AO + 3 relationships. Only the
        # pathway itself is stored; the seven stay on AOP-Wiki.
        aops = _by_type(state, "AdverseOutcomePathway")
        assert len(aops) == 1
        assert _by_type(state, "KeyEvent") == []
        assert _by_type(state, "KeyEventRelationship") == []

        # Result reports the stored pathway, and no relationship.
        assert result["aop_id"] == "610"
        assert result["aop_entity_id"] == aops[0].entity_id
        assert "relationships" not in result

    def test_result_lists_the_events_of_the_pathway_without_storing_them(self):
        # (#382) The composite used to report COUNTS only, so a caller had
        # nothing to pick the measured event WITH — the reason neither arm ever
        # linked an Assay to the event it measures.
        state = CrateState()
        result = resolve_aop(state, "610")

        # Every value comes off the fixture through `lookup_aop`; the test writes
        # none of them into state.
        assert {
            "@id": "https://aopwiki.org/events/888",
            "name": "Binding of inhibitor, NADH-ubiquinone oxidoreductase (complex I)",
            "eventType": "Molecular Initiating Event",
        } in result["events_detail"]
        # Detail and count describe the same four events, the ones the lookup
        # lists for the pathway, in its order.
        assert len(result["events_detail"]) == result["events"] == 4
        assert [d["@id"] for d in result["events_detail"]] == [
            e["@id"] for e in aopwiki.lookup_aop("610")["events"]
        ]
        # They are listed to choose from, and none of them is stored.
        assert _by_type(state, "KeyEvent") == []

    def test_event_types_discriminated_only_by_event_type_field(self):
        from builder.tools.composites import link_assay_to_key_event, scaffold_isa_backbone

        state = CrateState()
        scaffold = scaffold_isa_backbone(
            state, investigation={"name": "I"}, study={"name": "S"}, assay={"name": "A"}
        )
        resolve_aop(state, "610", study_id=scaffold["study_id"])
        link_assay_to_key_event(
            state,
            scaffold["assay_id"],
            [
                "Binding of inhibitor, NADH-ubiquinone oxidoreductase (complex I)",
                "Mitochondrial dysfunction",
                "Parkinsonian motor deficits",
            ],
        )
        events = _by_type(state, "KeyEvent")
        # Every stored event is @type KeyEvent; the only discriminator is eventType.
        types = {e.fields.get("eventType") for e in events}
        assert types == {"Molecular Initiating Event", "Key Event", "Adverse Outcome"}

    def test_aop_carries_its_own_facts_and_no_link_arrays(self):
        state = CrateState()
        resolve_aop(state, "610")
        aop = _by_type(state, "AdverseOutcomePathway")[0]
        assert aop.fields["identifier"] == "610"
        assert aop.fields["url"] == "https://aopwiki.org/aops/610"
        assert aop.fields["alternateName"] == "MC-I inhibition to Parkinsonism"
        assert "parkinsonian motor deficits" in aop.fields["name"]
        assert not _LINK_PROPERTIES & set(aop.fields)

    def test_no_relationship_and_no_link_is_stored(self):
        state = CrateState()
        resolve_aop(state, "610")
        assert _by_type(state, "KeyEventRelationship") == []
        assert not [e.entity_id for e in state.list_entities() if _LINK_PROPERTIES & set(e.fields)]

    def test_the_cached_answer_of_the_lookup_keeps_its_links(self):
        # `lookup_aop` is cached and hands every caller the same dict. Storing the
        # pathway without its links must not take them out of the answer the next
        # caller reads.
        from builder.tools import lookups as tool_lookups

        resolve_aop(CrateState(), "610")

        cached = tool_lookups.lookup_aop("610")["data"]["aop"]
        assert {
            "has_molecular_initiating_event",
            "has_key_event",
            "has_adverse_outcome",
            "has_key_event_relationship",
        } <= set(cached)

    def test_idempotent_no_duplicates(self):
        state = CrateState()
        resolve_aop(state, "610")
        resolve_aop(state, "610")
        assert len(_by_type(state, "AdverseOutcomePathway")) == 1
        assert _by_type(state, "KeyEvent") == []
        assert _by_type(state, "KeyEventRelationship") == []

    def test_unknown_aop_returns_error(self, monkeypatch):
        from builder.tools import lookups as tool_lookups

        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "nope"},
        )
        state = CrateState()
        result = resolve_aop(state, "999999")
        assert result.get("ok") is False
        assert _by_type(state, "AdverseOutcomePathway") == []


class TestResolveAopBuild:
    def test_graph_contains_the_pathway_and_nothing_else_of_it(self):
        state = CrateState()
        resolve_aop(state, "610")
        report = build_and_validate(state)
        # The build itself succeeds (no exception → report has conformance).
        assert "conformance" in report

        graph = _graph(state)

        # Membership, not equality: the AOP node carries its AOP class AND
        # schema:DefinedTerm, because the AOP classes resolve to
        # aopkb.org/aop_ontology#… and the base profile asks a described contextual
        # entity for a schema.org type. The AOP class stays first.
        aop = next(n for n in graph if _typed(n, "AdverseOutcomePathway"))
        assert aop["@type"][0] == "AdverseOutcomePathway"
        assert "schema:DefinedTerm" in aop["@type"]

        # The AOP @id is the resolvable AOP-Wiki IRI, and the node carries its own
        # facts: every value below is the fixture's.
        assert aop["@id"] == "https://aopwiki.org/aops/610"
        assert aop["identifier"] == "610"
        assert aop["url"] == "https://aopwiki.org/aops/610"
        assert aop["alternateName"] == "MC-I inhibition to Parkinsonism"
        assert "parkinsonian motor deficits" in aop["name"]

        # The rest of the pathway stays on AOP-Wiki: no assay names a key event
        # here, so the graph describes none, and nothing links the pathway to one.
        assert [n for n in graph if _typed(n, "KeyEvent")] == []
        assert [n for n in graph if _typed(n, "KeyEventRelationship")] == []
        assert not _LINK_PROPERTIES & set(aop)
        assert "aopwiki.org/events" not in json.dumps(graph)
        assert "aopwiki.org/relationships" not in json.dumps(graph)


class TestResolveAopStudyWiring:
    def test_wires_aop_onto_study(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        resolve_aop(state, "610", study_id=study.entity_id)

        # The AOP id is recorded on the Study's `aop` reference field.
        refs = study.fields.get("aop")
        assert refs is not None
        ids = [r.get("@id") if isinstance(r, dict) else r for r in refs]
        assert "https://aopwiki.org/aops/610" in ids

    def test_study_wiring_round_trips_into_graph(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        resolve_aop(state, "610", study_id=study.entity_id)

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
        result = resolve_aop(state, "610", study_id="nonexistent")
        assert result["aop_id"] == "610"

    @pytest.mark.parametrize("unnamed", [{}, {"study_id": ""}])
    def test_the_sole_study_is_wired_without_being_named(self, unnamed):
        # (#738) 2 of the 28 recorded sessions called this without `study_id`
        # and shipped 36 nodes attached to nothing. One Study is not a guess.
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})

        result = resolve_aop(state, "610", **unnamed)

        assert result["wired_to_study"] == study.entity_id
        ids = [r.get("@id") if isinstance(r, dict) else r for r in study.fields["aop"]]
        assert "https://aopwiki.org/aops/610" in ids

    def test_two_studies_are_not_guessed_between(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        uptake = draft_study(state, inv.entity_id, {"name": "Uptake"})
        viability = draft_study(state, inv.entity_id, {"name": "Viability"})

        result = resolve_aop(state, "610")

        assert result["wired_to_study"] is None
        assert "aop" not in uptake.fields
        assert "aop" not in viability.fields


class TestThePathwaySurvivesExport:
    """Read the bytes, not a reader.

    This asserted build → read → build through ``readers.existing_crate``, which had
    no production caller and did not round-trip a real crate (#711). What the test was
    really for is that the pathway reaches the file a third party opens, so it now
    reads ``ro-crate-metadata.json`` itself — the same claim, one layer closer to it,
    and it fails if export drops a node rather than if a private reader does.
    """

    @staticmethod
    def _nodes(graph: list[dict], type_name: str) -> list[dict]:
        """Nodes of *type_name*, tolerating a list ``@type`` and a prefixed term."""
        return [node for node in graph if _typed(node, type_name)]

    def test_the_written_crate_carries_the_pathway_alone(self, tmp_path):
        from builder.tools.builder import export_crate

        state = CrateState()
        resolve_aop(state, "610")
        export_crate(state, str(tmp_path / "crate"))

        doc = json.loads((tmp_path / "crate" / "ro-crate-metadata.json").read_text())
        graph = doc["@graph"]
        # The AOP-610 fixture is 1 MIE + 2 KE + 1 AO + 3 relationships, and none of
        # the seven is in the file: no assay names an event of it.
        assert len(self._nodes(graph, "AdverseOutcomePathway")) == 1
        assert self._nodes(graph, "KeyEvent") == []
        assert self._nodes(graph, "KeyEventRelationship") == []
        # The AOP keeps its resolvable IRI as its @id — not a minted '#' anchor, which
        # is what makes it citable outside this crate.
        pathway = self._nodes(graph, "AdverseOutcomePathway")[0]
        assert pathway["@id"] == "https://aopwiki.org/aops/610"
        assert not _LINK_PROPERTIES & set(pathway)


class TestTheWriterLeavesTheRestOfAPathwayOut:
    """Whatever a session holds, the crate carries a pathway and the key events an assay names.

    A session saved while the builder stored a whole pathway holds every key event,
    every key event relationship and the properties that link them. Such a session
    is left as it is, so the writer is where the rule holds.
    """

    _PATHWAY = "https://aopwiki.org/aops/610"
    _MEASURED = "https://aopwiki.org/events/177"

    def _saved_session(self, **assay_fields) -> CrateState:
        """A session saved with the whole of AOP 610, read back from its saved form.

        Every node of the lookup's answer is stored under its AOP-Wiki address with
        every field it carries, which is what the builder stored then.
        """
        from builder.state import Entity, EntityProvenance
        from builder.tools.composites import scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "Saved with the whole pathway"
        scaffold_isa_backbone(
            state,
            investigation={"name": "I"},
            study={"name": "S", "aop": [{"@id": self._PATHWAY}]},
            assay={"name": "Complex I activity assay", **assay_fields},
        )
        answer = aopwiki.lookup_aop("610")
        for node in (answer["aop"], *answer["events"], *answer["relationships"]):
            entity = Entity(
                entity_id=node["@id"],
                type=node["@type"],
                _provenance=EntityProvenance(created_by="lookup"),
            )
            entity.set_fields_from_dict(
                {k: v for k, v in node.items() if k not in ("@id", "@type")}, source="lookup"
            )
            state.add_entity(entity)
        return CrateState.from_dict(state.to_dict())

    def test_the_session_holds_the_whole_pathway(self):
        # The control for every test below: the rest of the pathway is there to be
        # left out, links included.
        state = self._saved_session(keyEvent={"@id": self._MEASURED})

        assert len(_by_type(state, "KeyEvent")) == 4
        assert len(_by_type(state, "KeyEventRelationship")) == 3
        assert _LINK_PROPERTIES <= {
            key for entity in state.list_entities() for key in entity.fields
        }

    def test_the_pathway_is_written_without_its_links(self):
        graph = _graph(self._saved_session(keyEvent={"@id": self._MEASURED}))

        pathway = next(n for n in graph if n["@id"] == self._PATHWAY)
        assert pathway["@type"] == ["AdverseOutcomePathway", "schema:DefinedTerm"]
        assert pathway["identifier"] == "610"
        assert pathway["alternateName"] == "MC-I inhibition to Parkinsonism"
        assert not _LINK_PROPERTIES & set(pathway)

    def test_only_the_key_event_an_assay_names_is_written(self):
        graph = _graph(self._saved_session(keyEvent={"@id": self._MEASURED}))

        events = [n for n in graph if _typed(n, "KeyEvent")]
        assert [n["@id"] for n in events] == [self._MEASURED]
        # The measured event keeps its own facts, as the lookup gave them.
        assert events[0]["@type"] == ["KeyEvent", "schema:DefinedTerm"]
        assert events[0]["name"] == "Mitochondrial dysfunction"
        assert events[0]["eventType"] == "Key Event"
        assert events[0]["identifier"] == "177"
        assert events[0]["short_name"] == "Event 177"

    def test_no_relationship_and_no_link_property_is_written(self):
        graph = _graph(self._saved_session(keyEvent={"@id": self._MEASURED}))

        assert [n for n in graph if _typed(n, "KeyEventRelationship")] == []
        assert not [n["@id"] for n in graph if _LINK_PROPERTIES & set(n)]
        assert "aopwiki.org/relationships" not in json.dumps(graph)

    def test_nothing_written_points_at_an_event_left_out(self):
        graph = _graph(self._saved_session(keyEvent={"@id": self._MEASURED}))

        written = json.dumps(graph)
        for left_out in (888, 889, 890):
            assert f"aopwiki.org/events/{left_out}" not in written

    def test_the_study_and_the_assay_name_them_as_before(self):
        graph = _graph(self._saved_session(keyEvent={"@id": self._MEASURED}))

        study = next(n for n in graph if n.get("additionalType") == "Study")
        assay = next(n for n in graph if n.get("additionalType") == "Assay")
        assert study["aop"] == {"@id": self._PATHWAY}
        assert assay["keyEvent"] == {"@id": self._MEASURED}

    @pytest.mark.parametrize(
        ("field", "written_as"),
        [
            ("key_event", "keyEvent"),
            ("keyEvent", "keyEvent"),
            ("key_events", "keyEvent"),
            ("mentions", "mentions"),
        ],
    )
    def test_an_assay_names_its_key_event_under_any_of_its_fields(self, field, written_as):
        graph = _graph(self._saved_session(**{field: self._MEASURED}))

        assert [n["@id"] for n in graph if _typed(n, "KeyEvent")] == [self._MEASURED]
        assay = next(n for n in graph if n.get("additionalType") == "Assay")
        assert assay[written_as] == {"@id": self._MEASURED}

    def test_an_assay_naming_two_key_events_gets_both(self):
        both = [self._MEASURED, "https://aopwiki.org/events/888"]
        graph = _graph(self._saved_session(keyEvent=[{"@id": i} for i in both]))

        assert sorted(n["@id"] for n in graph if _typed(n, "KeyEvent")) == sorted(both)

    def test_a_key_event_no_assay_names_is_not_written(self):
        graph = _graph(self._saved_session())

        assert [n for n in graph if _typed(n, "KeyEvent")] == []
        assert [n["@id"] for n in graph if _typed(n, "AdverseOutcomePathway")] == [self._PATHWAY]

    def test_the_session_is_left_as_it_is(self):
        state = self._saved_session(keyEvent={"@id": self._MEASURED})
        before = state.to_dict()["entities"]

        _graph(state)

        assert state.to_dict()["entities"] == before

    def test_the_written_file_holds_the_same(self, tmp_path):
        from builder.tools.builder import export_crate

        state = self._saved_session(keyEvent={"@id": self._MEASURED})
        export_crate(state, str(tmp_path / "crate"), validate=False)

        graph = json.loads((tmp_path / "crate" / "ro-crate-metadata.json").read_text())["@graph"]
        assert [n["@id"] for n in graph if _typed(n, "AdverseOutcomePathway")] == [self._PATHWAY]
        assert [n["@id"] for n in graph if _typed(n, "KeyEvent")] == [self._MEASURED]
        assert [n for n in graph if _typed(n, "KeyEventRelationship")] == []
        assert not [n["@id"] for n in graph if _LINK_PROPERTIES & set(n)]


class TestTheFindingsNameTheToolThatAnswersThem:
    """A Study with no pathway and an Assay with no key event are told which tool to call.

    Both shapes report the gap on ``schema:mentions``. A hint to set that
    property sends the agent to write an AOP-Wiki address by hand, which gives the
    crate a link and no entity behind it.
    """

    @staticmethod
    def _fix_for(finding: str) -> str:
        from builder.tools.composites import scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "No pathway yet"
        scaffold_isa_backbone(
            state, investigation={"name": "I"}, study={"name": "S"}, assay={"name": "A"}
        )
        issues = build_and_validate(state, severity="recommended", profile="tox")["issues"]
        return next(i["fix"] for i in issues if i["message"].startswith(finding))

    def test_the_study_is_pointed_at_the_pathway_tool(self):
        fix = self._fix_for("Study SHOULD reference its Adverse Outcome Pathway")

        assert "`resolve_aop`" in fix
        assert "`mentions`" not in fix

    def test_the_assay_is_pointed_at_the_key_event_tool(self):
        fix = self._fix_for("Assay SHOULD reference the Key Event")

        assert "`link_assay_to_key_event`" in fix
        assert "`mentions`" not in fix

    def test_the_assay_hint_asks_a_person_for_the_event(self):
        # Guidance shows this hint to a person as the question's suggestion. A
        # person can name the event the assay measures, and cannot call a tool.
        fix = self._fix_for("Assay SHOULD reference the Key Event")

        assert fix.startswith("Name the key event ")
        assert "Call " not in fix

    @staticmethod
    def _crate_and_finding(finding: str) -> tuple[CrateState, dict]:
        from builder.tools.composites import scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "No pathway yet"
        scaffold_isa_backbone(
            state, investigation={"name": "I"}, study={"name": "S"}, assay={"name": "A"}
        )
        issues = build_and_validate(state, severity="recommended", profile="tox")["issues"]
        return state, next(i for i in issues if i["message"].startswith(finding))

    def test_the_study_hint_can_be_followed_with_the_id_the_finding_gives(self):
        # The finding names the Study by the id the crate gives it, which is the
        # id the agent has in hand when it follows the hint.
        state, finding = self._crate_and_finding(
            "Study SHOULD reference its Adverse Outcome Pathway"
        )
        study = _by_type(state, "Study")[0]
        assert finding["entity_id"] != study.entity_id

        result = resolve_aop(state, "610", study_id=finding["entity_id"])

        assert result["wired_to_study"] == study.entity_id
        assert build_and_validate(state, severity="required", profile="isa")["conformance"] == {
            "isa": True
        }

    def test_the_assay_hint_can_be_followed_with_the_id_the_finding_gives(self):
        from builder.tools.composites import link_assay_to_key_event

        state, finding = self._crate_and_finding("Assay SHOULD reference the Key Event")
        resolve_aop(state, "610")
        assay = _by_type(state, "Assay")[0]
        assert finding["entity_id"] != assay.entity_id

        result = link_assay_to_key_event(state, finding["entity_id"], "mitochondrial dysfunction")

        assert result["ok"] is True
        assert assay.fields["keyEvent"] == "https://aopwiki.org/events/177"


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

    def test_no_key_events_says_to_resolve_the_aop_first(self) -> None:
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate([])
        out = link_assay_to_key_event(state, assay_id, "anything")
        assert out["ok"] is False
        assert "resolve_aop" in out["error"]

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
        assert assay["keyEvent"] == {"@id": "https://aopwiki.org/events/177"}


class TestLinkAssayToAKeyEventOfThePathway:
    """The same link, but over the events AOP-Wiki really lists for the pathway (#382).

    :class:`TestLinkAssayToKeyEvent` hand-builds its KeyEvents, which cannot show
    that the id committed onto the Assay is one the LOOKUP minted. These tests
    type only the event's lowercase NAME and let the AOP-610 fixture supply every
    id, so a name-derived or fabricated IRI fails them. The crate holds the
    pathway and none of its events until an Assay is linked to one.
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
            resolve_aop(state, aop_id, study_id=scaffold["study_id"])
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
        assert assay_node["keyEvent"] == {"@id": self._MITOCHONDRIAL_DYSFUNCTION}
        # And the target really is a KeyEvent node in the SAME graph — the whole
        # point is an edge to an event the crate describes, not a dangling reference.
        assert self._MITOCHONDRIAL_DYSFUNCTION in {e.entity_id for e in _by_type(state, "KeyEvent")}

    def test_only_the_matched_event_is_stored(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        events = _by_type(state, "KeyEvent")
        assert [e.entity_id for e in events] == [self._MITOCHONDRIAL_DYSFUNCTION]
        # It carries the facts the lookup gave for it, as a lookup's answer.
        assert events[0].fields == {
            "name": "Mitochondrial dysfunction",
            "eventType": "Key Event",
            "identifier": "177",
            "url": "https://aopwiki.org/events/177.json",
            "short_name": "Event 177",
            "biologicalOrganization": "Cellular",
        }
        assert events[0]._provenance.created_by == "lookup"
        assert _by_type(state, "KeyEventRelationship") == []

    def test_the_measured_event_reaches_the_crate_as_a_described_entity(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        graph = _graph(state)
        events = [n for n in graph if _typed(n, "KeyEvent")]
        assert [n["@id"] for n in events] == [self._MITOCHONDRIAL_DYSFUNCTION]
        assert events[0]["@type"] == ["KeyEvent", "schema:DefinedTerm"]
        assert events[0]["name"] == "Mitochondrial dysfunction"
        pathway = next(n for n in graph if _typed(n, "AdverseOutcomePathway"))
        assert not _LINK_PROPERTIES & (set(pathway) | set(events[0]))
        # The Study and the Assay carry the only connection between the two.
        study = next(n for n in graph if n.get("additionalType") == "Study")
        assert study["aop"] == {"@id": pathway["@id"]}
        assert [n for n in graph if _typed(n, "KeyEventRelationship")] == []

    def test_the_pathway_and_the_measured_event_raise_no_finding(self):
        # The widest tier, all three profiles. The Study's pathway is in place from
        # the start; the Assay's warning is the control, and the link clears it.
        from builder.tools.composites import link_assay_to_key_event

        def about_aop_wiki(state: CrateState) -> list[str]:
            issues = build_and_validate(state, severity="optional")["issues"]
            return [i["message"] for i in issues if "aopwiki.org" in json.dumps(i)]

        state, assay_id = self._crate()
        before = about_aop_wiki(state)
        assert len(before) == 1 and before[0].startswith("Assay SHOULD reference the Key Event")

        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        assert about_aop_wiki(state) == []

    def test_an_event_stated_by_its_address_is_matched_on_the_address(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        result = link_assay_to_key_event(state, assay_id, self._MITOCHONDRIAL_DYSFUNCTION)

        assert result["ok"] is True
        assert result["key_event_ids"] == [self._MITOCHONDRIAL_DYSFUNCTION]
        assert result["matched_names"] == ["Mitochondrial dysfunction"]
        assert [e.entity_id for e in _by_type(state, "KeyEvent")] == [
            self._MITOCHONDRIAL_DYSFUNCTION
        ]

    def test_an_event_stated_by_its_number_is_matched_on_the_number(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        result = link_assay_to_key_event(state, assay_id, "177")

        assert result["ok"] is True
        assert result["key_event_ids"] == [self._MITOCHONDRIAL_DYSFUNCTION]
        assert [e.entity_id for e in _by_type(state, "KeyEvent")] == [
            self._MITOCHONDRIAL_DYSFUNCTION
        ]

    @pytest.mark.parametrize(
        "stated",
        [
            "https://aopwiki.org/events/999999",
            "999999",
            # The pathway's own address and number name no key event of it.
            "https://aopwiki.org/aops/610",
            "610",
        ],
    )
    def test_an_address_or_number_the_pathway_does_not_list_writes_nothing(self, stated):
        # D5: an address or a number is used only once the lookup lists it for a
        # pathway in the crate. It is never taken on its own word.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        result = link_assay_to_key_event(state, assay_id, stated)

        assert result["ok"] is False
        assert "keyEvent" not in state.get_entity(assay_id).fields
        assert _by_type(state, "KeyEvent") == []

    def test_two_assays_measuring_one_event_share_its_entity(self):
        from builder.tools.composites import link_assay_to_key_event
        from builder.tools.drafters import draft_assay

        state, assay_id = self._crate()
        study_id = _by_type(state, "Study")[0].entity_id
        second = draft_assay(state, study_id, {"name": "Second complex I assay"})
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")
        link_assay_to_key_event(state, second.entity_id, "Mitochondrial dysfunction")

        assert [e.entity_id for e in _by_type(state, "KeyEvent")] == [
            self._MITOCHONDRIAL_DYSFUNCTION
        ]
        assays = [n for n in _graph(state) if n.get("additionalType") == "Assay"]
        assert [n["keyEvent"] for n in assays] == [{"@id": self._MITOCHONDRIAL_DYSFUNCTION}] * 2

    def test_linking_the_same_event_again_stores_it_once(self):
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")
        result = link_assay_to_key_event(state, assay_id, "Mitochondrial dysfunction")

        assert result["key_event_ids"] == [self._MITOCHONDRIAL_DYSFUNCTION]
        assert len(_by_type(state, "KeyEvent")) == 1

    def test_an_event_two_pathways_share_is_one_candidate(self):
        # AOP 611 here repeats two events of AOP 610 under the same AOP-Wiki ids.
        # One id is one event, whichever pathway lists it.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate(("610", "611"))
        result = link_assay_to_key_event(state, assay_id, "Parkinsonian motor deficits")

        assert result["ok"] is True
        assert result["key_event_ids"] == ["https://aopwiki.org/events/890"]
        offered = link_assay_to_key_event(state, assay_id, "TPO inhibition")["candidates"]
        assert len({c["@id"] for c in offered}) == len(offered) == 5

    def test_a_term_at_the_pathways_address_is_the_pathway_to_match_against(self):
        # A term drafted at the pathway's AOP-Wiki address before the pathway
        # composite ran keeps that address, and the composite reuses it rather than
        # store a second entity there. AOP-Wiki still lists the pathway's events at
        # that address, so the assay can be linked to one of them.
        from builder.tools.composites import link_assay_to_key_event, scaffold_isa_backbone
        from builder.tools.drafters import draft_defined_term

        state = CrateState()
        state.metadata.title = "AOP link"
        scaffold = scaffold_isa_backbone(
            state,
            investigation={"name": "I"},
            study={"name": "S"},
            assay={"name": "Complex I activity assay"},
        )
        draft_defined_term(state, "AOP 610", {"url": "https://aopwiki.org/aops/610"})
        resolve_aop(state, "610", study_id=scaffold["study_id"])
        assert _by_type(state, "AdverseOutcomePathway") == []

        result = link_assay_to_key_event(state, scaffold["assay_id"], "mitochondrial dysfunction")

        assert result["ok"] is True
        assert result["key_event_ids"] == [self._MITOCHONDRIAL_DYSFUNCTION]

    def test_a_term_at_the_pathways_address_is_named_when_aop_wiki_gives_nothing(self, monkeypatch):
        # The pathway is in the crate, held by the term; running the pathway
        # composite again would change nothing, so the answer does not ask for it.
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event, scaffold_isa_backbone
        from builder.tools.drafters import draft_defined_term

        state = CrateState()
        state.metadata.title = "AOP link"
        scaffold = scaffold_isa_backbone(
            state,
            investigation={"name": "I"},
            study={"name": "S"},
            assay={"name": "Complex I activity assay"},
        )
        draft_defined_term(state, "AOP 610", {"url": "https://aopwiki.org/aops/610"})
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )

        result = link_assay_to_key_event(state, scaffold["assay_id"], "mitochondrial dysfunction")

        assert result["ok"] is False
        assert result["unlisted_pathways"] == ["https://aopwiki.org/aops/610"]
        assert "resolve_aop" not in result["error"]

    def test_a_pathway_whose_events_aop_wiki_does_not_give_says_so(self, monkeypatch):
        # The pathway is in the crate, and AOP-Wiki cannot be asked for its events
        # when the Assay is to be linked. That is not "no match", and running the
        # pathway composite again is not the remedy.
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )
        result = link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        assert result["ok"] is False
        assert result["candidates"] == []
        assert "AOP-Wiki gave no key events" in result["error"]
        assert "resolve_aop" not in result["error"]
        assert "keyEvent" not in state.get_entity(assay_id).fields

    def test_a_stored_event_stays_a_candidate_when_aop_wiki_cannot_be_asked(self, monkeypatch):
        # An event one Assay measures is in the crate, so a second Assay can be
        # linked to it without AOP-Wiki.
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event
        from builder.tools.drafters import draft_assay

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")
        second = draft_assay(
            state, _by_type(state, "Study")[0].entity_id, {"name": "Second complex I assay"}
        )
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )
        result = link_assay_to_key_event(state, second.entity_id, "mitochondrial dysfunction")

        assert result["ok"] is True
        assert result["key_event_id"] == self._MITOCHONDRIAL_DYSFUNCTION

    def test_the_pathway_aop_wiki_gives_no_events_for_is_named(self, monkeypatch):
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )
        result = link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        assert result["ok"] is False
        assert result["unlisted_pathways"] == ["https://aopwiki.org/aops/610"]
        assert "https://aopwiki.org/aops/610" in result["error"]

    def test_a_name_compared_without_the_pathways_events_is_not_called_unmatched(self, monkeypatch):
        # The crate holds the event one Assay measures. With AOP-Wiki down, only
        # that event can be compared, so another event of the pathway matches
        # nothing. The answer names the pathway AOP-Wiki gave no events for, so the
        # name is not taken for one that names no event of it.
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event
        from builder.tools.drafters import draft_assay

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")
        second = draft_assay(
            state, _by_type(state, "Study")[0].entity_id, {"name": "Second complex I assay"}
        )
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )
        result = link_assay_to_key_event(
            state, second.entity_id, "Degeneration of dopaminergic neurons"
        )

        assert result["ok"] is False
        assert result["unlisted_pathways"] == ["https://aopwiki.org/aops/610"]
        assert "AOP-Wiki gave no key events for https://aopwiki.org/aops/610" in result["error"]
        assert "keyEvent" not in second.fields

    def test_a_link_made_without_the_pathways_events_says_so(self, monkeypatch):
        # One name matches the event the crate holds and the other is compared
        # with that event alone; the link is made and the outage reported with it.
        from builder.tools import lookups as tool_lookups
        from builder.tools.composites import link_assay_to_key_event
        from builder.tools.drafters import draft_assay

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")
        second = draft_assay(
            state, _by_type(state, "Study")[0].entity_id, {"name": "Second complex I assay"}
        )
        monkeypatch.setattr(
            tool_lookups,
            "lookup_aop",
            lambda aop_id: {"found": False, "data": {}, "error": "AOP-Wiki unavailable"},
        )
        result = link_assay_to_key_event(
            state,
            second.entity_id,
            ["mitochondrial dysfunction", "Degeneration of dopaminergic neurons"],
        )

        assert result["ok"] is True
        assert result["key_event_ids"] == [self._MITOCHONDRIAL_DYSFUNCTION]
        assert result["unlisted_pathways"] == ["https://aopwiki.org/aops/610"]

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
        # The candidates are the four fixture events, offered for a human choice,
        # and none of them is stored for having been offered.
        assert [c["name"] for c in result["candidates"]] == [
            e["name"] for e in aopwiki.lookup_aop("610")["events"]
        ]
        assert len(result["candidates"]) == 4
        assert _by_type(state, "KeyEvent") == []

    def test_ambiguous_name_across_two_pathways_writes_nothing(self):
        # Two pathways in the crate share an event NAME under different AOP-Wiki
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
        # D5 control: the committed reference must be an id the LOOKUP gave for an
        # event of the pathway, so an id minted from the name
        # (#KeyEvent_mitochondrial_...) or any other guess fails regardless of how
        # plausible it looks. The linking call stores the event itself, so being
        # in state no longer shows where an id came from; the lookup's answer does.
        from builder.tools.composites import link_assay_to_key_event

        state, assay_id = self._crate()
        link_assay_to_key_event(state, assay_id, "mitochondrial dysfunction")

        committed = state.get_entity(assay_id).fields["keyEvent"]
        # `set_fields` canonicalises a reference to its bare id, so the stored
        # value is the IRI string rather than the `{"@id": …}` it was passed.
        ref_id = committed.get("@id") if isinstance(committed, dict) else committed
        assert ref_id in {e["@id"] for e in aopwiki.lookup_aop("610")["events"]}
        assert {e.entity_id for e in _by_type(state, "KeyEvent")} == {ref_id}

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
