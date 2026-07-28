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
        stop_reason="cap_hit" if not success else "completed",
        model_name="gpt-4o",
        cost_usd=0.5,
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

    def test_per_case_carries_efficiency_and_termination_signals(self) -> None:
        # The A/B diff surfaces stop_reason / model / $ so a cap_hit "win" and the
        # cost gap are visible per case (#331).
        diff = compare_reports(_report("react-baseline"), _report("pipeline"))
        c2 = diff["cases"]["c2"]["react-baseline"]
        assert c2["stop_reason"] == "cap_hit"
        assert c2["model_name"] == "gpt-4o"
        assert c2["cost_usd"] == 0.5


def _noisy_case(
    case_id: str = "noisy",
    *,
    stop_reasons: list[str | None] | None = None,
) -> CaseResult:
    """A three-repeat case whose repeats genuinely diverge.

    Tokens and latency differ per repeat, and ``stop_reason`` (repeat #1) disagrees
    with the full ``stop_reasons`` list — the exact shape that makes a spread-blind
    comparison misleading.
    """
    return CaseResult(
        case_id=case_id,
        kind="minimal",
        success=True,
        conformance={"base": True, "isa": True, "tox": True},
        issues=[],
        input_tokens=100,
        output_tokens=0,
        tool_calls=5,
        iterations=3,
        latency_seconds=1.0,
        crate_hashes=["a", "b", "c"],
        deterministic=False,
        repeats=3,
        stop_reason="completed",
        model_name="gpt-4o",
        cost_usd=0.5,
        transient_retries=2,
        total_tokens_per_repeat=[100, 300, 200],
        latency_per_repeat=[1.0, 3.0, 2.0],
        stop_reasons=stop_reasons if stop_reasons is not None else ["completed"] * 3,
    )


def _noisy_report(label: str, **kwargs) -> EvalReport:
    return EvalReport(label=label, repeats=3, results=[_noisy_case(**kwargs)])


class TestCompareReportsCarriesSpread:
    """The A/B diff must show spread, not means alone (#400).

    ``--repeats`` defaults to 3 so variance can be reported; publishing only
    repeat #1 spends the compute and discards the result.
    """

    def test_per_case_carries_the_variance_block(self) -> None:
        diff = compare_reports(_noisy_report("react"), _noisy_report("pipeline"))
        variance = diff["cases"]["noisy"]["react"]["variance"]
        assert variance["total_tokens"]["mean"] == 200.0
        assert variance["total_tokens"]["min"] == 100
        assert variance["total_tokens"]["max"] == 300
        assert variance["total_tokens"]["stdev"] > 0
        assert set(variance["latency_seconds"]) == {"mean", "min", "max", "stdev"}

    def test_per_case_carries_every_repeat_not_just_the_first(self) -> None:
        entry = compare_reports(_noisy_report("react"))["cases"]["noisy"]["react"]
        assert entry["total_tokens_per_repeat"] == [100, 300, 200]
        assert entry["latency_per_repeat"] == [1.0, 3.0, 2.0]

    def test_per_case_carries_transient_retries(self) -> None:
        # A case that needed two network retries is not comparable to one that
        # needed none, even at identical tokens.
        entry = compare_reports(_noisy_report("react"))["cases"]["noisy"]["react"]
        assert entry["transient_retries"] == 2

    def test_mixed_termination_is_not_hidden_by_repeat_one(self) -> None:
        # The issue's headline case: self-terminated once, hit the cap twice. Read
        # through repeat #1's stop_reason alone that is a clean win; it is not.
        mixed = _noisy_report("react", stop_reasons=["completed", "cap_hit", "cap_hit"])
        entry = compare_reports(mixed)["cases"]["noisy"]["react"]
        assert entry["stop_reason"] == "completed"
        assert entry["stop_reasons"] == ["completed", "cap_hit", "cap_hit"]
        assert entry["stop_reasons"].count("cap_hit") == 2

    def test_variance_block_matches_the_ndjson_line(self) -> None:
        # One definition of spread: the compared entry and the written case line
        # must not drift apart.
        report = _noisy_report("react")
        entry = compare_reports(report)["cases"]["noisy"]["react"]
        assert entry["variance"] == report.results[0].to_dict()["variance"]

    def test_summary_already_carries_the_arm_level_cv(self) -> None:
        # Guards the claim in #400 that mean_total_tokens_cv "never reaches the
        # comparison" — it does, via the summaries block. Pinned so it stays true.
        diff = compare_reports(_noisy_report("react"))
        assert diff["summaries"]["react"]["mean_total_tokens_cv"] > 0

    def test_per_case_carries_cost_of_every_repeat(self) -> None:
        # The cost counterpart of the same fix (#401): a per-case entry shows what
        # every repeat cost, not repeat #1 alone.
        case = _noisy_case()
        case.cost_usd_per_repeat = [0.5, 1.0, 1.5]
        entry = compare_reports(EvalReport(label="react", repeats=3, results=[case]))
        assert entry["cases"]["noisy"]["react"]["cost_usd_per_repeat"] == [0.5, 1.0, 1.5]
        assert entry["cases"]["noisy"]["react"]["total_cost_usd"] == 3.0
