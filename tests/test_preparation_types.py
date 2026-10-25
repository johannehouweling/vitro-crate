"""A preparation step is typed from the ISA-Tox preparation-types term set (#785).

The profile publishes the set (``shapes/tox/ontology.ttl``); a step's protocol record
carries the type the lab stated as its ``intendedUse``, and the tox pass knows a
preparation step by that term being in the set.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import Namespace

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.assessment_graph import with_kinds
from builder.tools.builder import build_crate
from profiles.models.tox import PREPARATION_TYPES, PREPARATION_TYPES_SET
from profiles.ontology_iris import iri
from profiles.validator import validate_crate_dict

pytestmark = pytest.mark.timeout(180)

ONTOLOGY = Path(__file__).parents[1] / "profiles" / "shapes" / "tox" / "ontology.ttl"
SDO = Namespace("http://schema.org/")


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _build(tmp_path_factory, preparation_type=None) -> dict:
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="Neurite outgrowth"))
    state.add_entity(_ent("cell_1", "CellLineSample", name="SH-SY5Y", accession="CVCL_0019"))
    prep = {"preparation_type": preparation_type} if preparation_type else {}
    state.add_entity(
        _ent(
            "proc_prep",
            "LabProcess",
            name="Differentiate SH-SY5Y",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_1"],
            culture_medium="retinoic acid medium",
            **prep,
        )
    )
    state.add_entity(_ent("chem_1", "MolecularEntity", name="Rotenone"))
    state.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="Rotenone exposure",
            process_type="Exposure",
            assay_id="assay_1",
            chemicals="chem_1",
            duration="24 hours",
        )
    )
    out = tmp_path_factory.mktemp("prep") / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(state, str(out))["success"] is True
    return json.loads((out / "ro-crate-metadata.json").read_text())


def _by_id(doc, node_id):
    return next(n for n in doc["@graph"] if n["@id"] == node_id)


def _record_type(doc) -> str:
    step = next(n for n in doc["@graph"] if n.get("name") == "Differentiate SH-SY5Y")
    record = _by_id(doc, step["executesLabProtocol"]["@id"])
    return record["intendedUse"]["@id"]


@pytest.fixture(scope="module")
def differentiated(tmp_path_factory):
    return _build(tmp_path_factory, "differentiation")


@pytest.mark.parametrize(
    ("stated", "curie"),
    [
        (None, "EFO:0003789"),
        ("transfection", "OBI:0001152"),
        ("cell differentiation", "GO:0030154"),
        # A stably transfected line is the source, not a transfection step.
        ("stable transfection", "EFO:0003789"),
    ],
)
def test_the_record_carries_the_preparation_type_the_lab_stated(tmp_path_factory, stated, curie):
    assert _record_type(_build(tmp_path_factory, stated)) == iri(curie)


def test_the_type_is_a_term_of_the_preparation_types_set(differentiated):
    term = _by_id(differentiated, iri("GO:0030154"))
    assert term["inDefinedTermSet"] == {"@id": PREPARATION_TYPES_SET}
    assert term["termCode"] == "GO:0030154"
    # The type rides on the record, not on a parameter of the step.
    values = [n.get("value") for n in differentiated["@graph"] if n.get("@type") == "PropertyValue"]
    assert "differentiation" not in values, values


def _tox_messages(doc) -> list[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = validate_crate_dict(doc, severity="recommended", profile="tox")[0]
    return [i.message for i in result.issues]


def _without_parameters(doc, *unset_terms) -> dict:
    """*doc* with the preparation step's parameters gone and *unset_terms* stating no set."""
    bare = copy.deepcopy(doc)
    step = next(n for n in bare["@graph"] if n.get("name") == "Differentiate SH-SY5Y")
    for key in ("parameter", "parameterValue"):
        del step[key]
    for curie in unset_terms:
        del _by_id(bare, iri(curie))["inDefinedTermSet"]
    return bare


def test_a_step_typed_from_the_set_is_validated_as_a_preparation(differentiated):
    assert not any("ISA-Tox protocol types" in m for m in _tox_messages(differentiated))
    # Recognised as a preparation, the step owes its parameters.
    messages = _tox_messages(_without_parameters(differentiated))
    assert any("TestSystemPreparation process MUST have" in m for m in messages), messages


def test_a_growth_protocol_stated_without_the_set_still_types_its_step(tmp_path_factory):
    older = _without_parameters(_build(tmp_path_factory), "EFO:0003789")
    messages = _tox_messages(older)
    assert any("TestSystemPreparation process MUST have" in m for m in messages), messages
    assert not any("ISA-Tox protocol types" in m for m in messages), messages


def test_the_profile_publishes_the_set_the_builder_writes():
    graph = Graph().parse(ONTOLOGY)
    published = set(graph.objects(URIRef(PREPARATION_TYPES_SET), SDO.hasDefinedTerm))
    assert published == {URIRef(iri(c)) for c in PREPARATION_TYPES}


def test_the_readers_name_every_set_member_a_preparation():
    for curie in PREPARATION_TYPES:
        nodes = [
            {"@id": "#p", "@type": "LabProcess", "executesLabProtocol": {"@id": "#r"}},
            {"@id": "#r", "@type": "LabProtocol", "intendedUse": {"@id": iri(curie)}},
        ]
        assert with_kinds(nodes)[0]["additionalType"] == "TestSystemPreparation", curie
