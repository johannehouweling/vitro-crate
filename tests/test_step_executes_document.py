"""A step executes the deposited document itself.

Where the deposit holds a document for a step, the step's ``executesLabProtocol``
names that file, identified by its path in the crate. The kind of step
(``intendedUse``) and the chemicals (``reagent``) are written on the file's entity,
and what the lab stated about the procedure reaches it too. A step with no
document, or with one that is not a file, keeps a protocol record of its own.

A document carries one protocol type and one set of reagents, so steps share it
only when they agree on both. Where they do not, every step involved keeps its
record, based on the document.

Most tests read the metadata document the build assembles in memory, which is the
one the validator is handed. The tests of the written crate export to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance, FileClassification
from builder.tools.builder import assemble_crate, export_crate
from builder.tools.document_discovery import CLASS_PROTOCOL
from builder.tools.validation import build_and_validate, validate_document
from profiles.ontology_iris import iri

pytestmark = pytest.mark.timeout(300)

GROWTH = iri("EFO:0003789")
DIFFERENTIATION = iri("GO:0030154")
TREATMENT = iri("EFO:0003969")
ASSAY_PROTOCOL = iri("BAO:0000523")
DATA_TRANSFORMATION = iri("OBI:0200000")

SOP = "assay/SOP.docx"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state(*assays: str) -> CrateState:
    """A study with the named assays, two cell lines and two compounds."""
    state = CrateState()
    state.metadata.title = "Steps and their documents"
    state.add_entity(_ent("study_1", "Study", name="S"))
    for assay in assays or ("assay_1",):
        state.add_entity(_ent(assay, "Assay", name=assay, study_id="study_1"))
    state.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS"))
    state.add_entity(_ent("cell_b", "CellLineSample", name="H4"))
    state.add_entity(_ent("chem_1", "MolecularEntity", name="Silychristin"))
    state.add_entity(_ent("chem_2", "MolecularEntity", name="Amiodarone"))
    return state


def _file(state: CrateState, entity_id: str, path: str, *, assay: str | None = None, **fields):
    """A deposited file, placed in *assay* when one is named."""
    state.add_entity(_ent(entity_id, "File", name=Path(path).name, path=path, **fields))
    if assay is not None:
        placed = state.get_entity(assay)
        assert placed is not None, assay
        placed.fields.setdefault("hasPart", []).append(entity_id)


def _document(state: CrateState, entity_id: str, path: str, *, assay: str | None = None, **fields):
    """A deposited file the scan classed as a protocol."""
    _file(state, entity_id, path, assay=assay, additional_types=["LabProtocol"], **fields)


def _step(state: CrateState, entity_id: str, process_type: str | None, assay="assay_1", **fields):
    kind = {"process_type": process_type} if process_type else {}
    state.add_entity(
        _ent(entity_id, "LabProcess", name=entity_id, assay_id=assay, **kind, **fields)
    )


def _nodes(state: CrateState, *, include_all_scanned: bool = False) -> dict[str, dict]:
    """The assembled graph by ``@id``: the validated one, or with every scanned file."""
    crate = assemble_crate(
        state,
        output_dir=None,
        materialize_payload=False,
        include_all_scanned=include_all_scanned,
    )
    return {node["@id"]: node for node in crate.metadata.generate()["@graph"]}


def _ids(value) -> list[str]:
    if value is None:
        return []
    return [v["@id"] for v in (value if isinstance(value, list) else [value])]


def _types(node: dict) -> list[str]:
    found = node["@type"]
    return found if isinstance(found, list) else [found]


def _texts(value) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _executed(nodes: dict[str, dict]) -> set[str]:
    return {i for node in nodes.values() for i in _ids(node.get("executesLabProtocol"))}


class TestAStepExecutesTheDocumentsItFollows:
    def test_a_readout_executes_the_sop_of_its_assay(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [SOP]
        assert "#LabProcess_read_protocol" not in nodes, "a step that has a document has no record"
        assert _types(nodes[SOP]) == ["File", "LabProtocol"]
        assert nodes[SOP]["intendedUse"] == {"@id": ASSAY_PROTOCOL}
        assert nodes[SOP]["name"] == "SOP.docx", "the document keeps its own name"

    def test_a_step_with_several_documents_executes_each_of_them(self):
        state = _state()
        _document(state, "isolation", "assay/RNA isolation.docx", assay="assay_1")
        _document(state, "qpcr", "assay/qPCR.xlsx", assay="assay_1")
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        executed = _ids(nodes["#LabProcess_read"]["executesLabProtocol"])
        assert executed == ["assay/RNA%20isolation.docx", "assay/qPCR.xlsx"]
        assert "#LabProcess_read_protocol" not in nodes
        for document in executed:
            assert _types(nodes[document]) == ["File", "LabProtocol"]
            assert nodes[document]["intendedUse"] == {"@id": ASSAY_PROTOCOL}

    def test_an_analysis_executes_its_script(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _file(state, "script", "assay/fit_curves.R", assay="assay_1")
        _step(state, "read", "EndpointReadout")
        _step(state, "ana", "DataAnalysis")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_ana"]["executesLabProtocol"]) == ["assay/fit_curves.R"]
        script = nodes["assay/fit_curves.R"]
        assert _types(script) == ["File", "SoftwareSourceCode", "LabProtocol"]
        assert script["intendedUse"] == {"@id": DATA_TRANSFORMATION}
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [SOP]

    def test_a_culture_executes_the_document_of_its_line(self):
        state = _state()
        _document(state, "culture", "cell_line_protocols/cell culture protocol SK-N-AS.docx")
        _step(state, "tsp", "TestSystemPreparation", cell_line=["cell_a"], culture_medium="DMEM")
        nodes = _nodes(state)
        (document,) = _ids(nodes["#LabProcess_tsp"]["executesLabProtocol"])
        assert document == "cell_line_protocols/cell%20culture%20protocol%20SK-N-AS.docx"
        assert nodes[document]["intendedUse"] == {"@id": GROWTH}
        assert "#LabProcess_tsp_protocol" not in nodes

    def test_each_part_of_a_culture_of_two_lines_executes_its_own_document(self):
        state = _state()
        _document(state, "sk", "cell_line_protocols/cell culture protocol SK-N-AS.docx")
        _document(state, "h4", "cell_line_protocols/cell culture protocol H4.docx")
        _step(state, "tsp", "TestSystemPreparation", cell_line=["cell_a", "cell_b"])
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_tsp_SK-N-AS"]["executesLabProtocol"]) == [
            "cell_line_protocols/cell%20culture%20protocol%20SK-N-AS.docx"
        ]
        assert _ids(nodes["#LabProcess_tsp_H4"]["executesLabProtocol"]) == [
            "cell_line_protocols/cell%20culture%20protocol%20H4.docx"
        ]

    def test_a_document_the_step_names_is_executed_where_the_deposit_places_none(self):
        state = _state()
        _document(state, "sop", SOP)
        _step(state, "exp", "Exposure", chemicals=["chem_1"], labprotocol="sop")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_exp"]["executesLabProtocol"]) == [SOP]
        assert nodes[SOP]["intendedUse"] == {"@id": TREATMENT}

    def test_a_file_the_step_names_becomes_a_protocol(self):
        """A step may name a file the scan did not class as a protocol. Executed, it
        is one: the ISA profile requires the class of whatever a step executes."""
        state = _state()
        _file(state, "notes", "assay/method.txt")
        _step(state, "read", "EndpointReadout", labprotocol="notes")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == ["assay/method.txt"]
        assert _types(nodes["assay/method.txt"]) == ["File", "LabProtocol"]

    def test_an_untyped_step_executes_its_document_without_a_protocol_type(self):
        state = _state()
        _document(state, "sop", SOP)
        _step(state, "other", None, labprotocol="sop")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_other"]["executesLabProtocol"]) == [SOP]
        assert "intendedUse" not in nodes[SOP]


class TestWhatTheRecordCarriedIsOnTheDocument:
    def test_the_compounds_of_an_exposure_are_reagents_of_its_document(self):
        state = _state()
        _document(state, "sop", SOP)
        _step(state, "exp", "Exposure", chemicals=["chem_1", "chem_2"], labprotocol="sop")
        nodes = _nodes(state)
        assert _ids(nodes[SOP]["reagent"]) == [
            "#MolecularEntity_chem_1",
            "#MolecularEntity_chem_2",
        ]

    def test_the_substrate_of_a_readout_is_a_reagent_of_each_document(self):
        state = _state()
        _document(state, "isolation", "assay/isolation.docx", assay="assay_1")
        _document(state, "counting", "assay/counting.docx", assay="assay_1")
        _step(state, "read", "EndpointReadout", reagents=["chem_2"], substrate="Amiodarone")
        nodes = _nodes(state)
        for document in ("assay/isolation.docx", "assay/counting.docx"):
            assert _ids(nodes[document]["reagent"]) == ["#MolecularEntity_chem_2"]

    def test_what_the_lab_stated_reaches_the_document(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        state.add_entity(
            _ent(
                "stated",
                "LabProtocol",
                name="Readout procedure",
                description="Read at 450 nm.",
                url="https://www.protocols.io/view/readout",
            )
        )
        _step(state, "read", "EndpointReadout", labprotocol="stated")
        document = _nodes(state)[SOP]
        assert document["description"] == "Read at 450 nm."
        assert document["url"] == "https://www.protocols.io/view/readout"
        assert document["name"] == "SOP.docx", "the document keeps its own name"
        assert _texts(document["alternateName"]) == ["Readout procedure"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("url", {"@id": "https://www.protocols.io/view/readout"}),
            ("url", ["https://www.protocols.io/view/readout", "https://lab.example/readout"]),
            ("description", ["Read at 450 nm.", "Read again at 620 nm."]),
        ],
        ids=["a url as a reference", "two urls", "a description in two parts"],
    )
    def test_a_statement_that_is_no_text_reaches_the_document(self, field, value):
        """``set_fields`` stores a reference or a list as readily as a text, and the
        record carried such a statement as the lab made it."""
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        stated = {"name": "Readout procedure", "description": "Read at 450 nm.", field: value}
        state.add_entity(_ent("stated", "LabProtocol", **stated))
        _step(state, "read", "EndpointReadout", labprotocol="stated")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [SOP]
        assert nodes[SOP][field] == value

    def test_a_description_the_document_has_is_kept(self):
        """The precedent of ``draft_protocol``: only an empty field is filled. A
        description the build derived from the file's content is recorded with the
        same provenance as one the lab stated, so nothing tells the two apart."""
        state = _state()
        _document(state, "sop", SOP, assay="assay_1", description="Plate layout and wash steps.")
        state.add_entity(
            _ent(
                "stated",
                "LabProtocol",
                name="Readout procedure",
                description="Read at 450 nm.",
                url="https://www.protocols.io/view/readout",
            )
        )
        _step(state, "read", "EndpointReadout", labprotocol="stated")
        document = _nodes(state)[SOP]
        assert document["description"] == "Plate layout and wash steps."
        assert document["url"] == "https://www.protocols.io/view/readout"

    def test_a_stated_name_that_is_the_documents_own_adds_nothing(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        state.add_entity(_ent("stated", "LabProtocol", name="SOP.docx", description="As filed."))
        _step(state, "read", "EndpointReadout", labprotocol="stated")
        document = _nodes(state)[SOP]
        assert document["description"] == "As filed."
        assert "alternateName" not in document


class TestStepsShareADocumentOnlyWhenTheyAgree:
    CULTURE = "cell_line_protocols/cell%20culture%20protocol%20SK-N-AS.docx"

    def _two_cultures(self, first: dict | None = None, **second) -> CrateState:
        """Two assays culturing SK-N-AS, the first step stating *first*, the second
        *second*."""
        state = _state("assay_1", "assay_2")
        _document(state, "culture", "cell_line_protocols/cell culture protocol SK-N-AS.docx")
        _step(state, "tsp_1", "TestSystemPreparation", cell_line=["cell_a"], **(first or {}))
        _step(state, "tsp_2", "TestSystemPreparation", "assay_2", cell_line=["cell_a"], **second)
        return state

    def _records(self, nodes: dict[str, dict], *steps: str) -> list[dict]:
        """The record of each step, asserted to be what it executes and based on."""
        records = []
        for step in steps:
            assert _ids(nodes[step]["executesLabProtocol"]) == [f"{step}_protocol"], step
            records.append(nodes[f"{step}_protocol"])
        return records

    def test_cultures_of_one_line_execute_one_document(self):
        nodes = _nodes(self._two_cultures())
        for step in ("#LabProcess_tsp_1", "#LabProcess_tsp_2"):
            assert _ids(nodes[step]["executesLabProtocol"]) == [self.CULTURE]
        assert nodes[self.CULTURE]["intendedUse"] == {"@id": GROWTH}
        assert "reagent" not in nodes[self.CULTURE]
        assert self.CULTURE in _ids(nodes["#Study_study_1"]["hasPart"]), "several assays use it"

    def test_steps_of_different_kinds_keep_their_records(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "exp", "Exposure", chemicals=["chem_1"], labprotocol="sop")
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        exposure, readout = self._records(nodes, "#LabProcess_exp", "#LabProcess_read")
        assert _ids(exposure["isBasedOn"]) == _ids(readout["isBasedOn"]) == [SOP]
        assert exposure["intendedUse"] == {"@id": TREATMENT}
        assert readout["intendedUse"] == {"@id": ASSAY_PROTOCOL}
        assert _ids(exposure["reagent"]) == ["#MolecularEntity_chem_1"]
        assert "intendedUse" not in nodes[SOP] and "reagent" not in nodes[SOP]
        assert SOP not in _executed(nodes)

    def test_exposures_with_the_same_compounds_execute_their_sop(self):
        state = _state("assay_1", "assay_2")
        _document(state, "sop", SOP)
        _step(state, "exp_1", "Exposure", chemicals=["chem_1", "chem_2"], labprotocol="sop")
        _step(
            state, "exp_2", "Exposure", "assay_2", chemicals=["chem_2", "chem_1"], labprotocol="sop"
        )
        nodes = _nodes(state)
        for step in ("#LabProcess_exp_1", "#LabProcess_exp_2"):
            assert _ids(nodes[step]["executesLabProtocol"]) == [SOP]
        assert sorted(_ids(nodes[SOP]["reagent"])) == [
            "#MolecularEntity_chem_1",
            "#MolecularEntity_chem_2",
        ]

    def test_exposures_with_different_compounds_keep_their_records(self):
        state = _state("assay_1", "assay_2")
        _document(state, "sop", SOP)
        _step(state, "exp_1", "Exposure", chemicals=["chem_1"], labprotocol="sop")
        _step(state, "exp_2", "Exposure", "assay_2", chemicals=["chem_2"], labprotocol="sop")
        nodes = _nodes(state)
        first, second = self._records(nodes, "#LabProcess_exp_1", "#LabProcess_exp_2")
        assert _ids(first["reagent"]) == ["#MolecularEntity_chem_1"]
        assert _ids(second["reagent"]) == ["#MolecularEntity_chem_2"]
        assert "reagent" not in nodes[SOP], "a document never lists the compounds of two steps"

    def test_an_exposure_that_names_no_compound_is_still_reported(self):
        """One SOP named by an exposure with a compound and by one with none: the
        compound of the first must not answer for the second."""
        state = _state("assay_1", "assay_2")
        _document(state, "sop", SOP)
        _step(state, "exp_1", "Exposure", chemicals=["chem_1"], duration="24 h", labprotocol="sop")
        _step(state, "exp_2", "Exposure", "assay_2", duration="24 h", labprotocol="sop")
        assert "reagent" not in _nodes(state)[SOP]
        report = build_and_validate(state, severity="required", profile="tox")
        unnamed = [
            issue["entity_id"].rsplit("#", 1)[-1]
            for issue in report["issues"]
            if "whose reagents include" in issue["message"]
        ]
        assert unnamed == ["LabProcess_exp_2"], report["issues"]

    def test_a_culture_and_a_differentiation_of_one_line_keep_their_records(self):
        nodes = _nodes(self._two_cultures(preparation_type="differentiation"))
        culture, differentiation = self._records(nodes, "#LabProcess_tsp_1", "#LabProcess_tsp_2")
        assert culture["intendedUse"] == {"@id": GROWTH}
        assert differentiation["intendedUse"] == {"@id": DIFFERENTIATION}
        assert _ids(culture["isBasedOn"]) == _ids(differentiation["isBasedOn"]) == [self.CULTURE]
        assert "intendedUse" not in nodes[self.CULTURE]

    def test_steps_that_state_different_descriptions_keep_their_records(self):
        state = self._two_cultures({"labprotocol": "first"}, labprotocol="second")
        state.add_entity(_ent("first", "LabProtocol", name="Growth", description="DMEM, 37 °C."))
        state.add_entity(_ent("second", "LabProtocol", name="Growth", description="RPMI, 37 °C."))
        nodes = _nodes(state)
        first, second = self._records(nodes, "#LabProcess_tsp_1", "#LabProcess_tsp_2")
        assert (first["description"], second["description"]) == ("DMEM, 37 °C.", "RPMI, 37 °C.")
        assert "description" not in nodes[self.CULTURE]

    def test_steps_that_state_different_urls_keep_their_records(self):
        state = self._two_cultures({"labprotocol": "first"}, labprotocol="second")
        for entity_id, page in (("first", "a"), ("second", "b")):
            url = f"https://www.protocols.io/view/{page}"
            state.add_entity(
                _ent(entity_id, "LabProtocol", name="Growth", description="DMEM.", url=url)
            )
        nodes = _nodes(state)
        first, second = self._records(nodes, "#LabProcess_tsp_1", "#LabProcess_tsp_2")
        assert (first["url"], second["url"]) == (
            "https://www.protocols.io/view/a",
            "https://www.protocols.io/view/b",
        )
        assert "url" not in nodes[self.CULTURE]

    def test_steps_that_state_one_url_as_a_reference_execute_the_document(self):
        page = {"@id": "https://www.protocols.io/view/a"}
        state = self._two_cultures({"labprotocol": "first"}, labprotocol="second")
        for entity_id in ("first", "second"):
            state.add_entity(
                _ent(entity_id, "LabProtocol", name="Growth", description="DMEM.", url=dict(page))
            )
        nodes = _nodes(state)
        for step in ("#LabProcess_tsp_1", "#LabProcess_tsp_2"):
            assert _ids(nodes[step]["executesLabProtocol"]) == [self.CULTURE]
        assert nodes[self.CULTURE]["url"] == page

    def test_steps_that_state_different_links_keep_their_records(self):
        """Statements that are no text are compared as they were made."""
        state = self._two_cultures({"labprotocol": "first"}, labprotocol="second")
        links = {
            "first": ["https://www.protocols.io/view/a", "https://www.protocols.io/view/b"],
            "second": {"@id": "https://www.protocols.io/view/a"},
        }
        for entity_id, url in links.items():
            state.add_entity(
                _ent(entity_id, "LabProtocol", name="Growth", description="DMEM.", url=url)
            )
        nodes = _nodes(state)
        first, second = self._records(nodes, "#LabProcess_tsp_1", "#LabProcess_tsp_2")
        assert (first["url"], second["url"]) == (links["first"], links["second"])
        assert "url" not in nodes[self.CULTURE]

    def test_what_one_step_states_is_written_for_all_that_execute_the_document(self):
        state = self._two_cultures(labprotocol="stated")
        state.add_entity(_ent("stated", "LabProtocol", name="Growth", description="DMEM, 37 °C."))
        nodes = _nodes(state)
        for step in ("#LabProcess_tsp_1", "#LabProcess_tsp_2"):
            assert _ids(nodes[step]["executesLabProtocol"]) == [self.CULTURE]
        assert nodes[self.CULTURE]["description"] == "DMEM, 37 °C."
        assert _texts(nodes[self.CULTURE]["alternateName"]) == ["Growth"]

    def test_an_untyped_step_and_a_typed_one_keep_their_records(self):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "read", "EndpointReadout")
        _step(state, "other", None, labprotocol="sop")
        nodes = _nodes(state)
        readout, other = self._records(nodes, "#LabProcess_read", "#LabProcess_other")
        assert readout["intendedUse"] == {"@id": ASSAY_PROTOCOL}
        assert "intendedUse" not in other
        assert "intendedUse" not in nodes[SOP]

    def test_a_step_executes_all_of_its_documents_or_its_record(self):
        """The readout follows two documents and the exposure names one of them. The
        readout keeps its record: it does not execute the other document alone."""
        state = _state()
        _document(state, "isolation", "assay/isolation.docx", assay="assay_1")
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "exp", "Exposure", chemicals=["chem_1"], labprotocol="sop")
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        _exposure, readout = self._records(nodes, "#LabProcess_exp", "#LabProcess_read")
        assert _ids(readout["isBasedOn"]) == ["assay/isolation.docx", SOP]
        assert _executed(nodes) == {"#LabProcess_exp_protocol", "#LabProcess_read_protocol"}
        assert "intendedUse" not in nodes["assay/isolation.docx"]


class TestAStepWithoutADepositedFileKeepsItsRecord:
    def test_a_step_with_no_document_executes_its_record(self):
        state = _state()
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [
            "#LabProcess_read_protocol"
        ]
        record = nodes["#LabProcess_read_protocol"]
        assert _types(record)[0] == "LabProtocol" and "File" not in _types(record)
        assert record["intendedUse"] == {"@id": ASSAY_PROTOCOL}
        assert "isBasedOn" not in record

    def test_a_document_that_is_no_file_is_what_the_record_is_based_on(self):
        state = _state()
        state.add_entity(_ent("paper", "Publication", name="Method paper", doi="10.1234/method"))
        _step(state, "read", "EndpointReadout", labprotocol="paper")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [
            "#LabProcess_read_protocol"
        ]
        assert _ids(nodes["#LabProcess_read_protocol"]["isBasedOn"]) == [
            "https://doi.org/10.1234/method"
        ]
        assert "intendedUse" not in nodes["https://doi.org/10.1234/method"]

    def test_a_file_named_beside_another_entity_stays_with_the_record(self):
        state = _state()
        _document(state, "sop", SOP)
        state.add_entity(_ent("paper", "Publication", name="Method paper", doi="10.1234/method"))
        _step(state, "read", "EndpointReadout", labprotocol=["sop", "paper"])
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [
            "#LabProcess_read_protocol"
        ]
        assert _ids(nodes["#LabProcess_read_protocol"]["isBasedOn"]) == [
            SOP,
            "https://doi.org/10.1234/method",
        ]
        assert "intendedUse" not in nodes[SOP]


class TestTheBuildRecordListsOnlyTheRecords:
    def test_an_executed_document_is_no_result_of_the_build(self):
        """The build wrote the exposure's record. It did not write the SOP the readout
        executes: the deposit holds that file."""
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "exp", "Exposure", chemicals=["chem_1"])
        _step(state, "read", "EndpointReadout")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == [SOP]
        (action,) = [n for n in nodes.values() if n["@type"] == "CreateAction"]
        assert _ids(action["result"]) == ["./", "#LabProcess_exp_protocol"]


class TestTheWrittenCrateAndTheValidatedGraphAgree:
    """The export adds every scanned file as a plain ``File``, and validation runs
    on a graph without them. A document a step executes is a protocol in both."""

    def _deposit(self, tmp_path: Path, filename: str, *assays: str) -> CrateState:
        folder = tmp_path / "deposit" / "cell_line_protocols"
        folder.mkdir(parents=True)
        document = folder / filename
        document.write_text("Materials and methods: incubate and passage the culture.")
        state = _state(*assays)
        state.metadata.input_path = str(tmp_path / "deposit")
        state.scanned_files = [
            FileClassification(
                path=str(document),
                filename=filename,
                size=document.stat().st_size,
                mime_type=DOCX,
                classification=CLASS_PROTOCOL,
            )
        ]
        for n, assay in enumerate(assays or ("assay_1",), start=1):
            _step(state, f"tsp_{n}", "TestSystemPreparation", assay, cell_line=["cell_a"])
        return state

    @pytest.mark.parametrize("include_all_scanned", [False, True], ids=["validated", "written"])
    def test_a_scanned_culture_document_is_an_executed_protocol(
        self, tmp_path, include_all_scanned
    ):
        state = self._deposit(tmp_path, "cellculture-protocol-SK-N-AS.docx")
        nodes = _nodes(state, include_all_scanned=include_all_scanned)
        document = "cell_line_protocols/cellculture-protocol-SK-N-AS.docx"
        assert _ids(nodes["#LabProcess_tsp_1"]["executesLabProtocol"]) == [document]
        assert _types(nodes[document]) == ["File", "LabProtocol"]
        assert nodes[document]["intendedUse"] == {"@id": GROWTH}

    @pytest.mark.parametrize("include_all_scanned", [False, True], ids=["validated", "written"])
    def test_what_is_written_on_a_document_survives_a_path_with_a_space(
        self, tmp_path, include_all_scanned
    ):
        """Two cultures find the same scanned document, and the second finding
        replaces the node of a path that is percent-encoded."""
        state = self._deposit(tmp_path, "cell culture protocol SK-N-AS.docx", "assay_1", "assay_2")
        nodes = _nodes(state, include_all_scanned=include_all_scanned)
        document = "cell_line_protocols/cell%20culture%20protocol%20SK-N-AS.docx"
        for step in ("#LabProcess_tsp_1", "#LabProcess_tsp_2"):
            assert _ids(nodes[step]["executesLabProtocol"]) == [document]
        assert _types(nodes[document]) == ["File", "LabProtocol"]
        assert nodes[document]["intendedUse"] == {"@id": GROWTH}

    def test_the_written_crate_executes_nothing_that_is_no_protocol(self, tmp_path):
        state = self._deposit(tmp_path, "cellculture-protocol-SK-N-AS.docx")
        out = tmp_path / "crate"
        assert export_crate(state, str(out), embed_report=False, validate=False)["success"]
        written = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))
        nodes = {node["@id"]: node for node in written["@graph"]}
        (document,) = _ids(nodes["#LabProcess_tsp_1"]["executesLabProtocol"])
        assert (out / document).is_file(), "the executed document is in the payload"
        report = validate_document(written, severity="required", profile="isa")
        assert not [
            issue for issue in report["issues"] if "MUST be of type LabProtocol" in issue["message"]
        ], report["issues"]


class TestAMetadataOnlyCrateKeepsTheExecutedDocument:
    def test_the_document_is_described_at_its_local_path_with_what_it_carried(self, tmp_path):
        state = _state()
        _document(state, "sop", SOP, assay="assay_1")
        _step(state, "exp", "Exposure", chemicals=["chem_1"])
        _step(state, "read", "EndpointReadout", reagents=["chem_2"], substrate="Amiodarone")
        state.metadata.metadata_only = True
        out = tmp_path / "public"
        assert export_crate(state, str(out), embed_report=False, validate=False)["success"]
        written = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))
        nodes = {node["@id"]: node for node in written["@graph"]}
        assert SOP not in nodes, "a relative @id says the file is in the crate"
        document = nodes["#" + SOP]
        assert document["localPath"] == SOP
        assert _types(document) == ["File", "LabProtocol"]
        assert document["intendedUse"] == {"@id": ASSAY_PROTOCOL}
        assert _ids(document["reagent"]) == ["#MolecularEntity_chem_2"]
        assert _ids(nodes["#LabProcess_read"]["executesLabProtocol"]) == ["#" + SOP]
        (action,) = [n for n in nodes.values() if n["@type"] == "CreateAction"]
        assert _ids(action["result"]) == ["./", "#LabProcess_exp_protocol"]


class TestTheProfileAcceptsWhatAStepExecutes:
    @staticmethod
    def _findings(state: CrateState, needle: str) -> list[dict]:
        report = build_and_validate(state, severity="recommended", profile="tox")
        return [issue for issue in report["issues"] if needle in issue["message"]]

    def test_a_step_that_executes_several_documents_is_not_reported(self):
        state = _state()
        _document(state, "isolation", "assay/isolation.docx", assay="assay_1")
        _document(state, "counting", "assay/counting.docx", assay="assay_1")
        _step(state, "read", "EndpointReadout", endpoint="uptake")
        assert len(_ids(_nodes(state)["#LabProcess_read"]["executesLabProtocol"])) == 2
        assert self._findings(state, "LabProtocol") == []

    def _analysis(self, **fields) -> CrateState:
        """An analysis in an assay that holds a script, unless it names its protocol."""
        state = _state()
        if not fields:
            _file(state, "script", "assay/fit_curves.R", assay="assay_1")
        _file(state, "raw", "assay/raw.csv")
        _file(state, "out", "assay/ec50.csv")
        _step(
            state, "ana", "DataAnalysis", object=["raw"], result=["out"], software="R drc", **fields
        )
        return state

    def test_an_analysis_that_executes_its_script_follows_a_deposited_script(self):
        state = self._analysis()
        assert _ids(_nodes(state)["#LabProcess_ana"]["executesLabProtocol"]) == [
            "assay/fit_curves.R"
        ]
        assert self._findings(state, "deposited analysis script") == []

    def test_an_analysis_whose_record_is_based_on_a_script_does_too(self):
        """An untyped step names the script as well, so the analysis keeps its record."""
        state = self._analysis()
        _step(state, "other", None, labprotocol="script")
        nodes = _nodes(state)
        assert _ids(nodes["#LabProcess_ana"]["executesLabProtocol"]) == ["#LabProcess_ana_protocol"]
        assert _ids(nodes["#LabProcess_ana_protocol"]["isBasedOn"]) == ["assay/fit_curves.R"]
        assert self._findings(state, "deposited analysis script") == []

    def test_an_analysis_that_executes_a_wet_lab_document_is_reported(self):
        state = self._analysis(labprotocol="sop")
        _document(state, "sop", SOP)
        assert _ids(_nodes(state)["#LabProcess_ana"]["executesLabProtocol"]) == [SOP]
        (finding,) = self._findings(state, "deposited analysis script")
        assert finding["entity_id"].endswith("#LabProcess_ana")
