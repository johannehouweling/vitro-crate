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

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from rocrate.model import ContextEntity, DataEntity, File, Person
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity
from profiles.models.isa import LabProcess, Sample
from profiles.models.tox import (
    CellLineSample,
    LabProcessCellCulture,
    LabProcessDataAnalysis,
    LabProcessEndpointReadout,
    LabProcessExposure,
)

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
        "about",
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
        "doi",
        "dest_path",
        "path",
        "contentUrl",
    }
)


def populate_crate(
    state: CrateState,
    crate: ROCrate,
    output_dir: Path | None = None,
    *,
    materialize_payload: bool = True,
) -> None:
    """Populate `crate` from `state` using the ISA-Tox domain model.

    output_dir is the crate root being written; the Exposure condition table is
    materialised there as a (placeholder) CSV so it is a valid in-payload File.

    When ``materialize_payload`` is False (the in-memory build_and_validate path,
    #87) no payload file is written to disk — the condition-table File node is
    still added to the graph so the metadata document validates, but its CSV is
    not created. This keeps validation a zero-disk operation.
    """
    idx: dict[str, Any] = {}
    _populate_root_and_conformance(state, crate)
    _add_leaves(state, crate, idx, materialize_payload=materialize_payload)
    _add_structural(state, crate, idx)
    _add_processes(state, crate, idx, output_dir, materialize_payload=materialize_payload)
    _wire_mentions(state, idx)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(text)).strip("_") or "x"


def _scalar_props(entity: Entity, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Plain-value properties of an entity (references/discriminators removed)."""
    drop = _REF_FIELDS | _STRUCT_FIELDS | set(skip)
    return {k: v for k, v in entity.fields.items() if k not in drop and not k.startswith("@")}


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
    if t == "MolecularEntity" and f.get("pubchem_cid"):
        return f"https://pubchem.ncbi.nlm.nih.gov/compound/{f['pubchem_cid']}"
    if t == "Publication":
        ident = str(f.get("identifier", ""))
        doi = f.get("doi") or (ident if ident.startswith("10.") else None)
        if doi:
            d = str(doi).strip()
            return d if d.startswith("http") else f"https://doi.org/{d}"
    if eid.startswith(("#", "http://", "https://", "./")) or "://" in eid:
        return eid
    return "#" + _slug(t) + "_" + eid


def _file_dest(fe: Entity) -> str:
    """A relative URI path for a File data entity."""
    f = fe.fields
    path = f.get("dest_path") or f.get("path") or f.get("contentUrl")
    return str(path) if path else f"data/{_slug(f.get('name') or fe.entity_id)}"


def _file_source(fe: Entity, input_path: str | None) -> str | None:
    """Resolve the on-disk source for a File data entity, or ``None`` (#128).

    Returns an absolute path when the referenced file exists locally, so
    ``ro-crate-py`` copies it into the crate payload at ``crate.write()`` time
    (its ``_copy_file`` skips the copy when source and dest are the same file, so
    in-place builds where ``output_path == input_path`` are safe). Returns
    ``None`` for remote (``http(s)://``) references or files not found on disk —
    leaving the File as a metadata-only reference rather than a phantom copy.
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
    return str(src) if src.is_file() else None


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
        node = crate.add(Person(crate, _mint_id(person), properties=_scalar_props(person)))
        _idx_add(idx, person, node)
        crate.root_dataset.append_to("author", node)

    for chem in state.list_entities("MolecularEntity"):
        _idx_add(
            idx,
            chem,
            crate.add(
                ContextEntity(
                    crate,
                    _mint_id(chem),
                    properties={"@type": "MolecularEntity", **_scalar_props(chem)},
                )
            ),
        )

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

    for pub in state.list_entities("Publication"):
        node = crate.add(
            ContextEntity(
                crate,
                _mint_id(pub),
                properties={"@type": "ScholarlyArticle", **_scalar_props(pub)},
            )
        )
        _idx_add(idx, pub, node)
        crate.root_dataset.append_to("citation", node)

    for fe in state.list_entities("File"):
        # Resolve the on-disk source so ro-crate-py copies the file into the
        # payload at write() time (#128). Skip on the in-memory build_and_validate
        # path (materialize_payload=False) — nothing is written there.
        source = (
            _file_source(fe, state.metadata.input_path) if materialize_payload else None
        )
        _idx_add(
            idx,
            fe,
            crate.add(
                File(
                    crate,
                    source,
                    dest_path=_file_dest(fe),
                    properties={"@type": "File", **_scalar_props(fe)},
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
        _idx_add(
            idx,
            cl,
            CellLineSample(
                crate,
                identifier=_mint_id(cl),
                name=str(cl.fields.get("name", "")),
                sample_type=cell_term[0],
                accession=cl.fields.get("accession"),
                properties=_scalar_props(cl, skip=("name", "accession")) or None,
            ),
        )


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


_CONDITION_TABLE_HEADER = "cell_line,compound,concentration,unit,duration\n"

# Typed CSVW columns for the condition table: each maps a CSV column to an
# ontology property (propertyUrl) with a declared datatype. The cell-line and
# compound columns additionally resolve to in-crate entity ids (valueUrl, filled
# at build time), so the per-well design table is machine-readable rather than a
# header-only placeholder. Issue #94.
_CONDITION_TABLE_COLUMNS: tuple[dict[str, str], ...] = (
    {"titles": "cell_line", "datatype": "string", "propertyUrl": "http://schema.org/name"},
    {"titles": "compound", "datatype": "string", "propertyUrl": "http://schema.org/mentions"},
    {"titles": "concentration", "datatype": "double", "propertyUrl": "http://schema.org/value"},
    {"titles": "unit", "datatype": "string", "propertyUrl": "http://schema.org/unitText"},
    {"titles": "duration", "datatype": "string", "propertyUrl": "http://schema.org/duration"},
)


def _node_id(node: Any) -> str | None:
    """The @id of an ro-crate node (None if it has none)."""
    return getattr(node, "id", None)


def _build_condition_table_schema(
    crate: ROCrate, exp_slug: str, cells: list[Any], chems: list[Any]
) -> ContextEntity:
    """The csvw:Schema entity describing the condition table's typed columns.

    Each column is a ``csvw:Column`` graph node (ro-crate-py requires nested
    objects to be referenceable entities, not inline dicts) carrying a datatype
    and propertyUrl. The cell-line and compound columns additionally carry a
    ``valueUrl`` resolving to the in-crate Sample / MolecularEntity id, so a
    row's value maps to its entity.
    """
    value_urls = {
        "cell_line": _node_id(cells[0]) if cells else None,
        "compound": _node_id(chems[0]) if chems else None,
    }
    schema = crate.add(
        ContextEntity(
            crate,
            f"#{exp_slug}_condition_table_schema",
            properties={
                "@type": ["csvw:Schema", "CreativeWork"],
                "name": "Condition table schema",
            },
        )
    )
    for col in _CONDITION_TABLE_COLUMNS:
        title = col["titles"]
        props: dict[str, Any] = {"@type": "csvw:Column", **col}
        if value_urls.get(title):
            # Emit valueUrl as an {@id} reference (not a bare string): RO-Crate 1.2
            # REQUIRES entity links be reference objects, and flags string @ids.
            props["valueUrl"] = {"@id": value_urls[title]}
        column = crate.add(
            ContextEntity(crate, f"#{exp_slug}_col_{title}", properties=props)
        )
        schema.append_to("columns", column)
    return schema


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
    rel = f"data/{_slug(exp_pid)}_condition_table.csv"
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
        )

    if ptype == "EndpointReadout":
        return LabProcessEndpointReadout(
            crate,
            identifier=pid,
            name=name,
            samples=(samples or obj or None),
            labprotocol=protocol,
            result=result,
            detection_instrument=f.get("detection_instrument", "unknown"),
            instrument_manufacturer=f.get("instrument_manufacturer", "unknown"),
            measured_entity=f.get("measured_entity", "unknown"),
            technical_replicate=f.get("technical_replicate", "1"),
            endpoint=f.get("endpoint", "unknown"),
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
