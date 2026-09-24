"""A primary-cell test-system source is published and validated as one (#788).

Primary cells are not a cell line: NCIT:C16403 is "a permanently established
cell culture", and Cellosaurus holds no record for primary cells (FAQ Q19), so
the cell-line identifier SHOULD can never be met honestly. The source is the same
``CellLineSample`` state type told apart by ``source_kind``; the crate carries
``additionalType "PrimaryCell"`` and EFO's "primary cell" term, and the profile
asks it for species and cell type instead of a Cellosaurus accession.
"""

from __future__ import annotations

import copy
import json
import warnings

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import validate_crate_dict

pytestmark = pytest.mark.timeout(180)

PRIMARY_CELL_TERM = "http://www.ebi.ac.uk/efo/EFO_0002660"
CELL_LINE_TERM = "http://purl.obolibrary.org/obo/NCIT_C16403"


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _build(tmp_path_factory, **primary_fields) -> dict:
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="Tacrolimus nephrotoxicity"))
    state.add_entity(
        _ent(
            "cell_t19",
            "CellLineSample",
            name="kidney tubuloids, donor T19",
            source_kind="primary cells",
            **primary_fields,
        )
    )
    state.add_entity(_ent("cell_hk2", "CellLineSample", name="HK-2", accession="CVCL_2190"))
    state.add_entity(
        _ent(
            "proc_cult",
            "LabProcess",
            name="Culture tubuloids",
            process_type="TestSystemPreparation",
            assay_id="assay_1",
            cell_line=["cell_t19"],
            culture_medium="expansion medium",
        )
    )
    state.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="Tacrolimus exposure",
            process_type="Exposure",
            assay_id="assay_1",
            duration="24 hours",
        )
    )
    out = tmp_path_factory.mktemp("primary") / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(state, str(out))["success"] is True
    with open(out / "ro-crate-metadata.json") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    return _build(tmp_path_factory)


def _node(doc, name):
    return next(n for n in doc["@graph"] if n.get("name") == name)


def _tox(doc, severity="required"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = validate_crate_dict(doc, severity=severity, profile="tox")
    assert results, "no ISA-Tox pass ran"
    return results[0]


def _messages_on(doc, node_id):
    # The validator reports a fragment @id against the crate root ("./#…").
    issues = _tox(doc, "recommended").issues
    return [i.message for i in issues if (i.entity_id or "").endswith(node_id)]


def test_a_primary_cell_source_is_published_as_primary_cell(doc):
    node = _node(doc, "kidney tubuloids, donor T19")
    assert node["additionalType"] == "PrimaryCell"
    assert node["sampleType"] == {"@id": PRIMARY_CELL_TERM}
    term = next(n for n in doc["@graph"] if n["@id"] == PRIMARY_CELL_TERM)
    assert "IUCLID:108175" in term["termCode"]

    sibling = _node(doc, "HK-2")
    assert sibling["additionalType"] == "CellLine"
    assert sibling["sampleType"] == {"@id": CELL_LINE_TERM}
    assert _tox(doc).passed_required, _tox(doc).issues


def test_primary_cells_are_asked_for_species_and_cell_type_not_a_cellosaurus_id(
    doc, tmp_path_factory
):
    node_id = _node(doc, "kidney tubuloids, donor T19")["@id"]
    messages = _messages_on(doc, node_id)
    assert not any("Cellosaurus" in m for m in messages), messages
    assert any("species" in m for m in messages), messages
    assert any("cell type" in m for m in messages), messages

    described = _build(
        tmp_path_factory,
        taxonomicRange="Homo sapiens",
        cell_type="epithelial cell of proximal tubule",
    )
    node = _node(described, "kidney tubuloids, donor T19")
    messages = _messages_on(described, node["@id"])
    assert not any("species" in m or "cell type" in m for m in messages), messages
    # source_kind and cell_type are consumed structurally: the cell type is the one
    # characteristic, and neither field comes back as a stray PropertyValue.
    refs = node["additionalProperty"]
    ids = {r["@id"] for r in (refs if isinstance(refs, list) else [refs])}
    props = [n for n in described["@graph"] if n["@id"] in ids]
    assert [p.get("propertyID") for p in props] == [
        {"@id": "http://www.ebi.ac.uk/efo/EFO_0000324"}
    ], props


def test_a_primary_cell_source_without_sample_type_violates(doc):
    broken = copy.deepcopy(doc)
    node = _node(broken, "kidney tubuloids, donor T19")
    assert node["additionalType"] == "PrimaryCell"
    del node["sampleType"]
    assert not _tox(broken).passed_required
