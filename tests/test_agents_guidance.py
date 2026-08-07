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
from builder.tools.gap_analysis import Gap, GapReport, Tier, assess_gaps
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
            # As in tests/test_tools_repair.py: the missing RESULT is the gap
            # under test, so give the readout a real parameter rather than let
            # the separate additionalProperty MUST fire alongside it.
            detection_instrument="Plate reader",
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
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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

        def _fake_draft(entity_type, context, *, overrides=None, usage_sink=None):
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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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

        def _fake_draft(entity_type, context, *, overrides=None, usage_sink=None):
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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

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
        from builder.agents.pipeline.guidance import _next_actionable_gap

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
        from builder.agents.pipeline.guidance import _next_actionable_gap

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
        from builder.agents.pipeline.guidance import _ask_user

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
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_endpoint_readout_missing_result(n_files=1))
        human = ScriptedHuman()  # never consulted for an auto-fix
        summary = run_guidance(engine, human, max_rounds=6)

        # The real re-assessment after the deterministic repair has no MUST left.
        assert summary["remaining_gaps"]["must_open"] == 0
        assert summary["conformance"].get("tox") is True


# ---------------------------------------------------------------------------
# (LLM-mediated ask-user, Issue #244) — the "small guidance agent"
#
# When a provider is configured, the per-gap ask-user step is a bounded LLM
# exchange: PHRASE the gap into one human question, INTERPRET the free-text
# reply into a structured decision (commit / skip / clarify / from_file), and
# COMMIT only a clean structured value. With no provider it stays deterministic.
# The phrase/interpret leaves are STUBBED (monkeypatch) — no real LLM.
# ---------------------------------------------------------------------------


def _single_ask_gap_report(monkeypatch, gap: Gap, *, counts: dict[str, int]):
    """Patch ``assess_gaps`` to return ``gap`` once, then an empty report.

    Mirrors the existing two-report ``iter`` pattern: the gap is offered once,
    then (whether or not it was committed) the next round sees a clean report so
    the loop terminates promptly.
    """
    from builder.agents.pipeline import guidance

    reports = iter(
        [
            GapReport(gaps=[gap], counts=counts),
            GapReport(gaps=[], counts={"must_open": 0, "should_open": 0, "may_open": 0}),
        ]
    )
    monkeypatch.setattr(guidance, "assess_gaps", lambda _state: next(reports))


def _study_desc_gap(tier: Tier = "MUST") -> Gap:
    return Gap(
        tier=tier,
        source="shacl",
        entity_id="st1",
        entity_type="Study",
        property="https://schema.org/description",
        message="Study MUST have a description.",
        suggestion="A free-text study description.",
        fix_hint="ask-user",
        auto_fixable=False,
    )


class TestLLMMediatedAskUser:
    def _enable_provider(self, monkeypatch):
        """Make ``get_provider()`` (as seen by guidance) report a provider."""
        from builder.agents.pipeline import guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

    def test_idk_reply_is_skipped_not_stored(self, monkeypatch):
        """The headline regression: a free-text 'I don't know' reply must SKIP —
        it must NEVER be stored verbatim as the field value (#244)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        # The phrase leaf produces a clean question; the interpret leaf reads the
        # musing as a SKIP (action="skip"), carrying no value.
        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What does this study examine?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _reply, _ctx, **_kw: {"action": "skip"},
        )

        human = ScriptedHuman(
            input_answers=[_value("No idea which file you are talking about")]
        )
        run_guidance(engine, human, max_rounds=5)

        # The musing was NOT committed — the description is unchanged.
        applied = _get(engine, "st1")
        assert applied.fields.get("description") == original
        assert applied.fields.get("description") != (
            "No idea which file you are talking about"
        )

    def test_natural_language_reply_is_interpreted_to_clean_value(self, monkeypatch):
        """A NL reply carrying a value is interpreted to a clean committed value,
        not stored verbatim (#244)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What does this study examine?"
        )

        captured: dict[str, object] = {}

        def _interpret(question, reply, ctx, **_kw):
            captured["reply"] = reply
            return {
                "action": "commit",
                "value": "A dose-response study of acetaminophen hepatotoxicity.",
            }

        monkeypatch.setattr(guidance, "interpret_gap_reply", _interpret)

        verbose = "well it's about how acetaminophen damages the liver, dose-response"
        human = ScriptedHuman(input_answers=[_value(verbose)])
        run_guidance(engine, human, max_rounds=5)

        applied = _get(engine, "st1")
        # The CLEAN interpreted value landed — not the user's raw musing.
        assert applied.fields.get("description") == (
            "A dose-response study of acetaminophen hepatotoxicity."
        )
        assert applied.fields.get("description") != verbose
        assert captured["reply"] == verbose

    def test_clarify_asks_at_most_one_follow_up_then_skips(self, monkeypatch):
        """A clarify decision asks ONE follow-up; if still unresolved, it skips —
        the clarify path can never loop (#244)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What does this study examine?"
        )

        # Every interpretation says "clarify" — without a cap this loops forever.
        interpret_calls = {"n": 0}

        def _always_clarify(question, reply, ctx, **_kw):
            interpret_calls["n"] += 1
            return {"action": "clarify", "question": "Could you be more specific?"}

        monkeypatch.setattr(guidance, "interpret_gap_reply", _always_clarify)

        # The user keeps replying with vague answers.
        human = ScriptedHuman(
            input_answers=[_value("the assay"), _value("the other one"), _value("dunno")]
        )
        run_guidance(engine, human, max_rounds=5)

        # At most ONE follow-up was asked (initial reply + one clarify = 2 inputs),
        # and nothing was committed (a clarify never becomes a value).
        assert len(human.inputs) <= 2, human.inputs
        assert interpret_calls["n"] <= 2, interpret_calls["n"]
        assert _get(engine, "st1").fields.get("description") == original

    def test_clarify_then_commit_lands_the_clarified_value(self, monkeypatch):
        """One clarify follow-up that yields a value commits the clarified value."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What does this study examine?"
        )

        decisions = iter(
            [
                {"action": "clarify", "question": "Which endpoint?"},
                {"action": "commit", "value": "A viability assay study."},
            ]
        )
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c, **_kw: next(decisions)
        )

        human = ScriptedHuman(
            input_answers=[_value("an assay"), _value("cell viability")]
        )
        run_guidance(engine, human, max_rounds=5)

        assert _get(engine, "st1").fields.get("description") == (
            "A viability assay study."
        )

    def test_from_file_does_not_store_prose(self, monkeypatch):
        """A 'it's in a file' reply must NOT store the user's prose; it records a
        filename hint and does not commit a value (#244)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What does this study examine?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "from_file", "filename": "README.txt"},
        )

        human = ScriptedHuman(
            input_answers=[_value("it's all written up in README.txt")]
        )
        summary = run_guidance(engine, human, max_rounds=5)

        # The prose was NOT stored.
        assert _get(engine, "st1").fields.get("description") == original
        # The gap was surfaced (asked), not committed.
        assert any(a.get("entity_id") == "st1" for a in summary["asked"])
        assert not any(r.get("entity_id") == "st1" for r in summary["resolved"])

    def test_phrase_leaf_question_is_shown_to_the_user(self, monkeypatch):
        """The PHRASED question (not the raw SHACL message) is what the user sees."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        phrased = "In one sentence, what does this study set out to find?"
        monkeypatch.setattr(guidance, "phrase_gap_question", lambda _ctx, **_kw: phrased)
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c, **_kw: {"action": "skip"}
        )

        human = ScriptedHuman(input_answers=[_value("hmm")])
        run_guidance(engine, human, max_rounds=5)

        assert human.inputs, "the user must be prompted"
        prompt = human.inputs[0][0]
        assert phrased in prompt
        # The raw failed-check SHACL message is NOT shown verbatim as the question.
        assert "Study MUST have a description." not in prompt


class TestNoProviderDeterministicFallback:
    """With NO provider configured the guidance loop preserves today's
    deterministic ask-and-set behavior (#244)."""

    def test_no_provider_commits_nonempty_reply_verbatim(self, monkeypatch):
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        # No provider -> deterministic path; the leaves must NEVER be called.
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        def _boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("no-provider path must not call the LLM leaves")

        monkeypatch.setattr(guidance, "phrase_gap_question", _boom)
        monkeypatch.setattr(guidance, "interpret_gap_reply", _boom)

        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        human = ScriptedHuman(input_answers=[_value("A user-typed description.")])
        run_guidance(engine, human, max_rounds=5)

        # Deterministic: a non-empty reply is committed as-is (today's behavior).
        assert _get(engine, "st1").fields.get("description") == (
            "A user-typed description."
        )

    def test_no_provider_skip_does_not_commit(self, monkeypatch):
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap("SHOULD"),
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )

        human = ScriptedHuman(input_answers=[_skip()])
        run_guidance(engine, human, max_rounds=5)

        # Deterministic: an empty/skipped reply commits nothing.
        assert _get(engine, "st1").fields.get("description") == original


class TestReportOnlyNeverAskedWithLLM:
    """Report-only gaps stay report-only even with a provider configured:
    they are never phrased, interpreted, or offered to the user (#244)."""

    def test_report_only_gap_never_phrased_or_asked(self, monkeypatch):
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

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
        monkeypatch.setattr(
            guidance,
            "assess_gaps",
            lambda _state: GapReport(
                gaps=[fair_gap],
                counts={"must_open": 0, "should_open": 1, "may_open": 0},
            ),
        )

        def _boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("a report-only gap must never reach an LLM leaf")

        monkeypatch.setattr(guidance, "phrase_gap_question", _boom)
        monkeypatch.setattr(guidance, "interpret_gap_reply", _boom)

        human = ScriptedHuman()
        run_guidance(engine, human, max_rounds=5)

        # The report-only gap was never offered to the user.
        assert human.inputs == []
        assert human.presented == []


# ---------------------------------------------------------------------------
# (Issue #257, fix A) Every question NAMES the entity it is about
#
# A gap with a concrete ``entity_id`` resolves to a real state entity; its TYPE,
# NAME, KNOWN fields, and the MISSING field are threaded into the gap context the
# phrase leaf consumes, so the phrased question references the entity BY NAME and
# never a bare "this chemical / this protocol / this cell line".
# ---------------------------------------------------------------------------


def _backbone_with_compound(name: str = "Silychristin A") -> CrateState:
    """Backbone + a MolecularEntity with a known name but no CAS recorded."""
    state = _backbone()
    state.add_entity(_entity("chem1", "MolecularEntity", name=name))
    return state


class TestQuestionNamesEntity:
    def test_gap_context_carries_resolved_entity_name_and_type(self):
        """``_gap_context`` resolves the entity from state and includes its name,
        type, and known fields so the leaf can name it (Issue #257)."""
        from builder.agents.pipeline.guidance import _gap_context

        engine = AgentEngine(state=_backbone_with_compound("Silychristin A"))
        gap = Gap(
            tier="SHOULD",
            source="shacl",
            entity_id="chem1",
            entity_type="MolecularEntity",
            property="cas",
            message="MolecularEntity SHOULD record a CAS number.",
            suggestion="A CAS Registry Number.",
            fix_hint="ask-user",
            auto_fixable=False,
        )

        ctx = _gap_context(engine, gap)

        assert ctx.get("entity_name") == "Silychristin A"
        assert ctx.get("entity_type") == "MolecularEntity"
        # Known fields are surfaced so the leaf can ground the question.
        known = ctx.get("known_fields") or {}
        assert known.get("name") == "Silychristin A"
        # The missing field being asked about is the gap's property.
        assert ctx.get("property") == "cas"

    def test_phrased_question_names_the_entity_not_this_chemical(self, monkeypatch):
        """End-to-end: a gap on the named compound yields a question that contains
        the NAME, never a bare 'this chemical'."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone_with_compound("Silychristin A"))
        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

        cas_gap = Gap(
            tier="SHOULD",
            source="shacl",
            entity_id="chem1",
            entity_type="MolecularEntity",
            property="cas",
            message="MolecularEntity SHOULD record a CAS number.",
            suggestion="A CAS Registry Number like 103-90-2.",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        _single_ask_gap_report(
            monkeypatch,
            cas_gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )

        # A realistic phrase leaf: it echoes the entity name it is handed.
        def _phrase(ctx, **_kw):
            name = ctx.get("entity_name") or "this chemical substance"
            return f"What is the CAS Registry Number for {name}?"

        monkeypatch.setattr(guidance, "phrase_gap_question", _phrase)
        # An identifier gap: the user's prose can never become the value (D5).
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c, **_kw: {"action": "skip"}
        )

        human = ScriptedHuman(input_answers=[_value("dunno")])
        run_guidance(engine, human, max_rounds=5)

        assert human.inputs, "the user must be prompted"
        prompt = human.inputs[0][0]
        assert "Silychristin A" in prompt
        assert "this chemical" not in prompt.lower()

    def test_deterministic_prompt_names_the_entity(self):
        """Even the no-provider deterministic prompt names the entity (Issue #257)."""
        from builder.agents.pipeline.guidance import _ask_user

        engine = AgentEngine(state=_backbone_with_compound("Silychristin A"))
        gap = Gap(
            tier="SHOULD",
            source="shacl",
            entity_id="chem1",
            entity_type="MolecularEntity",
            property="description",
            message="MolecularEntity SHOULD have a description.",
            suggestion="A short free-text description.",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        human = ScriptedHuman(input_answers=[_value("ok")])
        _ask_user(engine, human, gap)

        assert human.inputs
        prompt = human.inputs[0][0]
        assert "Silychristin A" in prompt


# ---------------------------------------------------------------------------
# (Commit 2, #179) MIT gaps (entity_id=None) are GROUNDED in the real instance.
#
# MIT gaps are emitted crate-level with ``entity_id=None``, carrying only
# ``entity_type`` (e.g. "CellLineSample"). ``_resolve_entity_id`` short-circuits
# to None for any falsy entity_id, so the OLD ``_gap_context`` left the phrase
# leaf with a bare TYPE and NO name -> the model invented "HepG2" (the stock
# example). FIX: when ``_resolve_entity_id`` is None but ``entity_type`` is set,
# look the instance(s) up via ``engine.state.list_entities(entity_type)`` and
# thread the real display name into ``entity_name`` / ``known_fields``.
# ---------------------------------------------------------------------------


def _backbone_with_cell_line(name: str = "CHO-K1 OATP1C1") -> CrateState:
    """Backbone + a single CellLineSample with a known instance name."""
    state = _backbone()
    state.add_entity(_entity("cl1", "CellLineSample", name=name))
    return state


def _mit_cell_line_gap() -> Gap:
    """An MIT-style gap: crate-level (entity_id=None), carrying only the TYPE."""
    return Gap(
        tier="SHOULD",
        source="mit",
        entity_id=None,
        entity_type="CellLineSample",
        property="passage",
        message="Record the cell line passage number.",
        suggestion="The passage number at the time of the assay.",
        fix_hint="ask-user",
        auto_fixable=False,
    )


class TestMITGapGroundedInInstanceName:
    def test_gap_context_grounds_entityless_mit_gap_in_the_instance(self):
        """``_gap_context`` for an entity_id=None MIT gap resolves the single
        in-state instance and threads its NAME in (#179, Commit 2)."""
        from builder.agents.pipeline.guidance import _gap_context

        engine = AgentEngine(state=_backbone_with_cell_line("CHO-K1 OATP1C1"))
        ctx = _gap_context(engine, _mit_cell_line_gap())

        assert ctx.get("entity_name") == "CHO-K1 OATP1C1", (
            "an entity_id=None MIT gap must be grounded in the real instance name"
        )
        known = ctx.get("known_fields") or {}
        assert known.get("name") == "CHO-K1 OATP1C1"
        assert ctx.get("entity_type") == "CellLineSample"

    def test_phrased_question_names_the_real_cell_line_never_hepg2(self, monkeypatch):
        """End-to-end: a phrased question for the MIT gap names the REAL cell line
        ("CHO-K1") and never the fabricated stock example "HepG2" (#179)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone_with_cell_line("CHO-K1 OATP1C1"))
        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")
        _single_ask_gap_report(
            monkeypatch,
            _mit_cell_line_gap(),
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )

        # A realistic phrase leaf: it grounds on the name it is handed, else it
        # would invent the stock "HepG2" example (the bug).
        def _phrase(ctx, **_kw):
            name = ctx.get("entity_name") or "HepG2"
            return f"What passage number was {name} at during the assay?"

        monkeypatch.setattr(guidance, "phrase_gap_question", _phrase)
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c, **_kw: {"action": "skip"}
        )

        human = ScriptedHuman(input_answers=[_value("dunno")])
        run_guidance(engine, human, max_rounds=5)

        assert human.inputs, "the user must be prompted"
        prompt = human.inputs[0][0]
        assert "CHO-K1" in prompt
        assert "hepg2" not in prompt.lower()

    def test_multiple_instances_surface_their_names_not_a_bare_type(self):
        """When several instances of the type exist, the gap context must surface
        their names (disambiguation) rather than a bare nameless type (#179)."""
        from builder.agents.pipeline.guidance import _gap_context

        state = _backbone()
        state.add_entity(_entity("cl1", "CellLineSample", name="CHO-K1 OATP1C1"))
        state.add_entity(_entity("cl2", "CellLineSample", name="HepaRG"))
        engine = AgentEngine(state=state)

        ctx = _gap_context(engine, _mit_cell_line_gap())

        # The leaf must never receive a bare type with no name: the candidate
        # instance names are surfaced somewhere in the grounding context.
        blob = " ".join(
            str(v) for v in (ctx.get("known_fields") or {}).values()
        ) + " " + str(ctx.get("entity_name") or "")
        assert "CHO-K1 OATP1C1" in blob and "HepaRG" in blob, (
            "multiple instances must surface their names for disambiguation (#179)"
        )


# ---------------------------------------------------------------------------
# (Issue #257, fix C) 'from_file' READS the file and EXTRACTS + COMMITS the value
#
# When the interpret leaf returns ``from_file`` and the named/likely file is
# under an approved scan root, the loop reads it (via file_readers) and runs a
# bounded extraction leaf to pull the requested field value, then COMMITS it.
# An unreadable / outside-root file gracefully skips (commits nothing).
# ---------------------------------------------------------------------------


class TestFromFileReadsAndExtracts:
    def _enable_provider(self, monkeypatch):
        from builder.agents.pipeline import guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

    def test_from_file_reads_extracts_and_commits(self, monkeypatch, tmp_path):
        """The headline regression: a 'look in the file' reply must READ the file,
        EXTRACT the value, and COMMIT it — not log a hint and skip (Issue #257)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        # A real file under an approved scan root carrying the value.
        meta = tmp_path / "Assay-metadata.csv"
        meta.write_text(
            "field,value\n"
            "description,A CHO-K1 OATP1C1 uptake assay measuring transporter "
            "activity.\n"
        )

        state = _backbone()
        state.approved_scan_roots.add(str(tmp_path))
        engine = AgentEngine(state=state)
        self._enable_provider(monkeypatch)

        desc_gap = Gap(
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
        _single_ask_gap_report(
            monkeypatch,
            desc_gap,
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "from_file", "filename": str(meta)},
        )

        # The extraction leaf pulls the value from the file text.
        captured: dict[str, object] = {}

        def _extract(field, file_text, ctx, **_kw):
            captured["field"] = field
            captured["file_text"] = file_text
            return "A CHO-K1 OATP1C1 uptake assay measuring transporter activity."

        monkeypatch.setattr(guidance, "extract_field_from_file", _extract)

        human = ScriptedHuman(
            input_answers=[_value(f"look in {meta}")]
        )
        summary = run_guidance(engine, human, max_rounds=5)

        # The value was READ from the file, extracted, and COMMITTED (not skipped).
        applied = _get(engine, "st1")
        assert applied.fields.get("description") == (
            "A CHO-K1 OATP1C1 uptake assay measuring transporter activity."
        )
        # The extraction leaf actually saw the FILE TEXT (it was read).
        assert "OATP1C1" in str(captured.get("file_text", ""))
        assert captured.get("field") == "description"
        # The gap is recorded as resolved.
        assert any(r.get("entity_id") == "st1" for r in summary["resolved"])

    def test_from_file_outside_approved_root_skips_gracefully(
        self, monkeypatch, tmp_path
    ):
        """A file outside every approved scan root is NOT read — the loop skips
        gracefully and commits nothing (sandbox honoured, Issue #257)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        # The file exists, but NO approved scan root contains it.
        outside = tmp_path / "secret.csv"
        outside.write_text("description,leak\n")

        engine = AgentEngine(state=_backbone())  # approved_scan_roots is empty
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)

        desc_gap = Gap(
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
        _single_ask_gap_report(
            monkeypatch,
            desc_gap,
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "from_file", "filename": str(outside)},
        )

        def _boom_extract(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError(
                "an out-of-root file must never be read or extracted"
            )

        monkeypatch.setattr(guidance, "extract_field_from_file", _boom_extract)

        human = ScriptedHuman(input_answers=[_value(f"it's in {outside}")])
        summary = run_guidance(engine, human, max_rounds=5)

        # Nothing committed — the sandbox refused the read.
        assert _get(engine, "st1").fields.get("description") == original
        assert not any(r.get("entity_id") == "st1" for r in summary["resolved"])

    def test_from_file_unreadable_file_skips_gracefully(self, monkeypatch, tmp_path):
        """A from_file pointing at a missing/unreadable file under an approved root
        skips gracefully (no value extracted, nothing committed)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        state = _backbone()
        state.approved_scan_roots.add(str(tmp_path))
        engine = AgentEngine(state=state)
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)

        missing = tmp_path / "does-not-exist.csv"

        desc_gap = Gap(
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
        _single_ask_gap_report(
            monkeypatch,
            desc_gap,
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "from_file", "filename": str(missing)},
        )
        # Extraction must not even be reached (no readable text).
        monkeypatch.setattr(
            guidance,
            "extract_field_from_file",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("must not extract from an unreadable file")
            ),
        )

        human = ScriptedHuman(input_answers=[_value(f"see {missing}")])
        run_guidance(engine, human, max_rounds=5)

        assert _get(engine, "st1").fields.get("description") == original

    def test_from_file_identifier_field_is_not_committed_from_file(
        self, monkeypatch, tmp_path
    ):
        """D5: an identifier-bearing field is never committed from file text — even
        a from_file reply pointing at the value must not land it (lookups only)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        meta = tmp_path / "compound.csv"
        meta.write_text("cas,103-90-2\n")

        state = _backbone_with_compound("Acetaminophen")
        state.approved_scan_roots.add(str(tmp_path))
        engine = AgentEngine(state=state)
        self._enable_provider(monkeypatch)

        cas_gap = Gap(
            tier="SHOULD",
            source="shacl",
            entity_id="chem1",
            entity_type="MolecularEntity",
            property="cas",
            message="MolecularEntity SHOULD record a CAS number.",
            suggestion="A CAS Registry Number.",
            fix_hint="ask-user",
            auto_fixable=False,
        )
        _single_ask_gap_report(
            monkeypatch,
            cas_gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx, **_kw: "What is the CAS number?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "from_file", "filename": str(meta)},
        )
        # Even if the leaf were to return a CAS, the loop's D5 guard refuses it.
        monkeypatch.setattr(
            guidance, "extract_field_from_file", lambda *_a, **_k: "103-90-2"
        )

        human = ScriptedHuman(input_answers=[_value(f"cas is in {meta}")])
        run_guidance(engine, human, max_rounds=5)

        applied = _get(engine, "chem1")
        assert applied.fields.get("cas") != "103-90-2", (
            "D5: an identifier must come from a lookup, not file extraction"
        )


# ---------------------------------------------------------------------------
# (#275) A person/agent-typed field (creator/author/...) answered with a free-
# text name must mint a Person ENTITY and link it by REFERENCE — never store a
# literal string. A literal string leaves the ISA "creator MUST be of type
# Person" SHACL shape unsatisfied so the gap re-emits every round and isa=fail.
# These run over the REAL gap engine so they assert the gap actually closes.
# ---------------------------------------------------------------------------


def _backbone_with_creator_gap() -> AgentEngine:
    """A BASE/ISA/TOX-passing backbone whose Study/Assay still lack a creator.

    ``assess_gaps`` over this state emits a real ``schema:creator`` gap for the
    Study and the Assay (and a root/Investigation one) — the exact gap the
    S-VHPS26 run looped on.
    """
    return AgentEngine(state=_backbone())


def _creator_gaps(engine: AgentEngine) -> list[Gap]:
    """The live ``schema:creator`` gaps assessed over ``engine.state``."""
    return [
        g for g in assess_gaps(engine.state).gaps if "creator" in (g.property or "")
    ]


def _study_creator_gap(engine: AgentEngine) -> Gap:
    """The Study's live ``schema:creator`` gap (asserts exactly one exists)."""
    study_gaps = [
        g
        for g in _creator_gaps(engine)
        if (g.entity_type == "Study") or str(g.entity_id or "").endswith("Study_st1")
    ]
    assert study_gaps, "expected a real Study creator gap on the backbone"
    return study_gaps[0]


class TestPersonFieldMintsPersonEntity:
    """#275: a creator/author answer becomes a Person reference, not a string."""

    def test_creator_plain_name_mints_person_and_closes_gap(self, monkeypatch):
        """A plain name answered for a Study ``creator`` gap mints a Person and
        links it BY REFERENCE; the creator gap must not re-appear on re-assess."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _backbone_with_creator_gap()
        gap = _study_creator_gap(engine)

        # Drive only the Study creator gap, then a clean re-assess so the loop
        # terminates promptly (mirrors the existing two-report pattern).
        _single_ask_gap_report(
            monkeypatch,
            gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )
        # No provider -> deterministic ask-and-set path (offline determinism).
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        human = ScriptedHuman(input_answers=[_value("Fabian Wagenaars")])
        run_guidance(engine, human, max_rounds=5)

        # A Person entity was minted (NOT a literal string on the Study).
        persons = engine.state.list_entities("Person")
        assert persons, "a creator answer must mint a Person entity (#275)"
        person = next(
            p for p in persons if (p.fields.get("familyName") == "Wagenaars")
        )
        assert person.fields.get("givenName") == "Fabian"

        # The Study's creator is a REFERENCE to that Person, never a literal.
        study = _get(engine, "st1")
        creator = study.fields.get("creator")
        assert isinstance(creator, dict) and "@id" in creator, (
            "creator must be an {'@id': ...} reference, not a string (#275)"
        )
        assert not isinstance(creator, str)

        # The real gap engine now finds NO creator gap for the Study (it closed),
        # and ISA conformance holds for the person requirement.
        remaining = _creator_gaps(engine)
        study_remaining = [
            g
            for g in remaining
            if (g.entity_type == "Study") or str(g.entity_id or "").endswith("Study_st1")
        ]
        assert study_remaining == [], (
            "the Study creator gap must not re-appear once a Person is linked (#275)"
        )
        assert assess_gaps(engine.state).conformance.get("isa") is True

    def test_creator_name_with_orcid_verifies_and_attaches(self, monkeypatch):
        """A name + ORCID answer mints a Person with the VERIFIED ORCID (D5: the
        ORCID is only trusted once a lookup confirms the family name)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _backbone_with_creator_gap()
        gap = _study_creator_gap(engine)

        _single_ask_gap_report(
            monkeypatch,
            gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        # Stub the ORCID lookup so the verification is offline and deterministic
        # (D5): it resolves to a record whose familyName matches the answer.
        calls: list[str] = []

        def _fake_lookup_orcid(orcid_id: str):
            calls.append(orcid_id)
            return {
                "found": True,
                "data": {
                    "givenName": "Fabian",
                    "familyName": "Wagenaars",
                    "name": "Fabian Wagenaars",
                },
                "error": None,
            }

        monkeypatch.setattr(guidance, "lookup_orcid", _fake_lookup_orcid)

        human = ScriptedHuman(
            input_answers=[_value("Fabian Wagenaars, ORCID: 0000-0003-4766-7358")]
        )
        run_guidance(engine, human, max_rounds=5)

        assert calls, "a supplied ORCID must be verified via lookup_orcid (D5)"
        person = next(
            p
            for p in engine.state.list_entities("Person")
            if p.fields.get("familyName") == "Wagenaars"
        )
        assert person.fields.get("orcid") == "0000-0003-4766-7358", (
            "a verified ORCID must be attached to the Person (#275)"
        )

        study = _get(engine, "st1")
        creator = study.fields.get("creator")
        assert isinstance(creator, dict) and "@id" in creator
        assert assess_gaps(engine.state).conformance.get("isa") is True

    def test_unverified_orcid_is_not_attached(self, monkeypatch):
        """D5: an ORCID whose family name does NOT match the answer is dropped —
        the Person is still minted (a name is descriptive), just without it."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _backbone_with_creator_gap()
        gap = _study_creator_gap(engine)

        _single_ask_gap_report(
            monkeypatch,
            gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        # The lookup resolves to a DIFFERENT person -> unverified -> dropped.
        monkeypatch.setattr(
            guidance,
            "lookup_orcid",
            lambda _o: {
                "found": True,
                "data": {"familyName": "SomeoneElse"},
                "error": None,
            },
        )

        human = ScriptedHuman(
            input_answers=[_value("Fabian Wagenaars, ORCID: 0000-0003-4766-7358")]
        )
        run_guidance(engine, human, max_rounds=5)

        person = next(
            p
            for p in engine.state.list_entities("Person")
            if p.fields.get("familyName") == "Wagenaars"
        )
        assert not person.fields.get("orcid"), (
            "an unverified ORCID must NOT be attached to the Person (D5, #275)"
        )

    def test_creator_answer_is_never_a_literal_string(self, monkeypatch):
        """Regression for the exact bug: the answer must never land as a literal
        ``creator`` string (which leaves isa=fail)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _backbone_with_creator_gap()
        gap = _study_creator_gap(engine)
        _single_ask_gap_report(
            monkeypatch,
            gap,
            counts={"must_open": 0, "should_open": 1, "may_open": 0},
        )
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        human = ScriptedHuman(input_answers=[_value("Fabian Wagenaars")])
        run_guidance(engine, human, max_rounds=5)

        creator = _get(engine, "st1").fields.get("creator")
        assert not isinstance(creator, str), (
            "the regression: a creator must never be committed as a literal string"
        )


# ---------------------------------------------------------------------------
# (Commit 1, #179) The root ``./`` citation MUST gap: its answer is persisted
# via the publication composites and the gap is NEVER re-asked.
#
# ROOT CAUSE the test pins:
#   * the root citation MUST gap surfaces with ``entity_id == "./"`` and
#     ``property == "http://schema.org/citation"``;
#   * ``_resolve_entity_id`` returns ``None`` (``state.get_entity("./")`` is None,
#     repair strips ``"./"`` -> ``""``), and ``citation`` is not a crate-metadata
#     slot, so the OLD ``_apply_value`` silently dropped the answer (returned
#     False) -> the always-highest-priority citation MUST gap was re-drawn and
#     re-asked after ANY other commit (the per-report skip-set is reset on every
#     commit), so the user was asked 6+ times and the answer never landed.
#
# FIX: route the root citation answer to the publication composites
# (``draft_publication_with_authors`` for a DOI/URL, else ``resolve_publication``
# for a title) and add a per-RUN persistent skip-set keyed by gap IDENTITY so an
# un-appliable / already-answered gap is not re-drawn even after a commit clears
# the per-report index set.
# ---------------------------------------------------------------------------


def _root_citation_gap(tier: Tier = "MUST") -> Gap:
    """The root Data Entity ``citation`` MUST gap as the gap engine emits it."""
    return Gap(
        tier=tier,
        source="shacl",
        entity_id="./",
        entity_type=None,
        property="http://schema.org/citation",
        message="The Root Data Entity SHOULD reference a citation.",
        suggestion="A publication DOI or title.",
        fix_hint="ask-user",
        auto_fixable=False,
    )


class TestRootCitationGapPersistsAndIsNotReAsked:
    def _record_run_tool(self, engine, monkeypatch):
        """Record ``engine.run_tool`` calls and stub the publication composites.

        The composites make network calls (Crossref / ORCID), so they are stubbed
        to a deterministic success; every call is recorded so the test can assert
        the citation answer was routed to them.
        """
        calls: list[tuple[str, dict]] = []
        real_run_tool = engine.run_tool

        def _spy(tool_name, **kwargs):
            calls.append((tool_name, dict(kwargs)))
            if tool_name == "draft_publication_with_authors":
                return {"publication_id": "pub1", "doi": kwargs.get("doi"), "authors": []}
            if tool_name == "resolve_publication":
                return {"ok": True, "doi": "10.1/x", "entity_id": "pub1"}
            return real_run_tool(tool_name, **kwargs)

        monkeypatch.setattr(engine, "run_tool", _spy)
        return calls

    def test_doi_answer_routes_to_draft_publication_with_authors(self, monkeypatch):
        """A DOI answer for the root citation gap calls
        ``draft_publication_with_authors`` (the answer is persisted, not dropped)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        calls = self._record_run_tool(engine, monkeypatch)

        _single_ask_gap_report(
            monkeypatch,
            _root_citation_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        human = ScriptedHuman(input_answers=[_value("10.1016/j.tox.2021.152898")])
        summary = run_guidance(engine, human, max_rounds=5)

        pub_calls = [c for c in calls if c[0] == "draft_publication_with_authors"]
        assert pub_calls, (
            "a DOI answer to the root citation gap must call "
            "draft_publication_with_authors (#179)"
        )
        assert pub_calls[0][1].get("doi") == "10.1016/j.tox.2021.152898"
        # The gap was recorded as resolved (the answer landed, not silently dropped).
        assert any(
            "citation" in (r.get("property") or "") for r in summary["resolved"]
        ), "the citation answer must be recorded as resolved, never dropped"

    def test_title_answer_routes_to_resolve_publication(self, monkeypatch):
        """A non-DOI (title) answer routes to ``resolve_publication``."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        calls = self._record_run_tool(engine, monkeypatch)

        _single_ask_gap_report(
            monkeypatch,
            _root_citation_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        title = "Adverse outcome pathway-based assessment of TPO inhibition in vitro"
        human = ScriptedHuman(input_answers=[_value(title)])
        run_guidance(engine, human, max_rounds=5)

        resolve_calls = [c for c in calls if c[0] == "resolve_publication"]
        assert resolve_calls, (
            "a title answer to the root citation gap must call resolve_publication (#179)"
        )
        assert resolve_calls[0][1].get("title") == title
        assert not any(c[0] == "draft_publication_with_authors" for c in calls)

    def test_citation_gap_is_asked_at_most_once_across_the_run(self, monkeypatch):
        """The headline regression: the always-highest-priority root citation MUST
        gap must be asked AT MOST ONCE even when other gaps keep committing and
        re-assessment keeps re-emitting it (the per-RUN skip-set, #179)."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        monkeypatch.setattr(guidance, "get_provider", lambda: None)

        # The citation gap stays UN-progressable from the loop's POV: the stubbed
        # DOI lookup finds no confident match (the common live case — a title /
        # unresolvable DOI), so ``_apply_citation_value`` returns False and the
        # same citation gap re-emits every re-assess. A SECOND, progress-making gap
        # commits each round, which CLEARS the per-report index skip-set — so
        # without a per-RUN identity skip-set the citation gap would be re-drawn
        # and re-asked every round.
        calls: list[tuple[str, dict]] = []
        real_run_tool = engine.run_tool

        def _spy(tool_name, **kwargs):
            calls.append((tool_name, dict(kwargs)))
            if tool_name == "draft_publication_with_authors":
                return {"ok": False, "error": "DOI did not resolve"}
            if tool_name == "resolve_publication":
                return {"ok": False, "reason": "no confident DOI match"}
            return real_run_tool(tool_name, **kwargs)

        monkeypatch.setattr(engine, "run_tool", _spy)

        citation_gap = _root_citation_gap()
        # A distinct committable gap on the Study, re-emitted with a FRESH value
        # each round so committing it always makes progress (re-assess returns a
        # report that still contains the citation gap).
        round_n = {"i": 0}

        def _assess(_state):
            round_n["i"] += 1
            i = round_n["i"]
            if i > 6:
                # Eventually the Study desc is satisfied; only the citation remains
                # and it is now in the per-RUN skip-set -> the loop terminates.
                return GapReport(
                    gaps=[citation_gap],
                    counts={"must_open": 1, "should_open": 0, "may_open": 0},
                )
            study_gap = Gap(
                tier="MUST",
                source="shacl",
                entity_id="st1",
                entity_type="Study",
                property=f"https://schema.org/keywords#round{i}",
                message="Study needs another keyword.",
                suggestion="A keyword.",
                fix_hint="ask-user",
                auto_fixable=False,
            )
            return GapReport(
                gaps=[citation_gap, study_gap],
                counts={"must_open": 2, "should_open": 0, "may_open": 0},
            )

        monkeypatch.setattr(guidance, "assess_gaps", _assess)

        # Always-available answers: a DOI for the citation, a keyword for the Study.
        human = ScriptedHuman(
            input_answers=[_value("10.1/citation")] + [_value("kw") for _ in range(20)]
        )
        run_guidance(engine, human, max_rounds=15)

        # Count how many times the CITATION question was put to the user.
        citation_prompts = [
            p for (p, _ftype) in human.inputs if "citation" in p.lower()
        ]
        assert len(citation_prompts) <= 1, (
            f"the root citation gap must be asked at most once, got "
            f"{len(citation_prompts)} prompts: {citation_prompts}"
        )


# ---------------------------------------------------------------------------
# #375 — guidance commits must be HONEST.
#
# Four defects, one lane: an entity-scoped answer landed on the Root Data
# Entity, a reference-only field stored prose the builder then dropped, the
# offline path bypassed the D5 identifier guard, and "the setter returned True"
# was mistaken for "the gap cleared" (so the same question came back every
# round until the budget ran out).
#
# FIXTURE REALISM (CONTRIBUTING.md, #343): every gap below comes from the REAL
# gap engine over a REAL state. Never a hand-made ``Gap(entity_id="st1", ...)``
# — ``_mit_gaps`` never emits a non-None ``entity_id`` (gap_analysis.py), and
# that unrealistic double is precisely what hid these defects.
# ---------------------------------------------------------------------------

_ROOT_DESCRIPTION = "SENTINEL ROOT DESCRIPTION - about the study, not the assay"
_ROOT_ACCESSION = "10.5281/zenodo.123456"


def _honest_backbone(*, named_investigation: bool = True, protocols: int = 0) -> CrateState:
    """A backbone shaped the way the pipeline actually leaves one.

    ``_backbone_hints`` gives the Assay a ``name`` and nothing else, and the
    plan's description is merged onto the Study layer only — so
    ``Assay:description`` is an unfilled MIT slot on *every* pipeline run and is
    the highest-priority actionable gap in the report. The root carries sentinel
    metadata so a mis-routed commit is detectable.
    """
    state = CrateState()
    state.metadata.title = "FRTL-5 perchlorate thyroid study"
    state.metadata.description = _ROOT_DESCRIPTION
    state.metadata.accession = _ROOT_ACCESSION
    inv_fields = {"description": "d", "identifier": "INV-1"}
    if named_investigation:
        inv_fields["name"] = "Inv"
    state.add_entity(_entity("inv1", "Investigation", **inv_fields))
    state.add_entity(
        _entity("st1", "Study", name="St", description="d", investigation_id="inv1")
    )
    # Assay: name + study_id only, exactly as the pipeline leaves it.
    state.add_entity(_entity("as1", "Assay", name="FRTL-5 assay", study_id="st1"))
    for i in range(protocols):
        state.add_entity(_entity(f"lp{i}", "LabProtocol", name=f"Protocol {chr(65 + i)}"))
    return state


def _pick_gap(state: CrateState, predicate) -> Gap:
    """The single live gap from the REAL gap engine matching ``predicate``.

    Asserting the gap exists is itself part of each test: if the pipeline stops
    leaving this slot open, or the engine stops emitting the gap, the test must
    fail loudly rather than silently passing on an empty selection.
    """
    matches = [g for g in assess_gaps(state).gaps if predicate(g)]
    assert matches, "expected a live gap matching the predicate over this state"
    return matches[0]


def _mit_gap(state: CrateState, entity_type: str, prop: str) -> Gap:
    return _pick_gap(
        state,
        lambda g: g.source == "mit" and g.entity_type == entity_type and g.property == prop,
    )


class TestTypedGapCommitsToItsOwnEntity:
    """#375(a): a typed MIT gap writes to its instance, not the Root Data Entity."""

    def test_mit_assay_description_gap_writes_to_the_assay_not_the_root(self):
        """The gap that starts every interactive run must land on the Assay.

        Today ``_apply_value`` cannot resolve an entity (MIT gaps carry
        ``entity_id=None``), never consults ``gap.entity_type``, and falls
        through to ``set_crate_metadata`` — overwriting the root's
        ``description``, a Base RO-Crate MUST, while the Assay stays empty.
        """
        from builder.agents.pipeline.guidance import _apply_value

        engine = AgentEngine(state=_honest_backbone())
        gap = _mit_gap(engine.state, "Assay", "description")
        answer = "A resazurin viability readout at 24 h and 48 h."

        assert _apply_value(engine, gap, answer) is True

        # Two independent facts, neither hand-built, both wrong today.
        assert engine.state.metadata.description == _ROOT_DESCRIPTION, (
            "the root's description is a Base MUST and must not be clobbered by "
            "an answer about the Assay"
        )
        assay = engine.state.get_entity("as1")
        assert assay is not None and assay.fields.get("description") == answer

    def test_root_investigation_name_gap_still_reaches_set_crate_metadata(self):
        """Honesty control: the new guard must discriminate, not blanket-refuse.

        ``./`` folds the Investigation, so ``Investigation:name`` is the one MIT
        slot that legitimately belongs on the root. Its param is
        ``Investigation:name;Study:name;Assay:name`` and a param is a gap only
        when *none* of its slots is filled, so the state must leave all three
        unnamed for this gap to be live at all.
        """
        from builder.agents.pipeline.guidance import _apply_value

        state = CrateState()
        state.metadata.description = _ROOT_DESCRIPTION
        state.add_entity(_entity("inv1", "Investigation", description="d", identifier="INV-1"))
        state.add_entity(_entity("st1", "Study", description="d", investigation_id="inv1"))
        state.add_entity(_entity("as1", "Assay", study_id="st1"))
        engine = AgentEngine(state=state)
        gap = _mit_gap(engine.state, "Investigation", "name")

        assert _apply_value(engine, gap, "FRTL-5 perchlorate investigation") is True
        assert engine.state.metadata.title == "FRTL-5 perchlorate investigation"

    def test_typed_gap_with_two_instances_commits_nothing(self):
        """Ambiguous target -> commit nothing (D5), rather than guessing one."""
        from builder.agents.pipeline.guidance import _apply_value

        engine = AgentEngine(state=_honest_backbone(protocols=2))
        gap = _mit_gap(engine.state, "LabProtocol", "description")

        assert _apply_value(engine, gap, "Seed 20k cells/well, dose at 24 h.") is False
        assert engine.state.metadata.description == _ROOT_DESCRIPTION
        for eid in ("lp0", "lp1"):
            protocol = engine.state.get_entity(eid)
            assert protocol is not None and "description" not in protocol.fields


class TestReferenceFieldNeverCommittedAsLiteral:
    """#375(b): a ``_REF_FIELDS`` gap answered with prose is refused, not faked."""

    @staticmethod
    def _measurement_method_gap(state: CrateState) -> Gap:
        return _pick_gap(
            state,
            lambda g: g.source == "shacl"
            and str(g.property or "").endswith("measurementMethod"),
        )

    def test_measurement_method_prose_is_refused(self, monkeypatch):
        """The builder drops a non-resolvable literal on a reference property
        (``_scalar_props`` / ``_wire_reference``), so reporting success is a lie.

        ``measurementMethod`` prose is first offered to BAO — resolving it into a
        verified DefinedTerm is a real answer, not a fabricated one. What must
        never happen is committing the prose when the lookup CANNOT vouch for it,
        so the lookup is stubbed empty here. That also makes this hermetic: it
        previously reached the live BAO service and its verdict depended on
        whether the network (and the ontology) happened to agree.
        """
        import builder.tools.lookups as lookups_mod
        from builder.agents.pipeline.guidance import _apply_value

        monkeypatch.setattr(lookups_mod, "lookup_bao_term", lambda *a, **k: {})

        engine = AgentEngine(state=_honest_backbone())
        gap = self._measurement_method_gap(engine.state)

        assert _apply_value(engine, gap, "resazurin viability assay") is False
        assay = engine.state.get_entity("as1")
        assert assay is not None and "measurementMethod" not in assay.fields

    def test_resolvable_reference_still_commits(self):
        """Honesty control: the guard refuses PROSE, not references."""
        from builder.agents.pipeline.guidance import _apply_value

        state = _honest_backbone()
        state.add_entity(
            _entity("term1", "DefinedTerm", name="viability assay", url="http://x.org/BAO_1")
        )
        engine = AgentEngine(state=state)
        gap = self._measurement_method_gap(engine.state)

        assert _apply_value(engine, gap, "term1") is True
        assay = engine.state.get_entity("as1")
        assert assay is not None and assay.fields.get("measurementMethod") == "term1"


class TestIdentifierNeverCommittedFromProse:
    """#375(c): D5 holds on EVERY commit path, including offline."""

    @staticmethod
    def _cell_line_state() -> CrateState:
        state = _honest_backbone()
        state.add_entity(_entity("cl1", "CellLineSample", name="FRTL-5"))
        return state

    def test_offline_identifier_gap_commits_nothing(self, monkeypatch):
        """No provider -> ``_resolve_ask_user`` returned the prose verbatim,
        bypassing ``_deterministic_decision``'s D5 skip entirely."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import _resolve_gap

        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        engine = AgentEngine(state=self._cell_line_state())
        gap = _mit_gap(engine.state, "CellLineSample", "identifier")

        human = ScriptedHuman(input_answers=[_value("CVCL_9999")])
        resolved: list[dict] = []
        assert (
            _resolve_gap(engine, human, gap, resolved=resolved, asked=[], usage_sink=None)
            is False
        )

        assert engine.state.metadata.accession == _ROOT_ACCESSION, (
            "the dataset DOI must survive an answer about a cell line"
        )
        cell = engine.state.get_entity("cl1")
        assert cell is not None and "identifier" not in cell.fields
        assert resolved == []

    def test_draft_confirm_edit_cannot_commit_an_identifier(self):
        """The draft-confirm dialog feeds ``edits["value"]`` straight into
        ``_apply_value``; it is unguarded even WITH a provider configured."""
        from builder.agents.pipeline.guidance import _apply_value

        engine = AgentEngine(state=self._cell_line_state())
        gap = _mit_gap(engine.state, "CellLineSample", "identifier")

        assert _apply_value(engine, gap, "CVCL_9999") is False
        assert engine.state.metadata.accession == _ROOT_ACCESSION
        cell = engine.state.get_entity("cl1")
        assert cell is not None and "identifier" not in cell.fields


class TestNonClearingGapIsNotReAsked:
    """#375(d): "the setter returned True" is not "the gap cleared"."""

    def test_no_question_is_asked_twice_in_one_run(self, monkeypatch):
        """A gap whose answer cannot clear it must be suppressed by identity
        after ONE ask.

        Today the highest-priority gap (MIT ``Assay:description``) "progresses"
        every round without clearing, so it is re-drawn until ``max_rounds``
        runs out and the 18 actionable gaps behind it are never reached. The
        assertion is over *repeats*, not over one hard-coded prompt, so it
        cannot pass vacuously by the loop never reaching a particular gap.
        """
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        engine = AgentEngine(state=_honest_backbone())

        human = ScriptedHuman(input_answers=[_value("resazurin viability assay")] * 8)
        run_guidance(engine, human, max_rounds=5)

        prompts = [p for p, _ft in human.inputs]
        repeated = {p for p in prompts if prompts.count(p) > 1}
        assert not repeated, f"these questions were asked more than once: {repeated}"
        assert len(prompts) > 1, "the loop must move on to other gaps, not stall"

    def test_resolved_count_only_includes_gaps_that_actually_cleared(self, monkeypatch):
        """``format_guidance_summary`` prints ``resolved: N`` straight from this
        list, so a commit that did not clear its gap must not be counted."""
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: None)
        engine = AgentEngine(state=_honest_backbone())

        human = ScriptedHuman(input_answers=[_value("resazurin viability assay")] * 8)
        result = run_guidance(engine, human, max_rounds=5)

        # NB the oracle is per-gap, not a count: committing an answer MINTS
        # entities (a Person for a creator gap), and each new entity brings its
        # own unfilled slots, so the total gap count can legitimately RISE while
        # real progress is made. What must hold is that every gap reported
        # resolved is genuinely gone.
        still_open = {
            (g.source, g.entity_id, g.entity_type, g.property)
            for g in assess_gaps(engine.state).gaps
        }
        lying = [
            r
            for r in result["resolved"]
            if (r["source"], r["entity_id"], r["entity_type"], r["property"]) in still_open
        ]
        assert not lying, (
            f"{len(lying)} gap(s) were reported resolved but are still open: "
            f"{[(r['source'], r['entity_type'], r['property']) for r in lying]}"
        )


# ---------------------------------------------------------------------------
# #384 — the guidance tail's LLM calls must reach the usage sink.
#
# All four guidance leaves accept a ``usage_sink`` and the deterministic spine
# threads one, but the guidance tail never did: every phrase / interpret / draft /
# from-file-extract call (1-5 per gap, up to ``max_rounds`` gaps) was invisible to
# the accounting, so the status bar re-printed before EVERY gap question showed a
# frozen token count while real money was spent.
#
# The assertions here deliberately read the numbers back through
# ``ui._read_token_totals`` — the SAME function ``ui.print_status_bar`` uses — which
# re-parses ``profile.ndjson`` off disk. Nothing hand-builds the expected total, so
# a sink that is threaded but not wired to the displayed surface still fails.
# (Never assert through ``ui.snapshot_from_engine``: once tokens > 0 it calls
# ``builder.pricing.compute_cost``, which does a live urlopen on a cold cache.)
# ---------------------------------------------------------------------------


_PHRASED = "What does this study examine, in one or two sentences?"


def _profiled_engine(monkeypatch, tmp_path) -> AgentEngine:
    """A real engine whose real ``ProfilingLogger`` writes under ``tmp_path``.

    ``ui._read_token_totals`` re-imports ``SESSION_DIR`` from
    ``builder.tools.profiler`` on every call, so patching it there redirects both
    the writer and the reader — no session data lands in the repo.
    """
    import builder.tools.profiler as profiler_mod

    monkeypatch.setattr(profiler_mod, "SESSION_DIR", tmp_path)
    engine = AgentEngine(state=_backbone())
    engine.initialize()
    return engine


class TestGuidanceTokenAccounting:
    """#384: the interactive tail's token spend reaches the status bar."""

    @staticmethod
    def _drive(monkeypatch, tmp_path, *, reported: tuple):
        """One real ask-user round whose leaves each report ``reported`` usage.

        Returns ``(engine, summary, human)``. The stubs stand in for the two
        drafter-tier leaves only — the loop, the gap dispatch, the commit path and
        the profiler are all real.
        """
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _profiled_engine(monkeypatch, tmp_path)
        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

        def _phrase(gap_context, *, overrides=None, usage_sink):
            usage_sink(*reported)
            return _PHRASED

        def _interpret(question, reply, gap_context, *, overrides=None, usage_sink):
            usage_sink(*reported)
            return {"action": "commit", "value": reply.strip()}

        monkeypatch.setattr(guidance, "phrase_gap_question", _phrase)
        monkeypatch.setattr(guidance, "interpret_gap_reply", _interpret)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        human = ScriptedHuman(
            input_answers=[_value("A dose-response cytotoxicity study in HepG2 cells.")]
        )
        summary = run_guidance(engine, human, max_rounds=5)
        return engine, summary, human

    def test_status_bar_token_totals_include_the_guidance_leaves(self, monkeypatch, tmp_path):
        from builder.agents import ui

        engine, summary, _human = self._drive(
            monkeypatch, tmp_path, reported=(120, 35, "gpt-4o-mini")
        )

        # The round really happened (the harness is not idling past the leaves).
        assert _get(engine, "st1").fields.get("description") == (
            "A dose-response cytotoxicity study in HepG2 cells."
        )
        assert summary["resolved"], "the ask-user round must have committed"

        # Two leaf calls (phrase + interpret) x (120 in / 35 out), read back
        # through the status bar's own reader off the real profile.ndjson.
        assert ui._read_token_totals(engine.state.session_id) == (240, 70, "gpt-4o-mini")

    def test_guidance_summary_reports_its_own_usage(self, monkeypatch, tmp_path):
        _engine, summary, _human = self._drive(
            monkeypatch, tmp_path, reported=(120, 35, "gpt-4o-mini")
        )

        assert summary["usage"] == {
            "input_tokens": 240,
            "output_tokens": 70,
            "total_tokens": 310,
        }

    def test_token_totals_stay_zero_when_the_leaves_report_no_usage(self, monkeypatch, tmp_path):
        """Honesty control: the ``240`` above is the leaves' REPORTED usage flowing
        through the new plumbing — not "guidance ran", and not a pre-existing
        logger. ``(None, None, None)`` is what ``_extract_token_usage`` returns for
        a fake/offline model, and it must total zero while the gap still commits.
        """
        from builder.agents import ui

        engine, summary, _human = self._drive(
            monkeypatch, tmp_path, reported=(None, None, None)
        )

        assert ui._read_token_totals(engine.state.session_id) == (0, 0, "")
        assert summary["usage"]["total_tokens"] == 0
        assert summary["resolved"], "the gap must still commit when usage is unknown"

    def test_threading_the_sink_does_not_silently_fall_back(self, monkeypatch, tmp_path):
        """The trap: all four leaf calls sit inside a broad ``except Exception``
        whose fallback is the deterministic path, so a misnamed or positionally-
        passed ``usage_sink`` degrades SILENTLY to ``_ask_user_prompt`` instead of
        raising. A strict-signature stub makes that fallback observable: the
        sentinel question is what the human must have been prompted with.
        """
        from builder.agents.pipeline import guidance
        from builder.agents.pipeline.guidance import run_guidance

        engine = _profiled_engine(monkeypatch, tmp_path)
        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

        def _phrase(gap_context, *, usage_sink, overrides=None):
            usage_sink(7, 3, "gpt-4o-mini")
            return _PHRASED

        monkeypatch.setattr(guidance, "phrase_gap_question", _phrase)
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c, **_kw: {"action": "skip"},
        )
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        human = ScriptedHuman(input_answers=[_value("dunno")])
        run_guidance(engine, human, max_rounds=5)

        assert [p for p, _ft in human.inputs] == [_PHRASED], (
            "the phrase leaf fell back to the deterministic prompt — the sink was "
            "not accepted by its keyword name"
        )
