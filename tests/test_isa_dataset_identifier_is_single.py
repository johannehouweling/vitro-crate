"""An ISA Study or Assay carries exactly one ``schema:identifier`` (#739).

The ISA shapes cap that property at one per Study / Assay, and ``accession``,
``dsstoxId`` and other context terms expand to it. One of them emitted beside
the minted ``identifier`` fails "MUST have a non-empty identifier of type
string" — a message that reads as *missing* while the cause is *two*.
"""

from __future__ import annotations

import copy
import json
import warnings

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import validate_crate_dict

# Export runs the three SHACL passes; same headroom as test_builder_domain.
pytestmark = pytest.mark.timeout(120)


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    """A crate whose Study and Assay were drafted with identifier terms of their own."""
    state = CrateState()
    state.metadata.accession = "S-VHPS22"
    state.add_entity(_ent("study_1", "Study", name="Thyroid study", accession="S-VHPS22"))
    state.add_entity(
        _ent(
            "assay_1",
            "Assay",
            name="Deiodinase assay",
            accession="A-1",
            dsstox_id="DTXSID7020182",
            study_id="study_1",
        )
    )
    out = tmp_path_factory.mktemp("isa_ident") / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(state, str(out))["success"] is True
    with open(out / "ro-crate-metadata.json") as f:
        return json.load(f)


def _isa(doc):
    """The ISA pass over an in-memory document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = validate_crate_dict(doc, profile="isa")
    assert results, "no ISA pass ran"
    return results[0]


def _node(doc, node_id):
    return next(n for n in doc["@graph"] if n["@id"] == node_id)


@pytest.mark.parametrize("node_id", ["#Study_study_1", "#Assay_assay_1"])
def test_an_isa_dataset_carries_one_identifier_and_no_second_term(doc, node_id):
    node = _node(doc, node_id)
    assert isinstance(node["identifier"], str) and node["identifier"]
    assert not {"accession", "dsstoxId", "dsstox_id"} & set(node), node


def test_the_built_crate_passes_the_isa_pass(doc):
    result = _isa(doc)
    assert result.passed_required, [i.message for i in result.issues]


def test_a_second_identifier_on_a_study_is_the_violation(doc):
    """Pins the shape: the "non-empty identifier" message fires on TWO, not none."""
    perturbed = copy.deepcopy(doc)
    _node(perturbed, "#Study_study_1")["accession"] = "S-VHPS22"
    result = _isa(perturbed)
    assert not result.passed_required
    assert any("identifier" in i.message for i in result.issues), [i.message for i in result.issues]
