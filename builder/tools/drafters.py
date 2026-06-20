"""Entity drafting tools for the ISA-Tox RO-Crate Builder.

These tools create lightweight entity stubs in CrateState. They do NOT create
ROCrate objects — just the state representation with completion tracking.
"""

from __future__ import annotations

from typing import Any

from builder.state import CrateState, Entity, EntityProvenance


VALID_PROCESS_TYPES = frozenset({
    "CellCulture",
    "Exposure",
    "EndpointReadout",
    "DataAnalysis",
})


def _make_entity_id(prefix: str, name: str, hints: dict) -> str:
    """Generate a stable entity_id from hints, falling back to name."""
    if "entity_id" in hints:
        return hints["entity_id"]
    # Normalize name for use in entity_id
    base = name.lower().replace(" ", "_").replace("-", "_")
    # Remove non-alphanumeric chars (except underscores)
    base = "".join(c for c in base if c.isalnum() or c == "_")
    if not base:
        base = "unnamed"
    return f"{prefix}_{base}"


def draft_investigation(state: CrateState, hints: dict) -> Entity:
    """Create a new Investigation entity from hints.

    Args:
        state: The crate state to add the entity to.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created Investigation Entity.
    """
    name = hints.get("name", "Untitled Investigation")
    entity_id = _make_entity_id("inv", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Investigation",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_study(state: CrateState, investigation_id: str, hints: dict) -> Entity:
    """Create a new Study entity linked to an investigation.

    Args:
        state: The crate state to add the entity to.
        investigation_id: The entity_id of the parent Investigation.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created Study Entity.
    """
    name = hints.get("name", "Untitled Study")
    entity_id = _make_entity_id("study", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Study",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(hints, source="llm")
    entity.fields["investigation_id"] = investigation_id
    entity.set_field_status("investigation_id", "filled", "llm")
    state.add_entity(entity)
    return entity


def draft_assay(state: CrateState, study_id: str, hints: dict) -> Entity:
    """Create a new Assay entity linked to a study.

    Args:
        state: The crate state to add the entity to.
        study_id: The entity_id of the parent Study.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created Assay Entity.
    """
    name = hints.get("name", "Untitled Assay")
    entity_id = _make_entity_id("assay", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Assay",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(hints, source="llm")
    entity.fields["study_id"] = study_id
    entity.set_field_status("study_id", "filled", "llm")
    state.add_entity(entity)
    return entity


def draft_molecular_entity(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a MolecularEntity from compound name + hints.

    Args:
        state: The crate state to add the entity to.
        name: The compound name.
        hints: Dictionary of additional field values.

    Returns:
        The newly created MolecularEntity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    entity_id = _make_entity_id("chem", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="MolecularEntity",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_cell_line_sample(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a CellLineSample from cell line name + hints.

    Args:
        state: The crate state to add the entity to.
        name: The cell line name.
        hints: Dictionary of additional field values.

    Returns:
        The newly created CellLineSample entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    entity_id = _make_entity_id("cell", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="CellLineSample",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_process(state: CrateState, assay_id: str, process_type: str, hints: dict) -> Entity:
    """Create a LabProcess entity.

    Args:
        state: The crate state to add the entity to.
        assay_id: The entity_id of the parent Assay.
        process_type: One of CellCulture, Exposure, EndpointReadout, DataAnalysis.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created LabProcess Entity.

    Raises:
        ValueError: If process_type is not a valid process type.
    """
    if process_type not in VALID_PROCESS_TYPES:
        raise ValueError(
            f"Invalid process_type: {process_type!r}. "
            f"Must be one of: {', '.join(sorted(VALID_PROCESS_TYPES))}"
        )
    name = hints.get("name", f"{process_type} Process")
    entity_id = _make_entity_id("proc", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="LabProcess",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(hints, source="llm")
    entity.fields["assay_id"] = assay_id
    entity.set_field_status("assay_id", "filled", "llm")
    entity.fields["process_type"] = process_type
    entity.set_field_status("process_type", "filled", "llm")
    state.add_entity(entity)
    return entity


def draft_person(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a Person entity.

    Args:
        state: The crate state to add the entity to.
        name: The person's name.
        hints: Dictionary of additional field values.

    Returns:
        The newly created Person Entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    entity_id = _make_entity_id("person", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Person",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_organization(state: CrateState, name: str, hints: dict) -> Entity:
    """Create an Organization entity.

    Args:
        state: The crate state to add the entity to.
        name: The organization name.
        hints: Dictionary of additional field values.

    Returns:
        The newly created Organization Entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    entity_id = _make_entity_id("org", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Organization",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_publication(state: CrateState, doi: str, hints: dict) -> Entity:
    """Create a Publication entity from DOI + hints.

    Args:
        state: The crate state to add the entity to.
        doi: The DOI of the publication.
        hints: Dictionary of additional field values.

    Returns:
        The newly created Publication Entity.
    """
    merged_hints = dict(hints)
    if "identifier" not in merged_hints:
        merged_hints["identifier"] = doi
    name = hints.get("name", f"Publication {doi}")
    merged_hints["name"] = name
    entity_id = _make_entity_id("pub", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Publication",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity
