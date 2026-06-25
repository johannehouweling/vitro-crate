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
        {label: {success, total_tokens, latency_seconds, deterministic}}}}``.
    """
    labels = [r.label for r in reports]
    summaries = {r.label: r.summary() for r in reports}

    cases: dict[str, dict[str, Any]] = {}
    for report in reports:
        for result in report.results:
            per_label = cases.setdefault(result.case_id, {})
            per_label[report.label] = {
                "success": result.success,
                "total_tokens": result.total_tokens,
                "latency_seconds": round(result.latency_seconds, 4),
                "iterations": result.iterations,
                "tool_calls": result.tool_calls,
                "deterministic": result.deterministic,
            }

    return {"labels": labels, "summaries": summaries, "cases": cases}
