"""The Key Event question can actually be answered.

An assay's AOP annotation was asked every round and could never be committed.
Two things were wrong, and either alone was enough to make the gap permanent:

* The ISA-Tox shape expresses the annotation as ``schema:mentions`` — the same
  predicate that carries chemicals, organism and anatomy — so the gap arrived
  with ``property="mentions"``, missed the Key Event field set, and went down
  the generic path whose reference guard rejects prose. No reply the user could
  type would commit.
* The question never listed the Key Events. AOP-Wiki lists them for the pathway
  in the crate, but the reader was asked to name one from memory — and a name
  that is none of them cannot resolve to an entity, so a plausible answer would
  be refused anyway.

What is NOT fixed here, deliberately: which Key Event an assay measures. That is
a scientific judgement, and the crate offers its pathway's Key Events as options
rather than choosing among them. A rule that picked one would be right for the
crate it was written against and wrong for the next.

The crate holds the pathway and none of its events until an assay is linked to
one, so the options are read from AOP-Wiki when the question is asked, beside
any Key Event the crate already holds, and an answer stores the event it names.
"""

from __future__ import annotations

import pytest

from builder.agents.pipeline.guidance import (
    _ask_user_prompt,
    _is_key_event_gap,
    _key_event_candidates,
)
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.gap_analysis import Gap
from builder.tools.hitl import SimulatedHumanInterface


def _Engine(state: CrateState):
    """A real AgentEngine carrying *state* — the helpers take an engine, not a stub."""
    from builder.engine import AgentEngine

    engine = AgentEngine()
    engine.state = state
    return engine


def _gap(prop: str, message: str, entity_id: str | None = "./#Assay_a1") -> Gap:
    return Gap(
        tier="SHOULD",
        source="shacl",
        entity_id=entity_id,
        entity_type="Assay",
        property=prop,
        message=message,
        suggestion=None,
        fix_hint="ask-user",
        auto_fixable=False,
    )


@pytest.fixture
def engine():
    state = CrateState()
    for eid, name, kind in (
        (
            "KeyEvent:https://aopwiki.org/events/2376",
            "Inhibition, OATP1C1",
            "Molecular Initiating Event",
        ),
        (
            "KeyEvent:https://aopwiki.org/events/2258",
            "Inhibition, MCT8",
            "Molecular Initiating Event",
        ),
        ("KeyEvent:https://aopwiki.org/events/381", "Altered, white brain matter", "Key Event"),
    ):
        entity = Entity(
            entity_id=eid, type="KeyEvent", _provenance=EntityProvenance(created_by="lookup")
        )
        entity.set_fields_from_dict({"name": name, "eventType": kind}, source="lookup")
        state.add_entity(entity)
    return _Engine(state)


# What the AOP-Wiki stub lists for the pathway of `pathway_engine`: the events the
# `engine` fixture above stores by hand, under the same names and types.
_LISTED = (
    ("https://aopwiki.org/events/2376", "Inhibition, OATP1C1", "Molecular Initiating Event"),
    ("https://aopwiki.org/events/2258", "Inhibition, MCT8", "Molecular Initiating Event"),
    ("https://aopwiki.org/events/381", "Altered, white brain matter", "Key Event"),
)
_MCT8 = "https://aopwiki.org/events/2258"
_KEY_EVENT_GAP = "Assay SHOULD annotate ... Key Event ..."


def _lists_three_events(aop_id: str) -> dict:
    """A stand-in for ``lookup_aop``: one pathway, the three events of ``_LISTED``."""
    address = f"https://aopwiki.org/aops/{aop_id}"
    return {
        "found": True,
        "data": {
            "aop": {
                "@id": address,
                "@type": "AdverseOutcomePathway",
                "name": f"AOP {aop_id}",
                "identifier": str(aop_id),
                "url": address,
                "has_key_event": [{"@id": event} for event, _name, _kind in _LISTED],
            },
            "events": [
                {
                    "@id": event,
                    "@type": "KeyEvent",
                    "name": name,
                    "eventType": kind,
                    "identifier": event.rsplit("/", 1)[-1],
                }
                for event, name, kind in _LISTED
            ],
            "relationships": [],
        },
        "error": None,
    }


def _gives_nothing(aop_id: str) -> dict:
    """A stand-in for ``lookup_aop`` on a day AOP-Wiki cannot be asked."""
    return {"found": False, "data": {}, "error": "AOP-Wiki unavailable (transient)"}


@pytest.fixture
def pathway_engine(monkeypatch):
    """A crate with a Study, an Assay and their pathway, and none of its events."""
    from builder.tools import lookups as tool_lookups
    from builder.tools.composites import resolve_aop

    monkeypatch.setattr(tool_lookups, "lookup_aop", _lists_three_events)
    state = CrateState()
    for entity_id, kind, fields in (
        ("s1", "Study", {"name": "A study"}),
        ("a1", "Assay", {"name": "An assay", "study_id": "s1"}),
    ):
        entity = Entity(
            entity_id=entity_id, type=kind, _provenance=EntityProvenance(created_by="llm")
        )
        entity.set_fields_from_dict(fields, source="llm")
        state.add_entity(entity)
    resolve_aop(state, "464", study_id="s1")
    return _Engine(state)


class _Listening(SimulatedHumanInterface):
    """A human who is told things and asked nothing."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def notify(self, message: str) -> None:
        self.notes.append(message)


def _assay(engine) -> Entity:
    assay = engine.state.get_entity("a1")
    assert assay is not None
    return assay


class TestTheGapIsRecognised:
    def test_the_shape_expresses_it_as_mentions(self):
        """The reason it was unanswerable: `mentions`, not `keyEvent`."""
        gap = _gap(
            "http://schema.org/mentions",
            "Assay SHOULD annotate its measured endpoint with the corresponding "
            "AOP-Wiki Key Event via schema:mentions",
        )
        assert _is_key_event_gap(gap)

    def test_the_explicit_field_name_still_works(self):
        assert _is_key_event_gap(_gap("keyEvent", "MIT parameter linkage to AOPs"))

    def test_another_mentions_gap_is_not_a_key_event_gap(self):
        """`mentions` also carries chemicals, organism and anatomy."""
        gap = _gap("http://schema.org/mentions", "Study SHOULD mention the organism studied")
        assert not _is_key_event_gap(gap)

    def test_an_unrelated_gap_is_not_one(self):
        assert not _is_key_event_gap(_gap("description", "Assay SHOULD have a description"))


class TestTheOptionsAreOffered:
    def test_the_crates_key_events_are_the_candidates(self, engine):
        names = [c["name"] for c in _key_event_candidates(engine)]
        assert "Inhibition, OATP1C1" in names
        assert len(names) == 3

    def test_molecular_initiating_events_lead(self, engine):
        """An in-vitro assay usually measures one, so they are not buried."""
        first = _key_event_candidates(engine)[0]
        assert "initiating" in first["event_type"].casefold()

    def test_the_prompt_lists_them(self, engine):
        gap = _gap("http://schema.org/mentions", "Assay SHOULD annotate ... Key Event ...")
        prompt = _ask_user_prompt(gap, engine)
        assert "Inhibition, OATP1C1" in prompt
        assert "Altered, white brain matter" in prompt

    def test_a_crate_with_no_key_events_lists_nothing(self):
        gap = _gap("http://schema.org/mentions", "Assay SHOULD annotate ... Key Event ...")
        prompt = _ask_user_prompt(gap, _Engine(CrateState()))
        assert "Key Events to choose from" not in prompt

    def test_an_unnamed_key_event_is_not_offered(self):
        """An option a reader cannot recognise is not an option."""
        state = CrateState()
        entity = Entity(
            entity_id="KeyEvent:https://aopwiki.org/events/1",
            type="KeyEvent",
            _provenance=EntityProvenance(created_by="lookup"),
        )
        entity.set_fields_from_dict({"eventType": "Key Event"}, source="lookup")
        state.add_entity(entity)
        assert _key_event_candidates(_Engine(state)) == []


class TestTheOptionsComeFromAopWiki:
    """The crate holds the pathway and none of its events, so the options are the
    events AOP-Wiki lists for it."""

    def test_the_events_aop_wiki_lists_for_the_pathway_are_the_candidates(self, pathway_engine):
        assert pathway_engine.state.list_entities("KeyEvent") == []

        names = [c["name"] for c in _key_event_candidates(pathway_engine)]

        # Initiating events lead, as for the events a crate holds itself.
        assert names == ["Inhibition, MCT8", "Inhibition, OATP1C1", "Altered, white brain matter"]

    def test_the_prompt_lists_them(self, pathway_engine):
        prompt = _ask_user_prompt(_gap("http://schema.org/mentions", _KEY_EVENT_GAP), pathway_engine)

        assert "Key Events to choose from" in prompt
        for _event, name, _kind in _LISTED:
            assert name in prompt

    def test_listing_the_options_stores_none_of_them(self, pathway_engine):
        _ask_user_prompt(_gap("http://schema.org/mentions", _KEY_EVENT_GAP), pathway_engine)

        assert pathway_engine.state.list_entities("KeyEvent") == []

    def test_an_event_the_crate_already_holds_is_offered_once(self, pathway_engine):
        from builder.tools.composites import link_assay_to_key_event

        link_assay_to_key_event(pathway_engine.state, "a1", "Inhibition, MCT8")

        ids = [c["id"] for c in _key_event_candidates(pathway_engine)]
        assert sorted(ids) == sorted(event for event, _name, _kind in _LISTED)

    def test_nothing_is_listed_when_aop_wiki_gives_no_events(self, pathway_engine, monkeypatch):
        from builder.tools import lookups as tool_lookups

        monkeypatch.setattr(tool_lookups, "lookup_aop", _gives_nothing)

        assert _key_event_candidates(pathway_engine) == []
        prompt = _ask_user_prompt(_gap("http://schema.org/mentions", _KEY_EVENT_GAP), pathway_engine)
        assert "Key Events to choose from" not in prompt


class TestAnAnswerStoresTheEventItNames:
    """An answer is matched against the options, and the event it names is stored
    with the link. The shape reports the gap on ``mentions``; the link is the
    Assay's ``keyEvent`` whichever way the event was named."""

    def _gap(self) -> Gap:
        return _gap("http://schema.org/mentions", _KEY_EVENT_GAP, entity_id="./#Assay_a1")

    def test_a_named_event_is_stored_with_the_link(self, pathway_engine):
        from builder.agents.pipeline.guidance import _apply_value

        assert _apply_value(pathway_engine, self._gap(), "inhibition, mct8") is True

        stored = pathway_engine.state.list_entities("KeyEvent")
        assert [e.entity_id for e in stored] == [_MCT8]
        assert stored[0].fields["name"] == "Inhibition, MCT8"
        assert _assay(pathway_engine).fields["keyEvent"] == _MCT8

    def test_an_address_aop_wiki_lists_is_stored_with_its_event(self, pathway_engine):
        from builder.agents.pipeline.guidance import _apply_value
        from builder.tools.builder import assemble_crate

        assert _apply_value(pathway_engine, self._gap(), _MCT8) is True

        assay = _assay(pathway_engine)
        assert assay.fields["keyEvent"] == _MCT8
        assert "mentions" not in assay.fields
        # The crate describes the event the address names: it is no bare link.
        graph = assemble_crate(
            pathway_engine.state, materialize_payload=False, include_all_scanned=False
        ).metadata.generate()["@graph"]
        event = next(n for n in graph if n["@id"] == _MCT8)
        assert event["@type"] == ["KeyEvent", "schema:DefinedTerm"]
        assert event["name"] == "Inhibition, MCT8"

    def test_a_number_aop_wiki_lists_is_stored_with_its_event(self, pathway_engine):
        from builder.agents.pipeline.guidance import _apply_value

        assert _apply_value(pathway_engine, self._gap(), "2258") is True

        assert [e.entity_id for e in pathway_engine.state.list_entities("KeyEvent")] == [_MCT8]
        assert _assay(pathway_engine).fields["keyEvent"] == _MCT8

    def test_an_address_aop_wiki_does_not_list_is_refused_with_the_options(self, pathway_engine):
        """An address that names no event of the crate's pathway is not confirmed by
        the lookup, and the crate could not describe the event it points at. It is
        refused like any other answer that names none of the options (D5), rather
        than stored as a link to an event the crate does not describe."""
        from builder.agents.pipeline.guidance import _apply_value

        human = _Listening()

        assert (
            _apply_value(pathway_engine, self._gap(), "https://aopwiki.org/events/1002", human=human)
            is False
        )

        assay = _assay(pathway_engine)
        assert "keyEvent" not in assay.fields
        assert "mentions" not in assay.fields
        assert pathway_engine.state.list_entities("KeyEvent") == []
        assert "Inhibition, MCT8" in human.notes[0]

    def test_a_term_the_crate_holds_still_stands_in(self, pathway_engine):
        """Unchanged: an id pasted for something the crate holds is a reference, not
        an event to look for."""
        from builder.agents.pipeline.guidance import _apply_value

        term = Entity(
            entity_id="dt_ke_55", type="DefinedTerm", _provenance=EntityProvenance(created_by="llm")
        )
        term.set_fields_from_dict({"name": "KE 55"}, source="llm")
        pathway_engine.state.add_entity(term)
        gap = _gap("keyEvent", "MIT parameter linkage to AOPs", entity_id="a1")

        assert _apply_value(pathway_engine, gap, "dt_ke_55") is True

        assert _assay(pathway_engine).fields["keyEvent"] == "dt_ke_55"
        assert pathway_engine.state.list_entities("KeyEvent") == []

    def test_a_name_that_is_no_option_is_refused_with_the_options(self, pathway_engine):
        from builder.agents.pipeline.guidance import _apply_value

        human = _Listening()

        assert _apply_value(pathway_engine, self._gap(), "TPO inhibition", human=human) is False

        assert "keyEvent" not in _assay(pathway_engine).fields
        assert "Inhibition, MCT8" in human.notes[0]

    def test_the_user_is_told_when_aop_wiki_gave_no_events(self, pathway_engine, monkeypatch):
        from builder.agents.pipeline.guidance import _apply_value
        from builder.tools import lookups as tool_lookups

        monkeypatch.setattr(tool_lookups, "lookup_aop", _gives_nothing)
        human = _Listening()

        assert _apply_value(pathway_engine, self._gap(), "Inhibition, MCT8", human=human) is False

        assert "keyEvent" not in _assay(pathway_engine).fields
        assert "AOP-Wiki returned no key events" in human.notes[0]


class TestTheChoiceStaysTheScientists:
    def test_the_prompt_offers_rather_than_answers(self, engine):
        """Every Key Event is listed — the prompt does not single one out."""
        gap = _gap("http://schema.org/mentions", "Assay SHOULD annotate ... Key Event ...")
        prompt = _ask_user_prompt(gap, engine)
        for name in ("Inhibition, OATP1C1", "Inhibition, MCT8", "Altered, white brain matter"):
            assert name in prompt

    def test_the_leaf_is_told_to_offer_not_choose(self):
        from builder.agents.pipeline.leaves import _gap_context_block

        block = _gap_context_block(
            {"property": "mentions", "candidates": ["Inhibition, OATP1C1", "Inhibition, MCT8"]}
        )
        assert "do not pick one" in block
        assert "Inhibition, OATP1C1" in block

    def test_a_long_list_is_summarised_not_dropped(self):
        from builder.agents.pipeline.leaves import _gap_context_block

        block = _gap_context_block(
            {"property": "mentions", "candidates": [f"Event {n}" for n in range(30)]}
        )
        assert "and 18 more" in block


class TestAnAssayCanMeasureSeveralKeyEvents:
    """One experiment can report two events — a viability control beside the
    inhibition it is built to detect — so an answer naming several is a normal
    answer, not a malformed one.
    """

    def _crate(self) -> CrateState:
        state = CrateState()
        assay = Entity(
            entity_id="assay_1", type="Assay", _provenance=EntityProvenance(created_by="llm")
        )
        assay.set_fields_from_dict({"name": "An assay"}, source="llm")
        state.add_entity(assay)
        for eid, name in (
            ("KeyEvent:ke1", "Inhibition, OATP1C1"),
            ("KeyEvent:ke2", "Decreased cell viability"),
        ):
            event = Entity(
                entity_id=eid, type="KeyEvent", _provenance=EntityProvenance(created_by="lookup")
            )
            event.set_fields_from_dict({"name": name}, source="lookup")
            state.add_entity(event)
        return state

    def test_two_events_link_in_one_call(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(
            state, "assay_1", ["Inhibition, OATP1C1", "Decreased cell viability"]
        )
        assert result["ok"] is True
        assert result["key_event_ids"] == ["KeyEvent:ke1", "KeyEvent:ke2"]

    def test_a_second_answer_adds_rather_than_replaces(self):
        """Naming one event later is not a claim the earlier one was wrong."""
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        result = link_assay_to_key_event(state, "assay_1", "Decreased cell viability")
        assert result["key_event_ids"] == ["KeyEvent:ke1", "KeyEvent:ke2"]

    def test_the_same_event_twice_is_recorded_once(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        result = link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        assert result["key_event_ids"] == ["KeyEvent:ke1"]

    def test_a_good_name_survives_a_bad_one(self):
        """Refusing everything over one typo throws away correct science."""
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(
            state, "assay_1", ["Inhibition, OATP1C1", "Not a real event"]
        )
        assert result["ok"] is True
        assert result["key_event_ids"] == ["KeyEvent:ke1"]
        assert [u["name"] for u in result["unmatched"]] == ["Not a real event"]

    def test_no_name_matching_writes_nothing(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(state, "assay_1", ["Nope", "Also nope"])
        assert result["ok"] is False
        assay = state.get_entity("assay_1")
        assert assay is not None
        assert assay.fields.get("keyEvent") is None

    def test_the_single_name_form_is_unchanged(self):
        from builder.tools.composites import link_assay_to_key_event

        state = self._crate()
        result = link_assay_to_key_event(state, "assay_1", "Inhibition, OATP1C1")
        assert result["key_event_id"] == "KeyEvent:ke1"
        assert result["matched_name"] == "Inhibition, OATP1C1"


class TestSeveralNamesInOneAnswer:
    """A Key Event name contains commas, so the comma cannot be the separator."""

    def test_a_name_with_commas_stays_whole(self):
        from builder.agents.pipeline.guidance import _split_key_event_answer

        name = "Inhibition, organic anion-transporting polypeptide 1C1 (OATP1C1)"
        assert _split_key_event_answer(name) == [name]

    def test_a_semicolon_separates(self):
        from builder.agents.pipeline.guidance import _split_key_event_answer

        assert _split_key_event_answer("Inhibition, OATP1C1; Decreased viability") == [
            "Inhibition, OATP1C1",
            "Decreased viability",
        ]

    def test_a_spelled_out_and_separates(self):
        from builder.agents.pipeline.guidance import _split_key_event_answer

        assert _split_key_event_answer("Inhibition, OATP1C1 and Decreased viability") == [
            "Inhibition, OATP1C1",
            "Decreased viability",
        ]

    def test_an_empty_answer_is_not_split_into_nothing(self):
        from builder.agents.pipeline.guidance import _split_key_event_answer

        assert _split_key_event_answer("   ") == [""]
