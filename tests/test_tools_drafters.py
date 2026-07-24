"""Tests for builder/tools/drafters.py — entity drafting tools."""

from __future__ import annotations

import pytest
from rocrate.rocrate import ROCrate

from builder.state import CrateState
from builder.tools._crate_mapping import populate_crate
from builder.tools.drafters import (
    draft_assay,
    draft_cell_line_sample,
    draft_defined_term,
    draft_investigation,
    draft_molecular_entity,
    draft_organization,
    draft_person,
    draft_process,
    draft_property_value,
    draft_protocol,
    draft_publication,
    draft_sample,
    draft_study,
)
from profiles.context import ISA_TOX_CONTEXT


def _graph(state: CrateState) -> list[dict]:
    """Assemble the crate from state and return its JSON-LD @graph nodes."""
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


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
            state, "HepG2", {"accession": "CVCL_0027", "species": "Homo sapiens"}
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
            state,
            assay.entity_id,
            "Exposure",
            {"name": "24h Exposure", "duration": "24h"},
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
            state,
            "John Doe",
            {"orcid": "0000-0001-2345-6789", "affiliation": "University"},
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
            state,
            "University of Testing",
            {"ror": "https://ror.org/12345", "url": "https://test.edu"},
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
            state, "10.1234/example", {"title": "A Study", "journal": "Test Journal"}
        )

        assert entity.type == "Publication"
        assert entity.fields.get("identifier") == "10.1234/example"
        assert entity.fields.get("title") == "A Study"
        assert entity.fields.get("journal") == "Test Journal"


class TestFieldOverwrite:
    """Tests for field overwrite behavior."""

    def test_second_set_fields_from_dict_overwrites_value_and_source(self):
        """``set_fields_from_dict`` — the setter EVERY drafter uses — overwrites an
        existing field's value AND updates its source; it does not silently keep the
        old source.

        (The old test hand-mutated ``entity.fields[...]`` + ``set_field_status``
        directly, bypassing ``set_fields_from_dict`` entirely, so it never exercised
        the field-set path the drafters actually run.)
        """
        state = CrateState()
        entity = draft_investigation(state, {"name": "Original Name"})

        # First set: draft_investigation ran set_fields_from_dict(source="llm").
        fc = entity.get_field_status("name")
        assert fc is not None
        assert fc.status == "filled"
        assert fc.source == "llm"
        assert entity.fields["name"] == "Original Name"

        # A later user-sourced set through the REAL setter overwrites both value + source.
        entity.set_fields_from_dict({"name": "Updated Name"}, source="user")

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


class TestDraftProtocol:
    """Tests for draft_protocol."""

    def test_creates_entity_with_correct_type_and_auto_id(self):
        """draft_protocol creates a LabProtocol entity with auto-generated entity_id."""
        state = CrateState()
        entity = draft_protocol(state, {"name": "MTT Assay Protocol"})

        assert entity.type == "LabProtocol"
        assert entity.entity_id.startswith("proto_")
        assert entity.fields.get("name") == "MTT Assay Protocol"

    def test_with_hints_populates_fields_and_completion(self):
        """draft_protocol with hints populates fields and sets completion status."""
        state = CrateState()
        hints = {
            "name": "Cell Culture Protocol",
            "description": "Standard cell culture conditions",
            "protocol_type": "cell_culture",
            "version": "1.0",
        }
        entity = draft_protocol(state, hints)

        assert entity.fields["name"] == "Cell Culture Protocol"
        assert entity.fields["description"] == "Standard cell culture conditions"
        assert entity.fields["protocol_type"] == "cell_culture"
        assert entity.fields["version"] == "1.0"

        for field in hints:
            fc = entity.get_field_status(field)
            assert fc is not None, f"Missing completion for {field}"
            assert fc.status == "filled"
            assert fc.source == "llm"

        assert entity._provenance.created_by == "llm"

    def test_defaults_to_untitled(self):
        """draft_protocol uses 'Untitled Protocol' when no name is given."""
        state = CrateState()
        entity = draft_protocol(state, {})

        assert entity.fields.get("name") == "Untitled Protocol"
        assert entity.entity_id.startswith("proto_")

    def test_entity_added_to_state(self):
        """draft_protocol adds the entity to the state."""
        state = CrateState()
        entity = draft_protocol(state, {"name": "Test Protocol"})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity


class TestDraftSample:
    """Tests for draft_sample."""

    def test_creates_entity_with_correct_type_and_auto_id(self):
        """draft_sample creates a Sample entity with auto-generated entity_id."""
        state = CrateState()
        entity = draft_sample(state, {"name": "Sample A"})

        assert entity.type == "Sample"
        assert entity.entity_id.startswith("sample_")
        assert entity.fields.get("name") == "Sample A"

    def test_with_hints_populates_fields_and_completion(self):
        """draft_sample with hints populates fields and sets completion status."""
        state = CrateState()
        hints = {
            "name": "HepG2 Passage 5",
            "description": "HepG2 cells at passage 5 in 96-well plate",
            "sample_type": "cell_lysate",
            "collection_date": "2026-06-01",
        }
        entity = draft_sample(state, hints)

        assert entity.fields["name"] == "HepG2 Passage 5"
        assert entity.fields["description"] == "HepG2 cells at passage 5 in 96-well plate"
        assert entity.fields["sample_type"] == "cell_lysate"
        assert entity.fields["collection_date"] == "2026-06-01"

        for field in hints:
            fc = entity.get_field_status(field)
            assert fc is not None, f"Missing completion for {field}"
            assert fc.status == "filled"
            assert fc.source == "llm"

        assert entity._provenance.created_by == "llm"

    def test_defaults_to_untitled(self):
        """draft_sample uses 'Untitled Sample' when no name is given."""
        state = CrateState()
        entity = draft_sample(state, {})

        assert entity.fields.get("name") == "Untitled Sample"
        assert entity.entity_id.startswith("sample_")

    def test_entity_added_to_state(self):
        """draft_sample adds the entity to the state."""
        state = CrateState()
        entity = draft_sample(state, {"name": "Test Sample"})

        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity


def _node_by_id(graph: list[dict], node_id: str) -> dict | None:
    for node in graph:
        if node.get("@id") == node_id:
            return node
    return None


class TestDraftDefinedTerm:
    """Tests for draft_defined_term (Issue #141)."""

    def test_creates_defined_term_entity(self):
        state = CrateState()
        entity = draft_defined_term(
            state,
            "cell viability assay",
            {
                "term_code": "BAO:0002993",
                "in_defined_term_set": "http://www.bioassayontology.org/bao",
                "url": "http://www.bioassayontology.org/bao#BAO_0002993",
            },
        )
        assert entity.type == "DefinedTerm"
        assert entity.fields.get("name") == "cell viability assay"
        assert entity.fields.get("termCode") == "BAO:0002993"
        assert entity.fields.get("inDefinedTermSet") == "http://www.bioassayontology.org/bao"
        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity

    def test_lookup_result_id_is_used_as_dereferenceable_id(self):
        """A DefinedTerm built from a lookup IRI gets a dereferenceable @id."""
        state = CrateState()
        draft_defined_term(
            state,
            "cell viability assay",
            {"url": "http://www.bioassayontology.org/bao#BAO_0002993"},
        )
        graph = _graph(state)
        node = _node_by_id(graph, "http://www.bioassayontology.org/bao#BAO_0002993")
        assert node is not None, "DefinedTerm should be in @graph under its IRI @id"
        types = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
        assert "DefinedTerm" in types

    def test_defined_term_round_trips_into_graph(self):
        state = CrateState()
        draft_defined_term(state, "apoptosis", {"term_code": "GO:0006915"})
        graph = _graph(state)
        names = [n.get("name") for n in graph]
        assert "apoptosis" in names

    def test_defined_term_is_referenceable_as_mentions_target(self):
        """A looked-up DefinedTerm can be wired as a Study schema:mentions target."""
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        term = draft_defined_term(state, "liver injury", {"term_code": "MONDO:0005154"})
        study = draft_study(
            state, inv.entity_id, {"name": "Study", "mentions": term.entity_id}
        )
        graph = _graph(state)
        # The Study node should mention the DefinedTerm node.
        study_node = _node_by_id(graph, "#Study_" + study.entity_id)
        assert study_node is not None, "Study node should be in the graph"
        mentions = study_node.get("mentions")
        assert mentions is not None, "Study should carry a mentions edge to the term"
        ids = [m.get("@id") for m in (mentions if isinstance(mentions, list) else [mentions])]
        mentioned = [_node_by_id(graph, i) for i in ids]
        assert any(
            n is not None
            and "DefinedTerm" in (n["@type"] if isinstance(n["@type"], list) else [n["@type"]])
            for n in mentioned
        ), f"a DefinedTerm should be a mentions target; got {ids}"

    def test_iri_is_used_as_id_but_never_stored_as_a_field(self):
        """The looked-up IRI sets the @id but must NOT leak as an entity field.

        Regression for #286: ``draft_defined_term`` copied the IRI into
        ``merged_hints["entity_id"]`` and then ``set_fields_from_dict`` stored
        it as a literal ``entity_id`` field, which serialized as a bare JSON-LD
        key not in the @context and failed base validation.
        """
        state = CrateState()
        iri = "http://www.bioassayontology.org/bao#BAO_0002993"
        entity = draft_defined_term(
            state, "cell viability assay", {"term_code": "BAO:0002993", "url": iri}
        )
        # (a) the IRI is used as the entity's stable @id handle.
        assert entity.entity_id == iri
        # (b) but neither entity_id nor @id may exist as a literal FIELD.
        assert "entity_id" not in entity.fields
        assert "@id" not in entity.fields

    def test_assembled_node_has_no_entity_id_key(self):
        """The DefinedTerm @graph node must not carry a bare ``entity_id`` key.

        Regression for #286: such a key is absent from the RO-Crate @context and
        fails base conformance ("the occurrences of the JSON-LD key 'entity_id'
        are not allowed in the compacted format").
        """
        state = CrateState()
        iri = "http://www.bioassayontology.org/bao#BAO_0002993"
        draft_defined_term(state, "cell viability assay", {"url": iri})
        graph = _graph(state)
        node = _node_by_id(graph, iri)
        assert node is not None, "DefinedTerm should be in @graph under its IRI @id"
        assert "entity_id" not in node, f"entity_id leaked onto the node: {node}"

    def test_explicit_entity_id_hint_sets_id_without_leaking_field(self):
        """An explicit ``entity_id`` hint (an IRI) still sets the @id, not a field."""
        state = CrateState()
        iri = "http://purl.obolibrary.org/obo/GO_0006915"
        entity = draft_defined_term(state, "apoptosis", {"entity_id": iri})
        assert entity.entity_id == iri
        assert "entity_id" not in entity.fields
        graph = _graph(state)
        node = _node_by_id(graph, iri)
        assert node is not None
        assert "entity_id" not in node


class TestDraftersDoNotLeakReservedKeys:
    """Audit (#286): no drafter may persist internal @id/type handles as fields."""

    def test_no_drafter_persists_entity_id_or_at_id_as_a_field(self):
        """Passing entity_id/@id/type/@type to any drafter never makes a field."""
        reserved = {
            "entity_id": "http://example.org/x",
            "@id": "http://example.org/x",
            "type": "Bogus",
            "@type": "Bogus",
        }
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv", **reserved})
        study = draft_study(state, inv.entity_id, {"name": "Study", **reserved})
        entities = [
            inv,
            study,
            draft_assay(state, study.entity_id, {"name": "Assay", **reserved}),
            draft_molecular_entity(state, "Caffeine", dict(reserved)),
            draft_cell_line_sample(state, "HepG2", dict(reserved)),
            draft_process(state, study.entity_id, "CellCulture", dict(reserved)),
            draft_defined_term(state, "apoptosis", {"term_code": "GO:0006915", **reserved}),
            draft_property_value(state, "pH", {"value": "7", **reserved}),
            draft_person(state, "Ada Lovelace", dict(reserved)),
            draft_organization(state, "ACME", dict(reserved)),
            draft_protocol(state, {"name": "Proto", **reserved}),
            draft_sample(state, {"name": "Sample", **reserved}),
            draft_publication(state, "10.1/x", dict(reserved)),
        ]
        for ent in entities:
            for key in ("entity_id", "@id", "type", "@type"):
                assert key not in ent.fields, (
                    f"{ent.type} leaked reserved key {key!r} into fields: {ent.fields}"
                )


class TestDraftPropertyValue:
    """Tests for draft_property_value (Issue #141)."""

    def test_creates_property_value_entity(self):
        state = CrateState()
        entity = draft_property_value(
            state,
            "Passage Number",
            {"value": "5", "property_id": "http://purl.obolibrary.org/obo/EFO_0007061"},
        )
        assert entity.type == "PropertyValue"
        assert entity.fields.get("name") == "Passage Number"
        assert entity.fields.get("value") == "5"
        assert entity.fields.get("propertyID") == "http://purl.obolibrary.org/obo/EFO_0007061"
        retrieved = state.get_entity(entity.entity_id)
        assert retrieved is entity

    def test_property_value_carries_unit(self):
        state = CrateState()
        entity = draft_property_value(
            state, "Concentration", {"value": "10", "unit_text": "uM"}
        )
        assert entity.fields.get("unitText") == "uM"

    def test_property_value_round_trips_into_graph(self):
        state = CrateState()
        draft_property_value(state, "Passage Number", {"value": "5"})
        graph = _graph(state)
        pv_nodes = [
            n
            for n in graph
            if "PropertyValue"
            in (n["@type"] if isinstance(n.get("@type"), list) else [n.get("@type")])
        ]
        assert any(n.get("name") == "Passage Number" for n in pv_nodes)


class TestUnitsThreadedIntoProcesses:
    """Issue #143: units thread through to ParameterValue unitText."""

    def test_exposure_parameter_values_carry_unit_text(self):
        state = CrateState()
        inv = draft_investigation(state, {"name": "Inv"})
        study = draft_study(state, inv.entity_id, {"name": "Study"})
        assay = draft_assay(state, study.entity_id, {"name": "Assay"})
        draft_process(
            state,
            assay.entity_id,
            "Exposure",
            {
                "name": "24h Exposure",
                "duration": "24",
                "units": {"Exposure Duration": "h"},
            },
        )
        graph = _graph(state)
        units = [n.get("unitText") for n in graph if n.get("unitText")]
        assert "h" in units, f"Exposure Duration unitText 'h' should appear; got {units}"

    def test_cell_line_passage_growth_become_additional_properties(self):
        """CellLineSample passage/growth -> additionalProperty PropertyValue nodes."""
        state = CrateState()
        draft_cell_line_sample(
            state,
            "HepG2",
            {"accession": "CVCL_0027", "passage": "12", "growth": "adherent"},
        )
        graph = _graph(state)
        pv_nodes = [
            n
            for n in graph
            if "PropertyValue"
            in (n["@type"] if isinstance(n.get("@type"), list) else [n.get("@type")])
        ]
        names = {n.get("name") for n in pv_nodes}
        assert {"passage", "growth"} <= names, (
            f"passage/growth PropertyValues expected; got {names}"
        )
        # And the CellLineSample must reference them via additionalProperty.
        cell_nodes = [
            n
            for n in graph
            if n.get("additionalType") == "CellLine"
        ]
        assert cell_nodes, "CellLineSample node should exist"
        addl = cell_nodes[0].get("additionalProperty")
        assert addl is not None, "CellLineSample should carry additionalProperty"

    def test_cell_line_organ_tissue_become_additional_properties(self):
        """CellLineSample organ/tissue -> additionalProperty PropertyValue nodes
        carrying the ISA-Tox param/{organ,tissue} propertyID (gold #SampleCell_MDCK1).
        """
        state = CrateState()
        draft_cell_line_sample(
            state,
            "MDCK-I",
            {"accession": "CVCL_0592", "organ": "Brain", "tissue": "Neural tissue"},
        )
        graph = _graph(state)
        pv_nodes = [
            n
            for n in graph
            if "PropertyValue"
            in (n["@type"] if isinstance(n.get("@type"), list) else [n.get("@type")])
        ]
        by_name = {n.get("name"): n for n in pv_nodes}
        assert "Organ" in by_name, f"Organ PropertyValue expected; got {set(by_name)}"
        assert "Tissue" in by_name, f"Tissue PropertyValue expected; got {set(by_name)}"
        assert by_name["Organ"].get("value") == "Brain"
        assert by_name["Tissue"].get("value") == "Neural tissue"
        assert by_name["Organ"].get("propertyID") == {
            "@id": "https://w3id.org/ro/crate/isa-tox/1.0/param/organ"
        }, "Organ PV must carry the ISA-Tox param/organ propertyID as an @id node"
        assert by_name["Tissue"].get("propertyID") == {
            "@id": "https://w3id.org/ro/crate/isa-tox/1.0/param/tissue"
        }, "Tissue PV must carry the ISA-Tox param/tissue propertyID as an @id node"
        # The CellLineSample must reference both via additionalProperty.
        cell_nodes = [n for n in graph if n.get("additionalType") == "CellLine"]
        assert cell_nodes, "CellLineSample node should exist"
        addl_ids = {
            (it.get("@id") if isinstance(it, dict) else it)
            for it in (cell_nodes[0].get("additionalProperty") or [])
        }
        assert by_name["Organ"]["@id"] in addl_ids
        assert by_name["Tissue"]["@id"] in addl_ids

    def test_cell_line_organ_tissue_absent_emits_nothing(self):
        """No organ/tissue field -> no Organ/Tissue PropertyValue fabricated (D5)."""
        state = CrateState()
        draft_cell_line_sample(state, "HepG2", {"accession": "CVCL_0027"})
        graph = _graph(state)
        names = {n.get("name") for n in graph}
        assert "Organ" not in names
        assert "Tissue" not in names
