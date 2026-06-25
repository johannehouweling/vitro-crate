"""The agent-agnostic build contract the harness measures against.

The whole point of the harness is to compare *architectures* — today's prose-prompt
ReAct engine vs the planned deterministic pipeline (AGENTS.md §14). It does that
without knowing anything about either: it is handed a zero-arg ``agent_factory``
that returns a :class:`BuildAgent`, and it calls ``agent.build(case)`` once per
repeat. Swapping the factory swaps the architecture under test; the corpus, the
metrics, and the report are unchanged.

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
    """

    state: CrateState
    session_id: str | None = None
    error: str | None = None


@runtime_checkable
class BuildAgent(Protocol):
    """A thing that builds a crate from an :class:`~eval.corpus.EvalCase`.

    The single method the harness depends on. Implementations: the live ReAct
    factory (:mod:`eval.react_factory`), a future deterministic-pipeline factory,
    and the offline mock used by the tests.
    """

    def build(self, case: EvalCase) -> BuildOutcome:
        """Build a crate for *case* and return its outcome."""
        ...


# An agent_factory is any zero-arg callable returning a fresh BuildAgent.
AgentFactory = Callable[[], BuildAgent]
