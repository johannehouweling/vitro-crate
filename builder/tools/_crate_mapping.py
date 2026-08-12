"""Map a CrateState onto an ro-crate-py ROCrate using the ISA-Tox domain model.

This is the heart of crate assembly: each CrateState entity becomes a typed
graph node built from the `profiles/models` classes (the same classes the
ISA-Tox SHACL shapes target), with cross-entity references resolved and the
Investigation → Study → Assay → LabProcess derivation graph wired together.

Build order is dependency-first (leaves → structural datasets → processes) so a
process can resolve the Samples, Files and LabProtocol it references. Missing
structured data is defaulted or synthesized rather than fabricated, matching the
profile's "guidance over strictness" stance.

Identifiers follow RO-Crate best practice (ro-crate-1.2.0.md §Recommended
Identifiers): a resolvable URI when a field provides one, else a `#`-prefixed
local fragment; Files use a relative URI path.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from rocrate.model import ContextEntity, DataEntity, File, Person
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity
from profiles.licenses import describe_license
from profiles.models.isa import CharacteristicValue, LabProcess, Sample, param_id
from profiles.models.tox import (
    CellLineSample,
    LabProcessCellCulture,
    LabProcessDataAnalysis,
    LabProcessEndpointReadout,
    LabProcessExposure,
)
from profiles.ontology_iris import iri

logger = logging.getLogger(__name__)

ROCRATE_SPEC = "https://w3id.org/ro/crate/1.2"
# The ISA layer the tox profile actually extends (profiles/shapes/tox/profile.ttl
# prof:isProfileOf) and that resolves — the w3id ISA permalink is not yet live.
PROFILE_ISA = "https://github.com/nfdi4plants/isa-ro-crate-profile"
PROFILE_ISATOX = "https://w3id.org/ro/crate/isa-tox/1.0"
CELL_LINE_TERM_ID = iri("NCIT:C16403")

# The spellings a LabProcess's protocol link arrives under. `labprotocol` is what
# the draft schema advertises; `protocol` is the word the agent actually reaches
# for, and a link written under it used to resolve to nothing.
_PROTOCOL_ALIASES = ("labprotocol", "protocol")

# Fields that hold references to other entities (resolved via the index), not literals.
_REF_FIELDS = frozenset(
    {
        "samples",
        "labprotocol",
        # Alias of `labprotocol` (see `_PROTOCOL_ALIASES`). Listed here so the
        # raw state id is consumed as a reference rather than also shipping as a
        # literal `protocol` string on the process node.
        "protocol",
        "cell_line",
        "object",
        "result",
        "input",
        "output",
        "investigation_id",
        "study_id",
        "assay_id",
        "author",
        "mentions",
        "chemicals",
        "cell_lines",
        "biological_models",
        "biologicalModels",
        "has_part",
        "hasPart",
        # hasPart-family aliases (profiles/context.py): studies/assays/resources/
        # dataFiles all expand to schema:hasPart. Held here so _scalar_props strips
        # them as resolver inputs rather than leaking the raw id/{@id} onto the node
        # (#180 Lane C); they are re-emitted as resolved references by
        # _wire_dataset_aliases.
        "studies",
        "assays",
        "resources",
        "dataFiles",
        "about",
        # schema:about alias for a Study/Assay's LabProcess list (PageTab-aligned).
        "labProcesses",
        # schema:additionalProperty — PropertyValue reference(s) wired onto a
        # LabProcess (#180, gold #report_analysis). Held here so _scalar_props
        # strips the raw id/{@id} rather than leaking it onto the node; it is
        # re-emitted as a resolved reference by _add_processes.
        "additionalProperty",
        # schema:funder — root/Investigation funding Organization reference(s).
        "funder",
        # schema:measurementMethod — the Assay's BAO method DefinedTerm reference.
        "measurementMethod",
        "aop",
        "organism",
        "anatomy",
        "key_event",
        "keyEvent",
        "key_events",
        "derives_from",
        "derivesFrom",
    }
)

# Provenance edge verbs the `link` tool (builder/tools/provenance.py) accepts to
# wire one entity to another in the derivation chain. A strict subset of
# _REF_FIELDS (asserted by tests) so the edge vocabulary the agent uses and the
# reference resolver can never drift. Each maps to a short, model-readable gloss
# used in `link`'s rejection messages. Issue #88.
PROVENANCE_RELATIONS: dict[str, str] = {
    "object": "entity this process consumes as input (schema:object)",
    "input": "entity this process consumes (alias of object)",
    "samples": "sample(s) this process takes as input",
    "result": "entity this process produces as output (schema:result)",
    "output": "entity this process produces (alias of result)",
    "derives_from": "source entity this sample/output is derived from",
}

# ---------------------------------------------------------------------------
# Typed entity-draft schema (Issue #90, sub-task 1)
#
# Single source of truth for the per-entity-type parameter schema advertised by
# the ``draft_*`` tools. It replaces the schema-less ``hints: {type: object}``
# param so a weak model is told exactly which scalar and reference keys an entity
# accepts. ``ref_fields`` keys are a strict subset of ``_REF_FIELDS`` (asserted by
# test) so the advertised reference vocabulary and the crate-mapping resolver can
# never drift. Extra keys remain allowed (the schema is open) — this advertises
# the high-value fields without forbidding the long tail.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityDraftSchema:
    """The advertised draft parameters for one entity type.

    Attributes:
        scalar_fields: ``field name -> description`` for literal-valued fields
            (names, identifiers, free-text metadata).
        ref_fields: ``field name -> description`` for fields whose value is an
            entity reference (an ``entity_id`` or ``{"@id": ...}``). Keys are a
            subset of :data:`_REF_FIELDS`.
    """

    scalar_fields: dict[str, str] = dataclass_field(default_factory=dict)
    ref_fields: dict[str, str] = dataclass_field(default_factory=dict)


# Shared field descriptions reused across entity types.
_NAME = "Human-readable name (also used to mint the entity id)."
_DESC = "Free-text description."
_IDENTIFIER = "Identifier or accession for this entity."

ENTITY_DRAFT_SCHEMA: dict[str, EntityDraftSchema] = {
    "Investigation": EntityDraftSchema(
        scalar_fields={
            "name": _NAME,
            "description": _DESC,
            "identifier": _IDENTIFIER,
        },
    ),
    "Study": EntityDraftSchema(
        scalar_fields={"name": _NAME, "description": _DESC, "identifier": _IDENTIFIER},
        ref_fields={
            "aop": "AOP-Wiki id or entity ref the study investigates (schema:mentions).",
            "organism": "Organism term/entity the study concerns (schema:mentions).",
            "chemicals": "MolecularEntity id(s) studied (schema:mentions).",
            "cell_lines": "CellLineSample id(s) studied (schema:mentions).",
        },
    ),
    "Assay": EntityDraftSchema(
        scalar_fields={"name": _NAME, "description": _DESC, "identifier": _IDENTIFIER},
        ref_fields={
            "key_event": "AOP Key Event id/entity the assay measures (schema:mentions).",
        },
    ),
    "MolecularEntity": EntityDraftSchema(
        scalar_fields={
            "name": "Compound name (passed as the `name` argument).",
            "identifier": "CAS number or other identifier.",
            "inchikey": "InChIKey, if known.",
            "smiles": "SMILES string, if known.",
            "molecular_formula": "Molecular formula, if known.",
            "pubchem_cid": "PubChem CID (resolves the entity @id when present).",
        },
    ),
    "CellLineSample": EntityDraftSchema(
        scalar_fields={
            "name": "Cell-line name (passed as the `name` argument).",
            "accession": (
                "Cellosaurus accession (CVCL_*) as returned by a lookup for THIS "
                "cell line — never guessed, never reused from another line."
            ),
            "description": _DESC,
        },
    ),
    "LabProcess": EntityDraftSchema(
        scalar_fields={
            "name": _NAME,
            "description": _DESC,
            "culture_medium": "CellCulture: the culture medium used.",
            "duration": "Exposure: exposure duration.",
            "cell_seeding_density": "Exposure: cell seeding density.",
            "microplate": "Exposure: microplate format.",
            "detection_instrument": "EndpointReadout: detection instrument.",
            "instrument_manufacturer": "EndpointReadout: instrument manufacturer.",
            "measured_entity": "EndpointReadout: what is measured.",
            "technical_replicate": "EndpointReadout: number of technical replicates.",
            "endpoint": "EndpointReadout: the measured endpoint.",
            "data_processing": "DataAnalysis: data-processing description.",
            "software": "DataAnalysis: software used.",
            "units": (
                "Per-parameter unit map, e.g. {'Exposure Duration': 'h'}; "
                "threaded into the matching ParameterValue's unitText."
            ),
            "assay_kit": "EndpointReadout: assay kit used (optional ParameterValue).",
            "substrate": "EndpointReadout: substrate used (optional ParameterValue).",
            "acceptance_criteria": "DataAnalysis: acceptance criteria (optional ParameterValue).",
            "evaluation_criteria": "DataAnalysis: evaluation criteria (optional ParameterValue).",
        },
        ref_fields={
            "object": "Input entity the process consumes (alias: input).",
            "samples": "Sample id(s) the process takes as input.",
            "cell_line": "CellCulture: the cell-line Sample id consumed.",
            "result": "Output entity the process produces (alias: output).",
            "chemicals": "Exposure: MolecularEntity id(s) the cells are exposed to.",
            "labprotocol": "LabProtocol id this process follows.",
        },
    ),
    "LabProtocol": EntityDraftSchema(
        scalar_fields={
            "name": _NAME,
            "description": _DESC,
            "url": "Link to the protocol (e.g. protocols.io).",
        },
    ),
    "Sample": EntityDraftSchema(
        scalar_fields={"name": _NAME, "description": _DESC},
        ref_fields={
            "derives_from": "Source entity/sample this sample is derived from.",
        },
    ),
    "Person": EntityDraftSchema(
        scalar_fields={
            "name": "Person's name (passed as the `name` argument).",
            "givenName": "Given (first) name. ISA REQUIRES a non-empty given name.",
            "familyName": "Family (last) name.",
            "orcid": "ORCID iD (resolves the entity @id when present).",
            "email": "Email address.",
            "affiliation": "Affiliation (organization name or ROR id).",
        },
    ),
    "Organization": EntityDraftSchema(
        scalar_fields={
            "name": "Organization name (passed as the `name` argument).",
            "ror": "ROR id (resolves the entity @id when present).",
            "url": "Organization website URL.",
        },
    ),
    "Publication": EntityDraftSchema(
        scalar_fields={
            "name": "Title of the publication.",
            "identifier": "DOI or other identifier (defaults to the `doi` argument).",
            "doi": "DOI (resolves the entity @id when present).",
        },
        ref_fields={
            "author": "Person id(s) who authored the publication.",
        },
    ),
    "DefinedTerm": EntityDraftSchema(
        scalar_fields={
            "name": "Term label (passed as the `name` argument).",
            "term_code": (
                "Ontology term CURIE ('PREFIX:LOCALID', e.g. 'BAO:NNNNNNN', "
                "'GO:NNNNNNN') exactly as returned by the term lookup."
            ),
            "in_defined_term_set": "IRI of the term set / ontology the term belongs to.",
            "url": "Dereferenceable IRI for the term (used as the entity @id).",
            "description": _DESC,
        },
    ),
    "PropertyValue": EntityDraftSchema(
        scalar_fields={
            "name": "Property name (passed as the `name` argument).",
            "value": "The measured or asserted value.",
            "property_id": "Ontology IRI identifying the property key.",
            "unit_text": "Human-readable unit, e.g. 'uM' or 'h'.",
            "unit_code": "UN/CEFACT unit code, if known.",
        },
    ),
}


# The LabProcess scalars that are experimental PARAMETERS — the single source of
# truth for "what may be overlaid onto a process step's hints" (#379). Derived, not
# hand-written, so the deterministic pipeline's plan schema and the ReAct arm's
# hints schema cannot drift apart.
#
# Three deliberate exclusions:
#   * `name` drives the process `@id` via `drafters._make_entity_id` and is merged
#     separately — overlaying it would let a plan hijack the entity id;
#   * `description` is never read by `_build_process`, so it would be a dead field;
#   * `units` is advertised as a string but the code wants a dict keyed by
#     ParameterValue DISPLAY NAME (`profiles/models/tox.py` does `units.get("Exposure
#     Duration")`). A model that follows the advertised type passes a bare string and
#     the build raises `'str' object has no attribute 'get'`, which surfaces as an
#     error with zero routable issues. Excluded until that type is corrected.
LABPROCESS_PARAMETER_FIELDS: frozenset[str] = (
    frozenset(ENTITY_DRAFT_SCHEMA["LabProcess"].scalar_fields)
    - _REF_FIELDS
    - {"name", "description", "units"}
)


def draft_hints_schema(entity_type: str) -> dict[str, Any]:
    """Build the JSON-Schema for a ``draft_*`` tool's ``hints`` parameter.

    Returns an open object schema (``additionalProperties: true``) whose typed
    ``properties`` are the scalar and reference fields advertised for
    ``entity_type`` in :data:`ENTITY_DRAFT_SCHEMA`. Reference fields accept an
    entity id string or a list of ids. Falls back to a bare open object for
    unknown entity types.
    """
    schema = ENTITY_DRAFT_SCHEMA.get(entity_type)
    properties: dict[str, Any] = {}
    if schema is not None:
        for name, desc in schema.scalar_fields.items():
            properties[name] = {"type": "string", "description": desc}
        for name, desc in schema.ref_fields.items():
            properties[name] = {
                "description": desc + " Pass an entity_id (or a list of them).",
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            }
    return {
        "type": "object",
        "description": (
            f"Field values for the {entity_type}. The keys below are recognised; "
            "additional fields are allowed."
        ),
        "properties": properties,
        "additionalProperties": True,
    }


# Study/Assay annotation fields that expand to schema:mentions via the @context
# (paper §Methods: Study ← linked AOP; Assay endpoint ← corresponding Key Event).
_STUDY_MENTION_FIELDS = {
    "aop": "aop",
    "organism": "organism",
    "anatomy": "anatomy",
    "chemicals": "chemicals",
    "biological_models": "biologicalModels",
    "biologicalModels": "biologicalModels",
    "cell_lines": "biologicalModels",
    "mentions": "mentions",
}
_ASSAY_MENTION_FIELDS = {
    "key_event": "keyEvent",
    "keyEvent": "keyEvent",
    "key_events": "keyEvent",
    "mentions": "mentions",
}
# Discriminator / id-source fields consumed structurally, never emitted as literals.
_STRUCT_FIELDS = frozenset(
    {
        "process_type",
        "orcid",
        "ror",
        "pubchem_cid",
        # MolecularEntity identifier sources promoted to identifier PropertyValue
        # nodes (#180) — kept off the node as raw literals.
        "cas",
        "casrn",
        "cas_number",
        # EPA CompTox/DSSTox substance id promoted to a DTXSID identifier
        # PropertyValue (#179) — consumed structurally, not a stray node literal.
        "dtxsid",
        "doi",
        "dest_path",
        "path",
        "contentUrl",
        # Provisional-placeholder markers (#438) — consumed to materialise a
        # minimal typed table, never emitted as stray literals on the File node.
        "provisional",
        "table_kind",
        # LabProcess kwargs threaded into the typed subtype constructors (#143)
        # rather than emitted as stray literals on the process node.
        "units",
        "assay_kit",
        "substrate",
        "acceptance_criteria",
        "evaluation_criteria",
        # CellLineSample characteristics promoted to additionalProperty PropertyValue
        # nodes (#143 passage/growth, #180 organ/tissue) — consumed structurally,
        # never emitted as raw literals on the Sample node.
        "passage",
        "growth",
        "organ",
        "tissue",
        # draft_file's extra @type term(s) — consumed to co-type the File node
        # (#180, e.g. SoftwareSourceCode), never emitted as a literal property.
        "additional_types",
        # draft_file / attach_files' state-tracking placement label — not a
        # crate property and absent from the RO-Crate @context, so emitting it
        # raw fails the base context check (ro-crate-1.2_2.1). Strip it here.
        "role",
    }
)


def populate_crate(
    state: CrateState,
    crate: ROCrate,
    output_dir: Path | None = None,
    *,
    materialize_payload: bool = True,
    include_all_scanned: bool = True,
) -> None:
    """Populate `crate` from `state` using the ISA-Tox domain model.

    output_dir is the crate root being written; the Exposure condition table is
    materialised there as a (placeholder) CSV so it is a valid in-payload File.

    When ``materialize_payload`` is False (the in-memory build_and_validate path,
    #87) no payload file is written to disk — the condition-table File node is
    still added to the graph so the metadata document validates, but its CSV is
    not created. This keeps validation a zero-disk operation.

    ``include_all_scanned`` (#175) auto-includes every un-placed scanned file as a
    root ``File`` leaf so the crate packages the whole dataset; the hot
    build_and_validate path passes False (plain leaves don't move the verdict).
    """
    idx: dict[str, Any] = {}
    _populate_root_and_conformance(state, crate)
    _add_leaves(
        state,
        crate,
        idx,
        output_dir,
        materialize_payload=materialize_payload,
        include_all_scanned=include_all_scanned,
    )
    _add_structural(state, crate, idx)
    _add_processes(state, crate, idx, output_dir, materialize_payload=materialize_payload)
    # After the index is complete, so publisher/creator/contact can point at any
    # Person or Organization in the crate (or at a bare ORCID/ROR IRI).
    _wire_root_attribution(state, crate, idx)
    _add_generator_provenance(state, crate)
    _wire_mentions(state, idx)
    _wire_dataset_aliases(state, crate, idx)
    _mirror_profile_predicates(crate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(text)).strip("_") or "x"


# ---------------------------------------------------------------------------
# Identifier PropertyValue nodes (#180)
#
# Looked-up identifiers (a Person's ORCID, a MolecularEntity's CAS / PubChem CID)
# round-trip into the crate as `schema:PropertyValue` identifier nodes carrying
# their scheme (`name` + optional `propertyID` as an `@id` IRI node) instead of
# collapsing into an indistinguishable string. Ids mirror rocrate-wizard's
# `param_id` scheme (`#param_<slug(name)>_<sha1("name|value")[:10]>`) so the
# output matches the gold crate. NEVER fabricate values (D5) — only fields that
# came from a lookup or are already in state are wired here.
# ---------------------------------------------------------------------------


# Files the TOOL created rather than the lab: header-only templates minted to
# satisfy a profile shape, and the crate's own generated artifacts. Their name
# carries this marker so no reader — human or machine — mistakes a scaffold for
# measured data.
AUTOGENERATED_MARKER = "AUTOGENERATED"


def _autogenerated_name(name: str) -> str:
    """Prefix *name* with the autogenerated marker (idempotent)."""
    text = str(name or "").strip()
    if not text:
        return AUTOGENERATED_MARKER
    if text.upper().startswith(AUTOGENERATED_MARKER):
        return text
    return f"{AUTOGENERATED_MARKER} — {text}"


def _identifier_pv(
    crate: ROCrate, name: str, value: str, property_id_url: str | None = None
) -> ContextEntity:
    """Build (and add) a schema:PropertyValue identifier node with a stable id.

    The id is ``param_id(name, value)`` (the wizard scheme); ``propertyID`` is
    emitted as an ``{"@id": …}`` reference when a url is given, else omitted.

    Emitting it as a bare URI string is arguable — schema:propertyID does take
    Text|URL, and these values name WHICH scheme this is (ORCID, PubChem CID,
    DTXSID) rather than an entity the crate describes. ``profiles/models/tox.py``
    makes exactly that argument for its own ParameterValues. But it is a
    deliberate output change with gold-crate tests pinning the reference form
    (tests/test_crate_mapping_identifiers.py), so it belongs in a change of its
    own that updates them on purpose — not as a side effect of a report edit.

    Returns the added node so callers can reference it.
    """
    props: dict[str, Any] = {"@type": "PropertyValue", "name": name, "value": str(value)}
    if property_id_url:
        props["propertyID"] = {"@id": property_id_url}
    return crate.add(ContextEntity(crate, param_id(name, str(value)), properties=props))


# Per-MolecularEntity identifier fields, in the order the gold crate lists them
# (CAS first, then PubChem CID, then the EPA DTXSID). Each tuple is
# (field aliases, scheme name, propertyID url | None).
_MOLECULAR_IDENTIFIERS: tuple[tuple[tuple[str, ...], str, str | None], ...] = (
    (("cas", "casrn", "cas_number"), "CAS", None),
    (
        ("pubchem_cid",),
        "PubChem CID",
        "https://pubchem.ncbi.nlm.nih.gov/compound",
    ),
    # EPA CompTox/DSSTox substance id (#179). Appended AFTER CAS + PubChem CID so
    # existing identifier order and PropertyValue ids stay byte-stable; propertyID
    # is the CompTox chemical-details base, mirroring the PubChem-CID convention.
    (
        ("dtxsid",),
        "DTXSID",
        "https://comptox.epa.gov/dashboard/chemical/details",
    ),
)


def _first_field(entity: Entity, aliases: tuple[str, ...]) -> str | None:
    """The first non-empty value among ``aliases`` on ``entity`` (or None)."""
    for alias in aliases:
        value = entity.fields.get(alias)
        if value not in (None, ""):
            return str(value)
    return None


def _first_of(fields: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    """The first non-empty value among ``aliases`` in a raw field dict."""
    for alias in aliases:
        value = fields.get(alias)
        if value not in (None, "", [], {}):
            return value
    return None


def _build_identifier_pvs(
    crate: ROCrate,
    specs: tuple[tuple[tuple[str, ...], str, str | None], ...],
    entity: Entity,
) -> list[ContextEntity]:
    """The ordered identifier PropertyValue nodes for ``entity`` (empty if none)."""
    out: list[ContextEntity] = []
    for aliases, scheme_name, property_id_url in specs:
        value = _first_field(entity, aliases)
        if value is not None:
            out.append(_identifier_pv(crate, scheme_name, value, property_id_url))
    return out


@lru_cache(maxsize=1)
def _context_terms() -> frozenset[str]:
    """Every term name the ISA-Tox ``@context`` defines.

    The authority for "is this a real property?" — checked instead of guessing
    from the key's shape, so the snake_case AOP-Wiki vocabulary survives while a
    caller's invented ``release_date`` still does not.
    """
    from profiles.context import ISA_TOX_CONTEXT

    blocks = ISA_TOX_CONTEXT if isinstance(ISA_TOX_CONTEXT, list) else [ISA_TOX_CONTEXT]
    return frozenset(key for block in blocks if isinstance(block, dict) for key in block)


def _scalar_props(entity: Entity, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Plain-value properties of an entity (references/discriminators removed).

    Snake_case survivors are DROPPED **unless the context defines them**. Almost
    every term in the RO-Crate and ISA-Tox contexts is camelCase, so an
    underscored key that reached this point is usually a field a caller invented
    (``release_date`` instead of ``releaseDate``, ``measurement_method`` instead
    of ``measurementMethod``). Emitting one produced a bare JSON-LD key absent
    from the ``@context``, which fails BASE with "not allowed in the compacted
    JSON-LD context" — a failure the agent cannot fix by editing the crate,
    because the invalid key is regenerated from state on every build.

    The exception is real: the AOP-Wiki vocabulary is snake_case
    (``has_molecular_initiating_event``, ``has_key_event_relationship``,
    ``upstream_event``, …). Those ARE context terms, so the test is membership in
    the context, not the presence of an underscore — a purely syntactic rule
    silently emptied every materialised AOP subgraph.

    The mapper's OWN snake_case inputs (``pubchem_cid``, ``dest_path``,
    ``process_type``, ``cell_seeding_density``, …) never reach here: they are
    consumed upstream into identifiers, parameters and paths, and listed in
    ``_REF_FIELDS`` / ``_STRUCT_FIELDS`` / the caller's ``skip``.
    """
    drop = _REF_FIELDS | _STRUCT_FIELDS | set(skip)
    props: dict[str, Any] = {}
    for key, value in entity.fields.items():
        if key in drop or key.startswith("@"):
            continue
        if "_" in key and key not in _context_terms():
            # One line, and only the part that varies. The generic advice that
            # used to trail every one of these ("Use the context's property
            # instead (e.g. releaseDate, measurementMethod)") wrapped each
            # notice onto a second row and was identical every time; it belongs
            # in this comment, not repeated in the user's transcript. A field is
            # dropped because emitting a non-context term fails BASE conformance
            # — the fix is to write the context's own property name instead.
            logger.warning(
                "Dropped %r from %s (not a term in the crate's JSON-LD context)",
                key,
                entity.entity_id,
            )
            continue
        props[key] = value
    return props


def _bare_doi(raw: str) -> str:
    """Strip a DOI's URL or CURIE prefix down to the bare ``10.xxxx/...`` form.

    Recognises ``https://doi.org/`` / ``http://doi.org/`` URL forms and a ``doi:``
    CURIE prefix (case-insensitive). Returns the input unchanged when no prefix is
    present (it is already bare). Used to rebuild the canonical
    ``https://doi.org/<bare>`` @id from whatever DOI form a field carries (#179).
    """
    value = raw.strip()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    if value.lower().startswith("doi:"):
        return value[len("doi:") :]
    return value


def _chebi_purl(entity: Entity) -> str | None:
    """The resolvable ChEBI PURL for a ChEBI-fallback MolecularEntity, or None.

    ``lookup_compound``'s ChEBI fallback (no PubChem CID) carries the
    dereferenceable ontology IRI on ``sameAs`` as an ``{"@id": <PURL>}`` node and
    the ChEBI CURIE on ``chebiId`` (the OLS ``short_form`` ``CHEBI_<n>`` or the
    ``CHEBI:<n>`` colon form). Prefer the ``sameAs`` PURL verbatim when it is a
    ChEBI obo IRI, else derive ``http://purl.obolibrary.org/obo/CHEBI_<n>`` from
    the ``chebiId`` CURIE. Returns None when neither is present so the caller falls
    back to the local fragment — never fabricating an id (D5).
    """
    same_as = entity.fields.get("sameAs")
    iri = same_as.get("@id") if isinstance(same_as, dict) else same_as
    if isinstance(iri, str) and "obo/CHEBI_" in iri:
        return iri.strip()
    chebi_id = entity.fields.get("chebiId")
    if chebi_id:
        num = str(chebi_id).strip().replace("CHEBI:", "CHEBI_")
        if num.startswith("CHEBI_"):
            return f"http://purl.obolibrary.org/obo/{num}"
    return None


def _mint_id(entity: Entity) -> str:
    """A spec-correct @id: resolvable URI when available, else `#`-fragment.

    Fragment @ids are type-qualified (``#Sample_my_cell``) so two entities
    of different types sharing an ``entity_id`` produce distinct @ids and
    ro-crate-py never silently merges them in the graph (see Issue #57).
    """
    t, f, eid = entity.type, entity.fields, entity.entity_id
    if t == "Person" and f.get("orcid"):
        o = str(f["orcid"]).strip()
        return o if o.startswith("http") else f"https://orcid.org/{o}"
    if t == "Organization" and f.get("ror"):
        r = str(f["ror"]).strip()
        return r if r.startswith("http") else f"https://ror.org/{r.lstrip('/')}"
    if t == "MolecularEntity":
        # A PubChem CID is the preferred clean external @id. A ChEBI-fallback
        # compound (no CID) carries a resolvable ChEBI PURL on sameAs / chebiId —
        # use it instead of a `#MolecularEntity_<eid>` fragment so the @id is
        # externally resolvable (#179, D5: only ids the lookup produced).
        if f.get("pubchem_cid"):
            return f"https://pubchem.ncbi.nlm.nih.gov/compound/{f['pubchem_cid']}"
        chebi_purl = _chebi_purl(entity)
        if chebi_purl:
            return chebi_purl
    if t == "CellLineSample":
        # A Cellosaurus accession IS the cell line's identifier, and it
        # dereferences. Minting `#CellLineSample_cell_cho_k1` next to a field
        # holding CVCL_0214 published a private handle for something the world
        # already has a public name for — the same crate would not merge with
        # anyone else's, and a reader could not follow it. Person/ORCID,
        # Organization/ROR, MolecularEntity/PubChem and Publication/DOI already
        # work this way; the cell line was the gap.
        accession = str(f.get("accession") or f.get("rrid") or "").strip()
        accession = accession.removeprefix("RRID:").removeprefix("CVCL:").strip()
        if accession.startswith("http"):
            return accession
        if accession.upper().startswith("CVCL_"):
            return f"https://www.cellosaurus.org/{accession}"
    if t == "Publication":
        # Recognise a DOI on either `doi` or `identifier` in URL, CURIE, or bare
        # form. The real pipeline never sets a `doi` field — Crossref returns the
        # DOI as the full URL `https://doi.org/10...` on `identifier` (#179).
        # Because that value does not start with the literal `"10."`, the old
        # branch fell through to a `#Publication_...` fragment, so the auto-wired
        # root `citation` referenced a fragment @id and the base check
        # ro-crate-1.2_19.1 failed ("Citation … must be an absolute URI"). Minting
        # the absolute `https://doi.org/<bare>` URL keeps the citation @id valid.
        raw = str(f.get("doi") or f.get("identifier") or "").strip()
        is_doi = raw.startswith("10.") or "doi.org/" in raw or raw.lower().startswith("doi:")
        if is_doi:
            return raw if raw.startswith("http") else f"https://doi.org/{_bare_doi(raw)}"
    if eid.startswith(("#", "http://", "https://", "./")) or "://" in eid:
        return eid
    return "#" + _slug(t) + "_" + eid


def _contain_dest(raw: str, fallback: str) -> str:
    """Contain a crate-relative ``dest_path`` so it can never escape the crate
    output dir (#167).

    ``ro-crate-py`` writes a File at ``output_dir / dest_path`` and only blocks
    *absolute* dest paths, so an LLM/injection-set ``../../../escaped.csv`` would
    write source bytes **outside** the crate. This normalises *raw*, rejects an
    absolute path or any ``..`` component that climbs out of the crate root, and
    falls back to a safe in-crate *fallback* (``data/<slug>``) when the requested
    destination would escape. A remote URL is left untouched (it is not a write).
    """
    if raw.startswith(("http://", "https://")) or raw.startswith("#"):
        return raw
    # Absolute paths (POSIX or drive/UNC) can never stay inside the crate root.
    pure = Path(raw)
    if pure.is_absolute() or raw.startswith(("/", "\\")):
        logger.warning("Refusing absolute dest_path %r; falling back to %r", raw, fallback)
        return fallback
    # Normalise and reject anything that climbs above the crate root.
    normalized = PurePosixPath(os.path.normpath(raw.replace("\\", "/")))
    parts = normalized.parts
    if not parts or parts[0] in ("..", os.pardir) or any(p == ".." for p in parts):
        logger.warning("Refusing traversal dest_path %r; falling back to %r", raw, fallback)
        return fallback
    return normalized.as_posix()


def _file_dest(fe: Entity) -> str:
    """A relative URI path for a File data entity, contained to the crate root.

    The destination is sandboxed (#167): a traversal/absolute ``dest_path`` set
    by the LLM (or via prompt injection) is refused and replaced with the safe
    ``data/<slug>`` fallback so no payload byte is ever written outside the crate
    output directory.
    """
    f = fe.fields
    fallback = f"data/{_slug(f.get('name') or fe.entity_id)}"
    path = f.get("dest_path") or f.get("path") or f.get("contentUrl")
    if not path:
        return fallback
    return _contain_dest(str(path), fallback)


def _known_file_size(state: CrateState, fe: Entity, input_path: str | None) -> int | None:
    """Bytes for a drafted File, from whatever already knows — never a guess.

    Three places may know, in order of authority: the entity's own
    ``contentSize``, the scan that measured the file, and the file itself. A
    drafted File that matches none of them has no size stated, which is correct:
    a synthesized placeholder describes no bytes on disk.

    This exists because the size used to depend on HOW a file entered the crate.
    The scanned-file loop set it; the drafted-File loop emitted only the entity's
    own fields; and a file that was BOTH drafted and scanned took the drafted path
    and was skipped by the scanned one as already covered. So the same file in the
    same crate carried a size or not depending on which tool created it — two
    exports of one session differed on exactly this.
    """
    existing = fe.fields.get("contentSize")
    if existing:
        try:
            return int(str(existing))
        except (TypeError, ValueError):
            pass  # a malformed value is replaced below, not propagated

    dest = _file_dest(fe)
    for fc in getattr(state, "scanned_files", []) or []:
        if fc.size and (fc.filename == Path(dest).name or str(fc.path).endswith(dest)):
            return int(fc.size)

    source = _file_source(fe, input_path)
    if source:
        try:
            return Path(source).stat().st_size
        except OSError:
            logger.debug("Could not size %s for contentSize", source, exc_info=True)

    # A provisional table has no file on the in-memory path, but its payload is
    # the header line `_materialize_provisional_table` is about to write — so its
    # length is known without writing it. Sizing the CONTENT rather than the file
    # keeps the validated crate and the written one saying the same thing, which
    # is the whole reason these tables are described identically on both paths.
    if fe.fields.get("provisional"):
        spec = _PROVISIONAL_TABLES.get(str(fe.fields.get("table_kind") or "measurements"))
        if spec is not None:
            _name, columns = spec
            header = ",".join(c["titles"] for c in columns) + "\n"
            return len(header.encode("utf-8"))
    return None


def _file_source(fe: Entity, input_path: str | None) -> str | None:
    """Resolve the on-disk source for a File data entity, or ``None`` (#128).

    Returns an absolute path when the referenced file exists locally, so
    ``ro-crate-py`` copies it into the crate payload at ``crate.write()`` time
    (its ``_copy_file`` skips the copy when source and dest are the same file, so
    in-place builds where ``output_path == input_path`` are safe). Returns
    ``None`` for remote (``http(s)://``) references or files not found on disk —
    leaving the File as a metadata-only reference rather than a phantom copy.

    Security (#167): when ``input_path`` is known, a source is refused unless its
    **realpath** stays inside ``input_path``. This contains a symlink whose
    target escapes the input tree (the resolved path is what gets matched), so
    injection cannot package an arbitrary local file into the shareable crate.
    """
    f = fe.fields
    raw = f.get("path") or f.get("contentUrl") or f.get("dest_path")
    if not raw:
        return None
    raw = str(raw)
    if raw.startswith(("http://", "https://")):
        return None
    src = Path(raw)
    if not src.is_absolute() and input_path:
        src = Path(input_path) / raw
    if not src.is_file():
        return None
    # Contain against the input tree: refuse a source whose realpath escapes
    # ``input_path`` (e.g. a symlink pointing outside it). With no input_path we
    # cannot define a boundary, so fall back to the prior is_file() behaviour.
    if input_path:
        from builder.tools.scanner import _contain

        if _contain(src, {str(Path(input_path).resolve())}) is None:
            logger.warning(
                "Refusing File source %s — realpath escapes input tree %s (#167)",
                src,
                input_path,
            )
            return None
    return str(src)


def _resolve_many(idx: dict[str, Any], value: Any) -> list[Any]:
    """Resolve an entity reference (id, {@id}, or list thereof) to crate entities.

    Resolution order:
    1. Exact match on ``key``
    2. Strip a leading ``#`` and try again
    3. Try type-qualified fragments (``#Sample_cell_01``) via ``_resolve_typed``
       so references from state's bare ``entity_id`` can still resolve when
       ``_mint_id`` has type-qualified the fragment (Issue #57).
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[Any] = []
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key is None:
            continue
        ent = idx.get(key) or idx.get(str(key).lstrip("#"))
        if ent is not None:
            out.append(ent)
            continue
        # Fallback: try type-qualified lookup (bare entity_id → typed fragment)
        for etype in _ENTITY_TYPES:
            typed_key = f"{etype}:{key}"
            frag_key = "#" + _slug(etype) + "_" + str(key).lstrip("#")
            ent = idx.get(typed_key) or idx.get(frag_key)
            if ent is not None:
                out.append(ent)
                break
    return out


# Known entity types for typed-id resolution fallback.
_ENTITY_TYPES = (
    "Investigation",
    "Study",
    "Assay",
    "LabProcess",
    "LabProtocol",
    "Sample",
    "CellLineSample",
    "MolecularEntity",
    "Person",
    "Organization",
    "Publication",
    "DefinedTerm",
    "PropertyValue",
    "File",
    "AdverseOutcomePathway",
    "KeyEvent",
    "KeyEventRelationship",
)


def _resolve_one(idx: dict[str, Any], value: Any) -> Any:
    found = _resolve_many(idx, value)
    return found[0] if found else None


def _idx_add(idx: dict[str, Any], entity: Entity, node: Any) -> Any:
    """Register ``node`` in ``idx`` under both bare and type-qualified keys.

    The bare ``entity.entity_id`` key preserves backward compatibility for
    cross-references that use bare entity IDs. The type-qualified key
    ``{type}:{entity_id}`` guarantees uniqueness when two entity types
    share the same ``entity_id`` (e.g. Sample and CellLineSample, Issue #57).

    Returns ``node`` for chaining.
    """
    bare = entity.entity_id
    typed = f"{entity.type}:{bare}"
    idx[typed] = node
    if bare not in idx:
        idx[bare] = node
    return node


# ---------------------------------------------------------------------------
# Root, conformance & contextual/leaf entities
# ---------------------------------------------------------------------------


# Values a crate must carry under TWO predicates because the two profiles it
# declares ask for the same thing in different vocabularies. Each pair is
# (key the builder emits, key that mirrors it) — both are context terms, so the
# mirror lands on the second profile's IRI. Kept as data, not four call sites,
# so adding a profile means adding a row.
_PROFILE_MIRRORED_KEYS: tuple[tuple[str, str], ...] = (("parameter", "parameterValue"),)


def _mirror_profile_predicates(crate: ROCrate) -> None:
    """Re-emit values that two declared profiles name differently.

    A process's parameters are ``schema:additionalProperty`` to schema.org (and
    to our own tox shapes) and ``bioschemas:parameterValue`` to the ISA profile.
    Both are true, and a crate declaring conformance to both owes the reader
    both — writing only one leaves half the shapes looking at an IRI that is not
    there, which reads as missing data when the parameters are right in the
    node.

    The mirror is a reference to the SAME PropertyValue nodes, not a copy, so
    the graph gains a predicate rather than duplicate entities.
    """
    for entity in crate.get_entities():
        for source, mirror in _PROFILE_MIRRORED_KEYS:
            value = entity.get(source)
            if value not in (None, "", [], {}) and entity.get(mirror) in (None, "", [], {}):
                entity[mirror] = value


# The root identifier stays a PLAIN STRING, and the RO-Crate 1.2 recommendation
# "the Root Data Entity SHOULD use PropertyValue entities for identifiers"
# (Science-on-Schema.org) is deliberately left unsatisfied. The two profiles this
# crate declares contradict each other here:
#
#   ro-crate-1.2 should/2_root_data_entity_identifier.ttl
#       every schema:identifier on ./ SHOULD be a schema:PropertyValue  (Warning)
#   isa-ro-crate 0_investigation.ttl
#       schema:identifier on ./ MUST have sh:datatype xsd:string        (Violation)
#
# A PropertyValue is referenced by IRI, so satisfying the first breaks the
# second, and no mixed form escapes it: the SHOULD's SPARQL flags ANY identifier
# that is not a PropertyValue, while the MUST's sh:datatype constrains EVERY
# value of the path. Trading a Violation for a Warning is a bad trade — ISA
# conformance is the stronger claim, and the pipeline asserts it end to end.
#
# This was tried (the wrap ran last, after the ISA hierarchy had derived
# study/assay identifiers from the root's as text) and it flipped ISA conformance
# to False. Leaving the note so the next reader knows the finding is a decision,
# not an oversight.


def _license_value(crate: ROCrate, license_value: str) -> Any:
    """The root's ``license``: a described contextual entity when we can name it.

    The profile asks a License entity for a name and a description, and a bare
    URL string is neither. When `describe_license` recognises the URL, the crate
    carries a real entity that says what the licence is; when it does not, the
    value is left exactly as given rather than dressed up with an invented name.
    """
    value = (license_value or "").strip()
    described = describe_license(value)
    if described is None:
        return license_value
    return crate.add(
        ContextEntity(
            crate,
            value,
            properties={
                "@type": "CreativeWork",
                "name": described["name"],
                "description": described["description"],
            },
        )
    )


def _populate_root_and_conformance(state: CrateState, crate: ROCrate) -> None:
    m = state.metadata
    # Base RO-Crate MUST: the Root Data Entity has a name and a description.
    crate.root_dataset["name"] = m.title or "Untitled Investigation"
    crate.root_dataset["description"] = (
        m.description or m.title or "RO-Crate generated by vitro-crate."
    )
    if m.accession:
        crate.root_dataset["identifier"] = m.accession
    # Root dates (#180). Emit schema:releaseDate / schema:dateModified only when
    # set — never fabricated (D5). ro-crate-py auto-sets datePublished at crate
    # construction, so it is left untouched here unless explicitly provided.
    if m.release_date:
        crate.root_dataset["releaseDate"] = m.release_date
    if m.date_modified:
        crate.root_dataset["dateModified"] = m.date_modified
    crate.root_dataset["additionalType"] = "Investigation"
    # Base RO-Crate MUST: the Root Data Entity has a license. A license the user
    # gave wins; the ISA-Tox shape endorses this placeholder when none is known.
    if m.license:
        crate.root_dataset["license"] = _license_value(crate, m.license)
    elif not crate.root_dataset.get("license"):
        crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"

    # Conformance placement follows RO-Crate 1.2 (ro-crate-1.2.0.md §Profiles,
    # isa_tox.md §Conformance): the metadata file descriptor's conformsTo is
    # reserved for the single base-spec URI, while the profiles the crate targets
    # are declared on the Root Data Entity (./) — Issue #91.
    #
    # The base spec is now 1.2 (ROCRATE_SPEC). The #105 deferral to 1.1 is lifted:
    # roc-validator 0.11.0 ships a ro-crate-1.2 base profile
    # (crs4/rocrate-validator#164), so the base pass validates against 1.2 and
    # build_and_validate (#87) + the golden fixtures (#97) stay green — Issue #110.
    crate.metadata["conformsTo"] = {"@id": ROCRATE_SPEC}

    # Profiles the crate TARGETS, declared on ./ unconditionally — the three-layer
    # (RO-Crate → ISA → ISA-Tox) duck-typing architecture rests on this
    # declaration and validation should be able to see the profiles a crate claims
    # (#89, "guidance over strictness"), independent of any prior validation pass;
    # conformance is reported separately via build_and_validate (#87).
    for pid in (PROFILE_ISA, PROFILE_ISATOX):
        crate.root_dataset.append_to("conformsTo", {"@id": pid})
    crate.add(
        ContextEntity(
            crate,
            PROFILE_ISA,
            properties={
                "@type": ["CreativeWork", "Profile"],
                "name": "ISA RO-Crate Profile",
            },
        )
    )
    crate.add(
        ContextEntity(
            crate,
            PROFILE_ISATOX,
            properties={
                "@type": ["CreativeWork", "Profile"],
                "name": "ISA-Tox RO-Crate Profile",
                "version": "0.1.0-draft.1",
            },
        )
    )


# CellLineSample fields promoted to schema:additionalProperty Characteristic
# PropertyValue nodes (ISA Sample Characteristics; #143, #180).
#
# Each entry maps the recognised state field (plus any drafter aliases) to the
# PropertyValue's display ``name`` and its ``propertyID`` ontology IRI. ``organ``
# and ``tissue`` mirror the gold crate's #SampleCell_MDCK1 characteristics
# (PropertyValue "Organ"/"Tissue" with the ISA-Tox ``param/{organ,tissue}``
# propertyID); ``passage``/``growth`` keep their lower-case ISA names (#143).
@dataclass(frozen=True)
class _Characteristic:
    aliases: tuple[str, ...]  # candidate state field names (first non-empty wins)
    name: str  # PropertyValue display name
    property_id: str | None  # ontology IRI for propertyID, or None


_CELL_LINE_CHARACTERISTICS: tuple[_Characteristic, ...] = (
    _Characteristic(("passage",), "passage", iri("EFO:0007061")),
    _Characteristic(("growth",), "growth", iri("BAO:0002648")),
    _Characteristic(("organ",), "Organ", f"{PROFILE_ISATOX}/param/organ"),
    _Characteristic(("tissue",), "Tissue", f"{PROFILE_ISATOX}/param/tissue"),
)
# NB: the field names above (passage/growth/organ/tissue) are also listed in
# _STRUCT_FIELDS so _scalar_props strips them from the Sample node — they round-trip
# only as additionalProperty PropertyValue characteristics, never as raw literals.


def _cell_line_characteristics(crate: ROCrate, cl: Entity) -> list[Any]:
    """Build CharacteristicValue (PropertyValue) nodes for a CellLineSample.

    Promotes recognised culture-characteristic fields (``passage``, ``growth``,
    ``organ``, ``tissue``) to ISA Sample Characteristics — schema:additionalProperty
    PropertyValue nodes carrying the value and, when known, the property's ontology
    IRI as ``propertyID``. Returns an empty list when none are present (D5: a field
    that is absent is never fabricated).
    """
    out: list[Any] = []
    for char in _CELL_LINE_CHARACTERISTICS:
        value = _first_field(cl, char.aliases)
        if value in (None, ""):
            continue
        props: dict[str, Any] = {}
        if char.property_id:
            # An @id reference — see `_identifier_property_value`.
            props["propertyID"] = {"@id": char.property_id}
        out.append(
            CharacteristicValue(
                crate,
                param_id(char.name, str(value)),
                name=char.name,
                value=str(value),
                properties=props or None,
            )
        )
    return out


# Fields that describe how to reach a Person or Organization. Held back from the
# node's scalar properties and re-emitted as a ContactPoint entity instead.
_CONTACT_FIELDS: tuple[str, ...] = ("email", "telephone", "contactPoint", "contact_point")


def _attach_contact_point(crate: ROCrate, node: Any, entity: Entity) -> None:
    """Emit an entity's contact details as a ContactPoint, referenced from *node*.

    Both profiles ask for the same thing in the same way: an Organization's
    ``contactPoint`` SHOULD reference a ContactPoint contextual entity, and the
    root's authors/publishers SHOULD have one between them. An email written as a
    literal on the Person satisfies neither, because the shapes want an entity to
    point at — the same reference-not-literal rule that already governs
    affiliation and creator.

    Nothing is invented (D5): with no contact details in state this emits
    nothing, and the finding stays open rather than being answered with a made-up
    address. The details arrive from the human, which is the only place a contact
    for a real person can legitimately come from.
    """
    email = _first_field(entity, ("email", "contactPoint", "contact_point"))
    telephone = _first_field(entity, ("telephone",))
    if not email and not telephone:
        return
    properties: dict[str, Any] = {"@type": "ContactPoint", "contactType": "correspondence"}
    if email:
        # A `mailto:` prefix is how a human often writes it; the shapes and
        # schema.org both want the bare address.
        properties["email"] = email.removeprefix("mailto:").strip()
    if telephone:
        properties["telephone"] = telephone
    anchor = properties.get("email") or properties.get("telephone") or ""
    contact = crate.add(
        ContextEntity(crate, f"#contact_{_slug(str(anchor))}", properties=properties)
    )
    node.append_to("contactPoint", contact)


def _cell_line_term(crate: ROCrate) -> ContextEntity:
    """The shared, resolvable 'cell line' DefinedTerm for CellLineSample.sampleType."""
    return crate.add(
        ContextEntity(
            crate,
            CELL_LINE_TERM_ID,
            properties={
                "@type": "DefinedTerm",
                "name": "cell line",
                "termCode": ["NCIT:C16403", "IUCLID:108174"],
                "inDefinedTermSet": {"@id": "http://purl.obolibrary.org/obo/ncit.owl"},
            },
        )
    )


def _add_leaves(
    state: CrateState,
    crate: ROCrate,
    idx: dict[str, Any],
    output_dir: Path | None = None,
    *,
    materialize_payload: bool = True,
    include_all_scanned: bool = True,
) -> None:
    for org in state.list_entities("Organization"):
        org_node = crate.add(
            ContextEntity(
                crate,
                _mint_id(org),
                properties={"@type": "Organization", **_scalar_props(org, skip=_CONTACT_FIELDS)},
            )
        )
        _attach_contact_point(crate, org_node, org)
        _idx_add(idx, org, org_node)

    for person in state.list_entities("Person"):
        # affiliation is a reference, not a literal: resolve it to the in-crate
        # Organization node (or keep a bare IRI), and never emit it as a string.
        node = crate.add(
            Person(
                crate,
                _mint_id(person),
                properties=_scalar_props(person, skip=("affiliation", *_CONTACT_FIELDS)),
            )
        )
        _attach_contact_point(crate, node, person)
        _idx_add(idx, person, node)
        # A looked-up ORCID round-trips as an ORCID PropertyValue identifier (#180).
        orcid = person.fields.get("orcid")
        if orcid not in (None, ""):
            bare = str(orcid).strip().rsplit("/", 1)[-1]
            node.append_to("identifier", _identifier_pv(crate, "ORCID", bare, "https://orcid.org"))
        _wire_reference(
            node, "affiliation", person.fields.get("affiliation"), idx, keep_literal=True
        )
        crate.root_dataset.append_to("author", node)

    for chem in state.list_entities("MolecularEntity"):
        node = crate.add(
            ContextEntity(
                crate,
                _mint_id(chem),
                properties={"@type": "MolecularEntity", **_scalar_props(chem)},
            )
        )
        _idx_add(idx, chem, node)
        # Looked-up CAS / PubChem CID round-trip as identifier PropertyValues, in
        # gold-crate order (CAS first, then PubChem CID) (#180).
        for pv in _build_identifier_pvs(crate, _MOLECULAR_IDENTIFIERS, chem):
            node.append_to("identifier", pv)

    for dt in state.list_entities("DefinedTerm"):
        _idx_add(
            idx,
            dt,
            crate.add(
                ContextEntity(
                    crate,
                    _mint_id(dt),
                    properties={"@type": "DefinedTerm", **_scalar_props(dt)},
                )
            ),
        )

    for pv in state.list_entities("PropertyValue"):
        _idx_add(
            idx,
            pv,
            crate.add(
                ContextEntity(
                    crate,
                    _mint_id(pv),
                    properties={"@type": "PropertyValue", **_scalar_props(pv)},
                )
            ),
        )

    # AOP-Wiki subgraph nodes (Issue #180). Each is a contextual entity typed by
    # its own AOP class (AdverseOutcomePathway / KeyEvent / KeyEventRelationship)
    # whose @id is the resolvable AOP-Wiki IRI. Their link properties
    # (has_*/upstream_event/downstream_event) are already {"@id": …} reference
    # objects pointing at sibling AOP node @ids, so the subgraph is cross-linked
    # verbatim — no fabricated ids (D5). They are kept out of _REF_FIELDS so
    # _scalar_props preserves them rather than stripping them as resolver inputs.
    #
    # Each also carries schema:DefinedTerm, exactly as the csvw:Column nodes do.
    # The AOP classes resolve to https://aopwiki.org/ontology/… (profiles/context.py),
    # which is not a schema.org type — so the base profile asked every one of
    # these for one, 36 findings on a real crate. These are NOT cited vocabulary
    # we could argue our way out of describing: `materialize_aop_subgraph` fetches
    # them, names them and puts them in the graph deliberately, so the finding is
    # correct and the answer is to satisfy it. A Key Event IS a defined term —
    # an entry in a controlled vocabulary, with AOP-Wiki as its DefinedTermSet —
    # so this states what the node already is, under a term the base profile
    # reads. The AOP class stays first and unchanged, so the tox and ISA shapes
    # that match on it are untouched.
    for aop_type in ("AdverseOutcomePathway", "KeyEvent", "KeyEventRelationship"):
        for aop_entity in state.list_entities(aop_type):
            _idx_add(
                idx,
                aop_entity,
                crate.add(
                    ContextEntity(
                        crate,
                        _mint_id(aop_entity),
                        properties={
                            "@type": [aop_entity.type, "schema:DefinedTerm"],
                            **_scalar_props(aop_entity),
                        },
                    )
                ),
            )

    for pub in state.list_entities("Publication"):
        node = crate.add(
            ContextEntity(
                crate,
                _mint_id(pub),
                properties={"@type": "ScholarlyArticle", **_scalar_props(pub)},
            )
        )
        _idx_add(idx, pub, node)
        # ScholarlyArticle.author is an array of Person references (#180). Persons
        # are added earlier in this pass, so their nodes resolve from the index.
        for author in _resolve_many(idx, pub.fields.get("author")):
            node.append_to("author", author)
        crate.root_dataset.append_to("citation", node)

    for fe in state.list_entities("File"):
        # Resolve the on-disk source so ro-crate-py copies the file into the
        # payload at write() time (#128). Skip on the in-memory build_and_validate
        # path (materialize_payload=False) — nothing is written there.
        source = _file_source(fe, state.metadata.input_path) if materialize_payload else None
        # A synthesized placeholder has no file on disk, so `_file_source`
        # correctly returns None and the crate would claim a file it does not
        # contain (#438). Materialise a minimal typed table for it instead.
        provisional_rel: str | None = None
        if source is None and materialize_payload and fe.fields.get("provisional"):
            provisional_rel = _file_dest(fe)
            source = _materialize_provisional_table(fe, output_dir, provisional_rel)
            if source is None:
                provisional_rel = None
        # Co-type a source-code (or otherwise extra-typed) File as a @type list,
        # e.g. ["File", "SoftwareSourceCode"] for an analysis script (#180, gold
        # plot.py). A plain File keeps its scalar @type. additional_types is
        # consumed here, never emitted as a stray literal (see _STRUCT_FIELDS).
        file_type: Any = "File"
        extra_types = fe.fields.get("additional_types")
        if extra_types:
            seen: set[str] = set()
            file_type = []
            for t in ["File", *extra_types]:
                if t and t not in seen:
                    seen.add(t)
                    file_type.append(t)
        # A materialised provisional table is co-typed csvw:Table and carries an
        # explicit note that it holds no rows, so a consumer can never mistake
        # the template for data.
        props: dict[str, Any] = {"@type": file_type, **_scalar_props(fe)}
        # Every File gets a size if anything knows one — see `_known_file_size`.
        # The base profile asks each File Data Entity for a contentSize, and the
        # answer was already in the scan or on disk.
        size = _known_file_size(state, fe, state.metadata.input_path)
        if size is not None:
            props["contentSize"] = str(size)
        if fe.fields.get("provisional"):
            # Say so in the NAME, on every path. A description explains it, but
            # the name is what a file browser, the preview page and a reader's
            # eye actually show — and a template the tool minted to satisfy a
            # shape must never be mistaken for data somebody measured. Keyed on
            # the flag rather than on materialisation, so the validated crate and
            # the written one describe the file identically.
            props["name"] = _autogenerated_name(props.get("name") or fe.entity_id)
        if fe.fields.get("provisional"):
            # Keyed on the FLAG, not on materialisation — the same correction the
            # name above already carries. Keyed on `provisional_rel` these were
            # co-typed and described only in the written crate, so the in-memory
            # validation the agent actually reads reported every provisional table
            # as an undescribed File for the whole run, and the two crates
            # disagreed about what the same file is.
            types = file_type if isinstance(file_type, list) else [file_type]
            props["@type"] = [*types, "csvw:Table"]
            props["description"] = _PROVISIONAL_NOTE
        _idx_add(
            idx,
            fe,
            node := crate.add(
                File(
                    crate,
                    source,
                    dest_path=_file_dest(fe),
                    properties=props,
                )
            ),
        )
        if fe.fields.get("provisional"):
            # Keyed on the FLAG, not on materialisation — the same correction the
            # name, the co-type and the description above already carry.
            #
            # These columns are OURS: the build generates them from
            # `_PROVISIONAL_TABLES`, so the crate can declare them whether or not
            # a payload was written. Keyed on `provisional_rel`, the schema and
            # its columns existed only in the EXPORTED crate — nine nodes the
            # in-loop validation never saw, carrying what has historically been
            # this project's largest finding bucket. The agent iterated against a
            # graph with no CSVW schema at all, and those findings appeared for
            # the first time after export, when the loop that could have fixed
            # them had already finished.
            _attach_provisional_schema(crate, node, fe)

    for proto in state.list_entities("LabProtocol"):
        _idx_add(
            idx,
            proto,
            crate.add(
                ContextEntity(
                    crate,
                    _mint_id(proto),
                    properties={"@type": "LabProtocol", **_scalar_props(proto)},
                )
            ),
        )

    # Samples / CellLineSamples auto-add themselves (AutoAddContextEntity).
    cell_term: list[Any] = [None]
    for s in state.list_entities("Sample"):
        _idx_add(
            idx,
            s,
            Sample(
                crate,
                identifier=_mint_id(s),
                name=str(s.fields.get("name", "")),
                properties=_scalar_props(s, skip=("name",)) or None,
            ),
        )

    for cl in state.list_entities("CellLineSample"):
        if cell_term[0] is None:
            cell_term[0] = _cell_line_term(crate)
        characteristics = _cell_line_characteristics(crate, cl)
        _idx_add(
            idx,
            cl,
            CellLineSample(
                crate,
                identifier=_mint_id(cl),
                name=str(cl.fields.get("name", "")),
                sample_type=cell_term[0],
                accession=cl.fields.get("accession"),
                additionalProperty=characteristics or None,
                properties=_scalar_props(cl, skip=("name", "accession")) or None,
            ),
        )

    if include_all_scanned:
        _add_scanned_leaves(state, crate, materialize_payload=materialize_payload)


# RO-Crate-reserved filenames that must never be auto-added as payload leaves.
_RESERVED_CRATE_FILES = frozenset(
    {"ro-crate-metadata.json", "ro-crate-preview.html", "ro-crate-graph.mmd"}
)


def _add_scanned_leaves(state: CrateState, crate: ROCrate, *, materialize_payload: bool) -> None:
    """Package every scanned file not already a drafted File entity (#175).

    Auto-include is an honest *fallback*: files the agent has not explicitly
    placed are attached to the root ``hasPart`` as plain ``File`` leaves (dataset
    data whose assay/role is simply not annotated yet), so the exported crate
    never silently drops a file. Files the agent drafted/placed take precedence —
    they already have a File entity, so they are deduped out here by resolved
    source path (and dest). Semantic placement (under a Study/Assay/process, with
    a role) stays an agent decision via the drafting/linking tools; this only
    catches the untouched tail.
    """
    input_path = state.metadata.input_path
    root = Path(input_path).resolve() if input_path else None

    covered_src: set[str] = set()
    covered_dest: set[str] = set()
    for fe in state.list_entities("File"):
        src = _file_source(fe, input_path)
        if src:
            covered_src.add(str(Path(src).resolve()))
        covered_dest.add(_file_dest(fe))

    seen_dest: set[str] = set()
    for fc in state.scanned_files:
        if fc.filename in _RESERVED_CRATE_FILES:
            continue
        abspath = Path(fc.path)
        if not abspath.is_absolute() and input_path:
            abspath = Path(input_path) / fc.path
        try:
            abspath = abspath.resolve()
        except OSError:
            continue
        if str(abspath) in covered_src:
            continue

        dest: str | None = None
        if root is not None:
            try:
                dest = abspath.relative_to(root).as_posix()
            except ValueError:
                dest = None
        if not dest:
            dest = f"data/{fc.filename}"
        if dest in _RESERVED_CRATE_FILES or dest in covered_dest or dest in seen_dest:
            continue
        seen_dest.add(dest)

        source = str(abspath) if (materialize_payload and abspath.is_file()) else None
        props: dict[str, Any] = {"@type": "File", "name": fc.filename}
        if fc.mime_type:
            props["encodingFormat"] = fc.mime_type
        # The scan already measured this file, so the size costs nothing to state
        # and the base profile asks every File Data Entity for one.
        if fc.size:
            props["contentSize"] = str(fc.size)
        crate.add(File(crate, source, dest_path=dest, properties=props))


# ---------------------------------------------------------------------------
# Structural datasets & processes
# ---------------------------------------------------------------------------


def _child_ids(node: Any, key: str = "hasPart") -> list[str]:
    """The @id strings currently under ``node[key]`` (handles None/dict/list/str)."""
    value = node.get(key)
    if value is None:
        return []
    out: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        cid = getattr(item, "id", None)
        if cid is None and isinstance(item, str):
            cid = item
        if cid is not None:
            out.append(cid)
    return out


def _append_unique(node: Any, key: str, child: Any) -> None:
    """append_to(node, key, child) but skip if child's @id is already present."""
    cid = getattr(child, "id", None)
    if cid is None or cid not in _child_ids(node, key):
        node.append_to(key, child)


def _remove_child(node: Any, key: str, child_id: str) -> None:
    """Drop the reference to ``child_id`` from ``node[key]`` (ro-crate-py auto-adds
    every data entity to the root's hasPart; this un-parents it)."""
    value = node.get(key)
    if value is None:
        return
    items = value if isinstance(value, list) else [value]
    kept = [
        it
        for it in items
        if (getattr(it, "id", None) or (it if isinstance(it, str) else None)) != child_id
    ]
    if len(kept) != len(items):
        if kept:
            node[key] = kept
        else:
            del node[key]


def _isa_identifier(entity: Entity, parent_identifier: str | None, level: str) -> str:
    """A distinct, hierarchical ISA identifier so the levels never collide.

    The identifier nests under its parent and embeds the level
    (``FAB-2026`` → ``FAB-2026/study-study_1`` → ``…/assay-assay_1``), keeping each
    a single, non-empty string. ISA requires a non-empty identifier; the bare
    entity_id alone collides when the Investigation/Study/Assay were drafted with
    the same accession, and a Study and an Assay sharing an entity_id under the
    same parent would still collide without the level prefix.
    """
    slug = _slug(entity.entity_id)
    base = f"{level}-{slug}" if slug else level
    parent = (parent_identifier or "").rstrip("/")
    return f"{parent}/{base}" if parent and parent != "." else base


def _is_file_node(node: Any) -> bool:
    """True if an ro-crate node is a File/MediaObject data entity."""
    t = getattr(node, "type", None)
    if t is None:
        return False
    types = t if isinstance(t, list) else [t]
    return any(
        str(x).rsplit("/", 1)[-1].rsplit("#", 1)[-1] in ("File", "MediaObject") for x in types
    )


def _result_file_nodes(process_node: Any) -> list[Any]:
    """The File node(s) a built LabProcess produces (its schema:result/output)."""
    out: list[Any] = []
    seen: set[Any] = set()
    for key in ("output", "result"):
        value = process_node.get(key)
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            cid = getattr(item, "id", None)
            if cid is not None and _is_file_node(item) and cid not in seen:
                seen.add(cid)
                out.append(item)
    return out


def _attach_explicit_parts(node: Any, entity: Entity, idx: dict[str, Any], root: Any) -> None:
    """Move a Study/Assay entity's explicit ``hasPart`` File members under its node.

    ``attach_files`` (#177) records placement by appending File entity_ids to the
    dataset entity's ``hasPart`` field. Resolve those to their built File nodes
    and attach them under the dataset — **in addition to** the root's reference,
    never instead of it (#532). RO-Crate lets a data entity be ``hasPart`` of
    more than one Dataset, and the root's copy is what makes the file reachable
    at all: the file tree is walked from ``./`` through *directory* Datasets, and
    an ISA container is a contextual ``#Study_…`` / ``#Assay_…`` node, not a
    directory. Re-parenting therefore stranded every payload file — ro-crate-py
    refuses to load such a crate, while all three SHACL profiles pass it.
    """
    for key in ("hasPart", "has_part"):
        for child in _resolve_many(idx, entity.fields.get(key)):
            if child is node:
                continue
            _append_unique(node, "hasPart", child)


def _add_structural(state: CrateState, crate: ROCrate, idx: dict[str, Any]) -> None:
    root = crate.root_dataset

    # The Investigation IS the Root Data Entity (ISA: ./ represents the
    # Investigation). With exactly one Investigation, fold its scalar props onto
    # the root instead of emitting a duplicate #Investigation_* node, and index it
    # to the root so investigation_id references resolve to ./. (0 or 2+ is rare —
    # keep separate nodes.)
    investigations = state.list_entities("Investigation")
    if len(investigations) == 1:
        inv = investigations[0]
        # Same skip as the unfolded branch: agent references are resolved later
        # by `_wire_dataset_aliases`. Folding them on raw put the state id
        # `org_erasmus_mc` on the ROOT — the most visible node in the crate —
        # pointing at nothing, while the Organization itself sat under its ROR.
        for key, value in _scalar_props(inv, skip=_AGENT_REFERENCE_FIELDS).items():
            if key == "identifier":
                continue
            if root.get(key) in (None, ""):
                root[key] = value
        if root.get("identifier") in (None, ""):
            root["identifier"] = inv.fields.get("identifier") or inv.entity_id
        _idx_add(idx, inv, root)
    else:
        for inv in investigations:
            props = {
                "@type": "Dataset",
                "additionalType": "Investigation",
                # Agent references are stripped here and re-emitted RESOLVED by
                # _wire_dataset_aliases; left in, the raw state id ships beside
                # the resolved one and the crate carries both.
                **_scalar_props(inv, skip=_AGENT_REFERENCE_FIELDS),
            }
            props["identifier"] = _isa_identifier(inv, None, "investigation")
            node = crate.add(DataEntity(crate, _mint_id(inv), properties=props))
            _idx_add(idx, inv, node)
            _append_unique(root, "hasPart", node)

    root_ident = root.get("identifier") or "./"

    for st in state.list_entities("Study"):
        props = {
            "@type": "Dataset",
            "additionalType": "Study",
            **_scalar_props(st, skip=_AGENT_REFERENCE_FIELDS),
        }
        props["identifier"] = _isa_identifier(st, root_ident, "study")
        node = crate.add(DataEntity(crate, _mint_id(st), properties=props))
        _idx_add(idx, st, node)
        _append_unique(root, "hasPart", node)  # Study MUST be hasPart of the root
        _attach_explicit_parts(node, st, idx, root)

    for asy in state.list_entities("Assay"):
        props = {
            "@type": "Dataset",
            "additionalType": "Assay",
            **_scalar_props(asy, skip=_AGENT_REFERENCE_FIELDS),
        }
        parent = _resolve_one(idx, asy.fields.get("study_id")) or root
        props["identifier"] = _isa_identifier(asy, parent.get("identifier") or root_ident, "assay")
        node = crate.add(DataEntity(crate, _mint_id(asy), properties=props))
        _idx_add(idx, asy, node)
        # crate.add auto-added the Assay to the root's hasPart; nest it under its
        # Study instead (no double-parenting).
        if parent is not root:
            _remove_child(root, "hasPart", node.id)
        _append_unique(parent, "hasPart", node)
        _attach_explicit_parts(node, asy, idx, root)


def _synth_protocol(crate: ROCrate, assay_id: Any, cache: dict[str, Any]) -> ContextEntity:
    key = str(assay_id) if assay_id else "_default"
    if key not in cache:
        cache[key] = crate.add(
            ContextEntity(
                crate,
                f"#protocol_{_slug(key)}",
                properties={"@type": "LabProtocol", "name": f"Protocol for {key}"},
            )
        )
    return cache[key]


def _synth_sample(crate: ROCrate, sid: str, name: str, derives_from: Any = None) -> Sample:
    props: dict[str, Any] = {}
    if derives_from is not None:
        props["derivesFrom"] = derives_from
    ident = sid if sid.startswith("#") else "#" + _slug(sid)
    return Sample(crate, identifier=ident, name=name, properties=props or None)


# Typed CSVW columns for the condition table: each maps a CSV column to an
# ontology property (propertyUrl) with a declared datatype. The cell-line and
# compound columns additionally resolve to in-crate entity ids (valueUrl, filled
# at build time), so the per-well design table is machine-readable rather than a
# header-only placeholder. The 10-column contract mirrors the gold S-VHPS21 crate
# (Issue #180, Lane D — extends the original 5-column schema from #94).
_CONDITION_TABLE_COLUMNS: tuple[dict[str, str], ...] = (
    {
        "titles": "well_id",
        "datatype": "string",
        "propertyUrl": "http://purl.org/dc/terms/identifier",
    },
    {
        "titles": "assay",
        "datatype": "string",
        "propertyUrl": iri("NCIT:C60819"),
    },
    {
        "titles": "cell_line",
        "datatype": "string",
        "propertyUrl": iri("NCIT:C16403"),
    },
    {
        "titles": "compound",
        "datatype": "string",
        "propertyUrl": iri("CHEBI:23367"),
    },
    {
        "titles": "concentration_value",
        "datatype": "double",
        "propertyUrl": iri("PATO:0000033"),
    },
    {
        "titles": "concentration_unit",
        "datatype": "string",
        "propertyUrl": iri("IAO:0000039"),
    },
    {
        "titles": "exposure_duration",
        "datatype": "string",
        "propertyUrl": iri("NCIT:C83280"),
    },
    {
        "titles": "experiment",
        "datatype": "string",
        "propertyUrl": iri("EFO:0002091"),
    },
    {
        "titles": "technical_replicate",
        "datatype": "string",
        "propertyUrl": iri("EFO:0002090"),
    },
    {
        "titles": "control",
        "datatype": "string",
        "propertyUrl": iri("NCIT:C28143"),
    },
)

# Header line (column titles, in order) for the materialised condition-table CSV.
# Derived from _CONDITION_TABLE_COLUMNS so the placeholder header and the typed
# CSVW schema can never drift apart.
_CONDITION_TABLE_HEADER = ",".join(c["titles"] for c in _CONDITION_TABLE_COLUMNS) + "\n"

# The condition-table columns whose cells name in-crate entities, keyed to the
# entity type they resolve against. The single map consumed by the schema's
# valueUrl branch (_build_condition_table_schema), the pipeline's payload
# validation, and the eval scorer (#474) — one definition, no drift.
CONDITION_TABLE_REFERENCE_COLUMNS: dict[str, str] = {
    "compound": "MolecularEntity",
    "cell_line": "CellLineSample",
}

# Typed CSVW columns for the per-well raw-measurements table emitted as the
# EndpointReadout's result (Issue #180, Lane D). Typed exactly the way the
# condition table is (datatype + propertyUrl); the cell-content (measurement
# rows) is never fabricated — D5 — so the materialised CSV is header-only.
_RAW_MEASUREMENTS_COLUMNS: tuple[dict[str, str], ...] = (
    {
        "titles": "well_id",
        "datatype": "string",
        "propertyUrl": "http://purl.org/dc/terms/identifier",
    },
    {
        "titles": "measured_value",
        "datatype": "double",
        "propertyUrl": iri("IAO:0000109"),
    },
    {
        "titles": "measured_unit",
        "datatype": "string",
        "propertyUrl": iri("IAO:0000039"),
    },
)

_RAW_MEASUREMENTS_HEADER = ",".join(c["titles"] for c in _RAW_MEASUREMENTS_COLUMNS) + "\n"


# --- provisional placeholder tables (#438) ---------------------------------
# `draft_process_chain` synthesizes a result File for an EndpointReadout /
# DataAnalysis that has no explicit output, because a missing result fires a tox
# REQUIRED Violation. Those entities used to be metadata only: the exported crate
# listed a file in `hasPart` that no byte of the payload contained, and ro-crate-py
# warned "No source for …" on every export.
#
# They are now materialised the same way the condition and raw-measurements
# tables already are — a header-only CSV with a typed CSVW schema — so the
# reference resolves and the column contract tells a human exactly what to fill
# in. No measurement row is ever fabricated (D5): the file ships EMPTY below its
# header, and the node says so in `description` so a consumer cannot mistake a
# template for data.
_ANALYSIS_RESULT_COLUMNS: tuple[dict[str, str], ...] = (
    {
        "titles": "group",
        "datatype": "string",
        "propertyUrl": iri("NCIT:C43359"),
    },
    {
        "titles": "endpoint",
        "datatype": "string",
        "propertyUrl": iri("IAO:0000109"),
    },
    {
        "titles": "value",
        "datatype": "double",
        "propertyUrl": iri("IAO:0000109"),
    },
    {
        "titles": "unit",
        "datatype": "string",
        "propertyUrl": iri("IAO:0000039"),
    },
)

_PROVISIONAL_TABLES: dict[str, tuple[str, tuple[dict[str, str], ...]]] = {
    "measurements": ("Provisional measurements", _RAW_MEASUREMENTS_COLUMNS),
    "analysis": ("Provisional analysis results", _ANALYSIS_RESULT_COLUMNS),
}

_PROVISIONAL_NOTE = (
    "Provisional template generated to keep the derivation chain complete: the "
    "column headers below are a suggested minimal shape and the table contains NO "
    "rows. Replace it with the real output of this step, or fill in the rows."
)


# The zero-row condition table (#473). `_synth_condition_table` ships a
# header-only CSV typed csvw:Table with the FULL ten-column schema — datatype and
# propertyUrl on every column, valueUrl on cell_line/compound. Over zero rows
# every one of those claims is vacuously true, so a crate-only "is it CSVW-typed"
# check passed tautologically on a deposit whose population had failed. The only
# tell was the AUTOGENERATED name prefix, which is prose, not machine-readable.
#
# Same remedy as the provisional tables above: say it in `description`, so the
# difference between "conforms because it is populated" and "conforms because it
# is empty" is legible to a consumer rather than inferred from a name.
_EMPTY_CONDITION_TABLE_NOTE = (
    "This condition table contains NO rows: the column headers and CSVW schema "
    "below describe the intended per-well design, but no experimental condition "
    "was captured. Any schema-level conformance claim over these columns is "
    "vacuous until rows are populated."
)


def _materialize_provisional_table(fe: Entity, output_dir: Path | None, rel: str) -> str | None:
    """Write a provisional placeholder's header-only CSV and return its path.

    Returns ``None`` when there is nothing to write (not a provisional entity, or
    the in-memory validate path where no payload is materialised), leaving the
    caller's existing source resolution untouched.
    """
    if not fe.fields.get("provisional") or output_dir is None:
        return None
    kind = str(fe.fields.get("table_kind") or "measurements")
    spec = _PROVISIONAL_TABLES.get(kind)
    if spec is None:
        return None
    _name, columns = spec
    dest = output_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_text(",".join(c["titles"] for c in columns) + "\n", encoding="utf-8")
    return str(dest)


def _attach_provisional_schema(crate: ROCrate, node: Any, fe: Entity) -> None:
    """Give a materialised provisional table the same typed CSVW schema the
    condition / raw-measurements tables carry, so its columns are machine-readable
    rather than a bare header line."""
    kind = str(fe.fields.get("table_kind") or "measurements")
    spec = _PROVISIONAL_TABLES.get(kind)
    if spec is None:
        return
    name, columns = spec
    slug = _slug(fe.entity_id)
    schema = _build_csvw_schema(
        crate,
        schema_id=f"#{slug}_schema",
        schema_name=f"{name} schema",
        id_prefix=f"#{slug}",
        columns=columns,
    )
    node["tableSchema"] = {"@id": schema.id}
    node.append_to("conformsTo", schema)


# --- generator provenance -------------------------------------------------
# Every export records what produced the crate: the application and version, the
# model that drove it, and what the run cost in tokens, money and wall-clock.
# LLM-assisted metadata cannot be judged without knowing what assisted it, and
# the cost/time figures are the ones nobody can reconstruct once the session is
# gone — so they travel with the crate rather than living only in a log.
#
# Modelled the RO-Crate way: a `SoftwareApplication` for the tool, a second one
# for the model, and a `CreateAction` whose `instrument` is the tool, whose
# `result` is the Root Data Entity, and whose run metrics hang off it as
# `PropertyValue`s. The root `mentions` the action so it is reachable.
_GENERATOR_METRICS: tuple[tuple[str, str, str | None], ...] = (
    ("input_tokens", "Input tokens", None),
    ("output_tokens", "Output tokens", None),
    ("llm_calls", "LLM calls", None),
    ("duration_seconds", "Run duration", "s"),
    # Wall clock counts the user thinking; this is the machine's own effort.
    ("model_seconds", "Model time", "s"),
    ("cost_usd", "Estimated cost", "USD"),
)


def _wire_root_attribution(state: CrateState, crate: ROCrate, idx: dict[str, Any]) -> None:
    """Put crate-level attribution on the Root Data Entity.

    ``author`` on the root is populated from every drafted ``Person``, which
    answers "who is named in this crate" — not "who is responsible for this
    dataset". ``publisher`` / ``creator`` / ``contactPoint`` answer the second
    question, and a crate without them credits nobody a registry can resolve.

    Each value is an entity id or a resolvable IRI (ORCID / ROR), wired through
    the shared resolver so a bare identifier still emits a proper reference.
    """
    m = state.metadata
    for prop, value in (
        ("publisher", m.publisher),
        ("creator", m.creator),
        ("contactPoint", m.contact),
    ):
        if value:
            _wire_reference(crate.root_dataset, prop, value, idx)


# Per-vendor documentation-URL patterns. Keyed by LiteLLM's ``litellm_provider``
# — the vendor that actually made the model, which is NOT the configured
# provider (that records the API family, and a DeepSeek model served over an
# OpenAI-compatible endpoint reports "openai"). Only vendors whose per-model
# page follows a stable, verified path belong here; a guessed pattern would mint
# a confidently wrong identifier, which is worse than none (D5).
_MODEL_DOCS_URL_PATTERNS: dict[str, str] = {
    "openai": "https://developers.openai.com/api/docs/models/{model}",
    # Azure AI Foundry serves many vendors' models from one catalogue, and the
    # catalogue page is the right identity for a model accessed that way — it is
    # the deployment the crate was actually produced with.
    "azure_ai": "https://ai.azure.com/catalog/models/{model}",
    "azure": "https://ai.azure.com/catalog/models/{model}",
}


def _model_vendor(model: str) -> str | None:
    """Who served *model*, preferring what the user configured.

    Order matters:

    1. The **configured** ``model_provider`` — chosen explicitly during setup
       (``azure_ai``, ``deepseek``, …). It is a declaration, not a guess, and it
       describes where this crate's model actually ran: a gpt model reached
       through Azure AI belongs to the Azure catalogue, not to openai.com.
    2. LiteLLM's ``litellm_provider`` for the exact model — the vendor that made
       it, for when nothing was configured.

    ``None`` when neither answers, so the caller falls back rather than mislabel.
    """
    try:
        from builder.config import get_model_provider

        configured = get_model_provider()
        if configured:
            return str(configured).strip().lower()
    except Exception:  # noqa: BLE001 — config must never break an export
        logger.debug("configured model provider unavailable", exc_info=True)
    try:
        from builder.pricing import get_model_vendor

        return get_model_vendor(model)
    except Exception:  # noqa: BLE001 — identity lookup is best-effort
        logger.debug("model vendor lookup failed for %r", model, exc_info=True)
        return None


def _model_docs_url(model: str) -> str | None:
    """The authoritative documentation URL for *model*, or ``None`` if unknown.

    BASE requires a ``url`` on every ``SoftwareApplication``, so a model can
    only be modelled as one when a real page exists to point at.

    The vendor comes from the model table rather than from the configured
    provider or a name prefix, so a DeepSeek model served over an
    OpenAI-compatible endpoint is not handed an openai.com URL. A model with no
    known pattern returns ``None`` and is recorded as a plain PropertyValue
    instead — inventing a URL to satisfy a validator is the fabrication D5
    forbids.
    """
    name = (model or "").strip()
    if not name:
        return None
    vendor = _model_vendor(name)
    if vendor is None and name.lower().startswith("gpt-"):
        vendor = "openai"  # offline / unlisted model, but the family is explicit
    pattern = _MODEL_DOCS_URL_PATTERNS.get(vendor or "")
    return pattern.format(model=name) if pattern else None


def _add_generator_provenance(state: CrateState, crate: ROCrate) -> None:
    """Record the generating application, model and run cost on the crate."""
    gen = state.generator
    if not gen or not gen.name:
        return

    app = crate.add(
        ContextEntity(
            crate,
            gen.url or f"#{_slug(gen.name)}",
            properties={
                "@type": "SoftwareApplication",
                "name": gen.name,
                **({"version": gen.version} if gen.version else {}),
                **({"url": gen.url} if gen.url else {}),
            },
        )
    )

    action_props: dict[str, Any] = {
        "@type": "CreateAction",
        "name": f"{gen.name} build",
    }
    if gen.started_at:
        action_props["startTime"] = gen.started_at
    if gen.ended_at:
        action_props["endTime"] = gen.ended_at
    action = crate.add(ContextEntity(crate, f"#{_slug(gen.name)}_run", properties=action_props))
    action.append_to("instrument", app)
    action["result"] = {"@id": "./"}

    # The model is named as a PropertyValue of the run, NOT as a
    # SoftwareApplication instrument. Naming it is the point of this record — a
    # reader cannot judge LLM-assisted metadata without knowing what assisted it
    # — but BASE requires every SoftwareApplication to carry `url` AND `version`,
    # and a hosted model has neither authoritatively: there is no resolvable URL
    # for "gpt-5.6-luna", and the tool's own URL would name the wrong thing.
    # Typing it as software therefore blocked BASE behind a rule that could only
    # be satisfied by inventing an identifier (D5). A PropertyValue states
    # exactly what is known, which is the model's name and who served it.
    model_url = _model_docs_url(gen.model or "")
    if gen.model and model_url:
        # A real docs page exists, so the model can be a proper instrument of the
        # run: linked, resolvable, and satisfying BASE truthfully. ``version`` is
        # the exact model string the API reported — the most precise version
        # information available (a dated snapshot like gpt-4o-2024-08-06 carries
        # its own).
        model_props: dict[str, Any] = {
            "@type": "SoftwareApplication",
            "name": gen.model,
            "url": model_url,
            "version": gen.model,
        }
        # Name the vendor the URL points at, not the API family: recording
        # "openai" beside an ai.azure.com link describes two different things
        # and neither of them is where this crate's model ran.
        vendor = _model_vendor(gen.model) or gen.provider
        if vendor:
            model_props["provider"] = vendor
        action.append_to(
            "instrument",
            crate.add(ContextEntity(crate, f"#model_{_slug(gen.model)}", properties=model_props)),
        )

    for label, value in (
        # Named as a plain fact when it could not be an entity (no known docs
        # page), so the record still says which model produced the crate.
        ("Model", gen.model if not model_url else None),
        ("Model provider", gen.provider),
        ("Drafter model", gen.drafter_model),
    ):
        if not value:
            continue
        action.append_to(
            "additionalProperty",
            crate.add(
                ContextEntity(
                    crate,
                    param_id(label, str(value)),
                    properties={"@type": "PropertyValue", "name": label, "value": str(value)},
                )
            ),
        )

    for key, label, unit in _GENERATOR_METRICS:
        value = getattr(gen, key, None)
        if not value:
            continue
        pv_props: dict[str, Any] = {
            "@type": "PropertyValue",
            "name": label,
            "value": str(value),
        }
        if unit:
            pv_props["unitText"] = unit
        action.append_to(
            "additionalProperty",
            crate.add(ContextEntity(crate, param_id(label, str(value)), properties=pv_props)),
        )
    for key, value in (gen.settings or {}).items():
        action.append_to(
            "additionalProperty",
            crate.add(
                ContextEntity(
                    crate,
                    param_id(key, str(value)),
                    properties={
                        "@type": "PropertyValue",
                        "name": key,
                        "value": str(value),
                    },
                )
            ),
        )
    crate.root_dataset.append_to("mentions", action)


def _condition_table_rel(exp_pid: str) -> str:
    """Crate-relative path of an Exposure's condition-table CSV.

    Shared by the build (``_synth_condition_table``) and the row-population tool
    (``populate_condition_table``) so both target the exact same file.
    """
    return f"data/{_slug(exp_pid)}_condition_table.csv"


def _raw_measurements_rel(er_pid: str) -> str:
    """Crate-relative path of an EndpointReadout's raw-measurements CSV."""
    return f"data/{_slug(er_pid)}_raw_measurements.csv"


def _node_id(node: Any) -> str | None:
    """The @id of an ro-crate node (None if it has none)."""
    return getattr(node, "id", None)


def _build_csvw_schema(
    crate: ROCrate,
    *,
    schema_id: str,
    schema_name: str,
    id_prefix: str,
    columns: tuple[dict[str, str], ...],
    value_urls: dict[str, str | None] | None = None,
) -> ContextEntity:
    """Build a ``csvw:Schema`` entity from a tuple of typed column descriptors.

    Each column is emitted as a ``csvw:Column`` graph node (ro-crate-py requires
    nested objects to be referenceable entities, not inline dicts) carrying its
    ``datatype`` and ``propertyUrl``. Columns named in ``value_urls`` additionally
    get a ``valueUrl`` resolving to an in-crate entity id (emitted as an ``{@id}``
    reference). Shared by the condition table and the raw-measurements table so
    both are typed the same way (Issue #180, Lane D).
    """
    value_urls = value_urls or {}
    schema = crate.add(
        ContextEntity(
            crate,
            schema_id,
            properties={
                "@type": ["csvw:Schema", "CreativeWork"],
                "name": schema_name,
            },
        )
    )
    for col in columns:
        title = col["titles"]
        # `titles` is the CSVW property; `name` is the schema.org one. These nodes
        # also carry schema:DefinedTerm, which makes them Contextual Entities, and
        # RO-Crate requires those to have a `name` — carrying only `titles` earns
        # findings per column. The column title IS the human-readable name,
        # so this states nothing new, it states it under the term the base profile
        # reads.
        props: dict[str, Any] = {"@type": "csvw:Column", "name": title, **col}
        # propertyUrl stays an {"@id"} reference. Emitting it as a bare URI
        # string reads as the faithful CSVW form — propertyUrl IS typed as a
        # URI — and it is tempting to argue the term is only cited, not
        # described. But some of these terms ARE described here: a
        # CellLineSample materialises NCIT_C16403 as a `cell line` DefinedTerm,
        # and the base profile then flags "references NCIT_C16403 as a string"
        # and fails the whole pass. Making cited vocabulary a string was right
        # for propertyID, whose values (ORCID, PubChem, DTXSID scheme IRIs) the
        # crate never describes; it does not carry over here, and a run of
        # tests/test_pipeline_e2e.py is what says so.
        if col.get("propertyUrl"):
            props["propertyUrl"] = {"@id": col["propertyUrl"]}
        if value_urls.get(title):
            # Same rule for valueUrl: emit the resolved Sample / MolecularEntity
            # link as an {@id} reference, never a bare string @id.
            props["valueUrl"] = {"@id": value_urls[title]}
        column = crate.add(ContextEntity(crate, f"{id_prefix}_col_{title}", properties=props))
        schema.append_to("columns", column)
    return schema


def _build_condition_table_schema(
    crate: ROCrate,
    exp_slug: str,
    cells: list[Any],
    chems: list[Any],
    *,
    multivalued: set[str] | None = None,
) -> ContextEntity:
    """The csvw:Schema entity describing the condition table's typed columns.

    The cell-line and compound columns resolve their ``valueUrl`` to the in-crate
    Sample / MolecularEntity id, so a row's value maps to its entity (#94, #180).

    That is a claim about the **whole column** — ``cells[0]`` / ``chems[0]`` stand
    for every row. It is vacuous while the table is header-only, but once rows
    exist a multi-compound or multi-cell-line plate makes it false. *multivalued*
    names the columns the populated CSV shows carrying more than one distinct value
    (:func:`~builder.tools.data_content.condition_table_multivalued_columns`); each
    one drops its ``valueUrl`` rather than assert an unverified mapping (D5, #408).
    The guard is per-column: a single-valued ``cell_line`` keeps its claim even when
    ``compound`` loses one.
    """
    dropped = multivalued or set()
    value_urls: dict[str, str | None] = {
        "cell_line": None if "cell_line" in dropped else (_node_id(cells[0]) if cells else None),
        "compound": None if "compound" in dropped else (_node_id(chems[0]) if chems else None),
    }
    return _build_csvw_schema(
        crate,
        schema_id=f"#{exp_slug}_condition_table_schema",
        schema_name="Condition table schema",
        id_prefix=f"#{exp_slug}",
        columns=_CONDITION_TABLE_COLUMNS,
        value_urls=value_urls,
    )


def _synth_condition_table(
    crate: ROCrate,
    output_dir: Path | None,
    exp_pid: str,
    cells: list[Any],
    chems: list[Any],
    *,
    materialize_payload: bool = True,
) -> File:
    """The Exposure's result: the CSVW condition table (the per-well design table).

    Modelled as a ``File`` (the CSV) that is also a ``csvw:Table`` — a bare
    csvw:Table is rejected by the base ISA shape, which requires a process result
    to be a File/Sample/BioSample. A header-only placeholder CSV is materialised
    so the File is valid in-payload, and the table is described by a typed CSVW
    schema (``tableSchema`` + ``conformsTo`` → a ``csvw:Schema`` whose columns
    carry datatype/propertyUrl, with the cell-line/compound columns resolving to
    their entity ids via ``valueUrl``; #94). Per-row CSV population (intake of the
    actual well values) remains future work. The table also links (schema:about)
    the cell line(s) and compound(s) it concerns, so the compound is connected to
    the Exposure THROUGH its result (a MolecularEntity cannot be a process object
    under the ISA shape).
    """
    rel = _condition_table_rel(exp_pid)
    # Only touch disk when materialising payload for an on-disk export. The
    # in-memory validate path (#87) skips the write; the File node below still
    # carries dest_path=rel so the metadata graph is complete for validation.
    source: str | None = None
    multivalued: set[str] = set()
    row_count: int | None = None
    if materialize_payload and output_dir is not None:
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(_CONDITION_TABLE_HEADER, encoding="utf-8")
        # Point source at the file we just wrote so ro-crate-py records it as
        # payload (its _copy_file no-ops when source and dest are the same file)
        # instead of warning "No source for …" (#128).
        source = str(dest)
        # A pre-populated table (#408 (b)) may contradict the column-wide valueUrl
        # the schema is about to assert; read it back and drop the claims it breaks.
        # Local import: data_content already imports from this module.
        from builder.tools.data_content import (
            condition_table_multivalued_columns,
            condition_table_row_count,
        )

        multivalued = condition_table_multivalued_columns(str(dest))
        row_count = condition_table_row_count(str(dest))
    table_props: dict[str, Any] = {
        "@type": ["File", "csvw:Table"],
        "name": _autogenerated_name("Condition table"),
        # We write this CSV ourselves, so its media type is known outright rather
        # than guessed from the extension.
        "encodingFormat": "text/csv",
    }
    # What this table IS, which we know because we generate it. States the
    # structure only — never how many rows it holds, which is the row_count
    # branch's business.
    table_props["description"] = (
        "Per-well experimental conditions for this exposure: one row per well, "
        "typed by the linked CSVW schema."
    )
    # Only a DEFINITE zero earns the note. `row_count` is None on the in-memory
    # validate path, where there is no CSV to consult — and silence there is
    # correct, because "contains no rows" is a positive claim and we have not
    # looked. Once rows land the note is simply not emitted, so a populated
    # table never carries a stale emptiness claim.
    if row_count == 0:
        table_props["description"] = _EMPTY_CONDITION_TABLE_NOTE
    # Same reasoning as `_add_csvw_table_file`: size the CONTENT so the in-memory
    # validate path states it too, otherwise the agent is told the file has no
    # contentSize for the whole run and only the written crate disagrees.
    table_props["contentSize"] = str(
        _file_size(output_dir / rel)
        if (source and output_dir is not None)
        else len(_CONDITION_TABLE_HEADER.encode("utf-8"))
    )
    table = crate.add(File(crate, source, dest_path=rel, properties=table_props))
    for ent in list(cells) + list(chems):
        table.append_to("about", ent)
    # Typed CSVW: a linked schema entity carries the per-column datatype +
    # propertyUrl (and valueUrl resolving the cell-line/compound columns to their
    # entity ids). The table points to it via tableSchema (canonical CSVW) and
    # conformsTo (RO-Crate conformance — not a bare inline tableSchema dict).
    # Issue #94.
    schema = _build_condition_table_schema(
        crate, _slug(exp_pid), cells, chems, multivalued=multivalued
    )
    table["tableSchema"] = {"@id": schema.id}
    table.append_to("conformsTo", schema)
    return table


def _file_size(path: Path) -> int | None:
    """Byte count for *path*, or None when it cannot be read.

    A `contentSize` is a positive claim about a file, so a stat that fails
    produces no property rather than a zero — the base profile asking for the
    size is a smaller problem than the crate asserting the wrong one.
    """
    try:
        return path.stat().st_size
    except OSError:
        logger.debug("Could not size %s for contentSize", path, exc_info=True)
        return None


def _add_csvw_table_file(
    crate: ROCrate,
    output_dir: Path | None,
    *,
    rel: str,
    name: str,
    header: str,
    materialize_payload: bool,
) -> File:
    """Add a ``File`` that is also a ``csvw:Table``, materialising a header-only CSV.

    Shared by the condition table and the raw-measurements table: a bare
    ``csvw:Table`` is rejected by the ISA shape, so the node is a ``File`` (a valid
    process result) carrying only its header row in-payload — measurement / well
    rows are never fabricated (D5). The in-memory validate path skips the write.
    """
    source: str | None = None
    size: int | None = None
    if materialize_payload and output_dir is not None:
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(header, encoding="utf-8")
        # Point source at the file we just wrote so ro-crate-py records it as
        # payload (its _copy_file no-ops when source and dest are the same file).
        source = str(dest)
        size = _file_size(dest)
    props: dict[str, Any] = {
        "@type": ["File", "csvw:Table"],
        "name": _autogenerated_name(name),
        "encodingFormat": "text/csv",
        # What this table IS, which we know because we generate it — the base
        # profile asks every Data Entity for a description and these had none.
        "description": (
            f"{name}, generated by the build: a CSV whose columns are typed by "
            "the linked CSVW schema."
        ),
    }
    # Size the CONTENT, not just the file. A stat only works on the export path,
    # and the agent validates in memory — so a disk-only size left every one of
    # these reporting "SHOULD have a contentSize" for the whole run, and only
    # stopped once the crate was written, which is far too late to act on. We
    # compose the payload here, so its byte count is known either way: the file
    # on disk wins when there is one (it may already hold real rows), otherwise
    # the header we are about to write is the file.
    if size is None:
        size = len(header.encode("utf-8"))
    props["contentSize"] = str(size)
    return crate.add(File(crate, source, dest_path=rel, properties=props))


def _synth_raw_measurements(
    crate: ROCrate,
    output_dir: Path | None,
    er_pid: str,
    *,
    materialize_payload: bool = True,
) -> File:
    """An EndpointReadout's typed per-well raw-measurements ``csvw:Table``.

    Emitted *alongside* the process's explicit result file(s) — never as a
    substitute — so the "EndpointReadout MUST have a result" repair contract
    (resultless readouts still fire the issue) is untouched. Typed the same way as
    the condition table: a ``File``/``csvw:Table`` linked to a ``csvw:Schema``
    whose 3 columns carry datatype + propertyUrl. The CSV is header-only; no
    measurement rows are fabricated (D5). Issue #180, Lane D.
    """
    rel = _raw_measurements_rel(er_pid)
    table = _add_csvw_table_file(
        crate,
        output_dir,
        rel=rel,
        name="Raw measurements",
        header=_RAW_MEASUREMENTS_HEADER,
        materialize_payload=materialize_payload,
    )
    schema = _build_csvw_schema(
        crate,
        schema_id=f"#{_slug(er_pid)}_raw_measurements_schema",
        schema_name="Raw measurements schema",
        id_prefix=f"#{_slug(er_pid)}_raw",
        columns=_RAW_MEASUREMENTS_COLUMNS,
    )
    table["tableSchema"] = {"@id": schema.id}
    table.append_to("conformsTo", schema)
    return table


def _add_processes(
    state: CrateState,
    crate: ROCrate,
    idx: dict[str, Any],
    output_dir: Path | None,
    *,
    materialize_payload: bool = True,
) -> None:
    proto_cache: dict[str, Any] = {}
    for proc in state.list_entities("LabProcess"):
        f = proc.fields
        ptype = f.get("process_type") or f.get("additionalType") or ""
        pid = _mint_id(proc)
        name = f.get("name") or ptype or "Process"
        # `protocol` is the word an agent reaches for, and it wrote the real
        # LabProtocol id under it while this read only `labprotocol` — so the
        # link was in state, resolved to nothing, and every process fell through
        # to a synthesised placeholder. The crate then carried TWO protocols: a
        # stub each process pointed at, and the actual SOP nobody referenced.
        # Same failure as `data_processing` / `computational_tool` below; same
        # answer, accept both spellings.
        protocol = _resolve_one(idx, _first_of(f, _PROTOCOL_ALIASES)) or _synth_protocol(
            crate, f.get("assay_id"), proto_cache
        )
        node = _build_process(
            crate,
            ptype,
            pid,
            name,
            f,
            protocol,
            idx,
            output_dir,
            materialize_payload=materialize_payload,
        )
        _idx_add(idx, proc, node)
        # Wire any additionalProperty references onto the process (gold
        # #report_analysis -> [#pv_repro_score]). Mirrors the root/assay reference
        # wiring: only PropertyValues already present in state (or bare IRIs) are
        # referenced; an unresolvable, non-IRI value is dropped, never fabricated
        # (D5 — the score itself is computed elsewhere, not here).
        _wire_references(node, "additionalProperty", f.get("additionalProperty"), idx)
        assay = _resolve_one(idx, f.get("assay_id"))
        if assay is not None:
            assay.append_to("about", node)
            # Result Files are the data of this assay → attach them to the Assay's
            # hasPart (ISA), de-duped, while KEEPING the root's reference (#532).
            # "Reachable transitively via File → Assay → Study → ./" was the
            # premise for dropping it, and it is false: the file tree runs
            # through directory Datasets, and an Assay is a contextual node.
            for file_node in _result_file_nodes(node):
                _append_unique(assay, "hasPart", file_node)


def _build_process(
    crate: ROCrate,
    ptype: str,
    pid: str,
    name: str,
    f: dict[str, Any],
    protocol: Any,
    idx: dict[str, Any],
    output_dir: Path | None,
    *,
    materialize_payload: bool = True,
) -> Any:
    # input/object/samples are interchangeable aliases for the consumed inputs,
    # result/output for the produced outputs (see PROVENANCE_RELATIONS and the
    # link tool). Read both so a process round-tripped through the crate (which
    # serializes the `output`/`input` aliases) — or wired by the agent via link —
    # keeps its I/O instead of silently dropping it.
    samples = _resolve_many(idx, f.get("samples"))
    obj = _resolve_many(idx, f.get("object")) or _resolve_many(idx, f.get("input"))
    result = _resolve_many(idx, f.get("result")) or _resolve_many(idx, f.get("output"))

    if ptype == "CellCulture":
        # CellCulture MUST take a cell-line Sample as object; synthesize a
        # placeholder input Sample if none was referenced/resolved.
        cell_line = (
            _resolve_one(idx, f.get("cell_line"))
            or (samples[0] if samples else None)
            or (obj[0] if obj else None)
            or _synth_sample(crate, pid + "_input", f"Input ({name})")
        )
        if result:
            out = result[0]
        else:
            label = f"Cultured ({name})"
            out = _synth_sample(crate, pid + "_cultured", label, cell_line)
        return LabProcessCellCulture(
            crate,
            identifier=pid,
            name=name,
            cell_line=cell_line,
            culture_medium=f.get("culture_medium", "Standard medium"),
            result=out,
            labprotocol=protocol,
        )

    if ptype == "Exposure":
        # The Exposure takes the cultured cell Sample(s) as object and emits the
        # CSVW condition table as its result. ISA forbids a MolecularEntity as a
        # process object (objects MUST be File/Sample/BioSample — bundled
        # isa-ro-crate shape), so the compound is NOT in `object`; it is connected
        # THROUGH the condition table (table --about--> MolecularEntity) and, at a
        # glance, on the Study via schema:mentions. Per-well CSVW population
        # (tableSchema columns + CSV intake) is planned — see the wizard's
        # intake/condition_table.py.
        cells = samples or obj
        chems = _resolve_many(idx, f.get("chemicals"))
        # APPENDED, never substituted — the same contract the EndpointReadout
        # branch below states for its raw_measurements table (#531). The table is
        # the compound's only route to the process (a MolecularEntity cannot be
        # the object), so letting a drafter-declared `result` stand in for it
        # severed that route silently: the crate kept its compounds and lost
        # every link to them, and the declared file — never synthesized, so
        # never materialised — left the crate describing a file it lacks.
        out = list(result) + [
            _synth_condition_table(
                crate,
                output_dir,
                pid,
                cells,
                chems,
                materialize_payload=materialize_payload,
            )
        ]
        return LabProcessExposure(
            crate,
            identifier=pid,
            name=name,
            duration=f.get("duration"),
            cell_seeding_density=f.get("cell_seeding_density"),
            microplate=f.get("microplate"),
            samples=cells,
            labprotocol=protocol,
            result=out,
            units=f.get("units"),
        )

    if ptype == "EndpointReadout":
        # When the readout already emits result file(s), additionally synthesize a
        # typed raw-measurements csvw:Table alongside them (mirroring the gold
        # crate's EndpointReadout → raw_measurements.csv; Issue #180, Lane D). It
        # is appended, never substituted, so a resultless readout still fires the
        # "MUST have a result" issue for the deterministic repair loop (#179).
        er_result = list(result)
        if er_result:
            er_result.append(
                _synth_raw_measurements(
                    crate, output_dir, pid, materialize_payload=materialize_payload
                )
            )
        return LabProcessEndpointReadout(
            crate,
            identifier=pid,
            name=name,
            samples=(samples or obj or None),
            labprotocol=protocol,
            result=er_result,
            detection_instrument=f.get("detection_instrument"),
            instrument_manufacturer=f.get("instrument_manufacturer"),
            measured_entity=f.get("measured_entity"),
            technical_replicate=f.get("technical_replicate"),
            endpoint=f.get("endpoint"),
            assay_kit=f.get("assay_kit"),
            substrate=f.get("substrate"),
            units=f.get("units"),
        )

    if ptype == "DataAnalysis":
        # Accept the parameter names the agent is TOLD to use. The
        # draft_process_chain tool spec advertises `computational_tool` and
        # `data_calculation_and_statistics` — the labels the tox profile itself
        # names in its violation message — while this branch only ever read
        # `software` and `data_processing`. Values written under the advertised
        # names landed in state and were read by nobody, so the process built
        # with an empty parameter list and failed "DataAnalysis process MUST have
        # at least one schema:additionalProperty" no matter how correctly the
        # agent filled it in. One session spent four attempts and fourteen
        # minutes on that. Both spellings now feed the same PropertyValue.
        return LabProcessDataAnalysis(
            crate,
            identifier=pid,
            name=name,
            object=(obj or samples),
            result=result,
            labprotocol=protocol,
            data_processing=(
                f.get("data_processing") or f.get("data_calculation_and_statistics") or ""
            ),
            software=(f.get("software") or f.get("computational_tool") or ""),
            acceptance_criteria=f.get("acceptance_criteria"),
            evaluation_criteria=f.get("evaluation_criteria"),
            units=f.get("units"),
        )

    # Generic LabProcess (no domain discriminator).
    return LabProcess(
        crate,
        identifier=pid,
        name=name,
        labprotocol=protocol,
        object=(obj or samples or None) or None,
        result=(result or None) or None,
    )


# ---------------------------------------------------------------------------
# Ontology annotations (AOP / Key Event / organism / …) via schema:mentions
# ---------------------------------------------------------------------------


def _wire_reference(
    node: Any, prop: str, value: Any, idx: dict[str, Any], *, keep_literal: bool = False
) -> None:
    """Set ``node[prop]`` to a single entity reference when one can be resolved.

    The value may reference an in-crate entity (resolved via the index), an inline
    ``{"@id": …}`` object, or a bare resolvable IRI / ``#``-fragment — each emitted
    as an ``@id`` reference. A plain free-text value is kept verbatim only when
    ``keep_literal`` is True (e.g. a free-text affiliation is valid schema.org and
    dropping it would lose data); otherwise it is left unset rather than emitted as
    a string on a reference-only property. No-ops on None/empty.
    """
    if value in (None, ""):
        return
    ent = _resolve_one(idx, value)
    if ent is not None:
        node[prop] = ent
    elif isinstance(value, dict) and value.get("@id"):
        node[prop] = {"@id": value["@id"]}
    elif isinstance(value, str) and ("://" in value or value.startswith("#")):
        node[prop] = {"@id": value}
    elif keep_literal and isinstance(value, str):
        node[prop] = value


def _wire_mention(node: Any, prop: str, value: Any, idx: dict[str, Any]) -> None:
    """Append AOP/KeyEvent/organism annotations under an alias of schema:mentions.

    Each value may be a reference to an in-crate entity (resolved via the index),
    an inline ``{"@id": …}`` object, or a bare resolvable IRI (e.g. an AOP-Wiki id).
    """
    for item in value if isinstance(value, list) else [value]:
        if item is None:
            continue
        ent = _resolve_one(idx, item)
        if ent is not None:
            node.append_to(prop, ent)
        elif isinstance(item, dict) and item.get("@id"):
            node.append_to(prop, item)
        elif isinstance(item, str) and ("://" in item or item.startswith("#")):
            node.append_to(prop, {"@id": item})


def _node_for(idx: dict[str, Any], entity: Entity) -> Any:
    """Look up the graph node for ``entity`` by its type-qualified index key.

    ``_idx_add`` always registers the ``{type}:{entity_id}`` key but the bare
    ``entity_id`` key only when still free, so two entities of different types
    sharing an ``entity_id`` collide on the bare slot. Resolve via the typed key
    first (bare only as a defensive fallback) so annotations never land on the
    wrong node (Issue #93, same class as #57).
    """
    return idx.get(f"{entity.type}:{entity.entity_id}") or idx.get(entity.entity_id)


def _wire_mentions(state: CrateState, idx: dict[str, Any]) -> None:
    for st in state.list_entities("Study"):
        node = _node_for(idx, st)
        if node is None:
            continue
        for field, prop in _STUDY_MENTION_FIELDS.items():
            if field in st.fields:
                _wire_mention(node, prop, st.fields[field], idx)

    for asy in state.list_entities("Assay"):
        node = _node_for(idx, asy)
        if node is None:
            continue
        for field, prop in _ASSAY_MENTION_FIELDS.items():
            if field in asy.fields:
                _wire_mention(node, prop, asy.fields[field], idx)


# ---------------------------------------------------------------------------
# Reference-wiring for root funder/about + assay aliases (#180 Lane C)
#
# References the build previously dropped, resolved deterministically so they
# round-trip exactly as the gold crate emits them. NEVER fabricates an id (D5):
# every reference is resolved from a field already present in state, and an
# unresolvable, non-IRI value is left off rather than guessed.
# ---------------------------------------------------------------------------

# Assay reference aliases that expand to schema:hasPart (profiles/context.py).
# Resolved File/dataset refs are emitted under their PageTab key AND attached to
# the assay's structural hasPart (un-parented from the root) so containment is
# preserved and a loose data File is never dumped on the root.
_ASSAY_HASPART_ALIASES = ("dataFiles", "resources")


def _wire_references(node: Any, prop: str, value: Any, idx: dict[str, Any]) -> None:
    """Append resolved entity reference(s) under ``node[prop]`` (an array prop).

    Mirrors :func:`_wire_mention`: each item may reference an in-crate entity
    (resolved via the index), an inline ``{"@id": …}`` object, or a bare
    resolvable IRI / ``#``-fragment — each emitted as an ``@id`` reference,
    de-duped by id. A plain free-text value is dropped (never emitted as a string
    on a reference-only property). No-ops on None/empty.
    """
    if value in (None, ""):
        return
    for item in value if isinstance(value, list) else [value]:
        if item in (None, ""):
            continue
        ent = _resolve_one(idx, item)
        if ent is not None:
            _append_unique(node, prop, ent)
        elif isinstance(item, dict) and item.get("@id"):
            _append_unique_ref(node, prop, item["@id"])
        elif isinstance(item, str) and ("://" in item or item.startswith("#")):
            _append_unique_ref(node, prop, item)


def _append_unique_ref(node: Any, prop: str, ref_id: str) -> None:
    """append_to(node, prop, {"@id": ref_id}) but skip if ``ref_id`` already present."""
    if ref_id not in _child_ids(node, prop):
        node.append_to(prop, {"@id": ref_id})


# People/organisation references an ISA dataset can carry. Held here (not in
# _REF_FIELDS) because `affiliation` on a Person is wired separately with
# keep_literal=True — a free-text affiliation is valid schema.org and dropping it
# would lose data — while on a Dataset it is always meant to be an Organization.
_AGENT_REFERENCE_FIELDS: tuple[str, ...] = ("contributor", "affiliation", "publisher", "creator")


def _wire_dataset_aliases(state: CrateState, crate: ROCrate, idx: dict[str, Any]) -> None:
    """Resolve root funder/about + assay measurementMethod/dataFiles/resources.

    Runs after structural datasets and processes are added so every reference
    target (Organization, DataAnalysis LabProcess, DefinedTerm, File) is already
    in the index.

    * Root (the folded single Investigation) ``funder`` -> Organization ref(s)
      and ``about`` -> the LabProcess it reports on (mirrors the Assay
      ``about``->LabProcess wiring, for the Investigation/root).
    * Assay ``measurementMethod`` -> a single DefinedTerm reference.
    * Assay ``dataFiles`` / ``resources`` -> File references, also attached to the
      assay's ``hasPart`` while KEEPING the root's reference (#532) — both expand
      to schema:hasPart, and the root's copy is what keeps the file in the file
      tree, which is walked through directory Datasets rather than ISA nodes.
    """

    # Agent/organisation references on the ISA datasets. These were emitted
    # straight out of `_scalar_props`, i.e. as the raw STATE id — which happens
    # to be right for a Person (keyed by their ORCID, so state id == crate id)
    # and wrong for an Organization (state `org_utrecht_university`, crate
    # `https://ror.org/04pp8hn57`). The reference then pointed at nothing: an
    # undescribed node with no type and no name, tripping three checks each.
    # Resolving them through the index is what `funder` already does.
    for kind in ("Investigation", "Study", "Assay"):
        for entity in state.list_entities(kind):
            node = _node_for(idx, entity)
            if node is None:
                continue
            for field in _AGENT_REFERENCE_FIELDS:
                _wire_references(node, field, entity.fields.get(field), idx)

    for inv in state.list_entities("Investigation"):
        node = _node_for(idx, inv)
        if node is None:
            continue
        _wire_references(node, "funder", inv.fields.get("funder"), idx)
        _wire_references(node, "about", inv.fields.get("about"), idx)

    for asy in state.list_entities("Assay"):
        node = _node_for(idx, asy)
        if node is None:
            continue
        _wire_reference(node, "measurementMethod", asy.fields.get("measurementMethod"), idx)
        for alias in _ASSAY_HASPART_ALIASES:
            for child in _resolve_many(idx, asy.fields.get(alias)):
                if child is node:
                    continue
                # Emit under the PageTab alias key …
                _append_unique(node, alias, child)
                # … and nest under the assay's hasPart, keeping the root's
                # reference alongside it (#532). Both alias and hasPart expand to
                # schema:hasPart, so the RDF containment is a single edge; the
                # root's copy is what keeps the file inside the crate's file tree.
                _append_unique(node, "hasPart", child)
