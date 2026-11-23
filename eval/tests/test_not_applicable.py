"""A case the agent does not attempt is not a win (#609).

An agent may report a case ``not_applicable``. Averaged in, such a case scores
a capability gap as a cost win: $0, no tokens, and an empty crate that can still
pass conformance. The summary counts and names it instead, and
:func:`eval.report.compare_reports` quotes a head-to-head only over the cases
every run attempted.
"""

from __future__ import annotations

from eval.report import compare_reports
from eval.runner import CaseResult, EvalReport


def _report(label: str, rows: list[tuple[str, str, int, bool]]) -> EvalReport:
    return EvalReport(
        label=label,
        repeats=1,
        results=[
            CaseResult(
                case_id=cid, kind="structured", success=ok, conformance={}, issues=[],
                input_tokens=tok, output_tokens=0, tool_calls=0, iterations=0,
                latency_seconds=1.0, crate_hashes=[], deterministic=None, repeats=1,
                stop_reason=stop, total_tokens_per_repeat=[tok],
                success_per_repeat=[ok],
            )
            for cid, stop, tok, ok in rows
        ],
    )


class TestANotApplicableCaseIsLeftOut:
    def test_a_not_applicable_case_is_left_out_of_the_run_average(self) -> None:
        """Averaging in a case the agent did not attempt reports a capability
        gap as a cost win. It is counted and named instead."""
        run = EvalReport(
            label="run",
            repeats=1,
            results=[
                CaseResult(
                    case_id="real", kind="structured", success=True, conformance={},
                    issues=[], input_tokens=1000, output_tokens=500, tool_calls=4,
                    iterations=2, latency_seconds=10.0, crate_hashes=["a"],
                    deterministic=None, repeats=1, stop_reason="completed",
                    total_tokens_per_repeat=[1500],
                ),
                CaseResult(
                    case_id="prompt-only", kind="unstructured", success=False,
                    conformance={}, issues=[], input_tokens=0, output_tokens=0,
                    tool_calls=0, iterations=0, latency_seconds=0.0, crate_hashes=[],
                    deterministic=None, repeats=1, stop_reason="not_applicable",
                    total_tokens_per_repeat=[0],
                ),
            ],
        )
        summary = run.summary()

        assert summary["num_not_applicable"] == 1
        assert summary["num_cases_compared"] == 1
        assert summary["success_rate"] == 1.0, "the skipped case dragged the average"
        assert summary["mean_total_tokens"] == 1500.0


class TestTheHeadToHeadComparesLikeWithLike:
    """Excluding a case from ONE run's averages creates a second trap if the
    other run still counts it: two summaries side by side with different
    denominators read as a comparison and are not one. The head-to-head is
    computed over the cases EVERY run attempted (#609).
    """

    def test_head_to_head_uses_only_the_cases_every_run_attempted(self) -> None:
        full = _report("full", [("folder", "completed", 900, True),
                                ("chat", "completed", 5000, True)])
        partial = _report("partial", [("folder", "completed", 100, True),
                                      ("chat", "not_applicable", 0, False)])

        cmp = compare_reports(full, partial)

        assert cmp["head_to_head"]["case_ids"] == ["folder"]
        # The conversational case is one run's alone; folding it in would have
        # made the partial run look BETTER on success and the full run dearer.
        assert cmp["head_to_head"]["summaries"]["full"]["mean_total_tokens"] == 900.0
        assert cmp["head_to_head"]["summaries"]["partial"]["mean_total_tokens"] == 100.0
        assert cmp["head_to_head"]["summaries"]["partial"]["success_rate"] == 1.0
        assert cmp["head_to_head"]["summaries"]["full"]["success_rate"] == 1.0
        # The stop-reason counters are part of the same table and must be scoped
        # to the same cases — a count over all cases beside an average over some
        # is the very mismatch this block exists to remove.
        assert cmp["head_to_head"]["summaries"]["full"]["num_completed"] == 1
        assert cmp["head_to_head"]["summaries"]["partial"]["num_completed"] == 1
        assert cmp["head_to_head"]["summaries"]["partial"]["num_not_applicable"] == 0
