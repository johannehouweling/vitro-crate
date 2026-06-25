"""Agent-agnostic A/B evaluation harness for vitro-crate (Issue #179, task 6).

This package measures a crate-building *agent* against a fixed corpus of cases,
recording success, token cost, latency, iteration / tool-call counts, and a
determinism signal. The same harness runs the current ReAct engine today and a
future deterministic pipeline tomorrow — the only thing that changes is the
zero-arg ``agent_factory`` passed to :func:`eval.runner.run_eval`.

CI / unit tests stay strictly offline (mock agent factory); a *live* run makes
real LLM calls and is triggered by a human with credentials (``python -m eval``).
"""

from __future__ import annotations

from eval.agent_api import AgentFactory, BuildAgent, BuildOutcome
from eval.corpus import DEFAULT_CORPUS, EvalCase, reaches_isa_tox_conformance
from eval.metrics import ProfileMetrics, crate_graph_hash, mine_profile_metrics
from eval.report import compare_reports, write_report
from eval.runner import CaseResult, EvalReport, run_eval

__all__ = [
    "AgentFactory",
    "BuildAgent",
    "BuildOutcome",
    "CaseResult",
    "DEFAULT_CORPUS",
    "EvalCase",
    "EvalReport",
    "ProfileMetrics",
    "compare_reports",
    "crate_graph_hash",
    "mine_profile_metrics",
    "reaches_isa_tox_conformance",
    "run_eval",
    "write_report",
]
