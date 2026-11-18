"""A Study's or an Assay's identifier never reads as a web address it is not.

The identifier of a Study or an Assay that has no folder of its own is built under the
root's. When the root is identified by its DOI, that gave
``https://doi.org/10.6019/S-VHPS21/study-study_<name>``: a name dressed as a DOI, which
resolves to nothing. Such an identifier is built under the deposit's accession instead,
and with no accession under nothing.
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT

DOI = "https://doi.org/10.6019/S-VHPS21"


def _add(state: CrateState, entity_id: str, entity_type: EntityType, **fields) -> Entity:
    entity = Entity(
        entity_id=entity_id, type=entity_type, _provenance=EntityProvenance(created_by="lookup")
    )
    entity.set_fields_from_dict(fields, source="lookup")
    state.add_entity(entity)
    return entity


def _identifiers(state: CrateState) -> dict[str, str]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    graph = crate.metadata.generate()["@graph"]
    return {
        str(node.get("additionalType")): node["identifier"]
        for node in graph
        if node.get("additionalType") in ("Investigation", "Study", "Assay")
    }


def _state(*, doi: str | None, accession: str | None) -> CrateState:
    state = CrateState()
    state.metadata.title = "An investigation"
    state.metadata.doi = doi
    state.metadata.accession = accession
    _add(state, "inv_1", "Investigation", name="An investigation")
    _add(state, "study_1", "Study", name="A study")
    _add(state, "assay_1", "Assay", name="An assay", study_id="study_1")
    return state


class TestUnderADoi:
    def test_the_root_keeps_its_doi(self):
        assert _identifiers(_state(doi=DOI, accession="S-VHPS21"))["Investigation"] == DOI

    def test_the_study_is_named_under_the_accession(self):
        assert (
            _identifiers(_state(doi=DOI, accession="S-VHPS21"))["Study"] == "S-VHPS21/study-study_1"
        )

    def test_the_assay_is_named_under_its_study(self):
        assert (
            _identifiers(_state(doi=DOI, accession="S-VHPS21"))["Assay"]
            == "S-VHPS21/study-study_1/assay-assay_1"
        )

    def test_with_no_accession_they_are_named_under_nothing(self):
        found = _identifiers(_state(doi=DOI, accession=None))
        assert (found["Study"], found["Assay"]) == ("study-study_1", "study-study_1/assay-assay_1")

    def test_no_identifier_below_the_root_is_an_address(self):
        found = _identifiers(_state(doi=DOI, accession="S-VHPS21"))
        assert not any("://" in found[level] for level in ("Study", "Assay"))


class TestUnderAnAccession:
    def test_they_still_nest_under_the_accession(self):
        found = _identifiers(_state(doi=None, accession="S-VHPS26"))
        assert (found["Investigation"], found["Study"], found["Assay"]) == (
            "S-VHPS26",
            "S-VHPS26/study-study_1",
            "S-VHPS26/study-study_1/assay-assay_1",
        )
