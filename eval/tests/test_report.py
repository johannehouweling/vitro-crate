"""Tests for report serialization (ndjson + summary), fully offline.

These build an :class:`~eval.runner.EvalReport` by hand (no agent, no validation)
and assert the on-disk shape: one ndjson line per case plus a trailing summary
line, and a comparison helper that diffs two labeled reports.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.report import compare_reports, write_report
from eval.runner import CaseResult, EvalReport


def _case(case_id: str, *, success: bool, tokens: int, det: bool | None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        kind="minimal",
        success=success,
        conformance={"base": True, "isa": success, "tox": success},
        issues=[],
        input_tokens=tokens,
        output_tokens=tokens // 2,
        tool_calls=5,
        iterations=3,
        latency_seconds=1.25,
        crate_hashes=["abc", "abc"] if det else ["abc", "xyz"],
        deterministic=det,
        repeats=2,
    )


def _report(label: str) -> EvalReport:
    return EvalReport(
        label=label,
        repeats=2,
        results=[
            _case("c1", success=True, tokens=100, det=True),
            _case("c2", success=False, tokens=200, det=False),
        ],
    )


class TestWriteReport:
    def test_writes_one_ndjson_line_per_case_plus_summary(self, tmp_path: Path) -> None:
        out = tmp_path / "react-baseline.ndjson"
        write_report(_report("react-baseline"), out)

        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        # 2 case lines + 1 summary line.
        assert len(lines) == 3
        records = [line for line in lines if line.get("record") == "case"]
        summaries = [line for line in lines if line.get("record") == "summary"]
        assert len(records) == 2
        assert len(summaries) == 1

    def test_case_lines_carry_metrics(self, tmp_path: Path) -> None:
        out = tmp_path / "r.ndjson"
        write_report(_report("r"), out)
        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        c1 = next(line for line in lines if line.get("case_id") == "c1")
        assert c1["success"] is True
        assert c1["total_tokens"] == 150
        assert c1["deterministic"] is True
        assert c1["label"] == "r"

    def test_summary_line_has_aggregate_fields(self, tmp_path: Path) -> None:
        out = tmp_path / "r.ndjson"
        write_report(_report("r"), out)
        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        summary = next(line for line in lines if line.get("record") == "summary")
        assert summary["label"] == "r"
        assert summary["num_cases"] == 2
        assert summary["success_rate"] == 0.5
        assert "mean_total_tokens" in summary

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deep" / "r.ndjson"
        write_report(_report("r"), out)
        assert out.exists()


class TestCompareReports:
    def test_diffs_two_labeled_reports_per_metric(self) -> None:
        react = _report("react-baseline")
        pipeline = _report("pipeline")
        diff = compare_reports(react, pipeline)
        assert diff["labels"] == ["react-baseline", "pipeline"]
        # Per-metric side-by-side with both summaries present.
        assert "react-baseline" in diff["summaries"]
        assert "pipeline" in diff["summaries"]
        # A per-case table keyed by case_id, success for both labels.
        assert "cases" in diff
        assert set(diff["cases"]) == {"c1", "c2"}
