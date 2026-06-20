"""Tests for builder/tools/drafters.py — entity drafting tools."""

from __future__ import annotations

import pytest

from builder.state import CrateState, EntityProvenance
from builder.tools.drafters import (
    draft_assay,
    draft_cell_line_sample,
    draft_investigation,
    draft_molecular_entity,
    draft_organization,
    draft_person,
    draft_process,
    draft_publication,
    draft_study,
)


class TestDraftInvestigation:
    """Tests for draft_investigation."""

    def test_creates_entity_with_correct_type_and_auto_id(self):
        """draft_investigation creates an Investigation entity with auto-generated entity_id."""
        state = CrateState()
        entity = draft_investigation(state, {"name": "My Investigation"})

        assert entity.type == "Investigation"
        assert entity.entity_id.startswith("inv_")
        assert entity.fields.get("name") == "My Investigation"

    def test_with_hints_populates_fields_and_completion(self):
        """draft_investigation with hints populates fields and sets completion status."""
        state = CrateState()
        hints = {
            "name": "Tox Study",
            "description": "A toxicology study",
            "identifier": "Tox-001",
        }
        entity = draft_investigation(state, hints)

        assert entity.fields["name"] == "Tox Study"
        assert entity.fields["description"] == "A toxicology study"
        assert entity.fields["identifier"] == "Tox-001"

        # Fields from hints should be marked "filled"
        for field in hints:
            fc = entity.get_field_status(field)
            assert fc is not None, f"Missing completion for {field}"
            assert fc.status == "filled"
            assert fc.source == "llm"

        # Provenance should be set
        assert entity._provenance.created_by == "llm"


class TestDraftStudy:
    """Tests for draft_study."""

    def test_links_to_investigation(self):
        """draft_study sets investigation_id in study fields."""
        state = CrateState()
        inv = draft_investigation(state, {"name": "My Investigation"})

        entity = draft_study(state, inv.entity_id, {"name": "My Study"})

        assert entity.type == "Study"
        assert entity.entity_id.startswith("study_")
        assert entity.fields.get("name") == "My Study"
        assert entity.fields.get("investigation_id") == inv.entity_id


class TestDraftAssay:
    """Tests for draft_assay."""

    def test_links_to_study(self):
        """draft_assay sets study_id in assay fields."""
        state = CrateState()
        inv = draft_investigation(state, {"name": "My Investigation"})
        study = draft_study(state, inv.entity_id, {"name": "My Study"})

        entity = draft_assay(state, study.entity_id, {"name": "My Assay"})

        assert entity.type == "Assay"
        assert entity.entity_id.startswith("assay_")
        assert entity.fields.get("name") == "My Assay"
        assert entity.fields.get("study_id") == study.entity_id


class TestDraftMolecularEntity:
    """Tests for draft_molecular_entity."""

    def test_uses_name_in_entity_id_and_fields(self):
        """draft_molecular_entity uses the compound name in entity_id and fields."""
        state = CrateState()
        entity = draft_molecular_entity(state, "Pyrene", {"cas": "129-00-0"})

        assert entity.type == "MolecularEntity"
        assert "pyrene" in entity.entity_id
        assert entity.fields.get("name") == "Pyrene"
        assert entity.fields.get("cas") == "129-00-0"


class TestDraftCellLineSample:
    """Tests for draft_cell_line_sample."""

    def test_creates_cell_line_sample_entity(self):
        """draft_cell_line_sample creates a CellLineSample type entity."""
        state = CrateState()
        entity = draft_cell_line_sample(
            state, "HepG2",
            {"accession": "CVCL_0027", "species": "Homo sapiens"}
        )

        assert entity.type == "CellLineSample"
        assert "hepg2" in entity.entity_id
        assert entity.fields.get("name") == "HepG2"
        assert entity.fields.get("accession") == "CVCL_0027"
        assert entity.fields.get("species") == "Homo sapiens"


class TestDraftProcess:
    """Tests for draft_process."""

    def test_creates_lab_process_with_given_process_type(self):
        """draft_process creates LabProcess with given process_type."""
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        assay = draft_assay(state, study.entity_id, {"name": "Assay"})

        entity = draft_process(
            state, assay.entity_id, "Exposure",
            {"name": "24h Exposure", "duration": "24h"}
        )

        assert entity.type == "LabProcess"
        assert entity.fields.get("process_type") == "Exposure"
        assert entity.fields.get("assay_id") == assay.entity_id
        assert entity.fields.get("name") == "24h Exposure"
        assert entity.fields.get("duration") == "24h"


class TestDraftPerson:
    """Tests for draft_person."""

    def test_creates_person_entity(self):
        """draft_person creates a Person entity."""
        state = CrateState()
        entity = draft_person(
            state, "John Doe",
            {"orcid": "0000-0001-2345-6789", "affiliation": "University"}
        )

        assert entity.type == "Person"
        assert "john" in entity.entity_id
        assert entity.fields.get("name") == "John Doe"
        assert entity.fields.get("orcid") == "0000-0001-2345-6789"
        assert entity.fields.get("affiliation") == "University"


class TestDraftOrganization:
    """Tests for draft_organization."""

    def test_creates_organization_entity(self):
        """draft_organization creates an Organization entity."""
        state = CrateState()
        entity = draft_organization(
            state, "University of Testing",
            {"ror": "https://ror.org/12345", "url": "https://test.edu"}
        )

        assert entity.type == "Organization"
        assert "university" in entity.entity_id
        assert entity.fields.get("name") == "University of Testing"
        assert entity.fields.get("ror") == "https://ror.org/12345"
        assert entity.fields.get("url") == "https://test.edu"


class TestDraftPublication:
    """Tests for draft_publication."""

    def test_creates_publication_entity(self):
        """draft_publication creates a Publication entity from DOI + hints."""
        state = CrateState()
        entity = draft_publication(
            state, "10.1234/example",
            {"title": "A Study", "journal": "Test Journal"}
        )

        assert entity.type == "Publication"
        assert entity.fields.get("identifier") == "10.1234/example"
        assert entity.fields.get("title") == "A Study"
        assert entity.fields.get("journal") == "Test Journal"


class TestFieldOverwrite:
    """Tests for field overwrite behavior."""

    def test_setting_same_field_preserves_source(self):
        """Setting the same field twice overwrites value but preserves source tracking."""
        state = CrateState()
        hints = {"name": "Original Name"}
        entity = draft_investigation(state, hints)

        # First set: comes from hints
        fc = entity.get_field_status("name")
        assert fc is not None
        assert fc.status == "filled"
        assert fc.source == "llm"

        # Overwrite via fields directly
        entity.fields["name"] = "Updated Name"
        entity.set_field_status("name", "filled", "user")

        fc2 = entity.get_field_status("name")
        assert fc2 is not None
        assert fc2.status == "filled"
        assert fc2.source == "user"
        assert entity.fields["name"] == "Updated Name"


class TestEntityAddedToState:
    """Tests that each draft actually adds entity to state."""

    def test_draft_investigation_adds_to_state(self):
        state = CrateState()
        entity = draft_investigation(state, {"name": "Test"})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity

    def test_draft_study_adds_to_state(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        entity = draft_study(state, inv.entity_id, {"name": "Study"})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity

    def test_draft_molecular_entity_adds_to_state(self):
        state = CrateState()
        entity = draft_molecular_entity(state, "TestChem", {})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity

    def test_draft_process_adds_to_state(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        assay = draft_assay(state, study.entity_id, {"name": "Assay"})
        entity = draft_process(state, assay.entity_id, "CellCulture", {})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity


class TestDraftProcessErrors:
    """Tests for draft_process error handling."""

    def test_invalid_process_type_raises_value_error(self):
        """draft_process raises ValueError for invalid process_type."""
        state = CrateState()
        with pytest.raises(ValueError, match="Invalid process_type"):
            draft_process(state, "assay_001", "InvalidType", {})
