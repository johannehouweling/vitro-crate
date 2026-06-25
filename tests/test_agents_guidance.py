"""Tests for builder/agents/guidance.py — the guidance agent (#179, task 2b-G).

``run_guidance(engine, human)`` is the **deterministic, code-driven** HITL gap-
resolution loop over the gap engine's prioritized :class:`GapReport`. CODE owns
control flow (NOT a ReAct/LLM-orchestrated agent); the LLM is used only to draft
suggested values; the user is the authority (D5: confirm-before-commit). The loop:

1. ``assess_gaps`` -> a prioritized report.
2. terminate when no MUST gaps remain AND (the user signals done OR there are no
   actionable SHOULD/MAY gaps left).
3. otherwise take the highest-priority actionable gap and resolve by ``fix_hint``
   / ``auto_fixable``: auto-fix (no prompt), draft->confirm->commit (reject ->
   ask-user), or ask-user (prompt -> apply).
4. re-assess after each committed change; never loop forever (``max_rounds`` +
   no-progress guard); process MUST before SHOULD before MAY.

These tests are validation-heavy (the gap engine runs the owlrl SHACL validator),
so they carry the 120s marker like ``tests/test_tools_gap_analysis.py``. They use
a :class:`SimulatedHumanInterface`-style canned double and stub the drafter leaf /
lookups so there is NO network and NO real LLM call.
"""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.gap_analysis import Gap, GapReport, assess_gaps
from builder.tools.hitl import HumanResponse, InputResponse

pytestmark = pytest.mark.timeout(120)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_tools_gap_analysis.py helpers)
# ---------------------------------------------------------------------------


def _entity(entity_id: str, type_: EntityType, **fields) -> Entity:
    e = Entity(
        entity_id=entity_id,
        type=type_,
        fields=dict(fields),
        _provenance=EntityProvenance(created_by="llm"),
    )
    for key in fields:
        e.set_field_status(key, "filled", "llm")
    return e


def _backbone() -> CrateState:
    """A BASE/ISA/TOX-passing Investigation -> Study -> Assay backbone."""
    state = CrateState()
    state.metadata.title = "Gap test crate"
    state.add_entity(
        _entity("inv1", "Investigation", name="Inv", description="d", identifier="INV-1")
    )
    state.add_entity(
        _entity("st1", "Study", name="St", description="d", investigation_id="inv1")
    )
    state.add_entity(_entity("as1", "Assay", name="As", description="d", study_id="st1"))
    return state


def _endpoint_readout_missing_result(n_files: int = 1) -> CrateState:
    """Backbone + an EndpointReadout with no result + ``n_files`` File entities.

    The TOX MUST issue 'EndpointReadout MUST have a result' is auto-fixable iff
    exactly one un-wired File exists (the deterministic-repair rule's predicate).
    """
    state = _backbone()
    state.add_entity(
        _entity(
            "er1",
            "LabProcess",
            process_type="EndpointReadout",
            name="Readout",
            assay_id="as1",
        )
    )
    for i in range(n_files):
        state.add_entity(
            _entity(f"f{i}", "File", name=f"raw{i}.csv", dest_path=f"data/raw{i}.csv")
        )
    return state


# ---------------------------------------------------------------------------
# Test doubles for the HumanInterface (canned answers, NO network/UI)
# ---------------------------------------------------------------------------


class ScriptedHuman:
    """A HumanInterface double driven by canned, recorded answers.

    * ``present`` pops the next decision from ``present_answers`` (default
      "approved") and records the call in ``presented``.
    * ``request_input`` pops the next value from ``input_answers`` (default a
      skip) and records the call in ``inputs``.
    * ``done_after`` optionally makes ``request_input`` report "done" — see the
      ``DONE`` sentinel — to let a test signal the user wants to stop.
    """

    DONE = object()

    def __init__(
        self,
        present_answers: list[HumanResponse] | None = None,
        input_answers: list[InputResponse] | None = None,
    ) -> None:
        self._present = list(present_answers or [])
        self._input = list(input_answers or [])
        self.presented: list[tuple[str, list[str] | None, str | None]] = []
        self.inputs: list[tuple[str, str]] = []

    def present(
        self,
        context: str,
        options: list[str] | None = None,
        purpose: str | None = None,
    ) -> HumanResponse:
        self.presented.append((context, options, purpose))
        if self._present:
            return self._present.pop(0)
        return {"action": "approved", "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        self.inputs.append((prompt, field_type))
        if self._input:
            return self._input.pop(0)
        return {"value": None, "skipped": True}


def _approved() -> HumanResponse:
    return {"action": "approved", "comments": None, "edits": None}


def _rejected() -> HumanResponse:
    return {"action": "rejected", "comments": None, "edits": None}


def _get(engine: AgentEngine, entity_id: str) -> Entity:
    """Fetch a state entity, asserting it exists (narrows ``Entity | None``)."""
    entity = engine.state.get_entity(entity_id)
    assert entity is not None, f"entity not found: {entity_id}"
    return entity


def _value(v: str) -> InputResponse:
    return {"value": v, "skipped": False}


def _skip() -> InputResponse:
    return {"value": None, "skipped": True}


# ---------------------------------------------------------------------------
# Return shape & termination
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_returns_summary_dict(self):
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        human = ScriptedHuman()
        summary = run_guidance(engine, human, max_rounds=3)

        assert isinstance(summary, dict)
        assert set(summary) >= {"resolved", "asked", "remaining_gaps", "conformance"}
        assert isinstance(summary["resolved"], list)
        assert isinstance(summary["asked"], list)
        assert isinstance(summary["remaining_gaps"], dict)
        assert set(summary["remaining_gaps"]) == {"must_open", "should_open", "may_open"}

    def test_terminates_on_clean_backbone_with_user_done(self):
        """No MUST gaps + user declines every SHOULD/MAY -> terminate, no crash."""
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        # The user skips/declines every ask-user prompt -> the loop should stop
        # once no actionable progress can be made.
        human = ScriptedHuman()
        summary = run_guidance(engine, human, max_rounds=5)

        assert summary["remaining_gaps"]["must_open"] == 0


# ---------------------------------------------------------------------------
# (a) an auto_fixable MUST gap is fixed WITHOUT prompting
# ---------------------------------------------------------------------------


class TestAutoFixable:
    def test_auto_fixable_must_fixed_without_prompt(self):
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_endpoint_readout_missing_result(n_files=1))
        # Sanity: the missing-result MUST is auto_fixable before the loop runs.
        before = assess_gaps(engine.state)
        result_gaps = [
            g
            for g in before.gaps
            if g.tier == "MUST" and (g.property or "").endswith("result")
        ]
        assert result_gaps and all(g.auto_fixable for g in result_gaps)

        # max_rounds=1 isolates the highest-priority gap (the auto-fixable MUST,
        # sorted first) so we can assert it was resolved with zero prompts —
        # before the loop ever moves on to lower-tier SHOULD/MAY gaps.
        human = ScriptedHuman()
        summary = run_guidance(engine, human, max_rounds=1)

        # The auto-fix needed NO human interaction at all.
        assert human.presented == []
        assert human.inputs == []
        # The MUST gap was resolved deterministically.
        assert summary["remaining_gaps"]["must_open"] == 0
        assert any(r.get("fix_hint") == "fix_required_issues" for r in summary["resolved"])
        # Re-assessment reflects it: the EndpointReadout now has a result wired.
        after = assess_gaps(engine.state)
        assert not [
            g
            for g in after.gaps
            if g.tier == "MUST" and (g.property or "").endswith("result")
        ]


# ---------------------------------------------------------------------------
# (b) an ask-user gap prompts and the answer is applied
# ---------------------------------------------------------------------------


class TestAskUser:
    def test_ask_user_gap_prompts_and_applies_answer(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        # A single ask-user MUST gap on a real state entity (the Study), so the
        # answer is applied via set_fields and re-assessment can see it.
        state = _backbone()
        engine = AgentEngine(state=state)

        ask_gap = Gap(
            tier="MUST",
            source="shacl",
            entity_id="st1",
            entity_type="Study",
            property="https://schema.org/description",
            message="Study MUST have a description.",
            suggestion="A free-text study description.",
            fix_hint="ask-user",
            auto_fixable=False,
        )

        reports = iter(
            [
                GapReport(gaps=[ask_gap], counts={"must_open": 1, "should_open": 0, "may_open": 0}),
                GapReport(gaps=[], counts={"must_open": 0, "should_open": 0, "may_open": 0}),
            ]
        )
        monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))

        human = ScriptedHuman(input_answers=[_value("A new description from the user.")])
        summary = run_guidance(engine, human, max_rounds=5)

        # The user was prompted and the answer was applied to the Study entity.
        assert human.inputs, "an ask-user gap must prompt the human"
        applied = engine.state.get_entity("st1")
        assert applied is not None
        assert applied.fields.get("description") == "A new description from the user."
        assert any(a.get("entity_id") == "st1" for a in summary["asked"])

    def test_skipped_ask_user_is_not_applied(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        state = _backbone()
        engine = AgentEngine(state=state)
        original = _get(engine, "st1").fields.get("description")

        ask_gap = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="description",
            message="Capture the study description.",
            suggestion="desc hint",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        # The same single gap every round: skipping it makes no progress, so the
        # no-progress guard must terminate the loop.
        monkeypatch.setattr(
            guidance,
            "assess_gaps",
            lambda _state: GapReport(
                gaps=[ask_gap], counts={"must_open": 0, "should_open": 1, "may_open": 0}
            ),
        )

        human = ScriptedHuman(input_answers=[_skip()])
        summary = run_guidance(engine, human, max_rounds=5)

        # Skipped -> value unchanged; loop still terminated (no-progress guard).
        assert _get(engine, "st1").fields.get("description") == original
        assert summary["remaining_gaps"]["should_open"] == 1


# ---------------------------------------------------------------------------
# (c) a draftable gap drafts -> confirms -> commits; a rejected draft falls
#     back to ask-user
# ---------------------------------------------------------------------------


class TestDraftable:
    def test_draftable_gap_drafts_confirms_commits(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        state = _backbone()
        engine = AgentEngine(state=state)

        draft_gap = Gap(
            tier="MAY",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="description",
            message="A drafted study description.",
            suggestion="some MIT param",
            fix_hint="draft",
            auto_fixable=False,
        )
        reports = iter(
            [
                GapReport(
                    gaps=[draft_gap],
                    counts={"must_open": 0, "should_open": 0, "may_open": 1},
                ),
                GapReport(gaps=[], counts={"must_open": 0, "should_open": 0, "may_open": 0}),
            ]
        )
        monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))

        # Stub the drafter leaf — NO real LLM.
        called: dict[str, object] = {}

        def _fake_draft(entity_type, context, *, model=None):
            called["entity_type"] = entity_type
            return {"description": "A drafted value."}

        monkeypatch.setattr(guidance, "draft_entity_fields", _fake_draft)

        # The user confirms the drafted value.
        human = ScriptedHuman(present_answers=[_approved()])
        summary = run_guidance(engine, human, max_rounds=5)

        # The drafter was consulted and the user CONFIRMED before committing (D5).
        assert called.get("entity_type") == "Study"
        assert human.presented, "a draftable gap must show the user the draft first"
        # The confirmed draft was committed to state.
        assert _get(engine, "st1").fields.get("description") == "A drafted value."
        assert any(r.get("fix_hint") == "draft" for r in summary["resolved"])

    def test_rejected_draft_falls_back_to_ask_user(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        state = _backbone()
        engine = AgentEngine(state=state)

        draft_gap = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="description",
            message="Draftable description.",
            suggestion="hint",
            fix_hint="draft",
            auto_fixable=False,
        )
        reports = iter(
            [
                GapReport(
                    gaps=[draft_gap],
                    counts={"must_open": 0, "should_open": 1, "may_open": 0},
                ),
                GapReport(gaps=[], counts={"must_open": 0, "should_open": 0, "may_open": 0}),
            ]
        )
        monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))

        def _fake_draft(entity_type, context, *, model=None):
            return {"description": "A drafted value the user rejects."}

        monkeypatch.setattr(guidance, "draft_entity_fields", _fake_draft)

        # User REJECTS the draft, then supplies their own value via request_input.
        human = ScriptedHuman(
            present_answers=[_rejected()],
            input_answers=[_value("The user's own description.")],
        )
        run_guidance(engine, human, max_rounds=5)

        # Rejection fell through to ask-user, and the user's value was committed —
        # NOT the rejected draft (D5: never commit unconfirmed content).
        assert human.presented, "the draft must have been shown"
        assert human.inputs, "rejection must fall back to an ask-user prompt"
        assert (
            _get(engine, "st1").fields.get("description")
            == "The user's own description."
        )


# ---------------------------------------------------------------------------
# (d) termination: max_rounds + no-progress guards; MUST before SHOULD/MAY
# ---------------------------------------------------------------------------


class TestTermination:
    def test_max_rounds_bounds_the_loop(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())

        # A gap that is "resolved" (user supplies a value) but never disappears
        # from the report — without a round bound this loops forever.
        sticky = Gap(
            tier="MUST",
            source="shacl",
            entity_id="st1",
            entity_type="Study",
            property="https://schema.org/name",
            message="Study MUST have a name.",
            suggestion=None,
            fix_hint="ask-user",
            auto_fixable=False,
        )
        calls = {"n": 0}

        def _always_one(_state):
            calls["n"] += 1
            return GapReport(
                gaps=[sticky], counts={"must_open": 1, "should_open": 0, "may_open": 0}
            )

        monkeypatch.setattr(guidance, "assess_gaps", _always_one)

        # Endless supply of answers so only max_rounds can stop it.
        human = ScriptedHuman(input_answers=[_value(f"v{i}") for i in range(100)])
        summary = run_guidance(engine, human, max_rounds=4)

        # The loop ran a BOUNDED number of times.
        assert calls["n"] <= 4 + 2, calls["n"]
        assert isinstance(summary, dict)

    def test_no_progress_guard_stops_when_whole_round_unresolvable(self, monkeypatch):
        """The no-progress guard fires only when the WHOLE round is un-progressable.

        Two actionable ask-user gaps on real entities; the user skips *both*. No
        gap in the round can be progressed, so the loop must exhaust the report
        (skipping each gap once) and stop — without burning all 50 rounds and
        without re-asking the same gap forever.
        """
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())

        gap_a = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="name",
            message="Study SHOULD have a richer name.",
            suggestion="hint a",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        gap_b = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="as1",
            entity_type="Assay",
            property="description",
            message="Assay SHOULD have a description.",
            suggestion="hint b",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        calls = {"n": 0}

        def _always(_state):
            calls["n"] += 1
            return GapReport(
                gaps=[gap_a, gap_b],
                counts={"must_open": 0, "should_open": 2, "may_open": 0},
            )

        monkeypatch.setattr(guidance, "assess_gaps", _always)

        human = ScriptedHuman()  # always skips
        run_guidance(engine, human, max_rounds=50)

        # Stopped because the whole round was un-progressable, NOT by exhausting
        # 50 rounds. Both gaps were each offered once before the loop gave up.
        assert calls["n"] < 50, calls["n"]
        assert len(human.inputs) == 2, human.inputs

    def test_processes_must_before_should_before_may(self, monkeypatch):
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())

        must_gap = Gap(
            tier="MUST",
            source="shacl",
            entity_id="st1",
            entity_type="Study",
            property="https://schema.org/name",
            message="MUST gap.",
            suggestion=None,
            fix_hint="ask-user",
            auto_fixable=False,
        )
        should_gap = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="description",
            message="SHOULD gap.",
            suggestion=None,
            fix_hint="ask-user",
            auto_fixable=False,
        )
        # Round 1: both present, sorted MUST-first. Round 2: only SHOULD left.
        # Round 3: nothing.
        reports = iter(
            [
                GapReport(
                    gaps=[must_gap, should_gap],
                    counts={"must_open": 1, "should_open": 1, "may_open": 0},
                ),
                GapReport(
                    gaps=[should_gap], counts={"must_open": 0, "should_open": 1, "may_open": 0}
                ),
                GapReport(gaps=[], counts={"must_open": 0, "should_open": 0, "may_open": 0}),
            ]
        )
        monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))

        human = ScriptedHuman(
            input_answers=[_value("name value"), _value("desc value")]
        )
        summary = run_guidance(engine, human, max_rounds=5)

        # The MUST gap (Study name) was asked & applied BEFORE the SHOULD gap.
        asked_props = [a.get("property") for a in summary["asked"]]
        assert asked_props[0].endswith("name")
        # Both were applied to the entity.
        st = _get(engine, "st1")
        assert st.fields.get("name") == "name value"
        assert st.fields.get("description") == "desc value"


# ---------------------------------------------------------------------------
# (f) an uncommittable gap is SKIPPED, not fatal — the loop keeps going
# ---------------------------------------------------------------------------


class TestSkipUncommittableGaps:
    def test_uncommittable_gap_does_not_abort_remaining_gaps(self, monkeypatch):
        """A report-only FAIR gap sorted ahead of a committable SHOULD gap must be
        SKIPPED, and the committable gap behind it must still be resolved.

        This is the regression for the production bug: one un-progressable gap
        used to ``break`` the WHOLE loop, abandoning every remaining gap.
        """
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())

        # A FAIR report-only gap: entity_id None, property is an indicator token
        # that maps to no settable field. The loop can never commit it.
        fair_gap = Gap(
            tier="SHOULD",
            source="fair",
            entity_id=None,
            entity_type=None,
            property="RDA-F1-02M",
            message="FAIR indicator RDA-F1-02M not met: globally unique id.",
            suggestion="Dimension Findable (essential)",
            fix_hint="report-only",
            auto_fixable=False,
        )
        # A committable SHOULD gap on a real entity behind it.
        committable = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="description",
            message="Study SHOULD have a description.",
            suggestion="A free-text study description.",
            fix_hint="ask-user",
            auto_fixable=False,
        )

        reports = iter(
            [
                # Round 1: report-only gap is first (it would be drawn first), the
                # committable gap is behind it.
                GapReport(
                    gaps=[fair_gap, committable],
                    counts={"must_open": 0, "should_open": 2, "may_open": 0},
                ),
                # After the committable gap is resolved only the FAIR gap remains.
                GapReport(
                    gaps=[fair_gap],
                    counts={"must_open": 0, "should_open": 1, "may_open": 0},
                ),
            ]
        )
        monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))

        human = ScriptedHuman(input_answers=[_value("A user-supplied description.")])
        summary = run_guidance(engine, human, max_rounds=5)

        # The committable gap WAS resolved despite the FAIR gap in front of it.
        assert _get(engine, "st1").fields.get("description") == (
            "A user-supplied description."
        )
        assert any(r.get("entity_id") == "st1" for r in summary["resolved"])
        # The report-only FAIR gap was never offered to the user (no ask-user turn).
        assert not any("RDA-F1-02M" in prompt for prompt, _ in human.inputs), (
            human.inputs
        )

    def test_report_only_gap_is_never_actionable(self):
        """``_next_actionable_gap`` must never return a report-only gap."""
        from builder.agents.guidance import _next_actionable_gap

        report_only = Gap(
            tier="SHOULD",
            source="fair",
            entity_id=None,
            entity_type=None,
            property="RDA-F1-02M",
            message="FAIR indicator not met.",
            suggestion=None,
            fix_hint="report-only",
            auto_fixable=False,
        )
        report = GapReport(
            gaps=[report_only],
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )
        assert _next_actionable_gap(report, skipped=set()) is None

    def test_next_actionable_gap_respects_skip_set(self):
        """A gap already in the skip-set is passed over for the next actionable one."""
        from builder.agents.guidance import _next_actionable_gap

        first = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="st1",
            entity_type="Study",
            property="name",
            message="first",
            suggestion=None,
            fix_hint="ask-user",
            auto_fixable=False,
        )
        second = Gap(
            tier="SHOULD",
            source="mit",
            entity_id="as1",
            entity_type="Assay",
            property="description",
            message="second",
            suggestion=None,
            fix_hint="ask-user",
            auto_fixable=False,
        )
        report = GapReport(
            gaps=[first, second],
            counts={"must_open": 0, "should_open": 2, "may_open": 0},
        )
        assert _next_actionable_gap(report, skipped=set()) is first
        assert _next_actionable_gap(report, skipped={0}) is second
        assert _next_actionable_gap(report, skipped={0, 1}) is None


# ---------------------------------------------------------------------------
# (g) ask-user prompt is human-readable (field label + suggestion + format)
# ---------------------------------------------------------------------------


class TestAskUserPrompt:
    def test_ask_user_prompt_is_human_readable(self):
        """The prompt must name WHAT field and WHY, not echo the raw gap message."""
        from builder.agents.guidance import _ask_user

        engine = AgentEngine(state=_backbone())
        gap = Gap(
            tier="SHOULD",
            source="shacl",
            entity_id="st1",
            entity_type="Study",
            property="https://schema.org/description",
            message="Study MUST have a description.",
            suggestion="A free-text study description.",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        human = ScriptedHuman(input_answers=[_value("ok")])
        _ask_user(engine, human, gap)

        assert human.inputs, "ask-user must prompt the human"
        prompt = human.inputs[0][0]
        # Names the field label (the property's local name), not just the raw IRI.
        assert "description" in prompt.lower()
        # Carries the suggestion as guidance.
        assert "A free-text study description." in prompt
        # Tells the user what entity it applies to.
        assert "Study" in prompt


# ---------------------------------------------------------------------------
# (e) re-assessment reflects resolved gaps (integration, no mocks)
# ---------------------------------------------------------------------------


class TestReassessmentIntegration:
    def test_real_auto_fix_clears_must_on_reassess(self):
        """End-to-end over the REAL gap engine: an auto-fixable MUST clears."""
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_endpoint_readout_missing_result(n_files=1))
        human = ScriptedHuman()  # never consulted for an auto-fix
        summary = run_guidance(engine, human, max_rounds=6)

        # The real re-assessment after the deterministic repair has no MUST left.
        assert summary["remaining_gaps"]["must_open"] == 0
        assert summary["conformance"].get("tox") is True
