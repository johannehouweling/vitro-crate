"""Offline re-pricing of an existing eval report (#335).

The gpt-5.6-luna A/B re-run records per-case ``input_tokens`` / ``output_tokens``
but leaves ``cost_usd`` null (the model is deliberately unpriced in the public
table and the run passed no price override). Re-running the corpus purely to add a
cost column would spend real tokens for no new signal, so cost is re-derived here
**offline** from the numbers already in the report. The price is passed in, never
hardcoded, so no work-issued model's pricing enters the repo.
"""

from __future__ import annotations

import json

import pytest

from eval.reprice import reprice_file, reprice_main, reprice_records


def _case(**over):
    rec = {
        "record": "case",
        "case_id": "c",
        "input_tokens": 0,
        "output_tokens": 0,
        "model_name": "gpt-5.6-luna",
        "cost_usd": None,
    }
    rec.update(over)
    return rec


class TestRepriceRecords:
    def test_sets_case_cost_from_recorded_tokens(self) -> None:
        records = [_case(input_tokens=1_000_000, output_tokens=0)]
        out = reprice_records(records, price_input=1.10, price_output=6.60)
        assert out[0]["cost_usd"] == pytest.approx(1.10)

    def test_prices_output_tokens_too(self) -> None:
        records = [_case(input_tokens=0, output_tokens=1_000_000)]
        out = reprice_records(records, price_input=1.10, price_output=6.60)
        assert out[0]["cost_usd"] == pytest.approx(6.60)

    def test_recomputes_summary_total_as_sum_of_case_costs(self) -> None:
        records = [
            _case(input_tokens=1_000_000, output_tokens=0),
            _case(input_tokens=0, output_tokens=1_000_000),
            {"record": "summary", "total_cost_usd": None},
        ]
        out = reprice_records(records, price_input=1.10, price_output=6.60)
        summary = out[-1]
        assert summary["total_cost_usd"] == pytest.approx(1.10 + 6.60)

    def test_summary_total_is_none_when_no_cases(self) -> None:
        out = reprice_records(
            [{"record": "summary", "total_cost_usd": None}],
            price_input=1.10,
            price_output=6.60,
        )
        assert out[0]["total_cost_usd"] is None

    def test_leaves_non_cost_fields_untouched(self) -> None:
        records = [_case(input_tokens=10, output_tokens=5, success=True, tool_calls=3)]
        out = reprice_records(records, price_input=1.10, price_output=6.60)
        assert out[0]["success"] is True
        assert out[0]["tool_calls"] == 3
        assert out[0]["input_tokens"] == 10

    def test_does_not_mutate_the_input(self) -> None:
        records = [_case(input_tokens=1_000_000)]
        reprice_records(records, price_input=1.10, price_output=6.60)
        assert records[0]["cost_usd"] is None  # original untouched


class TestRepriceFile:
    def test_roundtrips_a_report_in_place(self, tmp_path) -> None:
        path = tmp_path / "r.ndjson"
        lines = [
            _case(input_tokens=1_000_000, output_tokens=0),
            {"record": "summary", "total_cost_usd": None},
        ]
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

        reprice_file(path, price_input=1.10, price_output=6.60)

        got = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        assert got[0]["cost_usd"] == pytest.approx(1.10)
        assert got[-1]["total_cost_usd"] == pytest.approx(1.10)

    def test_writes_to_out_when_given(self, tmp_path) -> None:
        src = tmp_path / "src.ndjson"
        dest = tmp_path / "dest.ndjson"
        src.write_text(json.dumps(_case(input_tokens=1_000_000)) + "\n", encoding="utf-8")

        reprice_file(src, price_input=1.10, price_output=6.60, out=dest)

        assert json.loads(src.read_text().strip())["cost_usd"] is None  # source untouched
        assert json.loads(dest.read_text().strip())["cost_usd"] == pytest.approx(1.10)


class TestRepriceCli:
    def test_cli_reprices_in_place(self, tmp_path) -> None:
        path = tmp_path / "r.ndjson"
        path.write_text(json.dumps(_case(input_tokens=1_000_000)) + "\n", encoding="utf-8")

        rc = reprice_main([str(path), "--price-input", "1.10", "--price-output", "6.60"])

        assert rc == 0
        assert json.loads(path.read_text().strip())["cost_usd"] == pytest.approx(1.10)

    def test_cli_requires_both_prices(self, tmp_path) -> None:
        path = tmp_path / "r.ndjson"
        path.write_text(json.dumps(_case()) + "\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            reprice_main([str(path), "--price-input", "1.10"])
