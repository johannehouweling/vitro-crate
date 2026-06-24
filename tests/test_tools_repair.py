"""Tests for fix_required_issues — deterministic issue->repair dispatch (#179).

``fix_required_issues`` closes the issue->repair gap that ``_order_required_issues``
left open: it consumes the routed issues from ``build_and_validate`` and attempts a
DETERMINISTIC repair per issue (no LLM, no network), based on the issue's
``{entity_id, property}`` and what already exists in CrateState. Repairs that need
NEW content, a NEW entity, or any fabricated identifier are out of scope (D5) and
are classified as ``remaining`` for the LLM leaf.

These tests are fully offline (validation runs against the bundled context, see
``tests/test_offline_validation.py``).
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.repair import fix_required_issues

# Every test exercises fix_required_issues, which runs the (deliberately uncached,
# owlrl-heavy) SHACL validator one or more times — legitimately slower than the
# 30s suite-wide CI default. Mirror tests/test_e2e_agent_eval.py and give this
# validation-heavy module headroom; the marker overrides the CLI --timeout.
pytestmark = pytest.mark.timeout(120)


def _entity(entity_id: str, type_: EntityType, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=dict(fields),
        _provenance=EntityProvenance(created_by="llm"),
    )


def _backbone() -> CrateState:
    """A BASE/ISA-passing Investigation -> Study -> Assay backbone."""
    state = CrateState()
    state.metadata.title = "Repair test crate"
    state.add_entity(
        _entity("inv1", "Investigation", name="Inv", description="d", identifier="INV-1")
    )
    state.add_entity(
        _entity("st1", "Study", name="St", description="d", investigation_id="inv1")
    )
    state.add_entity(
        _entity("as1", "Assay", name="As", description="d", study_id="st1")
    )
    return state


def _endpoint_readout_missing_result(n_files: int = 1) -> CrateState:
    """A backbone + an EndpointReadout with no result + ``n_files`` File entities.

    The TOX REQUIRED issue 'EndpointReadout MUST have a result' is deterministically
    fixable iff exactly one un-wired File exists (unambiguous target).
    """
    state = _backbone()
    state.add_entity(
        _entity(
            "er1",
            "LabProcess",
            process_type="EndpointReadout",
            name="Readout",
            assay_id="as1",
        )
    )
    for i in range(n_files):
        state.add_entity(
            _entity(f"f{i}", "File", name=f"raw{i}.csv", dest_path=f"data/raw{i}.csv")
        )
    return state


class TestReturnShape:
    def test_returns_ok_fixed_remaining(self):
        result = fix_required_issues(CrateState())
        assert set(result.keys()) >= {"ok", "fixed", "remaining"}
        assert isinstance(result["ok"], bool)
        assert isinstance(result["fixed"], list)
        assert isinstance(result["remaining"], list)


class TestDeterministicLinkRepair:
    def test_links_unique_file_as_process_result(self):
        """A missing process output with exactly one in-state File is auto-linked."""
        state = _endpoint_readout_missing_result(n_files=1)

        # fix_required_issues validates internally (before + after), so we assert
        # via its return value + the mutated state rather than running extra full
        # SHACL sweeps here (keeps this validation-heavy test within budget).
        result = fix_required_issues(state)

        # The File is now wired as the EndpointReadout's result in state.
        er = state.get_entity("er1")
        assert er is not None
        wired = er.fields.get("result")
        wired_ids = wired if isinstance(wired, list) else [wired]
        assert "f0" in wired_ids, er.fields

        # The tox REQUIRED issue is gone and recorded under ``fixed``.
        assert result["ok"] is True, result
        assert result["remaining"] == [], result["remaining"]
        assert any(
            (item["issue"]["property"] or "").endswith("result")
            for item in result["fixed"]
        ), result["fixed"]
        # result["ok"] is True (asserted above) IS the re-validation confirmation:
        # fix_required_issues re-runs the validator after the repair and only
        # reports ok when no REQUIRED issue remains — no extra sweep needed here.

    def test_each_fixed_item_records_what_was_done(self):
        state = _endpoint_readout_missing_result(n_files=1)
        result = fix_required_issues(state)
        assert result["fixed"], result
        item = result["fixed"][0]
        assert "issue" in item
        assert item.get("action"), "a fixed item must record the repair action taken"


class TestAmbiguousAndNewContentDeferred:
    def test_ambiguous_target_left_in_remaining(self):
        """Two candidate Files == ambiguous: NOT auto-linked, left for the LLM."""
        state = _endpoint_readout_missing_result(n_files=2)
        # Snapshot the process's I/O fields before the call.
        er_before = state.get_entity("er1")
        assert er_before is not None
        before_fields = dict(er_before.fields)

        result = fix_required_issues(state)

        # No deterministic repair could be made: result/output untouched.
        er = state.get_entity("er1")
        assert er is not None
        assert er.fields.get("result") == before_fields.get("result")
        assert er.fields.get("output") == before_fields.get("output")

        assert result["ok"] is False
        assert any(
            (item["issue"]["property"] or "").endswith("result")
            for item in result["remaining"]
        ), result["remaining"]

    def test_new_content_issue_not_fabricated(self):
        """A missing result with NO File in state needs NEW content -> remaining.

        Crucially, ``fix_required_issues`` must NOT fabricate a File or any id (D5):
        the File collection stays empty and the issue lands in ``remaining``.
        """
        state = _endpoint_readout_missing_result(n_files=0)

        result = fix_required_issues(state)

        assert state.list_entities("File") == [], "must not fabricate a File entity"
        assert result["ok"] is False
        assert any(
            (item["issue"]["property"] or "").endswith("result")
            for item in result["remaining"]
        ), result["remaining"]
        # Each remaining item explains why it could not be auto-fixed.
        for item in result["remaining"]:
            assert "issue" in item
            assert item.get("reason"), "a remaining item must record why it deferred"


class TestSideEffectSafety:
    def test_no_fixable_issue_does_not_mutate_state(self):
        """When nothing is deterministically fixable, state is left untouched."""
        state = _endpoint_readout_missing_result(n_files=0)
        snapshot = state.to_json()

        result = fix_required_issues(state)

        assert state.to_json() == snapshot, "state must not change when nothing is fixable"
        assert result["fixed"] == []

    def test_clean_crate_is_a_noop(self):
        """A crate with no REQUIRED issues returns ok with empty fixed/remaining."""
        state = _backbone()
        snapshot = state.to_json()

        result = fix_required_issues(state)

        assert result["ok"] is True
        assert result["fixed"] == []
        assert result["remaining"] == []
        assert state.to_json() == snapshot

    def test_idempotent(self):
        """Running twice yields the same end-state (second run is a no-op)."""
        state = _endpoint_readout_missing_result(n_files=1)
        fix_required_issues(state)
        after_first = state.to_json()
        second = fix_required_issues(state)
        assert state.to_json() == after_first
        assert second["ok"] is True
        assert second["fixed"] == []


class TestEngineRoutable:
    def test_callable_through_run_tool(self):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.initialize()
        for ent in _endpoint_readout_missing_result(n_files=1).list_entities():
            engine.state.add_entity(ent)

        result = engine.run_tool("fix_required_issues")
        assert result["ok"] is True
        er = engine.state.get_entity("er1")
        assert er is not None
        wired = er.fields.get("result")
        wired_ids = wired if isinstance(wired, list) else [wired]
        assert "f0" in wired_ids


class TestRegistered:
    def test_registered_in_registry(self):
        import builder.tools.repair  # noqa: F401  (triggers registration)
        from builder.tools.registry import TOOL_REGISTRY

        assert "fix_required_issues" in TOOL_REGISTRY

    def test_in_tool_specs(self):
        from builder.agents.tools_spec import TOOL_SPECS

        assert any(s["name"] == "fix_required_issues" for s in TOOL_SPECS)
