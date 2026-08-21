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
from builder.state import CrateState, FileClassification
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
    """A BASE/ISA-passing backbone over a deposit; returns (state, assay_id).

    The deposit carries a procedure document and one file of each data tier,
    because a data-producing step is only drafted when something evidences it and
    its ``result`` is the deposited file rather than a manufactured one (#589,
    #592). Without them the chain would legitimately stop at Exposure and this
    file would be testing a two-step chain by accident. The no-evidence and
    no-output cases have their own coverage in
    ``tests/test_missing_output_is_reported.py``.
    """
    state = CrateState()
    state.metadata.title = "Chain test crate"
    state.metadata.input_path = "/deposit"
    state.scanned_files = [
        FileClassification(
            path="/deposit/assay/SOP.docx",
            filename="SOP.docx",
            size=2048,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        FileClassification(
            path="/deposit/assay/raw data/plate.csv",
            filename="plate.csv",
            size=4096,
            mime_type="text/csv",
        ),
        FileClassification(
            path="/deposit/assay/processed data/fitted.csv",
            filename="fitted.csv",
            size=4096,
            mime_type="text/csv",
        ),
    ]
    ids = scaffold_isa_backbone(
        state,
        investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
        study={"name": "Study", "description": "d"},
        assay={"name": "Assay", "description": "d"},
    )
    return state, ids["assay_id"]


_FULL_CHAIN = [
    # Exposure / EndpointReadout / DataAnalysis each MUST carry at least one
    # schema:additionalProperty under the tox profile, and `_pv` no longer
    # publishes a placeholder like "unknown" as if it were a measurement — so a
    # chain that is expected to VALIDATE has to state a real parameter per step.
    {"process_type": "CellCulture", "hints": {"name": "Seed"}},
    {"process_type": "Exposure", "hints": {"name": "Dose", "duration": "24 hours"}},
    {
        "process_type": "EndpointReadout",
        "hints": {"name": "Read", "detection_instrument": "Plate reader"},
    },
    {
        "process_type": "DataAnalysis",
        "hints": {"name": "Analyse", "data_processing": "Four-parameter logistic fit"},
    },
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

        # The material flow continues into the EndpointReadout. The Exposure has
        # NO in-state output of its own (#285): its output is the build-time CSVW
        # condition table, so the chain hands its consumed material (the cultured
        # Sample) downstream — the EndpointReadout must consume it.
        assert not (
            _ref_ids(exp.fields.get("result")) | _ref_ids(exp.fields.get("output"))
        ), "Exposure must rely on the build's condition table, not an in-state output"
        er_in = (
            _ref_ids(er.fields.get("object"))
            | _ref_ids(er.fields.get("input"))
            | _ref_ids(er.fields.get("samples"))
        )
        assert exp_in & er_in, (
            "EndpointReadout must consume the material flowing through the Exposure"
        )

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


def _types(node: dict) -> set[str]:
    """The @type(s) of a built graph node as a set of strings."""
    t = node.get("@type")
    if isinstance(t, list):
        return {str(x) for x in t}
    return {str(t)} if t is not None else set()


def _node_ref_ids(value: object) -> set[str]:
    """Bare @ids referenced by a built node property (scalar / list / {@id})."""
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        ref = v.get("@id") if isinstance(v, dict) else v
        if isinstance(ref, str):
            out.add(ref)
    return out


class TestExposureOutputIsConditionTable:
    """Issue #285 — the Exposure's synthesized output must BE the CSVW condition
    table that ``about``-references the test compounds (the substances the cells
    were exposed to), NOT a generic placeholder result File.

    Before #285, ``draft_process_chain`` eagerly synthesized a *generic* result
    File for the Exposure (it is not a sample-producer and had no explicit
    ``result``). That populated the Exposure's ``result``, so the build's
    ``_synth_condition_table`` fallback — the ONLY path that wires
    ``table --about--> MolecularEntity`` — never fired. Compound reachability then
    rode solely on the Study ``schema:mentions`` edge: the compounds were modelled
    as "mentioned by the study" rather than "the conditions of the exposure
    process", semantically weaker than the ISA-Tox intent.

    The fix made the Exposure's output the condition table, so the compounds
    attach as TRUE exposure conditions while the Study ``mentions`` becomes a
    redundant backstop. Orphan count must stay 0 (#273) and validation unchanged.

    #650 later moved the table off ``result`` and onto ``executesLabProtocol``
    (the layout is what the run follows, not what it emits) and added the
    compounds as its ``reagent``s. What #285 guaranteed is unchanged and is still
    asserted here; only the edge the compounds are reached by has moved. The
    exposed-Sample half of that chain lives in
    :class:`TestExposureProducesTheExposedSample`.
    """

    def _exposure_chain_state(self) -> tuple[CrateState, list[str]]:
        """A backbone + chain whose Exposure carries two resolved compounds.

        Returns ``(state, compound_entity_ids)``. Mirrors the real materialize
        flow: the chain is drafted first, then the compounds are wired onto the
        Exposure via the ``chemicals`` ref field (as ``_materialize_plan`` does).
        """
        from builder.tools.drafters import draft_molecular_entity

        state, assay_id = _scaffold()
        chem1 = draft_molecular_entity(state, "Methimazole", {"pubchem_cid": "1349907"})
        chem2 = draft_molecular_entity(state, "Sodium iodide", {"pubchem_cid": "5238"})
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)

        exp = _processes_by_subtype(state)["Exposure"][0]
        exp.set_fields_from_dict(
            {"chemicals": [{"@id": chem1.entity_id}, {"@id": chem2.entity_id}]},
            source="llm",
        )
        return state, [chem1.entity_id, chem2.entity_id]

    @staticmethod
    def _built_graph(state: CrateState) -> list[dict]:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        return crate.metadata.generate()["@graph"]

    def test_the_condition_table_still_carries_the_compounds(self) -> None:
        """#285's guarantee, re-pointed by #650.

        #285 established that the compounds must attach as TRUE conditions of the
        exposure rather than riding on the Study's ``schema:mentions`` backstop,
        and made the condition table the Exposure's *result* to get there. #650
        moves the table to the protocol slot — the per-well layout is what the
        run follows, not what it produces — so the route changes while the
        guarantee does not: the table is still built, still names every compound,
        and is still reached from the Exposure.
        """
        state, _ = self._exposure_chain_state()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        exposure = next(
            n
            for n in graph
            if "LabProcess" in _types(n) and n.get("additionalType") == "Exposure"
        )
        reached = _node_ref_ids(exposure.get("executesLabProtocol"))
        condition_tables = [
            by_id[i] for i in reached if i in by_id and "csvw:Table" in _types(by_id[i])
        ]
        assert condition_tables, (
            "the Exposure must reach a CSVW condition table; reached="
            f"{[by_id[i].get('@type') for i in reached if i in by_id]}"
        )

        compound_node_ids = {
            n.get("@id") for n in graph if "MolecularEntity" in _types(n)
        }
        assert len(compound_node_ids) == 2, (
            f"expected 2 MolecularEntity nodes, got {compound_node_ids}"
        )
        about_ids: set[str] = set()
        for table in condition_tables:
            about_ids |= _node_ref_ids(table.get("about"))
        assert compound_node_ids <= about_ids, (
            "condition table's `about` must list every compound (the exposure "
            f"conditions); table about={about_ids}, compounds={compound_node_ids}"
        )

    def test_compounds_are_not_orphaned(self) -> None:
        """Wiring compounds through the condition table keeps orphan count 0 (#273)."""
        state, _ = self._exposure_chain_state()
        graph = self._built_graph(state)

        referenced: set[str] = set()

        def _collect(value: object) -> None:
            if isinstance(value, dict):
                ref = value.get("@id")
                if isinstance(ref, str):
                    referenced.add(ref)
                else:
                    for v in value.values():
                        _collect(v)
            elif isinstance(value, list):
                for item in value:
                    _collect(item)

        for node in graph:
            for key, value in node.items():
                if key in ("@id", "@type"):
                    continue
                _collect(value)

        compounds = [n for n in graph if "MolecularEntity" in _types(n)]
        assert compounds, "no MolecularEntity in built graph — test setup is wrong"
        orphans = [n["@id"] for n in compounds if n["@id"] not in referenced]
        assert orphans == [], f"orphaned compounds: {orphans}"

    @pytest.mark.timeout(120)
    def test_conformance_unchanged_with_condition_table(self) -> None:
        """The condition-table output must not regress ISA / ISA-Tox conformance."""
        state, _ = self._exposure_chain_state()
        report = build_and_validate(state)
        assert "error" not in report, report
        assert report["conformance"]["base"] is True, report
        assert report["conformance"]["isa"] is True, report
        assert report["conformance"]["tox"] is True, report
        assert report["ok"] is True, report


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

        from builder.agents.react.agent_loop import _build_args_schema
        from builder.agents.react.tools_spec import TOOL_SPECS

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


class TestExposureProducesTheExposedSample:
    """Issue #650 — the Exposure emits the **exposed Sample**, and the compounds
    reach the process through the protocol it executes.

    The chain the profile describes runs
    ``cultured sample --[Exposure]--> exposed sample``, but every step after
    CellCulture used to hang off the same cultured sample and no exposed-sample
    entity existed anywhere in the crate. The graph drew a star, so a reader
    could not see what was exposed to what.

    The compound cannot be a process object — ``isa-ro-crate/3_process.ttl``
    restricts ``schema:object`` to File/Sample/BioSample at Violation severity —
    and Bioschemas ``LabProcess`` has no other input slot. ``reagent`` is a
    ``LabProtocol`` property whose published range includes
    ``schema:MolecularEntity`` outright, so the compounds attach to the protocol
    the exposure executes. The per-well condition table is that protocol when the
    real SOP carries no experimental layout: it is what supplies the layout, not
    a product of the run.
    """

    def _exposure_chain_state(self) -> tuple[CrateState, list[str]]:
        from builder.tools.drafters import draft_molecular_entity

        state, assay_id = _scaffold()
        chem1 = draft_molecular_entity(state, "Methimazole", {"pubchem_cid": "1349907"})
        chem2 = draft_molecular_entity(state, "Sodium iodide", {"pubchem_cid": "5238"})
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)

        exp = _processes_by_subtype(state)["Exposure"][0]
        exp.set_fields_from_dict(
            {"chemicals": [{"@id": chem1.entity_id}, {"@id": chem2.entity_id}]},
            source="llm",
        )
        return state, [chem1.entity_id, chem2.entity_id]

    @staticmethod
    def _built_graph(state: CrateState) -> list[dict]:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        return crate.metadata.generate()["@graph"]

    @staticmethod
    def _process(graph: list[dict], subtype: str) -> dict:
        return next(
            n
            for n in graph
            if "LabProcess" in _types(n) and n.get("additionalType") == subtype
        )

    @staticmethod
    def _out_ids(node: dict) -> set[str]:
        return _node_ref_ids(node.get("output")) | _node_ref_ids(node.get("result"))

    @staticmethod
    def _in_ids(node: dict) -> set[str]:
        return _node_ref_ids(node.get("input")) | _node_ref_ids(node.get("object"))

    def test_the_exposure_emits_a_sample(self) -> None:
        """The Exposure's result is a Sample — the exposed cells."""
        state, _ = self._exposure_chain_state()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        exposure = self._process(graph, "Exposure")
        out_nodes = [by_id[i] for i in self._out_ids(exposure) if i in by_id]
        samples = [n for n in out_nodes if "Sample" in _types(n)]
        assert samples, (
            "Exposure must emit an exposed Sample as its result; got "
            f"{[n.get('@type') for n in out_nodes]}"
        )

    def test_the_exposed_sample_derives_from_the_cultured_one(self) -> None:
        """The exposed Sample records what it was made from, so the chain is
        traversable backwards as well as forwards."""
        state, _ = self._exposure_chain_state()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        exposure = self._process(graph, "Exposure")
        culture = self._process(graph, "CellCulture")
        cultured_ids = self._out_ids(culture)
        assert cultured_ids, "test setup: CellCulture produced nothing"

        exposed = [
            by_id[i]
            for i in self._out_ids(exposure)
            if i in by_id and "Sample" in _types(by_id[i])
        ]
        assert exposed, "Exposure emitted no Sample"
        derived = set()
        for node in exposed:
            derived |= _node_ref_ids(node.get("derivesFrom"))
        assert cultured_ids & derived, (
            "the exposed Sample must derivesFrom the cultured Sample; "
            f"derivesFrom={derived}, cultured={cultured_ids}"
        )

    def test_the_condition_table_is_the_protocol_not_the_result(self) -> None:
        """The per-well layout is what the exposure *follows*, not what it
        produces."""
        state, _ = self._exposure_chain_state()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        exposure = self._process(graph, "Exposure")
        out_nodes = [by_id[i] for i in self._out_ids(exposure) if i in by_id]
        assert not [n for n in out_nodes if "csvw:Table" in _types(n)], (
            "the condition table must not be the Exposure's result any more; "
            f"result={[n.get('@id') for n in out_nodes]}"
        )

        protocol_ids = _node_ref_ids(exposure.get("executesLabProtocol"))
        protocols = [by_id[i] for i in protocol_ids if i in by_id]
        assert [n for n in protocols if "csvw:Table" in _types(n)], (
            "the condition table must be one of the Exposure's protocols; "
            f"protocols={[n.get('@type') for n in protocols]}"
        )

    def test_the_protocol_carries_the_compounds_as_reagents(self) -> None:
        """``reagent`` is where Bioschemas puts the substances a protocol uses,
        and its range admits a MolecularEntity directly."""
        state, compound_ids = self._exposure_chain_state()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        compound_node_ids = {n.get("@id") for n in graph if "MolecularEntity" in _types(n)}
        assert len(compound_node_ids) == 2, (
            f"expected 2 MolecularEntity nodes, got {compound_node_ids}"
        )

        exposure = self._process(graph, "Exposure")
        reagents: set[str] = set()
        for pid in _node_ref_ids(exposure.get("executesLabProtocol")):
            node = by_id.get(pid)
            if node is not None:
                reagents |= _node_ref_ids(node.get("reagent"))
        assert compound_node_ids <= reagents, (
            "every compound must be a reagent of a protocol the exposure "
            f"executes; reagents={reagents}, compounds={compound_node_ids}"
        )


class TestTheChainFlowsThroughTheExposure:
    """Issue #650 — a readout measures what the exposure produced, and an
    analysis analyses what the readout recorded.

    The defect this pins: EndpointReadout took ``samples or obj`` and
    DataAnalysis took ``obj or samples``, both straight from the drafter, with no
    link to the step before. Handed a chain whose steps all name the cultured
    sample — which is what threading through an Exposure that produced nothing
    yields — the crate drew a star: 12 raw files and 15 processed files both
    hanging off the culture, and the analysis "analysing" a Sample where the
    profile asks for the raw data.

    Corrections are targeted, not blanket. A readout is redirected only when it
    consumes the culture *and* an exposure intervened; a readout in an assay with
    no exposure at all is measuring the culture and is left alone — the
    characterisation runs in a real deposit do exactly that.
    """

    @staticmethod
    def _built_graph(state: CrateState) -> list[dict]:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        return crate.metadata.generate()["@graph"]

    @staticmethod
    def _process(graph: list[dict], subtype: str) -> dict:
        return next(
            n
            for n in graph
            if "LabProcess" in _types(n) and n.get("additionalType") == subtype
        )

    @staticmethod
    def _out_ids(node: dict) -> set[str]:
        return _node_ref_ids(node.get("output")) | _node_ref_ids(node.get("result"))

    @staticmethod
    def _in_ids(node: dict) -> set[str]:
        return _node_ref_ids(node.get("input")) | _node_ref_ids(node.get("object"))

    def test_the_readout_consumes_the_exposed_sample(self) -> None:
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        graph = self._built_graph(state)

        exposure = self._process(graph, "Exposure")
        readout = self._process(graph, "EndpointReadout")
        culture = self._process(graph, "CellCulture")

        exposed = self._out_ids(exposure)
        cultured = self._out_ids(culture)
        consumed = self._in_ids(readout)

        assert consumed & exposed, (
            f"the readout must measure the exposed sample; it consumes {consumed}, "
            f"exposure produced {exposed}"
        )
        assert not (consumed & cultured), (
            "the readout must not still hang off the cultured sample; "
            f"consumes {consumed}, culture produced {cultured}"
        )

    def test_a_readout_without_an_exposure_keeps_the_cultured_sample(self) -> None:
        """A characterisation run has no exposure — it measures the culture, and
        that is the truth, not the star bug."""
        state, assay_id = _scaffold()
        draft_process_chain(
            state,
            assay_id,
            chain=[c for c in _FULL_CHAIN if c["process_type"] != "Exposure"],
        )
        graph = self._built_graph(state)

        culture = self._process(graph, "CellCulture")
        readout = self._process(graph, "EndpointReadout")
        assert self._in_ids(readout) & self._out_ids(culture), (
            "with no exposure in the assay the readout measures the cultured "
            f"sample; it consumes {self._in_ids(readout)}"
        )

    def test_the_analysis_consumes_data_not_a_sample(self) -> None:
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        readout = self._process(graph, "EndpointReadout")
        analysis = self._process(graph, "DataAnalysis")
        consumed = self._in_ids(analysis)

        assert consumed, "DataAnalysis MUST have an object (the data analysed)"
        samples = [i for i in consumed if i in by_id and "Sample" in _types(by_id[i])]
        assert not samples, (
            "the analysis must not be handed a Sample where the profile asks for "
            f"the raw/condition data being analysed; got {samples}"
        )
        assert consumed & self._out_ids(readout), (
            f"the analysis must consume what the readout recorded; consumes "
            f"{consumed}, readout produced {self._out_ids(readout)}"
        )

    def _star_wired(self) -> CrateState:
        """The wiring a real deposit arrived with: every step naming the cultured
        sample, which is what S-VHPS22 carried.

        ``draft_process_chain`` threads its own steps correctly, so a chain it
        builds cannot exhibit the defect. An agent that wires the processes
        itself can and did — 12 raw files and 15 processed files both hanging off
        one culture.
        """
        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        culture = _processes_by_subtype(state)["CellCulture"][0]
        cultured_id = next(iter(_ref_ids(culture.fields.get("result"))), None)
        assert cultured_id, "test setup: the culture produced nothing"

        for subtype in ("EndpointReadout", "DataAnalysis"):
            proc = _processes_by_subtype(state)[subtype][0]
            proc.set_fields_from_dict(
                {"object": [{"@id": cultured_id}], "samples": [{"@id": cultured_id}]},
                source="llm",
            )
        return state

    def test_a_star_wired_readout_is_redirected_to_the_exposed_sample(self) -> None:
        graph = self._built_graph(self._star_wired())
        readout = self._process(graph, "EndpointReadout")
        exposure = self._process(graph, "Exposure")
        culture = self._process(graph, "CellCulture")

        consumed = self._in_ids(readout)
        assert consumed & self._out_ids(exposure), (
            f"a readout wired to the culture must be redirected to the exposed "
            f"sample; consumes {consumed}"
        )
        assert not (consumed & self._out_ids(culture)), (
            f"the cultured sample must no longer be the readout's object: {consumed}"
        )

    def test_a_star_wired_analysis_is_redirected_to_the_readouts_data(self) -> None:
        state = self._star_wired()
        graph = self._built_graph(state)
        by_id = {n.get("@id"): n for n in graph}

        analysis = self._process(graph, "DataAnalysis")
        readout = self._process(graph, "EndpointReadout")
        consumed = self._in_ids(analysis)

        samples = [i for i in consumed if i in by_id and "Sample" in _types(by_id[i])]
        assert not samples, (
            "a Sample in the analysis's object is the wrong KIND for the slot — "
            f"the profile asks for the raw/condition data being analysed: {samples}"
        )
        assert consumed & self._out_ids(readout), (
            f"the analysis must consume the readout's data; consumes {consumed}"
        )

    def test_a_declared_file_object_on_the_analysis_is_left_alone(self) -> None:
        """Type-aware correction: a File is the right kind for the slot, so a
        drafter that named one keeps it."""
        from builder.tools.provenance import draft_file

        state, assay_id = _scaffold()
        draft_process_chain(state, assay_id, chain=_FULL_CHAIN)
        declared = draft_file(state, "declared.csv", path="data/declared.csv")
        analysis = _processes_by_subtype(state)["DataAnalysis"][0]
        analysis.set_fields_from_dict(
            {"object": [{"@id": declared.entity_id}]}, source="llm"
        )

        graph = self._built_graph(state)
        consumed = self._in_ids(self._process(graph, "DataAnalysis"))
        assert any("declared" in i for i in consumed), (
            f"a File the drafter named is the right kind and must be kept: {consumed}"
        )
