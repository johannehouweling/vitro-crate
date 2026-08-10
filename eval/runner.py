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
from eval.scorers import csvw_air_score, mit_propertyid_coverage

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
    repeat (trap 4, #335); :meth:`variance` derives a mean ± spread block from them.
    The headline fields above stay repeat #1's so existing consumers are unchanged.

    Cost follows the same additive shape (#401): ``cost_usd`` remains repeat #1's
    — one representative build, consistent with the token fields — while
    ``cost_usd_per_repeat`` prices **every** repeat and :attr:`total_cost_usd` sums
    them into what the run actually spent. ``input_tokens_per_repeat`` /
    ``output_tokens_per_repeat`` are recorded alongside so an offline reprice
    (:mod:`eval.reprice`) can rebuild that total under a different price instead of
    silently falling back to repeat #1.

    ``meets_quota`` / ``entity_counts`` are the additive content-quality signal:
    for cases that declare ``min_entities`` they say whether the build drafted the
    demanded domain content (and how much of each demanded type it drafted). For
    cases without a quota, ``meets_quota`` is ``None`` and ``entity_counts`` empty
    — the signal is purely additive and never affects ``success``.

    ``mit_propertyid`` / ``csvw_air`` are the manuscript's two evaluation axes
    (#474, :mod:`eval.scorers`), additive in the same mould: per-parameter MIT
    coverage joined via ``schema:propertyID``, and the row-level CSVW /
    AI-readiness score. Headlines are repeat #1's; ``*_per_repeat`` record every
    repeat (#405). ``csvw_air`` is ``None`` — "not assessed" — for an arm with
    no pipeline condition-table report (ReAct, mocks); ``mit_propertyid`` is
    ``None`` when the crate graph could not be assembled or the MIT YAML
    offers no joinable parameters — not assessed, never a fabricated zero.
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
    input_tokens_per_repeat: list[int] = field(default_factory=list)
    output_tokens_per_repeat: list[int] = field(default_factory=list)
    cost_usd_per_repeat: list[float | None] = field(default_factory=list)
    success_per_repeat: list[bool] = field(default_factory=list)
    conformance_per_repeat: list[dict[str, bool]] = field(default_factory=list)
    meets_quota_per_repeat: list[bool | None] = field(default_factory=list)
    mit_propertyid: float | None = None
    csvw_air: float | None = None
    mit_propertyid_detail: dict[str, Any] = field(default_factory=dict)
    csvw_air_detail: dict[str, Any] = field(default_factory=dict)
    mit_propertyid_per_repeat: list[float | None] = field(default_factory=list)
    csvw_air_per_repeat: list[float | None] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        """Combined input + output token count for the representative run."""
        return self.input_tokens + self.output_tokens

    @property
    def success_rate(self) -> float:
        """Fraction of this case's repeats that reached conformance (#405).

        ``success`` above is repeat #1's representative verdict; this is what the
        case actually achieved. A 1-in-3 case is ``0.33`` here and ``True`` there,
        and the difference is the whole point: intermittent conformance is the
        stochastic arm's characteristic failure mode, and it used to leave no trace
        in the report at all. Falls back to :attr:`success` when no per-repeat
        verdicts were recorded (a hand-built or pre-#405 result).
        """
        if not self.success_per_repeat:
            return 1.0 if self.success else 0.0
        return sum(1 for s in self.success_per_repeat if s) / len(self.success_per_repeat)

    @property
    def always_succeeds(self) -> bool:
        """True when EVERY repeat conformed — the strict reading (#405).

        Distinct from :attr:`deterministic`, which says the repeats produced an
        identical graph. A case can be non-deterministic yet always conformant, or
        non-deterministic and conformant only sometimes; those are very different
        results and read identically without this.
        """
        if not self.success_per_repeat:
            return self.success
        return all(self.success_per_repeat)

    @property
    def total_cost_usd(self) -> float | None:
        """USD actually spent on this case across **all** repeats (#401).

        Sums the priced repeats in :attr:`cost_usd_per_repeat`. ``None`` when no
        repeat had a known price, so an unpriced case reads as "cost unknown"
        rather than a misleading ``$0`` (trap 5). Falls back to the representative
        :attr:`cost_usd` when no per-repeat prices were recorded at all — a
        hand-built or pre-#401 result, where one draw is the only figure there is.
        """
        known = [c for c in self.cost_usd_per_repeat if c is not None]
        if known:
            return sum(known)
        return None if self.cost_usd_per_repeat else self.cost_usd

    def variance(self) -> dict[str, dict[str, float]]:
        """Mean ± spread across repeats for the noisy metrics (trap 4, #335).

        The single definition shared by :meth:`to_dict` (the ndjson case line) and
        :func:`eval.report.compare_reports` (the A/B diff), so the written report
        and the comparison can never drift apart.
        """
        return {
            "total_tokens": _spread(self.total_tokens_per_repeat),
            "latency_seconds": _spread(self.latency_per_repeat),
        }

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
            "input_tokens_per_repeat": self.input_tokens_per_repeat,
            "output_tokens_per_repeat": self.output_tokens_per_repeat,
            "cost_usd_per_repeat": self.cost_usd_per_repeat,
            "total_cost_usd": self.total_cost_usd,
            "success_per_repeat": self.success_per_repeat,
            "conformance_per_repeat": self.conformance_per_repeat,
            "meets_quota_per_repeat": self.meets_quota_per_repeat,
            "mit_propertyid": self.mit_propertyid,
            "csvw_air": self.csvw_air,
            "mit_propertyid_detail": self.mit_propertyid_detail,
            "csvw_air_detail": self.csvw_air_detail,
            "mit_propertyid_per_repeat": self.mit_propertyid_per_repeat,
            "csvw_air_per_repeat": self.csvw_air_per_repeat,
            "success_rate": self.success_rate,
            "always_succeeds": self.always_succeeds,
            "variance": self.variance(),
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

        ``success_rate`` is the share of **all** (case x repeat) builds that reached
        conformance — not repeat #1's pass rate (#405). ``num_success_all_repeats``
        (every repeat conformed) and ``num_success_any_repeat`` (at least one did)
        give the strict and optimistic readings alongside it; a flaky case shows up
        as the gap between them.
        """
        n = len(self.results)
        # ``success_rate`` spans repeats (#405): the mean of each case's own
        # conformant fraction, i.e. the share of all (case x repeat) builds that
        # worked. It used to be repeat #1's pass rate under a name every reader
        # takes to mean "how often this architecture works" — a 1-in-3 case scored
        # a full 1.0. The mean is chosen over any-repeat (optimistic, the old
        # behaviour by accident) and all-repeat (strict, but 1-of-3 and 2-of-3
        # collapse to the same 0) because it is the only one that preserves
        # magnitude AND stays comparable across different --repeats. Both stricter
        # readings are still reported below, named for what they count.
        success_rate = statistics.mean([r.success_rate for r in self.results]) if n else 0.0
        num_success_all_repeats = sum(1 for r in self.results if r.always_succeeds)
        num_success_any_repeat = sum(1 for r in self.results if r.success_rate > 0)
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
        # Each case contributes ALL its repeats (#401) — this is the run's actual
        # spend, matching what the API bills, not one repeat presented as the total.
        known_costs = [r.total_cost_usd for r in self.results if r.total_cost_usd is not None]
        total_cost_usd = sum(known_costs) if known_costs else None
        # The same money divided by the repeat count, so runs made with different
        # ``--repeats`` remain comparable to each other.
        mean_cost_usd_per_repeat = (
            total_cost_usd / max(1, self.repeats) if total_cost_usd is not None else None
        )
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
            # Both readings, each named for exactly what it counts (#405). The bare
            # ``num_success`` they replace could not say which one it meant.
            "num_success_all_repeats": num_success_all_repeats,
            "num_success_any_repeat": num_success_any_repeat,
            "success_rate": success_rate,
            "mean_total_tokens": statistics.mean(totals) if totals else 0.0,
            "median_total_tokens": statistics.median(totals) if totals else 0.0,
            "mean_latency_seconds": statistics.mean(latencies) if latencies else 0.0,
            "median_latency_seconds": statistics.median(latencies) if latencies else 0.0,
            "determinism_rate": det_rate if det_rate is not None else 0.0,
            "num_completed": num_completed,
            "num_cap_hit": num_cap_hit,
            "num_error": num_error,
            "total_cost_usd": total_cost_usd,
            "mean_cost_usd_per_repeat": mean_cost_usd_per_repeat,
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


def _assembled_graph(state: CrateState) -> list[dict[str, Any]] | None:
    """Assemble *state*'s ``@graph`` once, for the hash AND the #474 scorers.

    The identical assembly :func:`crate_graph_hash` performs internally —
    sharing it halves the per-repeat assembly cost. ``None`` on failure: the
    hash then falls back to its own degraded path and the scorer axes read
    not-assessed; additive axes never fail the harness.
    """
    try:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        doc = crate.metadata.generate()
        graph = doc.get("@graph")
        return graph if isinstance(graph, list) else None
    except Exception as exc:  # noqa: BLE001 - additive axes never fail the harness
        logger.warning("Graph assembly for scoring failed: %s", exc)
        return None


def _condition_table_report(outcome: BuildOutcome) -> dict[str, Any] | None:
    """The build's own condition-table record, or ``None`` when the arm has none.

    Lives at ``pipeline_result["materialized"]["condition_table"]`` — the field
    the pipeline arm reports and the ReAct arm (and mocks) leave ``None``, which
    is exactly the "not assessed" signal :func:`eval.scorers.csvw_air_score`
    expects.
    """
    result = outcome.pipeline_result
    if not isinstance(result, dict):
        return None
    materialized = result.get("materialized")
    if not isinstance(materialized, dict):
        return None
    table = materialized.get("condition_table")
    return table if isinstance(table, dict) else None


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
    repeat_graphs: list[list[dict[str, Any]] | None] = []
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
        repeat_graphs.append(_assembled_graph(outcome.state))
        hashes.append(crate_graph_hash(outcome.state, graph=repeat_graphs[-1]))

    first_outcome, first_latency = repeat_runs[0]  # repeats >= 1 guarantees one run

    # Score EVERY repeat (#405) — without it a case that conformed once in three
    # contributed a full 1.0 to success_rate and its two failures left no trace.
    #
    # Memoised on the crate hash, which is ALREADY computed above for the
    # determinism signal. The predicate is not free: `reaches_isa_tox_conformance`
    # runs a full 3-pass SHACL `build_and_validate`, so scoring naively would
    # triple this harness's validation cost at the default --repeats 3. Two repeats
    # with an identical canonical @graph cannot disagree about conformance, so the
    # verdict is computed once per DISTINCT crate: a deterministic arm pays exactly
    # what it paid before, and a stochastic one pays only for crates that genuinely
    # differ.
    scored: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    predicates: list[dict[str, Any]] = []
    quotas: list[dict[str, Any]] = []
    # The manuscript axes (#474) join against the assembled graph the build
    # loop above already produced for the hash (one assembly per repeat, shared).
    # The MIT score is memoised on the digest like the conformance predicate;
    # csvw_air additionally depends on the repeat's OWN pipeline report (the
    # condition-table path is per-session), so it is computed per repeat. Both
    # are guarded — one bad crate or CSV must never abort a multi-case run.
    mit_by_digest: dict[str, tuple[float | None, dict[str, Any]]] = {}
    mit_scores: list[float | None] = []
    air_scores: list[float | None] = []
    air_details: list[dict[str, Any]] = []
    for (outcome_i, _), digest, graph_i in zip(
        repeat_runs, hashes, repeat_graphs, strict=True
    ):
        verdict = scored.get(digest)
        if verdict is None:
            verdict = (
                reaches_isa_tox_conformance(outcome_i.state),
                meets_entity_quota(outcome_i.state, case.min_entities),
            )
            scored[digest] = verdict
        predicates.append(verdict[0])
        quotas.append(verdict[1])

        if digest not in mit_by_digest:
            if graph_i is None:
                mit_by_digest[digest] = (None, {})
            else:
                try:
                    mit = mit_propertyid_coverage(graph_i)
                    mit_by_digest[digest] = (
                        mit["coverage"],
                        {
                            "covered": mit["covered"],
                            "joinable": mit["joinable"],
                            "covered_ids": [
                                p["id"] for p in mit["per_param"] if p["bound"]
                            ],
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - additive axes never fail the harness
                    logger.warning("MIT scoring failed for %s: %s", case.case_id, exc)
                    mit_by_digest[digest] = (None, {})
        mit_scores.append(mit_by_digest[digest][0])

        if graph_i is None:
            air_scores.append(None)
            air_details.append({})
        else:
            try:
                air = csvw_air_score(
                    outcome_i.state, graph_i, _condition_table_report(outcome_i)
                )
                air_scores.append(air["score"])
                air_details.append({"reason": air["reason"], "columns": air["columns"]})
            except Exception as exc:  # noqa: BLE001 - additive axes never fail the harness
                logger.warning("CSVW/AIR scoring failed for %s: %s", case.case_id, exc)
                air_scores.append(None)
                air_details.append({})
    predicate = predicates[0]
    quota = quotas[0]

    # Mine every repeat's profile so the report carries the spread, not just one
    # noisy draw (trap 4, #335). The representative headline fields below stay
    # repeat #1's (dashboards read those) — the per-repeat arrays are additive.
    per_repeat_metrics = [
        mine_profile_metrics(profile_reader(o.session_id) if o.session_id else [])
        for o, _ in repeat_runs
    ]
    pm = per_repeat_metrics[0]
    # Price EVERY repeat (#401). Summing these is what the run actually cost;
    # ``cost_usd`` below stays repeat #1's representative figure.
    cost_usd_per_repeat = [
        compute_cost(m.input_tokens, m.output_tokens, m.model_name, price_override=price_override)
        for m in per_repeat_metrics
    ]
    cost_usd = cost_usd_per_repeat[0]

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
        input_tokens_per_repeat=[m.input_tokens for m in per_repeat_metrics],
        output_tokens_per_repeat=[m.output_tokens for m in per_repeat_metrics],
        cost_usd_per_repeat=cost_usd_per_repeat,
        success_per_repeat=[bool(p["success"]) for p in predicates],
        conformance_per_repeat=[dict(p["conformance"]) for p in predicates],
        meets_quota_per_repeat=[q["meets_quota"] for q in quotas],
        mit_propertyid=mit_scores[0],
        csvw_air=air_scores[0],
        mit_propertyid_detail=mit_by_digest[hashes[0]][1],
        csvw_air_detail=air_details[0],
        mit_propertyid_per_repeat=mit_scores,
        csvw_air_per_repeat=air_scores,
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
