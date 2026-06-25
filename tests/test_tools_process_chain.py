"""Tests for the ``draft_process_chain`` composite (Issue #179, task 3).

``draft_process_chain`` fuses the recurring
``draft_process`` + ``link`` sequence that wires the gold S-VHPS21 derivation
chain — ``Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
→[EndpointReadout]→ raw/result →[DataAnalysis]→ figures`` — into ONE
idempotent call. Its keystone job is to **synthesize the missing outputs** for
EndpointReadout / DataAnalysis (the two subtypes with no build-time output
fallback, AGENTS.md §14.3) so the chain never dangles into a tox Violation.

The validation-heavy class carries a 120s timeout (the #184 lesson) because each
``build_and_validate`` runs the full three-pass SHACL sweep.
"""

from __future__ import annotations

import inspect
import warnings

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.composites import draft_process_chain, scaffold_isa_backbone
from builder.tools.validation import build_and_validate


def _by_type(state: CrateState, type_name: str) -> list:
    return [e for e in state.list_entities() if e.type == type_name]


def _processes_by_subtype(state: CrateState) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in _by_type(state, "LabProcess"):
        out.setdefault(p.fields.get("process_type", ""), []).append(p)
    return out


def _ref_ids(value) -> set[str]:
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key:
            out.add(str(key).lstrip("#"))
    return out


def _scaffold() -> tuple[CrateState, str]:
    """A BASE/ISA-passing backbone; returns (state, assay_id)."""
    state = CrateState()
    state.metadata.title = "Chain test crate"
    ids = scaffold_isa_backbone(
        state,
        investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
        study={"name": "Study", "description": "d"},
        assay={"name": "Assay", "description": "d"},
    )
    return state, ids["assay_id"]


_FULL_CHAIN = [
    {"process_type": "CellCulture", "hints": {"name": "Seed"}},
    {"process_type": "Exposure", "hints": {"name": "Dose"}},
    {"process_type": "EndpointReadout", "hints": {"name": "Read"}},
    {"process_type": "DataAnalysis", "hints": {"name": "Analyse"}},
]


class TestChainCreation:
    def test_creates_all_four_processes(self):
        state, assay_id = _scaffold()
        result = draft_process_chain(state, assay_id, chain=_FULL_CHAIN)

        by_subtype = _processes_by_subtype(state)
        for t in ("CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"):
            assert len(by_subtype.get(t, [])) == 1, f"missing {t}: {by_subtype}"

        # Result reports one process id per step, in order.
        assert len(result["process_ids"]) == 4
        assert result["assay_id"] == assay_id

    def test_processes_carry_assay_id(self):
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        for p in _by_type(state, "LabProcess"):
            assert p.fields.get("assay_id") == assay_id

    def test_wires_sequential_provenance_edges(self):
        """Each step's output feeds the next step's input (object/input)."""
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        by = _processes_by_subtype(state)
        cc = by["CellCulture"][0]
        exp = by["Exposure"][0]
        er = by["EndpointReadout"][0]
        da = by["DataAnalysis"][0]

        # CellCulture produces a Sample that the Exposure consumes.
        cc_out = _ref_ids(cc.fields.get("result")) | _ref_ids(cc.fields.get("output"))
        exp_in = (
            _ref_ids(exp.fields.get("object"))
            | _ref_ids(exp.fields.get("input"))
            | _ref_ids(exp.fields.get("samples"))
        )
        assert cc_out, "CellCulture must have an output to feed the Exposure"
        assert cc_out & exp_in, "Exposure must consume the CellCulture output"

        # Exposure output feeds the EndpointReadout input.
        exp_out = _ref_ids(exp.fields.get("result")) | _ref_ids(exp.fields.get("output"))
        er_in = (
            _ref_ids(er.fields.get("object"))
            | _ref_ids(er.fields.get("input"))
            | _ref_ids(er.fields.get("samples"))
        )
        assert exp_out & er_in, "EndpointReadout must consume the Exposure output"

        # EndpointReadout output feeds the DataAnalysis object.
        er_out = _ref_ids(er.fields.get("result")) | _ref_ids(er.fields.get("output"))
        da_in = _ref_ids(da.fields.get("object")) | _ref_ids(da.fields.get("input"))
        assert er_out & da_in, "DataAnalysis must consume the EndpointReadout output"


class TestOutputSynthesis:
    def test_endpoint_readout_gets_a_result(self):
        """EndpointReadout (no build-time fallback) must end with a result."""
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        er = _processes_by_subtype(state)["EndpointReadout"][0]
        assert _ref_ids(er.fields.get("result")) | _ref_ids(er.fields.get("output"))

    def test_data_analysis_gets_object_and_result(self):
        """DataAnalysis must end with BOTH an object and a result."""
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        da = _processes_by_subtype(state)["DataAnalysis"][0]
        assert _ref_ids(da.fields.get("object")) | _ref_ids(da.fields.get("input"))
        assert _ref_ids(da.fields.get("result")) | _ref_ids(da.fields.get("output"))

    def test_synthesized_outputs_are_real_file_entities(self):
        """Any synthesized output id resolves to a real File entity (no dangling ref)."""
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        for p in _by_type(state, "LabProcess"):
            for fld in ("object", "input", "result", "output"):
                for tid in _ref_ids(p.fields.get(fld)):
                    assert state.get_entity(tid) is not None, (
                        f"{p.entity_id}.{fld} -> {tid} dangles"
                    )

    def test_no_fabricated_identifiers(self):
        """Synthesized placeholder Files carry NO measurement value / identifier (D5).

        A placeholder may carry a name/path/role/encodingFormat, but must not
        invent a CAS/accession/DOI/measured value etc.
        """
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        forbidden = {
            "identifier", "cas", "casrn", "cas_number", "pubchem_cid",
            "accession", "doi", "value", "measured_value", "inchikey",
        }
        for f in _by_type(state, "File"):
            leaked = forbidden & set(f.fields)
            assert not leaked, f"placeholder File {f.entity_id} fabricated {leaked}"


class TestPartialChains:
    def test_subset_chain_just_readout(self):
        """A chain may be a subset of the four types (partial chains work)."""
        state, assay_id = _scaffold()
        result = draft_process_chain(
            state, assay_id, chain=[{"process_type": "EndpointReadout", "hints": {}}]
        )
        assert len(result["process_ids"]) == 1
        er = _processes_by_subtype(state)["EndpointReadout"][0]
        # Even a lone EndpointReadout must get a synthesized result.
        assert _ref_ids(er.fields.get("result")) | _ref_ids(er.fields.get("output"))

    def test_explicit_outputs_are_respected(self):
        """An explicit result on a step is used instead of a synthesized one."""
        from builder.tools.provenance import draft_file

        state, assay_id = _scaffold()
        existing = draft_file(state, name="my_results.csv")
        draft_process_chain(
            state,
            assay_id,
            chain=[
                {
                    "process_type": "EndpointReadout",
                    "hints": {},
                    "result": [existing.entity_id],
                }
            ],
        )
        er = _processes_by_subtype(state)["EndpointReadout"][0]
        wired = _ref_ids(er.fields.get("result")) | _ref_ids(er.fields.get("output"))
        assert existing.entity_id in wired


class TestIdempotency:
    def test_rerun_does_not_duplicate(self):
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        n_proc = len(_by_type(state, "LabProcess"))
        n_file = len(_by_type(state, "File"))
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        assert len(_by_type(state, "LabProcess")) == n_proc
        assert len(_by_type(state, "File")) == n_file


class TestViaEngine:
    def test_callable_through_run_tool(self):
        engine = AgentEngine()
        engine.initialize()
        ids = engine.run_tool("scaffold_isa_backbone")
        result = engine.run_tool(
            "draft_process_chain",
            assay_id=ids["assay_id"],
            chain=_FULL_CHAIN,
        )
        assert result["process_ids"]


@pytest.mark.timeout(120)
class TestChainPassesValidation:
    """The KEY assertion: after one chain call there are NO EndpointReadout /
    DataAnalysis missing-result/object tox Violations (§14.3 trap closed)."""

    def test_no_dangling_output_violations(self):
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)

        report = build_and_validate(state)  # full 3-pass sweep at REQUIRED
        assert "error" not in report, report

        # Specifically: no missing-result / missing-object Violation on any
        # EndpointReadout / DataAnalysis process.
        offending = [
            iss
            for iss in report.get("issues", [])
            if (iss.get("property") or "").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            in ("result", "object")
        ]
        assert not offending, offending

        # The chain produces a tox-conformant crate end to end.
        assert report["conformance"]["tox"] is True, report
        assert report["ok"] is True, report

    def test_optional_validate_flag_returns_report(self):
        state, assay_id = _scaffold()
        result = draft_process_chain(
            state, assay_id, chain=_FULL_CHAIN, validate_after=True
        )
        assert "validation" in result
        assert result["validation"]["ok"] is True, result["validation"]

    def test_validate_after_default_skips_validation(self):
        """Omitting the flag (and passing None) must NOT run validation."""
        state, assay_id = _scaffold()
        result = draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        assert "validation" not in result

        state2, assay_id2 = _scaffold()
        result2 = draft_process_chain(
            state2, assay_id2, chain=_FULL_CHAIN, validate_after=None
        )
        assert "validation" not in result2


class TestNoPydanticShadowWarning:
    """The optional validation flag must not be named ``validate`` — that
    shadows ``pydantic.BaseModel.validate`` and makes pydantic emit a
    ``UserWarning`` on every run, for both the ``_build_args_schema`` model
    and the ``StructuredTool.from_function`` model (#189)."""

    def test_signature_has_no_validate_shadowing_param(self):
        """The function must not expose a ``validate`` parameter."""
        params = inspect.signature(draft_process_chain).parameters
        assert "validate" not in params, (
            "draft_process_chain still has a 'validate' param that shadows "
            f"BaseModel.validate; params: {list(params)}"
        )
        assert "validate_after" in params

    def test_args_schema_emits_no_shadow_warning(self):
        """Building the LLM-facing pydantic args model must not warn."""
        pytest.importorskip("pydantic")
        from typing import Any, cast

        from builder.agents.agent_loop import _build_args_schema
        from builder.agents.tools_spec import TOOL_SPECS

        spec = next(s for s in TOOL_SPECS if s["name"] == "draft_process_chain")
        name = cast(str, spec["name"])
        params = cast(dict[str, Any], spec["parameters"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            _build_args_schema(name, params)

    def test_structured_tool_emits_no_shadow_warning(self):
        """Introspecting the function signature for a tool must not warn."""
        StructuredTool = pytest.importorskip(
            "langchain_core.tools"
        ).StructuredTool

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            StructuredTool.from_function(
                func=draft_process_chain,
                name="draft_process_chain",
                description="wire a process chain",
            )
        shadow = [
            w
            for w in caught
            if issubclass(w.category, UserWarning) and "shadows" in str(w.message)
        ]
        assert not shadow, [str(w.message) for w in shadow]
