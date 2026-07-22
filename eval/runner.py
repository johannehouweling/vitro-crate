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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from builder.state import CrateState
from eval.agent_api import AgentFactory, BuildOutcome
from eval.corpus import EvalCase, meets_entity_quota, reaches_isa_tox_conformance
from eval.metrics import compute_cost, crate_graph_hash, mine_profile_metrics

logger = logging.getLogger(__name__)

# A profile_reader maps a session_id to its parsed profile.ndjson records.
ProfileReader = Callable[[str], list[dict[str, Any]]]


def _spread(values: Sequence[float]) -> dict[str, float]:
    """Return ``{mean, min, max, stdev}`` for *values* (trap 4, #335).

    ``stdev`` is the sample standard deviation, and ``0.0`` for fewer than two
    samples (a single draw has no spread). An empty sequence yields all-zeros so
    the report always carries the block, never a missing key.
    """
    if not values:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "stdev": 0.0}
    return {
        "mean": statistics.mean(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


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

    For the stochastic ReAct arm one draw is noisy, so ``total_tokens_per_repeat``
    / ``latency_per_repeat`` / ``stop_reasons`` additionally record **every**
    repeat (trap 4, #335); :meth:`to_dict` derives a mean ± spread block from them.
    The headline fields above stay repeat #1's so existing consumers are unchanged.

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
    stop_reason: str | None = None
    model_name: str | None = None
    cost_usd: float | None = None
    transient_retries: int = 0
    total_tokens_per_repeat: list[int] = field(default_factory=list)
    latency_per_repeat: list[float] = field(default_factory=list)
    stop_reasons: list[str | None] = field(default_factory=list)

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
            "stop_reason": self.stop_reason,
            "model_name": self.model_name,
            "cost_usd": self.cost_usd,
            "transient_retries": self.transient_retries,
            "total_tokens_per_repeat": self.total_tokens_per_repeat,
            "latency_per_repeat": [round(x, 4) for x in self.latency_per_repeat],
            "stop_reasons": self.stop_reasons,
            "variance": {
                "total_tokens": _spread(self.total_tokens_per_repeat),
                "latency_seconds": _spread(self.latency_per_repeat),
            },
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
        det_rate = sum(1 for r in decided if r.deterministic) / len(decided) if decided else None
        # Stop-reason breakdown (trap 2): a self-terminated run is a clean win; a
        # cap_hit is only valid-at-the-cutoff. Counting them keeps a ReAct "win" at
        # the recursion cap from reading as an unqualified success.
        num_cap_hit = sum(1 for r in self.results if r.stop_reason == "cap_hit")
        num_completed = sum(1 for r in self.results if r.stop_reason == "completed")
        num_error = sum(1 for r in self.results if r.stop_reason == "error")
        # Total $ over cases with a KNOWN price; ``None`` when no case was priced,
        # so an unpriced run reads as "cost unknown", not a misleading $0 (trap 5).
        known_costs = [r.cost_usd for r in self.results if r.cost_usd is not None]
        total_cost_usd = sum(known_costs) if known_costs else None
        # Per-repeat spread (trap 4, #335): the mean coefficient of variation of
        # total tokens across cases — one arm-level "how stochastic" number. ~0 for
        # a deterministic arm, high for a stochastic one. A case with a zero mean (no
        # model call) contributes CV 0 rather than dividing by zero.
        cvs: list[float] = []
        for r in self.results:
            spread = _spread(r.total_tokens_per_repeat)
            mean_tok = spread["mean"]
            cvs.append(spread["stdev"] / mean_tok if mean_tok else 0.0)
        mean_total_tokens_cv = statistics.mean(cvs) if cvs else 0.0
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
            "num_completed": num_completed,
            "num_cap_hit": num_cap_hit,
            "num_error": num_error,
            "total_cost_usd": total_cost_usd,
            "mean_total_tokens_cv": mean_total_tokens_cv,
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
        outcome = BuildOutcome(
            state=CrateState(), session_id=None, error=str(exc), stop_reason="error"
        )
    latency = time.perf_counter() - start
    return outcome, latency


# Substrings (case-insensitive) that mark a build error as a **transient**
# network/API failure rather than an architecture failure (trap 1). Named reason
# phrases only — no bare numeric HTTP codes, which false-positive on unrelated text
# (e.g. "500 entities"). A run whose error matches is re-run before it counts.
_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "connection error",
    "connection reset",
    "connection aborted",
    "connection refused",
    "econnreset",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "internal server error",
    "server error",
    "bad gateway",
    "gateway timeout",
    "rate limit",
    "too many requests",
    "overloaded",
    "try again",
)


def _is_transient_error(error: str | None) -> bool:
    """Return True if *error* looks like a transient network/API failure (trap 1).

    A transient error (connection drop, timeout, rate limit, 5xx, overload) is a
    property of the network, not of the architecture under test, so the harness
    re-runs it rather than scoring it as a build failure. Matching is on named
    reason phrases (see :data:`_TRANSIENT_ERROR_MARKERS`); anything else — a
    validation failure, a ``KeyError``, a non-conformant crate — is a genuine,
    measured result and is never retried.
    """
    if not error:
        return False
    text = error.lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _build_with_transient_retries(
    agent_factory: AgentFactory,
    case: EvalCase,
    *,
    max_transient_retries: int,
) -> tuple[BuildOutcome, float, int]:
    """Build one repeat, re-running on a transient error (trap 1).

    Returns the accepted ``(outcome, latency, transient_retries)``. A fresh agent
    is built for each attempt. Only a :func:`_is_transient_error` failure is
    retried, and at most *max_transient_retries* times; a genuine failure (or a
    success) is returned immediately.
    """
    transient_retries = 0
    while True:
        agent = agent_factory()
        outcome, latency = _safe_build(agent, case)
        if (
            outcome.error
            and _is_transient_error(outcome.error)
            and transient_retries < max_transient_retries
        ):
            transient_retries += 1
            logger.warning(
                "Transient failure on case %s (retry %d/%d): %s",
                case.case_id,
                transient_retries,
                max_transient_retries,
                outcome.error,
            )
            continue
        return outcome, latency, transient_retries


def _run_case(
    agent_factory: AgentFactory,
    case: EvalCase,
    *,
    repeats: int,
    profile_reader: ProfileReader,
    price_override: tuple[float, float] | None = None,
    max_transient_retries: int = 2,
) -> CaseResult:
    """Run a single case ``repeats`` times and aggregate its metrics."""
    hashes: list[str] = []
    repeat_runs: list[tuple[BuildOutcome, float]] = []
    error: str | None = None
    transient_retries = 0

    for _ in range(max(1, repeats)):
        # A fresh agent per repeat — the determinism check must not be polluted by
        # carried-over engine/session state. Each repeat re-runs transient failures
        # (trap 1) so a network blip is not scored as an architecture failure.
        outcome, latency, retries = _build_with_transient_retries(
            agent_factory, case, max_transient_retries=max_transient_retries
        )
        transient_retries += retries
        repeat_runs.append((outcome, latency))
        if outcome.error and error is None:
            error = outcome.error
        hashes.append(crate_graph_hash(outcome.state))

    first_outcome, first_latency = repeat_runs[0]  # repeats >= 1 guarantees one run
    state = first_outcome.state

    predicate = reaches_isa_tox_conformance(state)
    quota = meets_entity_quota(state, case.min_entities)

    # Mine every repeat's profile so the report carries the spread, not just one
    # noisy draw (trap 4, #335). The representative headline fields below stay
    # repeat #1's (cost, dashboards read those) — the per-repeat arrays are additive.
    per_repeat_metrics = [
        mine_profile_metrics(profile_reader(o.session_id) if o.session_id else [])
        for o, _ in repeat_runs
    ]
    pm = per_repeat_metrics[0]
    cost_usd = compute_cost(
        pm.input_tokens, pm.output_tokens, pm.model_name, price_override=price_override
    )

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
        stop_reason=first_outcome.stop_reason,
        model_name=pm.model_name,
        cost_usd=cost_usd,
        transient_retries=transient_retries,
        total_tokens_per_repeat=[m.total_tokens for m in per_repeat_metrics],
        latency_per_repeat=[lat for _, lat in repeat_runs],
        stop_reasons=[o.stop_reason for o, _ in repeat_runs],
    )


def run_eval(
    agent_factory: AgentFactory,
    corpus: tuple[EvalCase, ...] | list[EvalCase],
    *,
    repeats: int = 2,
    label: str = "eval",
    profile_reader: ProfileReader | None = None,
    price_override: tuple[float, float] | None = None,
    max_transient_retries: int = 2,
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
        price_override: ``(input_per_mtok, output_per_mtok)`` USD prices applied to
            every case's model (trap 5). When ``None``, cost is priced from
            :data:`eval.metrics.MODEL_PRICES` and left ``None`` for unpriced models.
        max_transient_retries: How many times to re-run a case on a *transient*
            network/API failure before it counts (trap 1). ``0`` disables retries.

    Returns:
        An :class:`EvalReport` with one :class:`CaseResult` per case.
    """
    reader = profile_reader or _default_profile_reader
    results = [
        _run_case(
            agent_factory,
            case,
            repeats=repeats,
            profile_reader=reader,
            price_override=price_override,
            max_transient_retries=max_transient_retries,
        )
        for case in corpus
    ]
    return EvalReport(label=label, repeats=repeats, results=results)
