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
from pathlib import Path, PurePosixPath
from typing import Any

from rocrate.model import ContextEntity, DataEntity, File, Person
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity
from profiles.models.isa import CharacteristicValue, LabProcess, Sample, param_id
from profiles.models.tox import (
    CellLineSample,
    LabProcessCellCulture,
    LabProcessDataAnalysis,
    LabProcessEndpointReadout,
    LabProcessExposure,
)

logger = logging.getLogger(__name__)

ROCRATE_SPEC = "https://w3id.org/ro/crate/1.2"
# The ISA layer the tox profile actually extends (profiles/shapes/tox/profile.ttl
# prof:isProfileOf) and that resolves — the w3id ISA permalink is not yet live.
PROFILE_ISA = "https://github.com/nfdi4plants/isa-ro-crate-profile"
PROFILE_ISATOX = "https://w3id.org/ro/crate/isa-tox/1.0"
CELL_LINE_TERM_ID = "http://purl.obolibrary.org/obo/NCIT_C16403"

# Fields that hold references to other entities (resolved via the index), not literals.
_REF_FIELDS = frozenset(
    {
        "samples",
        "labprotocol",
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
            "accession": "Cellosaurus accession, e.g. 'CVCL_0027'.",
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
            "term_code": "Ontology code, e.g. 'BAO:0002993' or 'GO:0006915'.",
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
        materialize_payload=materialize_payload,
        include_all_scanned=include_all_scanned,
    )
    _add_structural(state, crate, idx)
    _add_processes(state, crate, idx, output_dir, materialize_payload=materialize_payload)
    _wire_mentions(state, idx)
    _wire_dataset_aliases(state, crate, idx)


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


def _identifier_pv(
    crate: ROCrate, name: str, value: str, property_id_url: str | None = None
) -> ContextEntity:
    """Build (and add) a schema:PropertyValue identifier node with a stable id.

    The id is ``param_id(name, value)`` (the wizard scheme); ``propertyID`` is
    emitted as ``{"@id": property_id_url}`` when a url is given, else omitted.
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


def _scalar_props(entity: Entity, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Plain-value properties of an entity (references/discriminators removed)."""
    drop = _REF_FIELDS | _STRUCT_FIELDS | set(skip)
    return {k: v for k, v in entity.fields.items() if k not in drop and not k.startswith("@")}


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
            return value[len(prefix):]
    if value.lower().startswith("doi:"):
        return value[len("doi:"):]
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
        is_doi = (
            raw.startswith("10.")
            or "doi.org/" in raw
            or raw.lower().startswith("doi:")
        )
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
    # Base RO-Crate MUST: the Root Data Entity has a license. The ISA-Tox shape
    # endorses this exact placeholder when none is available.
    if not crate.root_dataset.get("license"):
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
    _Characteristic(("passage",), "passage", "https://bioregistry.io/EFO:0007061"),
    _Characteristic(
        ("growth",), "growth", "http://www.bioassayontology.org/bao#BAO_0002648"
    ),
    _Characteristic(
        ("organ",), "Organ", f"{PROFILE_ISATOX}/param/organ"
    ),
    _Characteristic(
        ("tissue",), "Tissue", f"{PROFILE_ISATOX}/param/tissue"
    ),
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
    *,
    materialize_payload: bool = True,
    include_all_scanned: bool = True,
) -> None:
    for org in state.list_entities("Organization"):
        _idx_add(
            idx,
            org,
            crate.add(
                ContextEntity(
                    crate,
                    _mint_id(org),
                    properties={"@type": "Organization", **_scalar_props(org)},
                )
            ),
        )

    for person in state.list_entities("Person"):
        # affiliation is a reference, not a literal: resolve it to the in-crate
        # Organization node (or keep a bare IRI), and never emit it as a string.
        node = crate.add(
            Person(crate, _mint_id(person), properties=_scalar_props(person, skip=("affiliation",)))
        )
        _idx_add(idx, person, node)
        # A looked-up ORCID round-trips as an ORCID PropertyValue identifier (#180).
        orcid = person.fields.get("orcid")
        if orcid not in (None, ""):
            bare = str(orcid).strip().rsplit("/", 1)[-1]
            node.append_to(
                "identifier", _identifier_pv(crate, "ORCID", bare, "https://orcid.org")
            )
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
                            "@type": aop_entity.type,
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
        source = (
            _file_source(fe, state.metadata.input_path) if materialize_payload else None
        )
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
        _idx_add(
            idx,
            fe,
            crate.add(
                File(
                    crate,
                    source,
                    dest_path=_file_dest(fe),
                    properties={"@type": file_type, **_scalar_props(fe)},
                )
            ),
        )

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


def _add_scanned_leaves(
    state: CrateState, crate: ROCrate, *, materialize_payload: bool
) -> None:
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
        str(x).rsplit("/", 1)[-1].rsplit("#", 1)[-1] in ("File", "MediaObject")
        for x in types
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


def _attach_explicit_parts(
    node: Any, entity: Entity, idx: dict[str, Any], root: Any
) -> None:
    """Move a Study/Assay entity's explicit ``hasPart`` File members under its node.

    ``attach_files`` (#177) records placement by appending File entity_ids to the
    dataset entity's ``hasPart`` field. Resolve those to their built File nodes,
    attach them under the dataset, and un-parent them from the root's auto-added
    ``hasPart`` — the same move D13 makes for a process's result Files.
    """
    for key in ("hasPart", "has_part"):
        for child in _resolve_many(idx, entity.fields.get(key)):
            if child is node:
                continue
            _remove_child(root, "hasPart", child.id)
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
        for key, value in _scalar_props(inv).items():
            if key == "identifier":
                continue
            if root.get(key) in (None, ""):
                root[key] = value
        if root.get("identifier") in (None, ""):
            root["identifier"] = inv.fields.get("identifier") or inv.entity_id
        _idx_add(idx, inv, root)
    else:
        for inv in investigations:
            props = {"@type": "Dataset", "additionalType": "Investigation", **_scalar_props(inv)}
            props["identifier"] = _isa_identifier(inv, None, "investigation")
            node = crate.add(DataEntity(crate, _mint_id(inv), properties=props))
            _idx_add(idx, inv, node)
            _append_unique(root, "hasPart", node)

    root_ident = root.get("identifier") or "./"

    for st in state.list_entities("Study"):
        props = {"@type": "Dataset", "additionalType": "Study", **_scalar_props(st)}
        props["identifier"] = _isa_identifier(st, root_ident, "study")
        node = crate.add(DataEntity(crate, _mint_id(st), properties=props))
        _idx_add(idx, st, node)
        _append_unique(root, "hasPart", node)  # Study MUST be hasPart of the root
        _attach_explicit_parts(node, st, idx, root)

    for asy in state.list_entities("Assay"):
        props = {"@type": "Dataset", "additionalType": "Assay", **_scalar_props(asy)}
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
    {"titles": "well_id", "datatype": "string",
     "propertyUrl": "http://purl.org/dc/terms/identifier"},
    {"titles": "assay", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/NCIT_C60819"},
    {"titles": "cell_line", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/NCIT_C16403"},
    {"titles": "compound", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/CHEBI_23367"},
    {"titles": "concentration_value", "datatype": "double",
     "propertyUrl": "http://purl.obolibrary.org/obo/PATO_0000033"},
    {"titles": "concentration_unit", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/IAO_0000039"},
    {"titles": "exposure_duration", "datatype": "string",
     "propertyUrl": "https://bioregistry.io/NCIT:C83280"},
    {"titles": "experiment", "datatype": "string",
     "propertyUrl": "https://bioregistry.io/EFO:0002091"},
    {"titles": "technical_replicate", "datatype": "string",
     "propertyUrl": "https://bioregistry.io/EFO:0002090"},
    {"titles": "control", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/NCIT_C28143"},
)

# Header line (column titles, in order) for the materialised condition-table CSV.
# Derived from _CONDITION_TABLE_COLUMNS so the placeholder header and the typed
# CSVW schema can never drift apart.
_CONDITION_TABLE_HEADER = ",".join(c["titles"] for c in _CONDITION_TABLE_COLUMNS) + "\n"

# Typed CSVW columns for the per-well raw-measurements table emitted as the
# EndpointReadout's result (Issue #180, Lane D). Typed exactly the way the
# condition table is (datatype + propertyUrl); the cell-content (measurement
# rows) is never fabricated — D5 — so the materialised CSV is header-only.
_RAW_MEASUREMENTS_COLUMNS: tuple[dict[str, str], ...] = (
    {"titles": "well_id", "datatype": "string",
     "propertyUrl": "http://purl.org/dc/terms/identifier"},
    {"titles": "measured_value", "datatype": "double",
     "propertyUrl": "http://purl.obolibrary.org/obo/IAO_0000109"},
    {"titles": "measured_unit", "datatype": "string",
     "propertyUrl": "http://purl.obolibrary.org/obo/IAO_0000039"},
)

_RAW_MEASUREMENTS_HEADER = ",".join(c["titles"] for c in _RAW_MEASUREMENTS_COLUMNS) + "\n"


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
        props: dict[str, Any] = {"@type": "csvw:Column", **col}
        # Emit propertyUrl as an {@id} reference rather than a bare string:
        # RO-Crate 1.2's base profile flags an IRI value used as a string when
        # that IRI is also a described entity (e.g. the cell-line NCIT_C16403,
        # which a CellLineSample materialises as a `cell line` DefinedTerm). The
        # CSVW range of propertyUrl is a URI, so an {@id} is the faithful form.
        if col.get("propertyUrl"):
            props["propertyUrl"] = {"@id": col["propertyUrl"]}
        if value_urls.get(title):
            # Same rule for valueUrl: emit the resolved Sample / MolecularEntity
            # link as an {@id} reference, never a bare string @id.
            props["valueUrl"] = {"@id": value_urls[title]}
        column = crate.add(
            ContextEntity(crate, f"{id_prefix}_col_{title}", properties=props)
        )
        schema.append_to("columns", column)
    return schema


def _build_condition_table_schema(
    crate: ROCrate, exp_slug: str, cells: list[Any], chems: list[Any]
) -> ContextEntity:
    """The csvw:Schema entity describing the condition table's typed columns.

    The cell-line and compound columns resolve their ``valueUrl`` to the in-crate
    Sample / MolecularEntity id, so a row's value maps to its entity (#94, #180).
    """
    value_urls: dict[str, str | None] = {
        "cell_line": _node_id(cells[0]) if cells else None,
        "compound": _node_id(chems[0]) if chems else None,
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
    if materialize_payload and output_dir is not None:
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(_CONDITION_TABLE_HEADER, encoding="utf-8")
        # Point source at the file we just wrote so ro-crate-py records it as
        # payload (its _copy_file no-ops when source and dest are the same file)
        # instead of warning "No source for …" (#128).
        source = str(dest)
    table = crate.add(
        File(
            crate,
            source,
            dest_path=rel,
            properties={"@type": ["File", "csvw:Table"], "name": "Condition table"},
        )
    )
    for ent in list(cells) + list(chems):
        table.append_to("about", ent)
    # Typed CSVW: a linked schema entity carries the per-column datatype +
    # propertyUrl (and valueUrl resolving the cell-line/compound columns to their
    # entity ids). The table points to it via tableSchema (canonical CSVW) and
    # conformsTo (RO-Crate conformance — not a bare inline tableSchema dict).
    # Issue #94.
    schema = _build_condition_table_schema(crate, _slug(exp_pid), cells, chems)
    table["tableSchema"] = {"@id": schema.id}
    table.append_to("conformsTo", schema)
    return table


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
    if materialize_payload and output_dir is not None:
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(header, encoding="utf-8")
        # Point source at the file we just wrote so ro-crate-py records it as
        # payload (its _copy_file no-ops when source and dest are the same file).
        source = str(dest)
    return crate.add(
        File(
            crate,
            source,
            dest_path=rel,
            properties={"@type": ["File", "csvw:Table"], "name": name},
        )
    )


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
        protocol = _resolve_one(idx, f.get("labprotocol")) or _synth_protocol(
            crate, f.get("assay_id"), proto_cache
        )
        node = _build_process(
            crate, ptype, pid, name, f, protocol, idx, output_dir,
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
            # hasPart (ISA), de-duped, and remove them from the root's hasPart
            # where crate.add auto-placed them. They stay reachable from the root
            # transitively (File → Assay → Study → ./).
            for file_node in _result_file_nodes(node):
                _append_unique(assay, "hasPart", file_node)
                _remove_child(crate.root_dataset, "hasPart", file_node.id)


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
        out = result or [
            _synth_condition_table(
                crate, output_dir, pid, cells, chems,
                materialize_payload=materialize_payload,
            )
        ]
        return LabProcessExposure(
            crate,
            identifier=pid,
            name=name,
            duration=f.get("duration", "unknown"),
            cell_seeding_density=f.get("cell_seeding_density", "NA"),
            microplate=f.get("microplate", "unknown"),
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
            detection_instrument=f.get("detection_instrument", "unknown"),
            instrument_manufacturer=f.get("instrument_manufacturer", "unknown"),
            measured_entity=f.get("measured_entity", "unknown"),
            technical_replicate=f.get("technical_replicate", "1"),
            endpoint=f.get("endpoint", "unknown"),
            assay_kit=f.get("assay_kit"),
            substrate=f.get("substrate"),
            units=f.get("units"),
        )

    if ptype == "DataAnalysis":
        return LabProcessDataAnalysis(
            crate,
            identifier=pid,
            name=name,
            object=(obj or samples),
            result=result,
            labprotocol=protocol,
            data_processing=f.get("data_processing", ""),
            software=f.get("software", ""),
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
      assay's ``hasPart`` and un-parented from the root (they expand to
      schema:hasPart, so reachability and containment are preserved).
    """
    root = crate.root_dataset

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
                # … and keep it reachable: nest under the assay's hasPart, removing
                # the root's auto-added top-level reference (it stays reachable
                # transitively File -> Assay -> Study -> ./). Both expand to
                # schema:hasPart, so the RDF containment is a single edge.
                _remove_child(root, "hasPart", child.id)
                _append_unique(node, "hasPart", child)
