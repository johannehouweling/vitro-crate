"""Tests for the agent-agnostic build API (offline).

The harness only ever talks to a :class:`~eval.agent_api.BuildAgent`: a zero-arg
``agent_factory`` returns one, and the runner calls ``.build(case)``. This keeps
the harness independent of *how* a crate is built (ReAct today, deterministic
pipeline tomorrow). These tests pin the contract with an in-memory fake.
"""

from __future__ import annotations

from builder.state import CrateState
from eval.agent_api import BuildAgent, BuildOutcome
from eval.corpus import EvalCase


def _trivial_case() -> EvalCase:
    return EvalCase(
        case_id="trivial",
        description="a case",
        kind="minimal",
        build_state=CrateState,
    )


class _FakeAgent:
    """A minimal BuildAgent returning a fixed state and a session id."""

    def build(self, case: EvalCase) -> BuildOutcome:
        factory = case.build_state or CrateState
        return BuildOutcome(state=factory(), session_id="fake-session")


class TestBuildOutcome:
    def test_carries_state_and_optional_session_id(self) -> None:
        state = CrateState()
        outcome = BuildOutcome(state=state, session_id="s1")
        assert outcome.state is state
        assert outcome.session_id == "s1"

    def test_session_id_defaults_to_none(self) -> None:
        outcome = BuildOutcome(state=CrateState())
        assert outcome.session_id is None
        assert outcome.error is None


class TestBuildAgentProtocol:
    def test_fake_agent_satisfies_the_protocol(self) -> None:
        agent: BuildAgent = _FakeAgent()
        outcome = agent.build(_trivial_case())
        assert isinstance(outcome, BuildOutcome)
        assert isinstance(outcome.state, CrateState)
        assert outcome.session_id == "fake-session"

    def test_isinstance_check_against_runtime_protocol(self) -> None:
        # BuildAgent is a runtime_checkable Protocol, so duck typing is verifiable.
        assert isinstance(_FakeAgent(), BuildAgent)
