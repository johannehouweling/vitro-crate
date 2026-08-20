"""The measured ReAct arm gets the budget the shipped ReAct arm gets (#609).

The eval used to drive the loop with a single bare ``app.invoke``: no wall-clock
timeout guard, no self-continue, and — the big one — no autonomous continuation.
The arm that ships answers its own narration for up to ``_MAX_AUTONOMOUS_TURNS``
before checking in with the user, so the eval was measuring an arm with a
strictly smaller budget than the one users run, and reporting the result as the
architecture's.

These tests drive the REAL production path with the model and graph faked; no
network, no provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.corpus import EvalCase
from eval.react_factory import make_react_agent_factory


class _FakeGraph:
    """A graph whose every turn narrates (never asks, never completes)."""

    def __init__(self, sink: list[str], reply: str = "I added the Investigation.") -> None:
        self._sink = sink
        self._reply = reply

    def invoke(self, payload: dict, config: dict) -> dict:
        from langchain_core.messages import AIMessage

        self._sink.append(str(payload["messages"][0].content))
        return {"messages": [AIMessage(content=self._reply)]}


@pytest.fixture
def faked_loop(monkeypatch):
    """Fake the model + graph inside the shipped loop; return the turn sink."""
    from builder.agents.react import agent_loop
    import builder.tools.session as session_mod

    turns: list[str] = []
    monkeypatch.setattr(agent_loop, "_build_chat_model", lambda **kw: object())
    monkeypatch.setattr(agent_loop, "_build_agent_graph", lambda *a, **k: _FakeGraph(turns))
    monkeypatch.setattr(session_mod, "save_session", lambda *a, **k: {"success": True})
    monkeypatch.setattr(agent_loop, "_finish_backstop", lambda engine, **kw: None)
    return turns


_CASE = EvalCase(
    case_id="budget-probe",
    description="drives the loop with a prompt and no input dir",
    kind="minimal",
    prompt="Build a minimal ISA-Tox crate.",
)


class TestTheShippedBudgetIsWhatGetsMeasured:
    def test_a_narrating_reply_auto_continues_instead_of_ending_the_build(
        self, faked_loop
    ) -> None:
        """One bare invoke is one turn. The shipped loop keeps working."""
        from builder.agents.react.agent_loop import _MAX_AUTONOMOUS_TURNS

        outcome = make_react_agent_factory()().build(_CASE)

        assert len(faked_loop) == _MAX_AUTONOMOUS_TURNS + 1, faked_loop
        assert outcome.stop_reason == "completed"

    def test_the_case_prompt_is_what_drives_the_first_turn(self, faked_loop) -> None:
        make_react_agent_factory()().build(_CASE)

        assert faked_loop[0] == _CASE.prompt

    def test_no_greeting_turn_is_paid_for_headlessly(self, faked_loop) -> None:
        """The greeting is a UI affordance — a model call whose only product is a
        welcome message nobody reads in a headless A/B. Charging the ReAct arm for
        it measures politeness, not build capability."""
        make_react_agent_factory()().build(_CASE)

        assert not any(t.startswith(("Greet the user", "The user has resumed")) for t in faked_loop)

    def test_the_run_never_reads_stdin(self, faked_loop, monkeypatch) -> None:
        """With nobody at the keyboard the session ends where it would prompt —
        it must not block a headless harness on a terminal read."""
        from builder.agents.react import agent_loop

        def _boom(*a: Any, **k: Any) -> str:
            raise AssertionError("the headless eval read stdin")

        monkeypatch.setattr(agent_loop.ui, "boxed_input", _boom)
        assert make_react_agent_factory()().build(_CASE).stop_reason == "completed"


class TestCapHitStillSurfaces:
    def test_a_recursion_cap_is_reported_as_cap_hit(self, monkeypatch, faked_loop) -> None:
        """`stop_reason="cap_hit"` keeps a valid-at-the-cutoff run from reading as
        a clean win (#331). The loop swallows `GraphRecursionError` internally, so
        it has to be reported out rather than caught by the eval."""
        from builder.agents.react import agent_loop
        from langgraph.errors import GraphRecursionError

        class _Capped:
            def invoke(self, payload: dict, config: dict) -> dict:
                raise GraphRecursionError("cap")

        monkeypatch.setattr(agent_loop, "_build_agent_graph", lambda *a, **k: _Capped())

        assert make_react_agent_factory()().build(_CASE).stop_reason == "cap_hit"
