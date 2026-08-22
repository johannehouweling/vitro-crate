"""Report serialization for the evaluation harness.

Two outputs, both designed so a ReAct run and a later pipeline run **diff cleanly**:

* :func:`write_report` — one labeled ndjson per harness run: a ``record: "case"``
  line per case (carrying every metric) followed by one ``record: "summary"`` line
  with the aggregates. ndjson keeps each run append-friendly and trivially loadable.
* :func:`compare_reports` — a structured A/B diff of two labeled reports: both
  summaries side by side plus a per-case success/token table keyed by ``case_id``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from eval.runner import EvalReport

logger = logging.getLogger(__name__)


def write_report(report: EvalReport, path: str | Path) -> Path:
    """Write *report* to *path* as ndjson (case lines + a summary line).

    Each case becomes one ``{"record": "case", "label": ..., ...metrics}`` line;
    the run ends with one ``{"record": "summary", ...aggregates}`` line. Parent
    directories are created as needed.

    Args:
        report: The harness report to serialize.
        path: Destination ndjson file path.

    Returns:
        The resolved :class:`~pathlib.Path` written.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for result in report.results:
        record: dict[str, Any] = {"record": "case", "label": report.label}
        record.update(result.to_dict())
        lines.append(json.dumps(record, default=str))

    summary = {"record": "summary"}
    summary.update(report.summary())
    lines.append(json.dumps(summary, default=str))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote eval report (%d cases) to %s", len(report.results), out)
    return out


def compare_reports(*reports: EvalReport) -> dict[str, Any]:
    """Diff two or more labeled reports into a comparison-ready dict.

    Args:
        *reports: The labeled reports to compare (typically a ReAct baseline and
            a pipeline run).

    Returns:
        ``{"labels": [...], "summaries": {label: summary}, "cases": {case_id:
        {label: {success, total_tokens, latency_seconds, deterministic}}},
        "head_to_head": {"case_ids": [...], "summaries": {label: summary}}}``.

        ``summaries`` is each arm over what IT attempted; ``head_to_head`` is every
        arm over the cases they all attempted, which is the only one of the two
        whose numbers can be quoted against each other (#609).
    """
    labels = [r.label for r in reports]
    summaries = {r.label: r.summary() for r in reports}

    cases: dict[str, dict[str, Any]] = {}
    for report in reports:
        for result in report.results:
            per_label = cases.setdefault(result.case_id, {})
            per_label[report.label] = {
                "success": result.success,
                # Validity across ALL repeats (#405), beside repeat #1's verdict:
                # a case that conformed once in three must not diff as a clean win
                # against one that conformed three times in three.
                "success_per_repeat": result.success_per_repeat,
                "success_rate": result.success_rate,
                "always_succeeds": result.always_succeeds,
                "total_tokens": result.total_tokens,
                "latency_seconds": round(result.latency_seconds, 4),
                "iterations": result.iterations,
                "tool_calls": result.tool_calls,
                "deterministic": result.deterministic,
                # Efficiency / termination signals (#331): a cap_hit "win" is not a
                # clean win, and $ is the headline efficiency differentiator.
                "stop_reason": result.stop_reason,
                "model_name": result.model_name,
                "cost_usd": result.cost_usd,
                # Additive content-quality signal — ``None`` for cases that do not
                # declare a min_entities quota.
                "meets_quota": result.meets_quota,
                # The manuscript's evaluation axes (#474): per-parameter
                # propertyID-joined MIT coverage, and the row-level CSVW /
                # AI-readiness score (``None`` = not assessed on that arm).
                "mit_propertyid": result.mit_propertyid,
                "csvw_typing": result.csvw_typing,
                # Spread across repeats (#400). Without these the diff reports
                # means with no dispersion, so a real tweak is indistinguishable
                # from run-to-run noise on the stochastic ReAct arm. Note
                # ``stop_reasons`` (all repeats) beside ``stop_reason`` (repeat #1):
                # a case that self-terminated once and hit the cap twice must not
                # read as a clean win.
                "variance": result.variance(),
                "total_tokens_per_repeat": result.total_tokens_per_repeat,
                "latency_per_repeat": [round(x, 4) for x in result.latency_per_repeat],
                "stop_reasons": result.stop_reasons,
                "transient_retries": result.transient_retries,
                # Cost of every repeat, and the case's true spend (#401).
                "cost_usd_per_repeat": result.cost_usd_per_repeat,
                "total_cost_usd": result.total_cost_usd,
            }

    # The head-to-head: every arm's aggregate over the cases they ALL attempted.
    # An arm reports ``not_applicable`` for a case it does not do at all (the
    # folder-driven pipeline on a conversational case, #609), and that case is
    # already out of its own averages — but it is still in the other arm's, so
    # the per-arm summaries below have different denominators and cannot be read
    # against each other. This block can be (#609).
    attempted_by_all = {
        case_id
        for case_id, per_label in cases.items()
        if len(per_label) == len(reports)
        and all(v.get("stop_reason") != "not_applicable" for v in per_label.values())
    }
    head_to_head = {
        "case_ids": sorted(attempted_by_all),
        "summaries": {r.label: r.summary(case_ids=attempted_by_all) for r in reports},
    }

    return {
        "labels": labels,
        "summaries": summaries,
        "cases": cases,
        "head_to_head": head_to_head,
    }
