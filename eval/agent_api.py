"""The agent-agnostic build contract the harness measures against.

The whole point of the harness is to compare *architectures* — the prose-prompt
ReAct engine vs the deterministic pipeline (AGENTS.md §14). It does that
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
from typing import Any, Callable, Protocol, runtime_checkable

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
            a valid-at-the-cutoff run, **not** a clean stop — trap 2, #331), or
            ``"error"`` (the build raised), or ``"not_applicable"`` (the case
            asks for something this arm does not do — a conversational,
            no-input-directory build on the folder-driven pipeline; it is counted
            and named, never averaged in as a cheap win, #609). ``None`` when the
            producer does not classify termination (e.g. a mock). A ``"cap_hit"`` preserves the
            partial crate and leaves ``error`` unset, so the conformance predicate
            still measures what the run produced at the cap.
        pipeline_result: The deterministic spine's structured report — the dict
            ``run_pipeline`` returns (conformance, issues, materialization
            outcomes incl. ``condition_table``, ``data_issues``). ``None`` for
            producers that do not report one (the ReAct arm, mocks) — see #422.
    """

    state: CrateState
    session_id: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    pipeline_result: dict[str, Any] | None = None


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
