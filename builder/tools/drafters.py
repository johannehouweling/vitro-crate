"""Entity drafting tools for the ISA-Tox RO-Crate Builder.

These tools create lightweight entity stubs in CrateState. They do NOT create
ROCrate objects — just the state representation with completion tracking.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from profiles.ontology_iris import iri

VALID_PROCESS_TYPES = frozenset(
    {
        "CellCulture",
        "Exposure",
        "EndpointReadout",
        "DataAnalysis",
    }
)

# A PropertyValue named "DOI"/"PubMedID" is SHACL-duck-typed by the tox profile
# (profiles/shapes/tox/10_doi_property_value.ttl, 11_pubmed_id_property_value.ttl)
# and MUST carry schema:propertyID with the exact OBI IRI as an @id node (the
# `sh:hasValue` of each shape). Default the IRI by name and always emit it as a
# reference object so the value is machine-resolvable rather than a string literal
# that silently fails the tox pass.
_IDENTIFIER_PROPERTY_IDS: dict[str, str] = {
    "DOI": iri("OBI:0002110"),  # digital object identifier
    "PubMedID": iri("OBI:0001617"),  # PubMed identifier
}


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


def draft_defined_term(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a schema:DefinedTerm contextual entity from an ontology term.

    Persists a looked-up ontology / AOP / Key-Event term (e.g. from
    ``lookup_aop`` / ``lookup_bao_term``) so it round-trips into the crate
    ``@graph`` and can be referenced (via ``link`` / ``set_fields``) as a
    ``mentions`` / ``measurementMethod`` / ``sampleType`` target.

    Args:
        state: The crate state to add the entity to.
        name: Human-readable term label.
        hints: Field values. Recognised keys (any extras are kept):

            - ``term_code`` / ``termCode``: the ontology code, e.g. ``"BAO:0002993"``.
            - ``in_defined_term_set`` / ``inDefinedTermSet``: the term set IRI.
            - ``url`` / ``entity_id`` / ``@id``: a dereferenceable IRI used as the
              entity's stable ``entity_id`` so the node's ``@id`` resolves.

    Returns:
        The newly created DefinedTerm Entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    # Normalize the camelCase schema.org property names the model emits.
    if "term_code" in merged_hints:
        merged_hints["termCode"] = merged_hints.pop("term_code")
    if "in_defined_term_set" in merged_hints:
        merged_hints["inDefinedTermSet"] = merged_hints.pop("in_defined_term_set")

    # A looked-up term carries a dereferenceable IRI; use it as the entity's
    # entity_id so _mint_id keeps it as the @id (an IRI containing "://" is
    # preserved verbatim). The IRI is the entity's @id HANDLE, not a property, so
    # the entity_id/@id keys are stripped from merged_hints before
    # set_fields_from_dict — otherwise they would serialize as bare JSON-LD keys
    # absent from the @context and fail base validation (Issue #286).
    iri = merged_hints.get("entity_id") or merged_hints.get("@id") or merged_hints.get("url")
    merged_hints.pop("entity_id", None)
    merged_hints.pop("@id", None)
    if iri:
        entity_id = str(iri)
    else:
        entity_id = _make_entity_id("dt", name, merged_hints)
    entity = Entity(
        entity_id=entity_id,
        type="DefinedTerm",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_property_value(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a schema:PropertyValue contextual entity (a typed key/value).

    Args:
        state: The crate state to add the entity to.
        name: The property name (also used to mint the entity id).
        hints: Field values. Recognised keys (any extras are kept):

            - ``value``: the measured / asserted value.
            - ``property_id`` / ``propertyID``: the ontology IRI for the key.
            - ``unit_text`` / ``unitText``: a human-readable unit (e.g. ``"uM"``).
            - ``unit_code`` / ``unitCode``: a UN/CEFACT unit code.

    Returns:
        The newly created PropertyValue Entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    for snake, camel in (
        ("property_id", "propertyID"),
        ("unit_text", "unitText"),
        ("unit_code", "unitCode"),
    ):
        if snake in merged_hints:
            merged_hints[camel] = merged_hints.pop(snake)

    # DOI / PubMedID PropertyValues MUST pin propertyID to the OBI IRI as an @id
    # node, or the tox profile raises a silent Violation. Default the IRI by name
    # when absent, and wrap any supplied IRI string as a reference object.
    obi_iri = _IDENTIFIER_PROPERTY_IDS.get(name)
    if obi_iri is not None:
        supplied = merged_hints.get("propertyID")
        iri = obi_iri
        if isinstance(supplied, dict) and supplied.get("@id"):
            iri = supplied["@id"]
        elif isinstance(supplied, str) and supplied:
            iri = supplied
        merged_hints["propertyID"] = {"@id": iri}

    entity_id = _make_entity_id("pv", name, merged_hints)
    entity = Entity(
        entity_id=entity_id,
        type="PropertyValue",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def split_person_name(name: str) -> tuple[str, str]:
    """Split a full name into ``(givenName, familyName)`` deterministically.

    A purely descriptive parse of a person's name (no identifier involved, so
    D5-safe). The contract:

    * **Inverted "Last, First" form** — a comma is the strongest split signal:
      ``"Wagenaars, J." -> ("J.", "Wagenaars")``. The text before the first comma
      is the family name, the text after is the given name; the comma and
      surrounding whitespace are stripped (internal dots in initials are kept).
    * **Plain "First Last" form** — the last whitespace-separated token is the
      family name and the rest is the given name: ``"Ada Lovelace" ->
      ("Ada", "Lovelace")``.
    * **Lone token** — a single bare token is treated as a *family-name
      candidate*, NOT silently mapped into the given name (the surname must never
      masquerade as a first name): ``"Wagenaars" -> ("", "Wagenaars")``. The
      empty given name is a genuine gap surfaced for a later lookup/guidance step
      rather than a fabricated first name.

    Returns ``("", "")`` for an empty/whitespace-only name.
    """
    text = (name or "").strip()
    if not text:
        return "", ""

    # Inverted "Last, First" form wins (a comma). Split on the FIRST comma so a
    # trailing-suffix comma never inverts the wrong way; strip the comma and
    # surrounding whitespace but keep internal dots ("J." stays "J.").
    if "," in text:
        family_part, _, given_part = text.partition(",")
        return given_part.strip(), family_part.strip()

    parts = text.split()
    if len(parts) == 1:
        # A lone bare token is a family-name candidate, not a first name.
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def draft_person(state: CrateState, name: str, hints: dict) -> Entity:
    """Create a Person entity.

    When the caller supplies neither ``givenName`` nor ``familyName`` in *hints*,
    a deterministic :func:`split_person_name` split of *name* is applied so EVERY
    ``draft_person`` path (materialize / ReAct / guidance / direct) produces an
    ISA-shaped Person — the ISA profile REQUIRES a non-empty ``schema:givenName``.
    Splitting a name is descriptive parsing, not identifier fabrication, so it is
    D5-safe; ORCID stays empty for a later lookup. An explicit ``givenName`` /
    ``familyName`` the caller passes is preserved (never overwritten by the split).

    Args:
        state: The crate state to add the entity to.
        name: The person's name.
        hints: Dictionary of additional field values.

    Returns:
        The newly created Person Entity.
    """
    merged_hints = dict(hints)
    merged_hints["name"] = name
    # Fall back to a deterministic split only when the caller gave no explicit
    # name parts at all (a partial hint is respected as-is, not second-guessed).
    if not merged_hints.get("givenName") and not merged_hints.get("familyName"):
        given, family = split_person_name(name)
        if given:
            merged_hints["givenName"] = given
        if family:
            merged_hints["familyName"] = family
    entity_id = _make_entity_id("person", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Person",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_protocol(state: CrateState, hints: dict) -> Entity:
    """Create a LabProtocol entity from hints.

    Args:
        state: The crate state to add the entity to.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created LabProtocol Entity.
    """
    merged_hints = dict(hints)
    if "name" not in merged_hints:
        merged_hints["name"] = "Untitled Protocol"
    name = merged_hints["name"]
    entity_id = _make_entity_id("proto", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="LabProtocol",
        _provenance=EntityProvenance(created_by="llm"),
    )
    entity.set_fields_from_dict(merged_hints, source="llm")
    state.add_entity(entity)
    return entity


def draft_sample(state: CrateState, hints: dict) -> Entity:
    """Create a Sample entity from hints.

    Args:
        state: The crate state to add the entity to.
        hints: Dictionary of field values to pre-populate.

    Returns:
        The newly created Sample Entity.
    """
    merged_hints = dict(hints)
    if "name" not in merged_hints:
        merged_hints["name"] = "Untitled Sample"
    name = merged_hints["name"]
    entity_id = _make_entity_id("sample", name, hints)
    entity = Entity(
        entity_id=entity_id,
        type="Sample",
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("draft_investigation", draft_investigation, takes_state=True)
TOOL_REGISTRY.register("draft_study", draft_study, takes_state=True)
TOOL_REGISTRY.register("draft_assay", draft_assay, takes_state=True)
TOOL_REGISTRY.register("draft_process", draft_process, takes_state=True)
TOOL_REGISTRY.register("draft_protocol", draft_protocol, takes_state=True)
TOOL_REGISTRY.register("draft_sample", draft_sample, takes_state=True)
TOOL_REGISTRY.register("draft_molecular_entity", draft_molecular_entity, takes_state=True)
TOOL_REGISTRY.register("draft_cell_line_sample", draft_cell_line_sample, takes_state=True)
TOOL_REGISTRY.register("draft_person", draft_person, takes_state=True)
TOOL_REGISTRY.register("draft_organization", draft_organization, takes_state=True)
TOOL_REGISTRY.register("draft_publication", draft_publication, takes_state=True)
TOOL_REGISTRY.register("draft_defined_term", draft_defined_term, takes_state=True)
TOOL_REGISTRY.register("draft_property_value", draft_property_value, takes_state=True)
