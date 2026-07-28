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


class TestRepriceAcrossRepeats:
    """A reprice must rebuild the ALL-repeat total, not repeat #1's (#401).

    ``reprice_records`` re-derives cost from the tokens already in the report. If
    it only ever read the repeat-#1 token fields it would quietly re-introduce the
    understatement #401 fixes, on every report it touched.
    """

    def _multi_repeat_case(self):
        # 1M / 2M / 3M input tokens at $2.50/Mtok -> $2.50 / $5.00 / $7.50.
        return _case(
            input_tokens=1_000_000,
            output_tokens=0,
            input_tokens_per_repeat=[1_000_000, 2_000_000, 3_000_000],
            output_tokens_per_repeat=[0, 0, 0],
        )

    def test_prices_every_repeat(self) -> None:
        out = reprice_records(
            [self._multi_repeat_case()], price_input=2.50, price_output=10.00
        )
        assert out[0]["cost_usd_per_repeat"] == pytest.approx([2.50, 5.00, 7.50])

    def test_case_total_sums_all_repeats(self) -> None:
        out = reprice_records(
            [self._multi_repeat_case()], price_input=2.50, price_output=10.00
        )
        assert out[0]["total_cost_usd"] == pytest.approx(15.00)

    def test_representative_cost_stays_repeat_one(self) -> None:
        out = reprice_records(
            [self._multi_repeat_case()], price_input=2.50, price_output=10.00
        )
        assert out[0]["cost_usd"] == pytest.approx(2.50)

    def test_summary_total_is_the_all_repeat_spend(self) -> None:
        records = [
            self._multi_repeat_case(),
            {"record": "summary", "repeats": 3, "total_cost_usd": None},
        ]
        out = reprice_records(records, price_input=2.50, price_output=10.00)
        assert out[-1]["total_cost_usd"] == pytest.approx(15.00)
        assert out[-1]["mean_cost_usd_per_repeat"] == pytest.approx(5.00)

    def test_legacy_report_without_per_repeat_arrays_still_reprices(self) -> None:
        # Backwards compatible: the stored *-luna.ndjson baselines predate #401 and
        # carry no per-repeat token arrays. They reprice from repeat #1 as before —
        # the only figure such a report contains.
        records = [
            _case(input_tokens=1_000_000, output_tokens=0),
            {"record": "summary", "repeats": 3, "total_cost_usd": None},
        ]
        out = reprice_records(records, price_input=2.50, price_output=10.00)
        assert out[0]["cost_usd"] == pytest.approx(2.50)
        assert out[0]["total_cost_usd"] == pytest.approx(2.50)
        assert out[-1]["total_cost_usd"] == pytest.approx(2.50)

    def test_ragged_arrays_fall_back_rather_than_mispricing(self) -> None:
        # Honesty control: mismatched input/output lengths are corrupt data, not a
        # licence to zip them and invent a price.
        records = [
            _case(
                input_tokens=1_000_000,
                output_tokens=0,
                input_tokens_per_repeat=[1_000_000, 2_000_000, 3_000_000],
                output_tokens_per_repeat=[0, 0],
            )
        ]
        out = reprice_records(records, price_input=2.50, price_output=10.00)
        assert out[0]["cost_usd"] == pytest.approx(2.50)
        assert out[0]["total_cost_usd"] == pytest.approx(2.50)


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
