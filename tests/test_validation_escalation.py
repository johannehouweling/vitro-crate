"""Focused tests for deterministic ReAct validation escalation."""

from __future__ import annotations

from builder.agents.react.agent_loop import _run_validation_escalation
from builder.engine import AgentEngine
from builder.state import Entity


class _InteractiveHuman:
    is_interactive = True

    def __init__(self, decisions: list[str]):
        self.decisions = list(decisions)
        self.present_calls: list[tuple[str, str | None]] = []

    def present(self, context, options=None, purpose=None):
        self.present_calls.append((context, purpose))
        action = self.decisions.pop(0)
        return {"action": action, "comments": None, "edits": None}

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


class _HeadlessHuman:
    is_interactive = False

    def __init__(self):
        self.present_calls = 0

    def present(self, context, options=None, purpose=None):
        self.present_calls += 1
        raise AssertionError("headless validation must not prompt")

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


def _engine(human):
    engine = AgentEngine(human_interface=human)
    engine.state.add_entity(Entity(entity_id="e1", type="Investigation"))
    return engine


def _stub_validation(engine, calls):
    def run_tool(tool_name, **kwargs):
        calls.append((tool_name, kwargs))
        severity = kwargs.get("severity", "required")
        return {
            "ok": True,
            "severity": severity,
            "profile": kwargs.get("profile", "all"),
            "conformance": {"base": True, "isa": True, "tox": True},
            "issues": [],
        }

    engine.run_tool = run_tool


def test_interactive_accepts_recommended_and_optional():
    human = _InteractiveHuman(["approved", "approved"])
    engine = _engine(human)
    calls = []
    _stub_validation(engine, calls)

    _run_validation_escalation(engine, {"ok": True})

    assert [kwargs["severity"] for _, kwargs in calls] == ["recommended", "optional"]
    assert len(human.present_calls) == 2
    assert all(purpose == "validation_escalation" for _, purpose in human.present_calls)


def test_interactive_declining_recommended_stops_cascade():
    human = _InteractiveHuman(["rejected"])
    engine = _engine(human)
    calls = []
    _stub_validation(engine, calls)

    _run_validation_escalation(engine, {"ok": True})

    assert calls == []
    assert len(human.present_calls) == 1


def test_interactive_accepts_recommended_then_declines_optional():
    human = _InteractiveHuman(["approved", "rejected"])
    engine = _engine(human)
    calls = []
    _stub_validation(engine, calls)

    _run_validation_escalation(engine, {"ok": True})

    assert [kwargs["severity"] for _, kwargs in calls] == ["recommended"]
    assert len(human.present_calls) == 2


def test_headless_skips_prompts_and_broader_validation():
    human = _HeadlessHuman()
    engine = _engine(human)
    calls = []
    _stub_validation(engine, calls)

    _run_validation_escalation(engine, {"ok": True})

    assert calls == []
    assert human.present_calls == 0


def test_unchanged_content_is_not_prompted_twice():
    human = _InteractiveHuman(["rejected"])
    engine = _engine(human)
    calls = []
    _stub_validation(engine, calls)

    _run_validation_escalation(engine, {"ok": True})
    _run_validation_escalation(engine, {"ok": True})

    assert len(human.present_calls) == 1


def test_writeback_files_every_tier_the_gate_covered():
    """A gate is a FLOOR, not a filter — it files every tier it evaluated.

    ``severity="recommended"`` runs the REQUIRED checks too, so its result
    carries both tiers and both are filed. Filing only the named tier left the
    REQUIRED list describing an older crate while the fingerprint said the
    verdict was current.
    """
    engine = _engine(_HeadlessHuman())
    # The tier comes from the CALL's `severity=` kwarg, not from the result dict:
    # `build_and_validate` returns only {"ok", "conformance", "issues"}, so
    # `run_tool` forwards the severity it was called with.
    engine._writeback_validation(
        "build_and_validate",
        {
            "conformance": {"base": True},
            "issues": [
                {"severity": "required", "profile": "base", "message": "required"},
                {"severity": "recommended", "profile": "isa", "message": "should"},
                {"severity": "optional", "profile": "tox", "message": "may"},
            ],
        },
        severity="recommended",
    )
    # REQUIRED is covered by a RECOMMENDED gate, so it is filed, not dropped.
    assert "required" in engine.state.validation.required_issues[0]
    assert "should" in engine.state.validation.should_issues[0]
    # OPTIONAL is ABOVE the gate — never evaluated, so never filed.
    assert engine.state.validation.may_issues == []
    assert engine.state.validation.assessed_tiers == {"required", "recommended"}

    engine._writeback_validation(
        "build_and_validate",
        {
            "conformance": {"base": True},
            "issues": [
                {"severity": "optional", "profile": "tox", "message": "may"},
            ],
        },
        severity="optional",
    )
    assert "may" in engine.state.validation.may_issues[0]
    assert engine.state.validation.assessed_tiers == {"required", "recommended", "optional"}
