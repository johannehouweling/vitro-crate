"""The Key Event question can actually be answered.

An assay's AOP annotation was asked every round and could never be committed.
Two things were wrong, and either alone was enough to make the gap permanent:

* The ISA-Tox shape expresses the annotation as ``schema:mentions`` — the same
  predicate that carries chemicals, organism and anatomy — so the gap arrived
  with ``property="mentions"``, missed the Key Event field set, and went down
  the generic path whose reference guard rejects prose. No reply the user could
  type would commit.
* The question never listed the Key Events. The crate fetches an AOP's whole
  subgraph, so they were sitting in state the whole time, but the reader was
  asked to name one from memory — and a name that is not in the crate cannot
  resolve to an entity, so a plausible answer would be refused anyway.

What is NOT fixed here, deliberately: which Key Event an assay measures. That is
a scientific judgement, and the crate offers its own Key Events as options
rather than choosing among them. A rule that picked one would be right for the
crate it was written against and wrong for the next.
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
        assert "Key Events in this crate" not in prompt

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
