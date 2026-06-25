"""Unit tests for the profile-mining / hashing metrics (offline, fast).

These exercise the pure metric helpers against canned ``profile.ndjson`` records
and small ``CrateState`` fixtures — never a live model or the network.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from eval.metrics import (
    ProfileMetrics,
    crate_graph_hash,
    mine_profile_metrics,
)


class TestMineProfileMetrics:
    """``mine_profile_metrics`` aggregates tokens / iterations / tool calls."""

    def test_empty_records_yield_zeroed_metrics(self) -> None:
        m = mine_profile_metrics([])
        assert isinstance(m, ProfileMetrics)
        assert m.input_tokens == 0
        assert m.output_tokens == 0
        assert m.total_tokens == 0
        assert m.tool_calls == 0
        assert m.iterations == 0

    def test_sums_model_node_tokens_and_counts_tool_calls(self) -> None:
        records = [
            {"event": "node_start", "node": "model", "iteration": 1},
            {
                "event": "node_end",
                "node": "model",
                "iteration": 1,
                "input_tokens": 100,
                "output_tokens": 40,
            },
            {"event": "tool_call", "tool": "scan_files", "iteration": 1},
            {
                "event": "node_end",
                "node": "model",
                "iteration": 2,
                "input_tokens": 200,
                "output_tokens": 60,
            },
            {"event": "tool_call", "tool": "draft_investigation", "iteration": 2},
            {"event": "tool_call", "tool": "build_and_validate", "iteration": 2},
        ]
        m = mine_profile_metrics(records)
        assert m.input_tokens == 300
        assert m.output_tokens == 100
        assert m.total_tokens == 400
        assert m.tool_calls == 3
        # The highest observed iteration counter is the iteration count.
        assert m.iterations == 2

    def test_ignores_non_model_node_ends_for_tokens(self) -> None:
        records = [
            {
                "event": "node_end",
                "node": "tools",
                "iteration": 1,
                "input_tokens": 999,
                "output_tokens": 999,
            },
            {
                "event": "node_end",
                "node": "model",
                "iteration": 1,
                "input_tokens": 10,
                "output_tokens": 5,
            },
        ]
        m = mine_profile_metrics(records)
        assert m.input_tokens == 10
        assert m.output_tokens == 5

    def test_tolerates_missing_or_null_token_fields(self) -> None:
        records = [
            {"event": "node_end", "node": "model", "iteration": 1},
            {
                "event": "node_end",
                "node": "model",
                "iteration": 2,
                "input_tokens": None,
                "output_tokens": 7,
            },
        ]
        m = mine_profile_metrics(records)
        assert m.input_tokens == 0
        assert m.output_tokens == 7
        assert m.iterations == 2


def _state_with(name: str) -> CrateState:
    state = CrateState()
    # The root Dataset name is driven by metadata.title, so vary that to get a
    # crate whose @graph genuinely differs.
    state.metadata.title = name
    state.add_entity(
        Entity(
            entity_id="inv",
            type="Investigation",
            fields={"name": name, "description": "d", "identifier": "id"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


class TestCrateGraphHash:
    """``crate_graph_hash`` is a stable content hash of the assembled crate."""

    def test_identical_states_hash_identically(self) -> None:
        assert crate_graph_hash(_state_with("X")) == crate_graph_hash(_state_with("X"))

    def test_different_content_hashes_differently(self) -> None:
        assert crate_graph_hash(_state_with("X")) != crate_graph_hash(_state_with("Y"))

    def test_hash_ignores_the_build_timestamp(self) -> None:
        # datePublished is auto-set to wall-clock now() by ro-crate-py; the
        # determinism signal must not flip just because two runs happened at
        # different times. Build twice with a forced clock gap and assert stable.
        import time

        a = crate_graph_hash(_state_with("Stable"))
        time.sleep(0.01)
        b = crate_graph_hash(_state_with("Stable"))
        assert a == b

    def test_hash_is_a_hex_string(self) -> None:
        h = crate_graph_hash(_state_with("X"))
        assert isinstance(h, str)
        assert len(h) == 64  # sha256 hexdigest
        int(h, 16)  # parses as hex
