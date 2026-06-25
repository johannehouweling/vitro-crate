"""The harness runner — runs an agent over the corpus and reports metrics.

:func:`run_eval` is the single entry point. It is **agent-agnostic**: it takes a
zero-arg ``agent_factory`` (returns a :class:`~eval.agent_api.BuildAgent`), runs
each corpus case ``repeats`` times, and records per-case:

* **success** (bool) + the per-layer ``conformance`` map (via ``build_and_validate``);
* **tokens** (input / output / total) mined from the run's ``profile.ndjson``;
* **latency** (wall-clock seconds for the build);
* **iteration count** and **tool-call count** (also from ``profile.ndjson``);
* a **determinism** signal — a stable hash of the resulting crate ``@graph`` across
  repeats; identical hashes ⇒ deterministic.

The same call shape runs today's ReAct engine and a future pipeline — only the
factory changes — so a ReAct :class:`EvalReport` and a pipeline one diff cleanly.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from builder.state import CrateState
from eval.agent_api import AgentFactory, BuildOutcome
from eval.corpus import EvalCase, meets_entity_quota, reaches_isa_tox_conformance
from eval.metrics import crate_graph_hash, mine_profile_metrics

logger = logging.getLogger(__name__)

# A profile_reader maps a session_id to its parsed profile.ndjson records.
ProfileReader = Callable[[str], list[dict[str, Any]]]


def _default_profile_reader(session_id: str) -> list[dict[str, Any]]:
    """Read ``sessions/<session_id>/profile.ndjson`` into records (offline-safe).

    Returns an empty list when the file is absent — exactly what the mock-backed
    tests expect when an agent does not profile.
    """
    from builder.tools.dashboard import read_profile
    from builder.tools.profiler import SESSION_DIR

    if not session_id:
        return []
    return read_profile(SESSION_DIR / session_id / "profile.ndjson")


@dataclass
class CaseResult:
    """Per-case aggregated metrics across ``repeats`` runs.

    The token / iteration / tool-call figures come from the **first** repeat's
    profile (representative of one build); latency is the first repeat's wall
    clock. ``crate_hashes`` holds one hash per repeat, and ``deterministic`` is
    ``True``/``False`` when there is more than one repeat to compare, else ``None``.

    ``meets_quota`` / ``entity_counts`` are the additive content-quality signal:
    for cases that declare ``min_entities`` they say whether the build drafted the
    demanded domain content (and how much of each demanded type it drafted). For
    cases without a quota, ``meets_quota`` is ``None`` and ``entity_counts`` empty
    — the signal is purely additive and never affects ``success``.
    """

    case_id: str
    kind: str
    success: bool
    conformance: dict[str, bool]
    issues: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    tool_calls: int
    iterations: int
    latency_seconds: float
    crate_hashes: list[str]
    deterministic: bool | None
    repeats: int
    error: str | None = None
    meets_quota: bool | None = None
    entity_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Combined input + output token count for the representative run."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (one ndjson line per case)."""
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "success": self.success,
            "conformance": self.conformance,
            "num_issues": len(self.issues),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "iterations": self.iterations,
            "latency_seconds": round(self.latency_seconds, 4),
            "crate_hashes": self.crate_hashes,
            "deterministic": self.deterministic,
            "repeats": self.repeats,
            "error": self.error,
            "meets_quota": self.meets_quota,
            "entity_counts": self.entity_counts,
        }


@dataclass
class EvalReport:
    """The full report for one harness run (one architecture, one label)."""

    label: str
    repeats: int
    results: list[CaseResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Aggregate the per-case results into a comparison-ready dict.

        Keyed so a ReAct run and a pipeline run can be diffed field by field:
        success rate, mean/median tokens and latency, and the determinism rate
        (over cases where determinism is defined).
        """
        n = len(self.results)
        successes = sum(1 for r in self.results if r.success)
        totals = [r.total_tokens for r in self.results]
        latencies = [r.latency_seconds for r in self.results]
        decided = [r for r in self.results if r.deterministic is not None]
        det_rate = (
            sum(1 for r in decided if r.deterministic) / len(decided) if decided else None
        )
        return {
            "label": self.label,
            "repeats": self.repeats,
            "num_cases": n,
            "num_success": successes,
            "success_rate": (successes / n) if n else 0.0,
            "mean_total_tokens": statistics.mean(totals) if totals else 0.0,
            "median_total_tokens": statistics.median(totals) if totals else 0.0,
            "mean_latency_seconds": statistics.mean(latencies) if latencies else 0.0,
            "median_latency_seconds": statistics.median(latencies) if latencies else 0.0,
            "determinism_rate": det_rate if det_rate is not None else 0.0,
        }


def _safe_build(agent: Any, case: EvalCase) -> tuple[BuildOutcome, float]:
    """Run one build, returning its outcome and wall-clock latency.

    Any exception from the agent is captured into a failed ``BuildOutcome`` with an
    empty state so one bad case never aborts the whole sweep.
    """
    start = time.perf_counter()
    try:
        outcome = agent.build(case)
    except Exception as exc:  # noqa: BLE001 — a build crash is a measured failure
        logger.warning("Build raised for case %s: %s", case.case_id, exc)
        outcome = BuildOutcome(state=CrateState(), session_id=None, error=str(exc))
    latency = time.perf_counter() - start
    return outcome, latency


def _run_case(
    agent_factory: AgentFactory,
    case: EvalCase,
    *,
    repeats: int,
    profile_reader: ProfileReader,
) -> CaseResult:
    """Run a single case ``repeats`` times and aggregate its metrics."""
    hashes: list[str] = []
    first_outcome: BuildOutcome | None = None
    first_latency = 0.0
    error: str | None = None

    for i in range(max(1, repeats)):
        # A fresh agent per repeat — the determinism check must not be polluted by
        # carried-over engine/session state.
        agent = agent_factory()
        outcome, latency = _safe_build(agent, case)
        if i == 0:
            first_outcome, first_latency = outcome, latency
        if outcome.error and error is None:
            error = outcome.error
        hashes.append(crate_graph_hash(outcome.state))

    assert first_outcome is not None  # repeats >= 1 guarantees this
    state = first_outcome.state

    predicate = reaches_isa_tox_conformance(state)
    quota = meets_entity_quota(state, case.min_entities)

    profile_records = (
        profile_reader(first_outcome.session_id) if first_outcome.session_id else []
    )
    pm = mine_profile_metrics(profile_records)

    deterministic: bool | None = None
    if len(hashes) > 1:
        deterministic = len(set(hashes)) == 1

    return CaseResult(
        case_id=case.case_id,
        kind=case.kind,
        success=predicate["success"],
        conformance=predicate["conformance"],
        issues=predicate["issues"],
        input_tokens=pm.input_tokens,
        output_tokens=pm.output_tokens,
        tool_calls=pm.tool_calls,
        iterations=pm.iterations,
        latency_seconds=first_latency,
        crate_hashes=hashes,
        deterministic=deterministic,
        repeats=max(1, repeats),
        error=error,
        meets_quota=quota["meets_quota"],
        entity_counts=quota["entity_counts"],
    )


def run_eval(
    agent_factory: AgentFactory,
    corpus: tuple[EvalCase, ...] | list[EvalCase],
    *,
    repeats: int = 2,
    label: str = "eval",
    profile_reader: ProfileReader | None = None,
) -> EvalReport:
    """Run *agent_factory* over *corpus* and return an :class:`EvalReport`.

    Args:
        agent_factory: Zero-arg callable returning a fresh
            :class:`~eval.agent_api.BuildAgent`. Called once per repeat per case
            so no state leaks between runs (critical for the determinism signal).
        corpus: The cases to evaluate (e.g. ``eval.corpus.DEFAULT_CORPUS``).
        repeats: How many times to build each case (>= 1). With ``repeats == 1``
            determinism is undefined and reported as ``None``.
        label: A run label (e.g. ``"react-baseline"``) recorded in the report.
        profile_reader: Override for reading a session's profile records (the
            tests inject a canned reader); defaults to reading
            ``sessions/<id>/profile.ndjson`` from disk.

    Returns:
        An :class:`EvalReport` with one :class:`CaseResult` per case.
    """
    reader = profile_reader or _default_profile_reader
    results = [
        _run_case(agent_factory, case, repeats=repeats, profile_reader=reader)
        for case in corpus
    ]
    return EvalReport(label=label, repeats=repeats, results=results)
