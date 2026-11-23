"""The agent-agnostic build contract the harness measures against.

The harness measures a build without knowing how it works: it is handed a
zero-arg ``agent_factory`` that returns a :class:`BuildAgent`, and it calls
``agent.build(case)`` once per repeat. The live factory drives the ReAct build
(:mod:`eval.react_factory`); the offline tests hand in mocks. The corpus, the
metrics, and the report are the same either way.

A :class:`BuildAgent` returns a :class:`BuildOutcome`: the finished
:class:`~builder.state.CrateState` plus an optional ``session_id`` the runner uses
to locate the run's ``profile.ndjson`` (the source of token / latency / iteration /
tool-call metrics). A failed build sets ``error`` and may return a partial state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from builder.state import CrateState
from eval.corpus import EvalCase


@dataclass
class BuildOutcome:
    """The result of one agent build.

    Attributes:
        state: The crate state the agent produced (possibly partial on error).
        session_id: The build's session id, used to find its ``profile.ndjson``.
            ``None`` when the agent does not profile (e.g. a mock).
        error: A short error string if the build raised / failed, else ``None``.
        stop_reason: How the build terminated — ``"completed"`` (the agent
            self-terminated), ``"cap_hit"`` (the ReAct loop hit its recursion cap;
            a valid-at-the-cutoff run, **not** a clean stop — trap 2, #331),
            ``"error"`` (the build raised), or ``"not_applicable"`` (the agent
            does not attempt the case; it is counted and named, never averaged in
            as a cheap win, #609). ``None`` when the producer does not classify
            termination (e.g. a mock). A ``"cap_hit"`` preserves the partial crate
            and leaves ``error`` unset, so the conformance predicate still
            measures what the run produced at the cap.
    """

    state: CrateState
    session_id: str | None = None
    error: str | None = None
    stop_reason: str | None = None


@runtime_checkable
class BuildAgent(Protocol):
    """A thing that builds a crate from an :class:`~eval.corpus.EvalCase`.

    The single method the harness depends on. Implementations: the live ReAct
    factory (:mod:`eval.react_factory`) and the offline mocks used by the tests.
    """

    def build(self, case: EvalCase) -> BuildOutcome:
        """Build a crate for *case* and return its outcome."""
        ...


# An agent_factory is any zero-arg callable returning a fresh BuildAgent.
AgentFactory = Callable[[], BuildAgent]
