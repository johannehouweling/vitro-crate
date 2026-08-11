"""What a deployment pays is not what the price table says.

LiteLLM's table mirrors PUBLIC list prices. A proxy in front of a model charges
whatever it charges: one billed 1.00/6.00 per 1M where the table lists
0.22/1.32 for the same model — a ~4.5x gap. The lookup
was right and the footer was still wrong by 4.5x, quietly, in the number people
read to decide whether a run was worth it.

So the deployment can state its own rates, per 1,000,000 tokens — the unit every
vendor price list uses.
"""

from __future__ import annotations

import pytest

from builder.pricing import compute_cost, price_overrides

MODEL = "gpt-5.6-luna"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("VITRO_PRICE_INPUT_PER_1M", raising=False)
    monkeypatch.delenv("VITRO_PRICE_OUTPUT_PER_1M", raising=False)


class TestTheOverrideWins:
    def test_both_rates(self, monkeypatch):
        monkeypatch.setenv("VITRO_PRICE_INPUT_PER_1M", "1.00")
        monkeypatch.setenv("VITRO_PRICE_OUTPUT_PER_1M", "6.00")
        out = compute_cost(1_000_000, 1_000_000, MODEL, provider="openai")
        assert out["input_cost"] == pytest.approx(1.0)
        assert out["output_cost"] == pytest.approx(6.0)

    def test_one_alone_leaves_the_other_on_the_table(self, monkeypatch):
        """A proxy may only differ on one side; the table still covers the rest."""
        table = compute_cost(1_000_000, 1_000_000, MODEL, provider="openai")
        monkeypatch.setenv("VITRO_PRICE_INPUT_PER_1M", "1.00")
        out = compute_cost(1_000_000, 1_000_000, MODEL, provider="openai")
        assert out["input_cost"] == pytest.approx(1.0)
        assert out["output_cost"] == pytest.approx(table["output_cost"])

    def test_it_prices_a_model_the_table_never_heard_of(self, monkeypatch):
        """Without an override this is None — which is why the override exists."""
        assert compute_cost(1000, 1000, "some-private-proxy-model")["total_cost"] is None
        monkeypatch.setenv("VITRO_PRICE_INPUT_PER_1M", "2.00")
        monkeypatch.setenv("VITRO_PRICE_OUTPUT_PER_1M", "4.00")
        out = compute_cost(1000, 1000, "some-private-proxy-model")
        assert out["total_cost"] == pytest.approx(0.006)


class TestItStaysOutOfTheWay:
    def test_unset_means_the_table(self):
        assert price_overrides() == {}
        out = compute_cost(1_000_000, 0, MODEL, provider="openai")
        assert out["input_cost"] == pytest.approx(0.22), "published list price"

    @pytest.mark.parametrize("bad", ["", "   ", "free", "1,00", "-1.0"])
    def test_a_bad_value_is_ignored_not_fatal(self, monkeypatch, bad):
        """Cost display is advisory. A typo must not take the session down."""
        monkeypatch.setenv("VITRO_PRICE_INPUT_PER_1M", bad)
        assert "input_cost_per_token" not in price_overrides()
        out = compute_cost(1_000_000, 0, MODEL, provider="openai")
        assert out["input_cost"] == pytest.approx(0.22)

    def test_zero_is_honoured(self, monkeypatch):
        """A flat-rate or internally-billed deployment really does pay nothing."""
        monkeypatch.setenv("VITRO_PRICE_INPUT_PER_1M", "0")
        monkeypatch.setenv("VITRO_PRICE_OUTPUT_PER_1M", "0")
        assert compute_cost(1_000_000, 1_000_000, MODEL)["total_cost"] == 0
