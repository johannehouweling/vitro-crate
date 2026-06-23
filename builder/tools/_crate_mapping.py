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

ROCRATE_SPEC = "https://w3id.org/ro/crate/1.1"
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
    _add_leaves(state, crate, idx)
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
    # The base spec stays pinned to 1.1 (not 1.2) deliberately: roc-validator
    # 0.10.0 bundles no ro-crate-1.2 base profile, and its base pass hard-requires
    # the 1.1 URI on the descriptor (profiles/ro-crate/must/1_file-descriptor_
    # metadata.ttl: `sh:hasValue <https://w3id.org/ro/crate/1.1>`). Declaring 1.2
    # there fails REQUIRED validation, which build_and_validate (#87) and the
    # golden fixtures (#97) rely on staying green. ro-crate-py 0.15 still emits a
    # 1.2 @context; fully unifying the version on 1.2 is deferred until an upstream
    # validator ships a 1.2 base profile (tracked on #91).
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


def _add_leaves(state: CrateState, crate: ROCrate, idx: dict[str, Any]) -> None:
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
        _idx_add(
            idx,
            fe,
            crate.add(
                File(
                    crate,
                    None,
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


def _add_structural(state: CrateState, crate: ROCrate, idx: dict[str, Any]) -> None:
    for inv in state.list_entities("Investigation"):
        props = {
            "@type": "Dataset",
            "additionalType": "Investigation",
            **_scalar_props(inv),
        }
        props.setdefault("identifier", inv.entity_id)  # ISA MUST: non-empty identifier
        node = crate.add(DataEntity(crate, _mint_id(inv), properties=props))
        _idx_add(idx, inv, node)
        crate.root_dataset.append_to("hasPart", node)

    for st in state.list_entities("Study"):
        props = {"@type": "Dataset", "additionalType": "Study", **_scalar_props(st)}
        props.setdefault("identifier", st.entity_id)
        node = crate.add(DataEntity(crate, _mint_id(st), properties=props))
        _idx_add(idx, st, node)
        crate.root_dataset.append_to("hasPart", node)

    for asy in state.list_entities("Assay"):
        props = {"@type": "Dataset", "additionalType": "Assay", **_scalar_props(asy)}
        props.setdefault("identifier", asy.entity_id)
        node = crate.add(DataEntity(crate, _mint_id(asy), properties=props))
        _idx_add(idx, asy, node)
        parent = _resolve_one(idx, asy.fields.get("study_id")) or crate.root_dataset
        parent.append_to("hasPart", node)


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
    so the File is valid in-payload; per-row CSVW population (tableSchema columns
    with valueUrl, CSV intake) is planned — see the wizard's
    intake/condition_table.py. The table links (schema:about) the cell line(s) and
    compound(s) it concerns, so the compound is connected to the Exposure THROUGH
    its result (a MolecularEntity cannot be a process object under the ISA shape).
    """
    rel = f"data/{_slug(exp_pid)}_condition_table.csv"
    # Only touch disk when materialising payload for an on-disk export. The
    # in-memory validate path (#87) skips the write; the File node below still
    # carries dest_path=rel so the metadata graph is complete for validation.
    if materialize_payload and output_dir is not None:
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(_CONDITION_TABLE_HEADER, encoding="utf-8")
    table = crate.add(
        File(
            crate,
            None,
            dest_path=rel,
            properties={"@type": ["File", "csvw:Table"], "name": "Condition table"},
        )
    )
    for ent in list(cells) + list(chems):
        table.append_to("about", ent)
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
    samples = _resolve_many(idx, f.get("samples"))
    obj = _resolve_many(idx, f.get("object"))
    result = _resolve_many(idx, f.get("result"))

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


def _wire_mentions(state: CrateState, idx: dict[str, Any]) -> None:
    for st in state.list_entities("Study"):
        node = idx.get(st.entity_id)
        if node is None:
            continue
        for field, prop in _STUDY_MENTION_FIELDS.items():
            if field in st.fields:
                _wire_mention(node, prop, st.fields[field], idx)

    for asy in state.list_entities("Assay"):
        node = idx.get(asy.entity_id)
        if node is None:
            continue
        for field, prop in _ASSAY_MENTION_FIELDS.items():
            if field in asy.fields:
                _wire_mention(node, prop, asy.fields[field], idx)
