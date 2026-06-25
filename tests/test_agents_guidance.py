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
    from builder.agents import guidance

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
        from builder.agents import guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

    def test_idk_reply_is_skipped_not_stored(self, monkeypatch):
        """The headline regression: a free-text 'I don't know' reply must SKIP —
        it must NEVER be stored verbatim as the field value (#244)."""
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
            guidance, "phrase_gap_question", lambda _ctx: "What does this study examine?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _reply, _ctx: {"action": "skip"},
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx: "What does this study examine?"
        )

        captured: dict[str, object] = {}

        def _interpret(question, reply, ctx):
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx: "What does this study examine?"
        )

        # Every interpretation says "clarify" — without a cap this loops forever.
        interpret_calls = {"n": 0}

        def _always_clarify(question, reply, ctx):
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx: "What does this study examine?"
        )

        decisions = iter(
            [
                {"action": "clarify", "question": "Which endpoint?"},
                {"action": "commit", "value": "A viability assay study."},
            ]
        )
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c: next(decisions)
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        original = _get(engine, "st1").fields.get("description")
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        monkeypatch.setattr(
            guidance, "phrase_gap_question", lambda _ctx: "What does this study examine?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c: {"action": "from_file", "filename": "README.txt"},
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

        engine = AgentEngine(state=_backbone())
        self._enable_provider(monkeypatch)
        _single_ask_gap_report(
            monkeypatch,
            _study_desc_gap(),
            counts={"must_open": 1, "should_open": 0, "may_open": 0},
        )

        phrased = "In one sentence, what does this study set out to find?"
        monkeypatch.setattr(guidance, "phrase_gap_question", lambda _ctx: phrased)
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c: {"action": "skip"}
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
        from builder.agents.guidance import _gap_context

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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
        def _phrase(ctx):
            name = ctx.get("entity_name") or "this chemical substance"
            return f"What is the CAS Registry Number for {name}?"

        monkeypatch.setattr(guidance, "phrase_gap_question", _phrase)
        # An identifier gap: the user's prose can never become the value (D5).
        monkeypatch.setattr(
            guidance, "interpret_gap_reply", lambda _q, _r, _c: {"action": "skip"}
        )

        human = ScriptedHuman(input_answers=[_value("dunno")])
        run_guidance(engine, human, max_rounds=5)

        assert human.inputs, "the user must be prompted"
        prompt = human.inputs[0][0]
        assert "Silychristin A" in prompt
        assert "this chemical" not in prompt.lower()

    def test_deterministic_prompt_names_the_entity(self):
        """Even the no-provider deterministic prompt names the entity (Issue #257)."""
        from builder.agents.guidance import _ask_user

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
# (Issue #257, fix C) 'from_file' READS the file and EXTRACTS + COMMITS the value
#
# When the interpret leaf returns ``from_file`` and the named/likely file is
# under an approved scan root, the loop reads it (via file_readers) and runs a
# bounded extraction leaf to pull the requested field value, then COMMITS it.
# An unreadable / outside-root file gracefully skips (commits nothing).
# ---------------------------------------------------------------------------


class TestFromFileReadsAndExtracts:
    def _enable_provider(self, monkeypatch):
        from builder.agents import guidance

        monkeypatch.setattr(guidance, "get_provider", lambda: "openai")

    def test_from_file_reads_extracts_and_commits(self, monkeypatch, tmp_path):
        """The headline regression: a 'look in the file' reply must READ the file,
        EXTRACT the value, and COMMIT it — not log a hint and skip (Issue #257)."""
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
            guidance, "phrase_gap_question", lambda _ctx: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c: {"action": "from_file", "filename": str(meta)},
        )

        # The extraction leaf pulls the value from the file text.
        captured: dict[str, object] = {}

        def _extract(field, file_text, ctx):
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
            guidance, "phrase_gap_question", lambda _ctx: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c: {"action": "from_file", "filename": str(outside)},
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
            guidance, "phrase_gap_question", lambda _ctx: "Describe the study."
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c: {"action": "from_file", "filename": str(missing)},
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
        from builder.agents import guidance
        from builder.agents.guidance import run_guidance

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
            guidance, "phrase_gap_question", lambda _ctx: "What is the CAS number?"
        )
        monkeypatch.setattr(
            guidance,
            "interpret_gap_reply",
            lambda _q, _r, _c: {"action": "from_file", "filename": str(meta)},
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
