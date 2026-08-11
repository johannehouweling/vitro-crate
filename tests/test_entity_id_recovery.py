"""A wrong entity id should cost one turn, not a hunt.

Session 20260811_133825, traced end to end:

* `draft_process_chain` returns its minted `process_ids`, so the model HAS them;
* it later passes `proc_cell_culture_for_sk_n_as_cell_culture_for_thyroid_receptor_transactivation`
  where `proc_sk_n_as_cell_culture_for_thyroid_receptor_transactivation` exists —
  the id rebuilt from the naming pattern rather than copied;
* `set_fields` answers "Entity not found" and nothing else;
* so it calls `list_entities(LabProcess)` to find the real id;
* the repeat guard suppresses that and points at the compact state summary,
  which carries counts and eight recent ids — not the list it needs;
* four bounces, then the idle ladder ends the turn.

Both ends of that are fixed here: the error names the near misses, and the
suppressed read hands back the ids it was asked for.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from builder.tools.drafters import draft_assay, draft_investigation
from builder.tools.management import entity_not_found_message, set_fields


@pytest.fixture
def state():
    st = CrateState()
    draft_investigation(st, {"name": "S-VHPS22", "description": "D"})
    for name in (
        "SK N AS cell culture for thyroid receptor transactivation",
        "Analysis of KLF9 expression",
        "Radioactive T3T4 transporter exposure",
    ):
        draft_assay(st, "study_x", {"name": name})
    return st


class TestTheErrorNamesTheNearMiss:
    def test_the_observed_id_gets_its_real_one_suggested(self, state):
        real = next(e.entity_id for e in state.list_entities() if "sk_n_as" in e.entity_id)
        msg = entity_not_found_message(
            state, "assay_cell_culture_for_sk_n_as_cell_culture_for_thyroid_receptor"
        )
        assert "Entity not found" in msg
        assert real in msg
        assert "Did you mean" in msg

    def test_it_says_where_ids_come_from(self, state):
        """The behaviour to correct is rebuilding the id, not mistyping it."""
        real = next(e.entity_id for e in state.list_entities() if "klf9" in e.entity_id)
        msg = entity_not_found_message(state, real + "_readout")
        assert "minted by the drafting tools" in msg

    def test_nothing_similar_still_names_real_ids(self, state):
        msg = entity_not_found_message(state, "completely_unrelated_xyzzy")
        assert "Existing ids include" in msg
        assert "Did you mean" not in msg

    def test_an_empty_crate_says_so(self):
        msg = entity_not_found_message(CrateState(), "proc_anything")
        assert "no entities yet" in msg

    def test_it_never_resolves_silently(self, state):
        """Suggesting is safe; resolving would edit a DIFFERENT entity.

        `resolve_entity_id` is exact on purpose, and this must not change that.
        """
        real = next(e.entity_id for e in state.list_entities() if "klf9" in e.entity_id)
        with pytest.raises(ValueError) as exc:
            set_fields(state, real + "_typo", {"description": "x"})
        assert real in str(exc.value)
        # The near-miss entity is untouched — nothing was written to it.
        assert state.get_entity(real).fields.get("description") != "x"

    def test_the_message_survives_a_broken_state(self):
        class Exploding(CrateState):
            def list_entities(self, *a, **k):
                raise RuntimeError("boom")

        msg = entity_not_found_message(Exploding(), "proc_x")
        assert msg == "Entity not found: proc_x"


class TestTheSuppressedReadAnswersTheQuestion:
    def _corrective(self, engine, entity_type):
        from builder.agents.react.agent_loop import _suppressed_query_answer

        return _suppressed_query_answer(engine, "list_entities", {"entity_type": entity_type})

    def test_it_lists_the_ids_of_the_type_asked_for(self):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        draft_investigation(engine.state, {"name": "I", "description": "D"})
        draft_assay(engine.state, "study_x", {"name": "Thyroid hormone uptake assay"})
        out = self._corrective(engine, "Assay")
        assert "assay_thyroid_hormone_uptake_assay" in out
        assert "Copy one of these verbatim" in out

    def test_an_empty_type_says_to_draft_one(self):
        from builder.engine import AgentEngine

        out = self._corrective(AgentEngine(), "LabProcess")
        assert "no LabProcess entities" in out

    def test_an_untyped_query_adds_nothing(self):
        """`list_entities()` with no type is already answered by the summary."""
        from builder.agents.react.agent_loop import _suppressed_query_answer
        from builder.engine import AgentEngine

        assert _suppressed_query_answer(AgentEngine(), "list_entities", {}) == ""

    def test_other_state_queries_add_nothing(self):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        draft_investigation(engine.state, {"name": "I", "description": "D"})
        from builder.agents.react.agent_loop import _suppressed_query_answer

        assert _suppressed_query_answer(engine, "get_status", {}) == ""
