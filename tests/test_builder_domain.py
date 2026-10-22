"""Tests for the full ISA-Tox domain-model mapping in build_crate.

These pin the contract from profiles/docs/isa_tox.md: Study/Assay as
Dataset+additionalType linked by hasPart, LabProcess subtypes discriminated by
additionalType with input/output (→ schema:object/result via the context), one
protocol record per step based on the deposit's documents, the Exposure's
compounds as its record's reagents, conformsTo gating on validation, and the
#-fragment / resolvable-URI id scheme.
"""

from __future__ import annotations

import json

import pytest

from builder.state import CrateState, Entity, EntityProvenance, FileClassification
from builder.tools.assessment_graph import with_kinds
from builder.tools.builder import build_crate



# Every test here exports a crate, and each export now runs the uncached,
# owlrl-heavy validator over all three profiles at the full severity gate (#446)
# — ~10s per export locally, and the 2-vCPU CI runner is ~2-3x slower, which puts
# the whole module against the CI-wide `--timeout=30`. Same headroom, for the
# same reason, that the other export-heavy modules already take
# (test_export_smoke, test_readers, test_path_traversal, test_html_xss).
# Headroom, not a licence to grow: no test in this module is changed.
pytestmark = pytest.mark.timeout(120)

def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _build(state, tmp_path):
    out = tmp_path / "crate"
    result = build_crate(state, str(out))
    assert result["success"] is True, result
    with open(out / "ro-crate-metadata.json") as f:
        graph = json.load(f)["@graph"]
    return graph, {e["@id"]: e for e in graph}


def _ids(value):
    """Normalize a hasPart/about/object value to a list of @id strings."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [v.get("@id") if isinstance(v, dict) else v for v in items]


def _kind(by_id, pid):
    """What a built step is, read from its record as the reports read it (#799)."""
    return {n["@id"]: n.get("additionalType") for n in with_kinds(list(by_id.values()))}[pid]


def _documents(by_id, pid):
    """What a step's protocol record is based on; each step executes exactly one (#783)."""
    (record,) = _ids(by_id[pid].get("executesLabProtocol"))
    return _ids(by_id[record].get("isBasedOn"))


class TestStructuralDatasets:
    def test_study_and_assay_are_datasets_with_additionaltype(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="My Study"))
        state.add_entity(_ent("assay_1", "Assay", name="My Assay", study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        assert by_id["#Study_study_1"]["@type"] == "Dataset"
        assert by_id["#Study_study_1"]["additionalType"] == "Study"
        assert by_id["#Assay_assay_1"]["@type"] == "Dataset"
        assert by_id["#Assay_assay_1"]["additionalType"] == "Assay"

    def test_haspart_wiring(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        # root contains the study; study contains the assay
        assert "#Study_study_1" in _ids(by_id["./"].get("hasPart"))
        assert "#Assay_assay_1" in _ids(by_id["#Study_study_1"].get("hasPart"))


class TestISAHierarchy:
    """ISA structure fixes: single Investigation is the root, distinct
    identifiers per level, and result Files live under their producing Assay."""

    def _types_of(self, node):
        t = node.get("@type")
        return t if isinstance(t, list) else [t]

    def test_single_investigation_is_the_root_not_a_separate_node(self, tmp_path):
        state = CrateState()
        state.metadata.accession = "FAB-2026"
        state.add_entity(_ent("inv_1", "Investigation", name="Fabian"))
        state.add_entity(_ent("study_1", "Study", name="S", investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        graph, by_id = _build(state, tmp_path)

        # The Investigation is the root ./ — there is no separate #Investigation_* node.
        investigations = [n for n in graph if n.get("additionalType") == "Investigation"]
        assert len(investigations) == 1
        assert investigations[0]["@id"] == "./"
        assert "#Investigation_inv_1" not in by_id

    def test_isa_levels_have_distinct_identifiers(self, tmp_path):
        # All three deliberately share an identifier value — the builder must
        # still emit three DISTINCT, single-string identifiers.
        state = CrateState()
        state.metadata.accession = "FAB-2026"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv", identifier="FAB-2026"))
        state.add_entity(_ent("study_1", "Study", name="S", identifier="FAB-2026",
                              investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="A", identifier="FAB-2026",
                              study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        root_id = by_id["./"]["identifier"]
        study_id = by_id["#Study_study_1"]["identifier"]
        assay_id = by_id["#Assay_assay_1"]["identifier"]
        for ident in (root_id, study_id, assay_id):
            assert isinstance(ident, str) and ident  # single non-empty string
        assert len({root_id, study_id, assay_id}) == 3  # all distinct

    def test_study_still_referenced_from_root_haspart(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        state.add_entity(_ent("study_1", "Study", name="S", investigation_id="inv_1"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        _, by_id = _build(state, tmp_path)

        # Study stays a separate node typed Study, referenced from the root.
        assert by_id["#Study_study_1"]["additionalType"] == "Study"
        assert "#Study_study_1" in _ids(by_id["./"].get("hasPart"))

    def test_result_file_attached_to_producing_assay_haspart(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("raw", "File", name="raw.csv", dest_path="data/raw.csv"))
        state.add_entity(_ent("er", "LabProcess", process_type="EndpointReadout",
                              name="Readout", assay_id="assay_1", result=["raw"]))
        _, by_id = _build(state, tmp_path)

        # The raw file is hasPart of its producing Assay …
        assert "data/raw.csv" in _ids(by_id["#Assay_assay_1"].get("hasPart"))
        # … and stays on the root too, which is what keeps it in the file tree
        # (#532 — an ISA container is a contextual node, not a directory).
        assert "data/raw.csv" in _ids(by_id["./"].get("hasPart"))

    def test_assay_haspart_is_deduped(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("raw", "File", name="raw.csv", dest_path="data/raw.csv"))
        # two processes on the same assay both yield the same file
        state.add_entity(_ent("er", "LabProcess", process_type="EndpointReadout",
                              name="R", assay_id="assay_1", result=["raw"]))
        state.add_entity(_ent("da", "LabProcess", process_type="DataAnalysis",
                              name="D", assay_id="assay_1", object=["raw"], result=["raw"]))
        _, by_id = _build(state, tmp_path)

        parts = _ids(by_id["#Assay_assay_1"].get("hasPart"))
        assert parts.count("data/raw.csv") == 1

    def test_an_assay_without_a_study_still_builds(self, tmp_path):
        """A process whose Assay names no Study must not take the build down.

        Nesting an executed protocol under its Assay consults the Study's parts
        first, so a study-level protocol is not re-parented per assay. That
        lookup returns nothing for a bare Assay — every minimal fixture in this
        suite, and any deposit whose backbone is still incomplete — and reading
        parts off it crashed `assemble_crate` outright rather than degrading to
        "no study-level protocols to skip" (#650).
        """
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))  # no study_id
        state.add_entity(_ent("raw", "File", name="raw.csv", dest_path="data/raw.csv"))
        state.add_entity(
            _ent("er", "LabProcess", process_type="EndpointReadout", name="R",
                 assay_id="assay_1", result=["raw"])
        )
        _, by_id = _build(state, tmp_path)
        assert "data/raw.csv" in _ids(by_id["#Assay_assay_1"].get("hasPart"))



class TestTheAssayProtocolReachesItsStep:
    """#650 item 3 — an assay's deposited protocols reach the step they describe.

    Only TestSystemPreparation ever got a protocol. EndpointReadout and DataAnalysis fell
    through to nothing, so S-VHPS22's nine assay-scoped documents — the
    transporter assay, Bradford, deiodinase activity, the UPLC run, RNA
    isolation, cDNA/qPCR — were carried in the payload and followed by no step.

    Three rules, each pinned below, on what the step's protocol record is based on:

    * **The default is EndpointReadout.** Measuring is what an assay's SOP
      describes; culture and analysis are reached only by an explicit cue, or,
      for analysis, by being source code (#786).
    * **A step's record is based on ALL of its documents, not one.** Every assay
      in the deposit ships two to four (assay_04 has three sequential ones: RNA
      isolation, DNase, cDNA/qPCR). The single-hit D5 rule still governs the
      culture lookup, where a *cell line* has to disambiguate; here there is no
      subject to be ambiguous about, and picking one of four would be a silent
      choice rather than an honest list.
    * **Nothing is minted.** A step with no document has a record based on
      nothing (D17).
    """

    ASSAY_DIR = "assay_02_deiodinase"

    def _state(self, *, docs=("4.1 Deiodinase activity assay.docx",), steps=(
        ("proc_read", "EndpointReadout"),
    )):
        state = CrateState()
        state.metadata.input_path = "/deposit"
        state.add_entity(_ent("study_1", "Study", name="S"))
        doc_ids = []
        for n, nm in enumerate(docs, start=1):
            eid = f"doc_{n}"
            doc_ids.append(eid)
            state.add_entity(
                _ent(
                    eid,
                    "File",
                    name=nm,
                    path=f"{self.ASSAY_DIR}/{nm}",
                    additional_types=["LabProtocol"],
                )
            )
        state.add_entity(
            _ent("assay_1", "Assay", name="A1", study_id="study_1", hasPart=doc_ids)
        )
        for pid, ptype in steps:
            state.add_entity(
                _ent(
                    pid,
                    "LabProcess",
                    name=ptype,
                    process_type=ptype,
                    assay_id="assay_1",
                    endpoint="deiodinase activity",
                    detection_instrument="gamma counter",
                )
            )
        return state

    def test_the_readouts_record_is_based_on_the_assays_protocol(self, tmp_path):
        _, by_id = _build(self._state(), tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_read")
        assert executed, "the readout's record is based on none of its assay's protocols"
        assert any("Deiodinase" in e for e in executed), executed

    def test_the_protocol_is_the_document_itself(self, tmp_path):
        _, by_id = _build(self._state(), tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_read")
        assert executed and not executed[0].startswith("#"), (
            f"a protocol's @id must be its file path, not a minted stub: {executed}"
        )
        assert self.ASSAY_DIR in executed[0], executed

    def test_the_record_is_based_on_every_protocol_the_assay_ships(self, tmp_path):
        """Two to four per assay is the norm; one-of-four would be a silent pick."""
        state = self._state(
            docs=("4.1 Deiodinase activity assay.docx", "3.3 Bradford protocol.xlsx")
        )
        _, by_id = _build(state, tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_read")
        assert sum("Deiodinase" in e for e in executed) == 1, executed
        assert sum("Bradford" in e for e in executed) == 1, executed

    def test_an_analysis_document_goes_to_the_analysis_not_the_readout(self, tmp_path):
        state = self._state(
            docs=(
                "4.1 Deiodinase activity assay.docx",
                "5.4 Data analysis and curve fitting.docx",
            ),
            steps=(("proc_read", "EndpointReadout"), ("proc_ana", "DataAnalysis")),
        )
        _, by_id = _build(state, tmp_path)
        # A crate @id percent-encodes its spaces; compare on the decoded path.
        def _paths(pid):
            return [
                e.replace("%20", " ")
                for e in _documents(by_id, pid)
            ]

        readout = _paths("#LabProcess_proc_read")
        analysis = _paths("#LabProcess_proc_ana")
        assert any("Data analysis" in e for e in analysis), analysis
        assert not any("Data analysis" in e for e in readout), (
            f"an analysis protocol must not be claimed by the readout: {readout}"
        )
        assert not any("Deiodinase" in e for e in analysis), (
            f"a readout protocol must not be claimed by the analysis: {analysis}"
        )

    def test_a_deposited_script_goes_to_the_analysis_not_the_readout(self, tmp_path):
        """#786: a script is analysis because of what it is, whatever it is called."""
        state = self._state(
            docs=("4.1 Deiodinase activity assay.docx", "plot.py"),
            steps=(("proc_read", "EndpointReadout"), ("proc_ana", "DataAnalysis")),
        )
        _, by_id = _build(state, tmp_path)
        assert _documents(by_id, "#LabProcess_proc_ana") == [
            f"{self.ASSAY_DIR}/plot.py"
        ]
        assert not any(
            "plot.py" in e for e in _documents(by_id, "#LabProcess_proc_read")
        )

    def test_an_analysis_with_no_document_of_its_own_is_based_on_none(self, tmp_path):
        """DataAnalysis comes up empty on the real deposit, and that is correct.

        No S-VHPS22 document describes the analysis step, so the honest output is
        a record based on nothing — not a stub document standing in for a
        procedure nobody wrote (D17).
        """
        state = self._state(
            steps=(("proc_read", "EndpointReadout"), ("proc_ana", "DataAnalysis"))
        )
        _, by_id = _build(state, tmp_path)
        assert _documents(by_id, "#LabProcess_proc_ana") == []
        assert not any(str(i).startswith("#protocol_") for i in by_id), (
            f"a stub was minted: {[i for i in by_id if str(i).startswith('#protocol_')]}"
        )

    def test_another_assays_protocol_is_not_borrowed(self, tmp_path):
        """Scoping is the assay's own hasPart, not "any protocol in the crate"."""
        state = self._state()
        state.add_entity(
            _ent(
                "doc_other",
                "File",
                name="4.3 Protocol UPLC runnen.docx",
                path="assay_03_metabolism/4.3 Protocol UPLC runnen.docx",
                additional_types=["LabProtocol"],
            )
        )
        state.add_entity(_ent("assay_2", "Assay", name="A2", study_id="study_1",
                              hasPart=["doc_other"]))
        _, by_id = _build(state, tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_read")
        assert not any("UPLC" in e for e in executed), (
            f"assay_02's readout borrowed assay_03's protocol: {executed}"
        )


class TestEveryStepExecutesOneProtocolRecord:
    """#783 / #799 — every ISA-Tox step executes exactly one protocol record.

    Bioschemas LabProcess and ISA-JSON give a process ONE protocol. The build
    writes it: a LabProtocol at ``<process>_protocol`` whose ``intendedUse`` is
    the step's protocol type as an ontology term, which is ``isBasedOn`` the
    documents the deposit places on the step, or what the drafter named where it
    places none, and whose ``reagent``s, on the Exposure, are the compounds applied. It carries no
    description of the builder's own: that is the lab's to state (#670).
    """

    TERMS = {
        "#LabProcess_p_tsp": "EFO:0003789",
        "#LabProcess_p_exp": "EFO:0003969",
        "#LabProcess_p_read": "BAO:0000523",
        "#LabProcess_p_da": "OBI:0200000",
    }

    def _state(self):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(
            _ent("sop", "File", name="SOP.docx", path="assay/SOP.docx",
                 additional_types=["LabProtocol"])
        )
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1", hasPart=["sop"]))
        state.add_entity(_ent("cell", "CellLineSample", name="HepG2", accession="CVCL_0027"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        state.add_entity(_ent("raw", "File", name="raw.csv", path="assay/raw.csv"))
        state.add_entity(_ent("out", "File", name="out.csv", path="assay/out.csv"))
        state.add_entity(
            _ent("p_tsp", "LabProcess", name="Culture", process_type="TestSystemPreparation",
                 assay_id="assay_1", cell_line="cell", culture_medium="DMEM")
        )
        state.add_entity(
            _ent("p_exp", "LabProcess", name="Exposure", process_type="Exposure",
                 assay_id="assay_1", chemicals="chem_1", duration="24 h", labprotocol="sop")
        )
        state.add_entity(
            _ent("p_read", "LabProcess", name="Readout", process_type="EndpointReadout",
                 assay_id="assay_1", result="raw", detection_instrument="plate reader")
        )
        state.add_entity(
            _ent("p_da", "LabProcess", name="Analysis", process_type="DataAnalysis",
                 assay_id="assay_1", object="raw", result="out", software="Prism")
        )
        return state

    def test_each_step_executes_its_typed_record(self, tmp_path):
        from profiles.ontology_iris import iri

        _, by_id = _build(self._state(), tmp_path)
        for pid, curie in self.TERMS.items():
            executed = _ids(by_id[pid].get("executesLabProtocol"))
            assert executed == [f"{pid}_protocol"], (pid, executed)
            record = by_id[executed[0]]
            types = record["@type"] if isinstance(record["@type"], list) else [record["@type"]]
            assert "LabProtocol" in types and "File" not in types, types
            assert "description" not in record, record
            assert "additionalType" not in by_id[pid], by_id[pid]
            assert record.get("name") == f"{by_id[pid]['name']} protocol", record
            assert record.get("intendedUse") == {"@id": iri(curie)}, record
            term = by_id[iri(curie)]
            assert term["@type"] == "DefinedTerm" and term["termCode"] == curie
            assert term["name"]
        labelled = [n["@id"] for n in by_id.values() if "Sample" in str(n.get("@type"))
                    and n.get("additionalType")]
        assert labelled == [], "a source is typed by its sampleType (#799)"

    def test_the_exposure_record_names_its_compounds_and_its_sop(self, tmp_path):
        graph, by_id = _build(self._state(), tmp_path)
        record = by_id["#LabProcess_p_exp_protocol"]
        assert _ids(record.get("reagent")) == ["#MolecularEntity_chem_1"], record
        assert _ids(record.get("isBasedOn")) == ["assay/SOP.docx"], record
        executed = {i for n in graph for i in _ids(n.get("executesLabProtocol"))}
        assert "assay/SOP.docx" not in executed, "only records are executed"
        assert by_id["assay/SOP.docx"]["@type"] == ["File", "LabProtocol"]

    def test_the_deposit_outranks_what_was_named(self, tmp_path):
        """#789: a record is based on the deposit's documents for its step, and on a
        document the drafter named only where the deposit places none. What the lab
        stated about a step fills that step's record; it is no node of its own."""
        state = self._state()
        state.add_entity(
            _ent("stated", "LabProtocol", name="Readout procedure", description="Read at 450 nm.")
        )
        state.get_entity("p_read").fields["labprotocol"] = "stated"
        state.get_entity("p_da").fields["labprotocol"] = "sop"
        _, by_id = _build(state, tmp_path)
        read = by_id[_ids(by_id["#LabProcess_p_read"]["executesLabProtocol"])[0]]
        assert _ids(read.get("isBasedOn")) == ["assay/SOP.docx"]
        assert (read["name"], read["description"]) == ("Readout procedure", "Read at 450 nm.")
        assert _documents(by_id, "#LabProcess_p_da") == ["assay/SOP.docx"]
        assert not [i for i in by_id if i.startswith("#LabProtocol_")]

    def test_a_record_never_replaces_an_entity_holding_its_id(self, tmp_path):
        """``<process>_protocol`` can already be taken: by a step drafted as
        "Exposure protocol" beside "Exposure", or by an entity drafted under the id
        a finding reported. The record steps aside instead of replacing it."""
        state = self._state()
        state.add_entity(
            _ent("p_exp_protocol", "LabProcess", name="Exposure protocol",
                 process_type="EndpointReadout", assay_id="assay_1")
        )
        state.add_entity(_ent("#LabProcess_p_read_protocol", "Sample", name="Drafted"))
        _, by_id = _build(state, tmp_path)
        assert "LabProcess" in str(by_id["#LabProcess_p_exp_protocol"]["@type"])
        assert by_id["#LabProcess_p_read_protocol"]["name"] == "Drafted"
        for pid in ("#LabProcess_p_exp", "#LabProcess_p_read"):
            (record,) = _ids(by_id[pid].get("executesLabProtocol"))
            assert record != f"{pid}_protocol", record
            assert "LabProtocol" in str(by_id[record]["@type"]), by_id[record]
        (exp_record,) = _ids(by_id["#LabProcess_p_exp"]["executesLabProtocol"])
        assert _ids(by_id[exp_record].get("reagent")) == ["#MolecularEntity_chem_1"]


class TestLabProcessSubtypes:
    def _state_with_process(self, process_type, **proc_fields):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(
            _ent(
                "proc_1",
                "LabProcess",
                name=process_type,
                process_type=process_type,
                assay_id="assay_1",
                **proc_fields,
            )
        )
        return state

    def test_cell_culture(self, tmp_path):
        state = self._state_with_process(
            "TestSystemPreparation",
            cell_line="cell_1",
            culture_medium="DMEM",
            result="sample_out",
        )
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2", accession="CVCL_0027"))
        state.add_entity(_ent("sample_out", "Sample", name="cultured"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert _kind(by_id, proc["@id"]) == "TestSystemPreparation"
        # No document is invented: this deposit holds no culture document, so the
        # step's record is based on nothing (D17).
        assert _documents(by_id, "#LabProcess_proc_1") == []
        # input → schema:object, output → schema:result (via the @context).
        # An accession-backed CellLineSample carries its Cellosaurus IRI as @id,
        # and the process wiring must follow it.
        assert "https://www.cellosaurus.org/CVCL_0027" in _ids(proc.get("input"))
        assert "#Sample_sample_out" in _ids(proc.get("output"))

    def test_exposure_object_is_cells_result_is_the_exposed_sample(self, tmp_path):
        # ISA forbids a MolecularEntity as a process object (objects MUST be
        # File/Sample/BioSample). The compound is therefore NOT in `object`; it
        # reaches the process as a `reagent` of the Exposure's protocol record
        # (#783). The Exposure's own result is the exposed Sample (#650).
        state = self._state_with_process(
            "Exposure",
            samples="sample_cult",
            chemicals="chem_1",
            duration="24h",
            microplate="96-well",
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert _kind(by_id, proc["@id"]) == "Exposure"

        obj_ids = _ids(proc.get("input"))  # input → schema:object
        assert "#Sample_sample_cult" in obj_ids  # the cells (Sample) — allowed
        assert "#MolecularEntity_chem_1" not in obj_ids  # MolecularEntity — ISA-forbidden

        result_ids = _ids(proc.get("output"))  # output → schema:result (MUST)
        assert result_ids, "Exposure MUST emit a result (the exposed Sample)"
        exposed = by_id[result_ids[0]]
        et = (
            exposed["@type"]
            if isinstance(exposed["@type"], list)
            else [exposed["@type"]]
        )
        assert "Sample" in et, f"Exposure result must be the exposed Sample: {et}"
        assert "#Sample_sample_cult" in _ids(exposed.get("derivesFrom")), (
            "the exposed Sample must derive from the cultured one"
        )

        # the compound reaches the process as a reagent of its protocol record,
        # and the condition table names it too
        (record,) = _ids(proc.get("executesLabProtocol"))
        assert "#MolecularEntity_chem_1" in _ids(by_id[record].get("reagent"))
        table = next(n for i, n in by_id.items() if str(i).endswith("condition_table.csv"))
        assert "#MolecularEntity_chem_1" in _ids(table.get("about"))

    def test_exposure_condition_table_is_typed_csvw(self, tmp_path):
        # Issue #94 / #180: the condition table must be typed CSVW — a linked
        # schema entity with the gold crate's full 10-column schema, each column
        # carrying datatype + propertyUrl, the cell-line/compound columns
        # resolving to their entity ids (valueUrl).
        state = self._state_with_process(
            "Exposure",
            samples="sample_cult",
            chemicals="chem_1",
            duration="24h",
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        _, by_id = _build(state, tmp_path)
        table = next(n for i, n in by_id.items() if str(i).endswith("condition_table.csv"))

        # the table is linked to a schema entity via conformsTo (not a bare key)
        schema_ids = [s for s in _ids(table.get("conformsTo")) if "schema" in s]
        assert schema_ids, "condition table must conformTo a schema entity"
        schema = by_id[schema_ids[0]]
        stype = schema["@type"] if isinstance(schema["@type"], list) else [schema["@type"]]
        assert "csvw:Schema" in stype

        cols = [by_id[cid] for cid in _ids(schema.get("columns"))]
        by_title = {c["titles"]: c for c in cols}
        assert set(by_title) == {
            "well_id",
            "assay",
            "cell_line",
            "compound",
            "concentration_value",
            "concentration_unit",
            "exposure_duration",
            "experiment",
            "technical_replicate",
            "control",
        }
        # every column is typed: datatype + propertyUrl
        for col in by_title.values():
            assert col["datatype"]
            assert col["propertyUrl"]
        # entity-resolving columns carry references to the resolved entities
        assert "#MolecularEntity_chem_1" in str(by_title["compound"].get("valueUrl"))
        assert "#Sample_sample_cult" in str(by_title["cell_line"].get("valueUrl"))

    def test_the_condition_table_is_data_about_the_exposed_sample(self, tmp_path):
        """#783 — the table is the registry of the exposed units, not a protocol.

        One row per well, so it is ``about`` the exposed Sample(s) the Exposure
        produced. It is data of its assay, nested there and kept in the root's
        ``hasPart`` (#532), and no step executes it.
        """
        state = self._state_with_process(
            "Exposure", samples="sample_cult", chemicals="chem_1", duration="24h"
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        graph, by_id = _build(state, tmp_path)
        table = next(n for n in graph if str(n["@id"]).endswith("condition_table.csv"))
        assert table["@type"] == ["File", "csvw:Table"]
        assert "reagent" not in table and "intendedUse" not in table, table
        exposed = [
            i
            for i in _ids(by_id["#LabProcess_proc_1"].get("output"))
            if "Sample" in str(by_id[i]["@type"])
        ]
        about = _ids(table.get("about"))
        assert exposed and set(exposed) <= set(about), (exposed, about)
        assert "#Sample_sample_cult" not in about, "the cultured Sample is not what it lists"
        assert table["@id"] in _ids(by_id["#Assay_assay_1"].get("hasPart"))
        assert table["@id"] in _ids(by_id["./"].get("hasPart"))
        executed = {i for n in graph for i in _ids(n.get("executesLabProtocol"))}
        assert table["@id"] not in executed

    def test_endpoint_readout_result_is_file(self, tmp_path):
        state = self._state_with_process(
            "EndpointReadout",
            samples="sample_x",
            result="file_raw",
            endpoint="viability",
            detection_instrument="plate reader",
        )
        state.add_entity(_ent("sample_x", "Sample", name="exposed"))
        state.add_entity(
            _ent(
                "file_raw",
                "File",
                name="raw.csv",
                dest_path="assays/a1/dataset/raw_data/raw.csv",
            )
        )
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert _kind(by_id, proc["@id"]) == "EndpointReadout"
        assert "assays/a1/dataset/raw_data/raw.csv" in _ids(proc.get("output"))

    def test_data_analysis(self, tmp_path):
        state = self._state_with_process(
            "DataAnalysis",
            object="file_raw",
            result="file_proc",
            software="R",
            data_processing="normalise",
        )
        state.add_entity(_ent("file_raw", "File", name="raw.csv", dest_path="raw.csv"))
        state.add_entity(_ent("file_proc", "File", name="out.csv", dest_path="out.csv"))
        _, by_id = _build(state, tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert _kind(by_id, proc["@id"]) == "DataAnalysis"
        assert "raw.csv" in _ids(proc.get("input"))
        assert "out.csv" in _ids(proc.get("output"))

    def test_process_attached_to_assay_via_about(self, tmp_path):
        state = self._state_with_process("TestSystemPreparation", cell_line="cell_1")
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        _, by_id = _build(state, tmp_path)
        assert "#LabProcess_proc_1" in _ids(by_id["#Assay_assay_1"].get("about"))

    def test_no_protocol_is_invented_when_the_deposit_has_none(self, tmp_path):
        """A stub document claims a procedure nobody wrote.

        This used to synthesize `#protocol_<assay>` for every step that named no
        protocol of its own — an assertion that cannot fail is not a check (#620)
        — and put an entity in the crate whose `@id` was a fragment rather than a
        file anyone deposited.

        A deposited protocol IS its file. With no such document, the step's record
        is based on nothing, and the gap stays visible (D17).
        """
        state = self._state_with_process("TestSystemPreparation", cell_line="cell_1")
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        graph, by_id = _build(state, tmp_path)
        assert _documents(by_id, "#LabProcess_proc_1") == []
        invented = [
            n["@id"] for n in graph if str(n.get("@id", "")).startswith("#protocol_")
        ]
        assert invented == [], f"no protocol entity may be minted: {invented}"

    def test_an_attached_protocol_is_the_file_itself(self, tmp_path):
        """A deposited protocol IS its document — @id is its path — and the step's
        record is based on it.

        No `schema:HowTo` companion is added here, and that is right: the supertype
        pass exists so every entity carries a type in the schema.org namespace, and
        `File` already is one (the context maps it to `schema:MediaObject`). The
        companion is for a node that would otherwise have none, like a bare
        `LabProtocol`.
        """
        state = self._state_with_process("TestSystemPreparation", cell_line="cell_1")
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        state.add_entity(
            _ent(
                "proto_doc",
                "File",
                name="cell culture protocol HepG2.docx",
                path="cell_line_protocols/cell culture protocol HepG2.docx",
                additional_types=["LabProtocol"],
            )
        )
        _, by_id = _build(state, tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_1")
        assert executed, "the culture's record must be based on the deposited protocol"
        assert not executed[0].startswith("#"), (
            f"a protocol's @id must be its file path, not a minted fragment: {executed}"
        )
        assert "cell_line_protocols" in executed[0], executed
        proto = by_id[executed[0]]
        types = proto["@type"] if isinstance(proto["@type"], list) else [proto["@type"]]
        assert "LabProtocol" in types and "File" in types, types


class TestOntologyAnnotations:
    """AOP on the Study, Key Event on the Assay (paper §Methods, via mentions)."""

    def test_study_annotated_with_aop_entity(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S", aop="aop_37"))
        state.add_entity(_ent("aop_37", "DefinedTerm", name="AOP 37"))
        _, by_id = _build(state, tmp_path)
        assert "#DefinedTerm_aop_37" in _ids(by_id["#Study_study_1"].get("aop"))

    def test_study_aop_inline_iri(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S", aop="https://aopwiki.org/aops/37"))
        _, by_id = _build(state, tmp_path)
        assert "https://aopwiki.org/aops/37" in _ids(by_id["#Study_study_1"].get("aop"))

    def test_assay_annotated_with_key_event(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A", key_event="ke_55"))
        state.add_entity(_ent("ke_55", "DefinedTerm", name="KE 55"))
        _, by_id = _build(state, tmp_path)
        assert "#DefinedTerm_ke_55" in _ids(by_id["#Assay_assay_1"].get("keyEvent"))

    def test_free_text_key_event_is_not_emitted(self, tmp_path):
        """#382: prose on ``keyEvent`` reaches no crate — it must not be stored.

        ``_wire_mention`` emits a mention only for a resolvable entity, an inline
        ``{"@id": …}`` or a bare IRI, so a depositor's words ("Mitochondrial
        dysfunction") are dropped without a trace. This is why the guidance tail
        resolves such an answer through ``link_assay_to_key_event`` instead of
        committing it: a literal commit would report success and lose the answer.
        Keep the absence asserted — "fixing" this by keeping literals would put an
        unresolvable string into the exported crate.
        """
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A", keyEvent="Mitochondrial dysfunction"))
        _, by_id = _build(state, tmp_path)
        assert "keyEvent" not in by_id["#Assay_assay_1"]

    def test_mentions_resolve_by_typed_key_when_study_and_assay_share_id(self, tmp_path):
        """Issue #93: with a Study and an Assay sharing an entity_id, each must
        receive its own mention annotations.

        ``_idx_add`` registers the bare ``entity_id`` key only for whichever
        entity is added first (the Study, since structural datasets add studies
        before assays). Resolving mentions via the bare key therefore mis-attached
        the Assay's keyEvent to the Study node. Resolution must use the
        type-qualified ``{type}:{entity_id}`` key instead.
        """
        state = CrateState()
        state.add_entity(_ent("shared", "Study", name="S", aop="https://aopwiki.org/aops/37"))
        state.add_entity(
            _ent(
                "shared",
                "Assay",
                name="A",
                study_id="shared",
                key_event="https://aopwiki.org/events/888",
            )
        )
        _, by_id = _build(state, tmp_path)

        study = by_id["#Study_shared"]
        assay = by_id["#Assay_shared"]
        # Each annotation lands on its own node…
        assert "https://aopwiki.org/aops/37" in _ids(study.get("aop"))
        assert "https://aopwiki.org/events/888" in _ids(assay.get("keyEvent"))
        # …and not leaked onto the other node via the colliding bare key.
        assert "https://aopwiki.org/events/888" not in _ids(study.get("keyEvent"))
        assert "https://aopwiki.org/aops/37" not in _ids(assay.get("aop"))


class TestIdentifiersAndConformance:
    def test_local_ids_are_hash_prefixed(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("study_1", "Study", name="S"))
        _, by_id = _build(state, tmp_path)
        assert "#Study_study_1" in by_id
        # bare id must not be emitted (type-qualified fragment is used instead)
        assert "study_1" not in by_id

    def test_person_uses_orcid_uri(self, tmp_path):
        state = CrateState()
        state.add_entity(_ent("person_1", "Person", name="Jane Doe", orcid="0000-0002-1825-0097"))
        _, by_id = _build(state, tmp_path)
        assert "https://orcid.org/0000-0002-1825-0097" in by_id

    def test_profiles_declared_on_root_data_entity(self, tmp_path):
        # Issue #91: RO-Crate 1.2 recommends profile declarations on the Root
        # Data Entity (./), reserving the metadata descriptor's conformsTo for
        # the single base-spec URI. The ISA + ISA-Tox profiles the crate TARGETS
        # are therefore declared on ./ — unconditionally, on a fresh state (#89).
        state = CrateState()
        _, by_id = _build(state, tmp_path)

        root_conforms = _ids(by_id["./"].get("conformsTo"))
        assert "https://github.com/nfdi4plants/isa-ro-crate-profile" in root_conforms
        assert "https://w3id.org/ro/crate/isa-tox/1.0" in root_conforms
        # both declared profiles also exist as Profile contextual entities
        for pid in (
            "https://github.com/nfdi4plants/isa-ro-crate-profile",
            "https://w3id.org/ro/crate/isa-tox/1.0",
        ):
            pt = by_id[pid]["@type"]
            assert "Profile" in (pt if isinstance(pt, list) else [pt])

    def test_descriptor_conformsto_is_base_spec_only(self, tmp_path):
        # Issue #91/#110: the metadata file descriptor's conformsTo is reserved
        # for the single base-spec URI (profiles moved to ./). The base spec is
        # now 1.2 — roc-validator 0.11.0 ships a ro-crate-1.2 base profile
        # (crs4/rocrate-validator#164), so the #105 deferral is lifted.
        state = CrateState()
        _, by_id = _build(state, tmp_path)
        desc_conforms = _ids(by_id["ro-crate-metadata.json"].get("conformsTo"))
        assert desc_conforms == ["https://w3id.org/ro/crate/1.2"]

    def test_every_protocol_record_is_a_result_of_the_build(self, tmp_path):
        """#783 — the build's CreateAction states it wrote each protocol record
        from what the deposit holds, beside the crate itself."""
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("cell_1", "CellLineSample", name="HepG2"))
        state.add_entity(
            _ent("proc_1", "LabProcess", name="Culture", process_type="TestSystemPreparation",
                 assay_id="assay_1", cell_line="cell_1", culture_medium="DMEM")
        )
        graph, _ = _build(state, tmp_path)
        records = {i for n in graph for i in _ids(n.get("executesLabProtocol"))}
        assert records, "no step executes a record — this pins nothing"
        action = next(n for n in graph if n.get("@type") == "CreateAction")
        assert {"./"} | records <= set(_ids(action.get("result"))), action.get("result")


class TestTheCultureRecordsEveryCellLine:
    """Issue #650 — a culture of two cell lines must say so.

    ``cell_line`` is a list field: S-VHPS22's "Neural cell culture for thyroid
    hormone uptake" names SK-N-AS *and* H4. The build read it with
    ``_resolve_one``, which takes the head of the list, so the second line was
    dropped — H4 was cultured by nothing in the whole crate despite carrying its
    own cell-line entity and its own study-level culture protocol.
    """

    def _culture_state(self, cell_line):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS"))
        state.add_entity(_ent("cell_b", "CellLineSample", name="H4"))
        state.add_entity(
            _ent(
                "proc_1",
                "LabProcess",
                name="Neural cell culture",
                process_type="TestSystemPreparation",
                assay_id="assay_1",
                cell_line=cell_line,
                culture_medium="DMEM",
            )
        )
        return state

    def test_every_named_cell_line_is_cultured(self, tmp_path):
        """#650's guarantee, under #678's shape.

        The line must not be dropped — that was the defect. It no longer reaches
        *one* process, because a culture of two lines is now two culturing
        activities; it reaches one of them.
        """
        state = self._culture_state(["cell_a", "cell_b"])
        graph, _ = _build(state, tmp_path)
        consumed = {
            cid
            for n in with_kinds(graph)
            if n.get("additionalType") == "TestSystemPreparation"
            for cid in _ids(n.get("input"))
        }
        assert "#CellLineSample_cell_a" in consumed, consumed
        assert "#CellLineSample_cell_b" in consumed, (
            f"the second cell line was dropped: {consumed}"
        )

    def test_no_cultured_sample_derives_from_more_than_one_line(self, tmp_path):
        """Reversed by #678, deliberately.

        This test used to assert the opposite — that one cultured Sample derived
        from *both* lines. That is a co-culture claim, and S-VHPS22's cells were
        cultured separately: the deposit ships one culture protocol per line. The
        lineage is still complete, but it is spread over one sample per line
        rather than concentrated in a mixture.
        """
        state = self._culture_state(["cell_a", "cell_b"])
        graph, by_id = _build(state, tmp_path)
        lineage = set()
        for n in with_kinds(graph):
            if n.get("additionalType") != "TestSystemPreparation":
                continue
            for out_id in _ids(n.get("output")):
                derived = _ids(by_id[out_id].get("derivesFrom"))
                assert len(derived) <= 1, (
                    f"{out_id} derives from {derived} — that asserts a co-culture"
                )
                lineage.update(derived)
        assert {"#CellLineSample_cell_a", "#CellLineSample_cell_b"} <= lineage, (
            f"both lines must still be accounted for across the cultures: {lineage}"
        )

    def test_a_single_cell_line_still_works(self, tmp_path):
        state = self._culture_state("cell_a")
        _, by_id = _build(state, tmp_path)
        assert "#CellLineSample_cell_a" in _ids(
            by_id["#LabProcess_proc_1"].get("input")
        )


class TestACultureFindsTheCellLineItIsNamedFor:
    """Issue #650 — a process called "SK-N-AS culture for …" must not consume a
    synthesized stub while the SK-N-AS entity sits in the same crate.

    S-VHPS22's TR-activation culture carried no ``cell_line`` field, so the build
    fell through to ``_synth_sample(pid + "_input")`` and the crate recorded a
    placeholder as the material being cultured.

    ``wire_unreferenced_domain_entities`` does not catch this: it rescues
    *orphans*, and ``cell_sk_n_as`` was referenced by three other cultures, so it
    was never orphaned and the culture that needed it was left alone. These
    fixtures reproduce that — a second culture holds the reference — rather than
    the easy single-culture case the wiring pass already handles.
    """

    def _state(self, proc_name, lines, *, anchored):
        """*lines* are (id, name); *anchored* ids are referenced by a second
        culture, so the orphan-wiring pass cannot rescue them."""
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        for eid, nm in lines:
            state.add_entity(_ent(eid, "CellLineSample", name=nm))
        state.add_entity(
            _ent(
                "proc_anchor",
                "LabProcess",
                name="Anchor culture",
                process_type="TestSystemPreparation",
                assay_id="assay_1",
                cell_line=list(anchored),
                culture_medium="DMEM",
            )
        )
        state.add_entity(
            _ent(
                "proc_1",
                "LabProcess",
                name=proc_name,
                process_type="TestSystemPreparation",
                assay_id="assay_1",
                culture_medium="DMEM",
            )
        )
        return state

    def test_a_cell_line_named_only_in_the_title_is_resolved(self, tmp_path):
        state = self._state(
            "SK-N-AS culture for thyroid hormone receptor activation",
            [("cell_a", "SK-N-AS"), ("cell_b", "H4")],
            anchored=("cell_a", "cell_b"),
        )
        _, by_id = _build(state, tmp_path)
        consumed = _ids(by_id["#LabProcess_proc_1"].get("input"))
        assert "#CellLineSample_cell_a" in consumed, (
            f"the cell line named in the title must be consumed, not a stub: {consumed}"
        )

    def test_a_title_naming_nothing_still_gets_its_placeholder(self, tmp_path):
        """No match is not a licence to guess."""
        state = self._state(
            "Neural cell culture",
            [("cell_a", "SK-N-AS"), ("cell_b", "H4")],
            anchored=("cell_a", "cell_b"),
        )
        _, by_id = _build(state, tmp_path)
        consumed = _ids(by_id["#LabProcess_proc_1"].get("input"))
        assert not (
            {"#CellLineSample_cell_a", "#CellLineSample_cell_b"} & set(consumed)
        ), f"nothing in the name matched, so nothing may be claimed: {consumed}"
        assert consumed, "a TestSystemPreparation MUST still consume something"

    def test_two_matching_names_are_not_guessed_between(self, tmp_path):
        """An ambiguous title picks neither."""
        state = self._state(
            "SK-N-AS and H4 culture",
            [("cell_a", "SK-N-AS"), ("cell_b", "H4")],
            anchored=("cell_a", "cell_b"),
        )
        _, by_id = _build(state, tmp_path)
        consumed = _ids(by_id["#LabProcess_proc_1"].get("input"))
        assert not (
            {"#CellLineSample_cell_a", "#CellLineSample_cell_b"} & set(consumed)
        ), f"two lines match the title, so neither may be picked: {consumed}"


class TestTheCultureProtocolIsStudyLevel:
    """Issue #650 — culturing a cell line is a study-level activity, so its
    protocol is one deposited document shared by every assay that grows that line.

    A culture with no protocol of its own used to fall through to a synthesized
    `#protocol_<assay>` stub, so two assays growing SK-N-AS invented two different
    protocols for the same procedure while the deposit's real documents were
    referenced by nothing. S-VHPS22 keeps `cell_line_protocols/` at study level
    with exactly one document per line — H4, MO3.13, SK-N-AS — and none for any
    combination.

    A deposited protocol IS its file: `@id` is the document's path, with `name`
    derived on top, and each culture's protocol record is based on it (#783).
    Nothing is minted.
    """

    CULTURE_DOCS = (
        ("proto_sk", "cell culture protocol SK-N-AS.docx"),
        ("proto_h4", "cell culture protocol H4.docx"),
    )

    def _two_assays(self, *, second_line="cell_a", docs=CULTURE_DOCS, extra_docs=()):
        state = CrateState()
        state.metadata.input_path = "/deposit"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A1", study_id="study_1"))
        state.add_entity(_ent("assay_2", "Assay", name="A2", study_id="study_1"))
        state.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS"))
        state.add_entity(_ent("cell_b", "CellLineSample", name="H4"))
        for eid, nm in tuple(docs) + tuple(extra_docs):
            state.add_entity(
                _ent(
                    eid,
                    "File",
                    name=nm,
                    path=f"cell_line_protocols/{nm}",
                    additional_types=["LabProtocol"],
                )
            )
        for n, (assay, line) in enumerate(
            (("assay_1", "cell_a"), ("assay_2", second_line)), start=1
        ):
            state.add_entity(
                _ent(
                    f"proc_{n}",
                    "LabProcess",
                    name=f"Culture {n}",
                    process_type="TestSystemPreparation",
                    assay_id=assay,
                    cell_line=[line],
                    culture_medium="DMEM",
                )
            )
        return state

    def test_two_assays_growing_one_line_share_a_protocol(self, tmp_path):
        _, by_id = _build(self._two_assays(), tmp_path)
        first = _documents(by_id, "#LabProcess_proc_1")
        second = _documents(by_id, "#LabProcess_proc_2")
        assert first and set(first) == set(second), (
            "two assays culturing SK-N-AS follow ONE document; "
            f"got {first} and {second}"
        )

    def test_the_protocol_is_the_document_itself(self, tmp_path):
        _, by_id = _build(self._two_assays(), tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_1")
        assert executed and not executed[0].startswith("#"), (
            f"a protocol's @id must be its file path, not a minted fragment: {executed}"
        )
        assert "cell_line_protocols" in executed[0], executed

    def test_cultures_of_different_lines_do_not_share(self, tmp_path):
        _, by_id = _build(self._two_assays(second_line="cell_b"), tmp_path)
        first = _documents(by_id, "#LabProcess_proc_1")
        second = _documents(by_id, "#LabProcess_proc_2")
        assert first and second and set(first) != set(second), (
            f"different cell lines are different procedures: {first} vs {second}"
        )

    def test_a_line_takes_its_own_document_before_the_named_protocol(self, tmp_path):
        """#789, per line: a line the deposit holds a document for is based on it
        alone, never on another line's; what the lab stated about the culture fills
        each line's record."""
        state = self._two_assays(docs=self.CULTURE_DOCS[:1])
        state.add_entity(
            _ent("named", "LabProtocol", name="Culture procedure", description="DMEM, 37 °C.")
        )
        state.list_entities("LabProcess")[0].set_fields_from_dict(
            {"cell_line": ["cell_a", "cell_b"], "labprotocol": "named"}, source="llm"
        )
        _, by_id = _build(state, tmp_path)
        (own,) = _documents(by_id, "#LabProcess_proc_1_SK-N-AS")
        assert "SK-N-AS" in own, own
        assert _documents(by_id, "#LabProcess_proc_1_H4") == []
        (h4,) = _ids(by_id["#LabProcess_proc_1_H4"]["executesLabProtocol"])
        assert by_id[h4]["name"] == "Culture procedure"

    def test_a_culture_of_two_lines_follows_one_document_per_line(self, tmp_path):
        """One document per cell line, never a composite: the deposit holds no
        document for any combination of lines.

        Since #678 the two lines are two cultures, so "one document per line"
        means each part's record is based on its own single document rather than
        one part's on two. The document itself is still shared by identity — a
        protocol is study-level, and the same SK-N-AS procedure serves every
        assay that grows SK-N-AS.
        """
        state = self._two_assays()
        state.list_entities("LabProcess")[0].set_fields_from_dict(
            {"cell_line": ["cell_a", "cell_b"]}, source="llm"
        )
        graph, by_id = _build(state, tmp_path)
        parts = [
            n
            for n in with_kinds(graph)
            if n.get("additionalType") == "TestSystemPreparation"
            and str(n["@id"]).startswith("#LabProcess_proc_1")
        ]
        assert len(parts) == 2, f"a culture of two lines is two cultures: {parts}"
        for part in parts:
            executed = _documents(by_id, part["@id"])
            assert len(executed) == 1, (
                f"{part['@id']} follows {executed} — one line, one document"
            )
        mixed = {e for part in parts for e in _documents(by_id, part["@id"])}
        assert len(mixed) == 2, (
            f"two lines follow two distinct documents, not a composite: {mixed}"
        )
        single = _documents(by_id, "#LabProcess_proc_2")
        assert set(single) <= mixed, (
            f"the SK-N-AS document must be the SAME entity in both: {mixed} / {single}"
        )

    def test_no_protocol_is_invented_when_the_deposit_has_none(self, tmp_path):
        graph, by_id = _build(self._two_assays(docs=()), tmp_path)
        assert _documents(by_id, "#LabProcess_proc_1") == []
        invented = [
            n["@id"] for n in graph if str(n.get("@id", "")).startswith("#protocol_")
        ]
        assert invented == [], f"no protocol entity may be minted: {invented}"

    def test_a_protocol_for_a_different_step_is_not_claimed_as_the_culture_one(
        self, tmp_path
    ):
        """Naming the cell line is not enough — the document must be about
        culturing.

        A file classified as a protocol and naming SK-N-AS may still describe the
        deiodinase readout. Basing the TestSystemPreparation's protocol on it would
        assert it explains how that line was grown, which nobody checked.
        """
        state = self._two_assays(
            docs=(), extra_docs=(("proto_assay", "4.1 Deiodinase assay protocol SK-N-AS.docx"),)
        )
        _, by_id = _build(state, tmp_path)
        executed = _documents(by_id, "#LabProcess_proc_1")
        assert not any("Deiodinase" in str(i) for i in executed), (
            "an assay protocol that merely names the cell line must not be "
            f"claimed as the culture protocol: {executed}"
        )
        assert executed == [], (
            f"and nothing may be invented in its place: {executed}"
        )

    def test_the_culture_protocol_is_linked_to_the_study(self, tmp_path):
        """Followed by its process AND hung off the Study it governs — a protocol
        shared by several assays is a study-level document."""
        _, by_id = _build(self._two_assays(), tmp_path)
        executed = set(_documents(by_id, "#LabProcess_proc_1"))
        study = by_id["#Study_study_1"]
        linked = set(_ids(study.get("hasPart"))) | set(_ids(study.get("mentions")))
        assert executed and executed <= linked, (
            f"the culture protocol must hang off the Study too; executed={executed} "
            f"study links={linked}"
        )

    def test_a_deposited_protocol_is_a_part_of_the_study(self, tmp_path):
        """A real document is payload, so it belongs in the Study's hasPart."""
        _, by_id = _build(self._two_assays(), tmp_path)
        parts = _ids(by_id["#Study_study_1"].get("hasPart"))
        assert any("cell_line_protocols" in str(i) for i in parts), (
            f"a deposited culture protocol belongs in the Study's hasPart: {parts}"
        )

    def test_a_scanned_protocol_document_is_attached(self, tmp_path):
        """The S-VHPS22 case: the document is in the deposit but nobody attached it.

        Its three `cell_line_protocols/*.docx` were scanned and never became File
        entities — 48 entities from 54 scanned files — so the crate invented stubs
        while the real protocols sat in the deposit unreferenced. The scan is the
        evidence the file exists; the builder attaches it rather than mint anything.
        """
        deposit = tmp_path / "deposit" / "cell_line_protocols"
        deposit.mkdir(parents=True)
        doc = deposit / "20251114_cell culture protocol SK-N-AS.docx"
        doc.write_text("Materials and methods: incubate and pipette the culture.")

        state = self._two_assays(docs=())
        state.metadata.input_path = str(tmp_path / "deposit")
        state.scanned_files = [
            FileClassification(
                path=str(doc),
                filename=doc.name,
                size=doc.stat().st_size,
                mime_type=(
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"
                ),
            )
        ]
        graph, by_id = _build(state, tmp_path / "out")
        executed = _documents(by_id, "#LabProcess_proc_1")
        assert executed, "the deposit's own culture protocol must be attached"
        assert "cell_line_protocols" in executed[0], executed
        attached = by_id[executed[0]]
        types = (
            attached["@type"]
            if isinstance(attached["@type"], list)
            else [attached["@type"]]
        )
        assert "File" in types and "LabProtocol" in types, types
        # What kind of protocol it is belongs to the culture's record (#783).
        assert "intendedUse" not in attached, attached
        assert not any(
            str(n.get("@id", "")).startswith("#protocol_") for n in graph
        ), "nothing may be minted alongside the real document"

    def test_the_builder_writes_no_free_text_intended_use(self, tmp_path):
        """A step's protocol type is an ontology term on its record (#783, #799).

        The builder used to stamp its own sentences — "Cell culture", "Per-well
        exposure layout" — on the documents and the condition table.
        """
        from profiles.ontology_iris import iri

        state = self._two_assays()
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin A"))
        state.add_entity(
            _ent("proc_exp", "LabProcess", name="Exposure", process_type="Exposure",
                 assay_id="assay_1", chemicals="chem_1", duration="24h")
        )
        graph, by_id = _build(state, tmp_path)
        assert by_id["#LabProcess_proc_1_protocol"]["intendedUse"] == {
            "@id": iri("EFO:0003789")
        }
        assert not [n["@id"] for n in graph if isinstance(n.get("intendedUse"), str)]


class TestSynthesizedSamplesCarryTheirType:
    """Issue #650 — a cultured or exposed sample says what kind of material it is.

    Both are cell cultures: `OBI:0001876` is *"a material entity comprised of
    cultured cells and the media in which they are being propagated or stored"*,
    which is exactly what they are. Exposure changes a sample's **state**, not its
    kind, so both carry the same type — and no `tox:` class is invented for either.

    The distinction between them is the compound and dose the exposed one received.
    Those are ISA **Factors**, and a factor cannot be attached to a sample that
    stands for several wells at once: one sample carrying FactorValues for ten
    compounds would assert a ten-compound cocktail. They arrive with the per-arm
    samples in #654.
    """

    def _chain(self):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS"))
        state.add_entity(_ent("chem_1", "MolecularEntity", name="Amiodarone"))
        state.add_entity(
            _ent(
                "proc_cult",
                "LabProcess",
                name="Culture",
                process_type="TestSystemPreparation",
                assay_id="assay_1",
                cell_line=["cell_a"],
                culture_medium="DMEM",
            )
        )
        state.add_entity(
            _ent(
                "proc_exp",
                "LabProcess",
                name="Expose",
                process_type="Exposure",
                assay_id="assay_1",
                chemicals=["chem_1"],
                duration="24 h",
            )
        )
        return state

    @staticmethod
    def _sample_of(by_id, process_id):
        out = _ids(by_id[process_id].get("output"))
        return [by_id[i] for i in out if "Sample" in str(by_id[i].get("@type"))]

    def test_the_cultured_sample_declares_its_type(self, tmp_path):
        _, by_id = _build(self._chain(), tmp_path)
        samples = self._sample_of(by_id, "#LabProcess_proc_cult")
        assert samples, "the culture produced no Sample"
        term_ids = _ids(samples[0].get("sampleType"))
        assert term_ids, f"a cultured sample MUST say what it is: {samples[0]}"
        term = by_id[term_ids[0]]
        assert "OBI:0001876" in str(term.get("termCode")), term

    def test_the_exposed_sample_declares_the_same_type(self, tmp_path):
        _, by_id = _build(self._chain(), tmp_path)
        cultured = self._sample_of(by_id, "#LabProcess_proc_cult")
        exposed = self._sample_of(by_id, "#LabProcess_proc_exp")
        assert exposed, "the exposure produced no Sample"
        assert _ids(exposed[0].get("sampleType")) == _ids(cultured[0].get("sampleType")), (
            "exposure changes a sample's state, not its kind — both are cell "
            f"cultures; cultured={_ids(cultured[0].get('sampleType'))} "
            f"exposed={_ids(exposed[0].get('sampleType'))}"
        )

    def test_the_term_is_one_shared_entity(self, tmp_path):
        """One DefinedTerm node, referenced twice — not two copies."""
        graph, by_id = _build(self._chain(), tmp_path)
        terms = [
            n
            for n in graph
            if "DefinedTerm" in str(n.get("@type"))
            and "OBI:0001876" in str(n.get("termCode"))
        ]
        assert len(terms) == 1, f"expected one shared cell-culture term, got {terms}"

    def test_no_factor_is_attached_to_a_sample_standing_for_many_wells(self, tmp_path):
        """A single exposed sample must not claim the compounds as its own Factors.

        The exposure names its compounds because the assay tested them across
        separate wells. Hanging them on one sample asserts a cocktail. Factors
        arrive with the per-arm samples (#654).
        """
        _, by_id = _build(self._chain(), tmp_path)
        exposed = self._sample_of(by_id, "#LabProcess_proc_exp")
        factors = [
            by_id[i]
            for i in _ids(exposed[0].get("additionalProperty"))
            if by_id.get(i, {}).get("additionalType") == "FactorValue"
        ]
        assert factors == [], (
            f"a per-exposure sample may carry no exposure Factors yet: {factors}"
        )

    def _chain_with_drafted_result(self):
        """The real-crate shape: the drafter supplies the culture's result.

        `_synth_sample` only runs when the TestSystemPreparation has no `result` of its own,
        and in a real deposit `draft_process_chain` always supplies one — so on
        every crate we have actually built, the cultured sample was a drafted
        entity that carried neither its type nor its lineage.
        """
        state = self._chain()
        state.add_entity(_ent("sample_cult", "Sample", name="cultured cells"))
        state.list_entities("LabProcess")[0].set_fields_from_dict(
            {"result": ["sample_cult"]}, source="llm"
        )
        return state

    def test_a_drafted_cultured_sample_still_declares_its_type(self, tmp_path):
        _, by_id = _build(self._chain_with_drafted_result(), tmp_path)
        sample = by_id["#Sample_sample_cult"]
        term_ids = _ids(sample.get("sampleType"))
        assert term_ids, f"a drafted cultured sample must say what it is: {sample}"
        assert "OBI:0001876" in str(by_id[term_ids[0]].get("termCode"))

    def test_a_drafted_cultured_sample_records_its_lineage(self, tmp_path):
        _, by_id = _build(self._chain_with_drafted_result(), tmp_path)
        derived = _ids(by_id["#Sample_sample_cult"].get("derivesFrom"))
        assert "#CellLineSample_cell_a" in derived, (
            f"the cultured sample must derive from the line it was grown from: {derived}"
        )

    def test_a_drafters_own_values_are_not_overwritten(self, tmp_path):
        """Same type-aware rule as everywhere else: fill the gap, never overrule."""
        state = self._chain_with_drafted_result()
        state.add_entity(_ent("other_line", "CellLineSample", name="MO3.13"))
        for e in state.list_entities("Sample"):
            if e.entity_id == "sample_cult":
                e.set_fields_from_dict({"derives_from": ["other_line"]}, source="llm")
        _, by_id = _build(state, tmp_path)
        derived = _ids(by_id["#Sample_sample_cult"].get("derivesFrom"))
        assert "#CellLineSample_other_line" in derived, (
            f"a lineage the drafter stated must survive: {derived}"
        )


class TestADataAnalysisStatesOnlyWhatItWasTold:
    """A parameter nobody stated is absent — not ``"unknown"``, not ``[]`` (#740).

    ``_pv`` refuses a placeholder, so ``data_processing or "unknown"`` could never
    publish one; what it left behind was ``"parameter": []``, a second spelling
    of "absent" that every reader then has to know. The node states no
    parameter at all, and the tox REQUIRED shape fires on it, which is the
    prompt to go and fill it in (D5).
    """

    def _state(self, **hints):
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        state.add_entity(_ent("file_raw", "File", name="raw.csv", dest_path="raw.csv"))
        state.add_entity(_ent("file_proc", "File", name="out.csv", dest_path="out.csv"))
        state.add_entity(
            _ent(
                "proc_1",
                "LabProcess",
                name="Data Analysis",
                process_type="DataAnalysis",
                assay_id="assay_1",
                object="file_raw",
                result="file_proc",
                **hints,
            )
        )
        return state

    @staticmethod
    def _tox_required_on(tmp_path, prop):
        from profiles.validator import validate_crate_dict

        with open(tmp_path / "crate" / "ro-crate-metadata.json") as f:
            doc = json.load(f)
        results = validate_crate_dict(doc, profile="tox")
        assert results, "no ISA-Tox pass ran"
        return [i for i in results[0].issues if i.property == prop]

    def test_stating_nothing_publishes_no_parameter(self, tmp_path):
        graph, by_id = _build(self._state(), tmp_path)
        proc = by_id["#LabProcess_proc_1"]
        assert "parameter" not in proc and "parameterValue" not in proc, proc
        assert not [
            e
            for e in graph
            if "PropertyValue" in str(e.get("@type")) and e.get("value") == "unknown"
        ]
        assert self._tox_required_on(tmp_path, "http://schema.org/additionalProperty"), (
            "a DataAnalysis stating no parameter passed the additionalProperty MUST"
        )

    def test_stating_a_tool_publishes_exactly_that(self, tmp_path):
        _, by_id = _build(self._state(software="Prism 9"), tmp_path)
        params = [by_id[i] for i in _ids(by_id["#LabProcess_proc_1"].get("parameter"))]
        assert [(p["name"], p["value"]) for p in params] == [("Computational Tool", "Prism 9")]
        assert self._tox_required_on(tmp_path, "http://schema.org/additionalProperty") == []
