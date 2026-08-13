"""Composite drafter tools — deterministic macros over the pure drafters (#154).

The drafters in :mod:`builder.tools.drafters` are pure state mutations (no LLM,
no network), so the recurring multi-call sequences the system prompt prescribes
can be fused into a single deterministic tool. A weak model then reaches a
BASE-passing Investigation -> Study -> Assay backbone in one tool call instead
of 3-4 round-trips, and never thrashes on threading freshly-minted ids across
turns.

These are convenience macros, not a workflow graph: they only chain existing
pure tools, so they stay transport-agnostic (the MCP server can reuse them) and
consistent with the "Toolbox, Not Graph" design (AGENTS.md §1).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools._resolve_cache import (
    DEFAULT_RESOLVE_TIMEOUT,
    compound_cache,
    normalize_compound_name,
    resolve_concurrency,
    run_with_timeout,
)
from builder.tools.drafters import (
    VALID_PROCESS_TYPES,
    _make_entity_id,
    draft_assay,
    draft_cell_line_sample,
    draft_investigation,
    draft_molecular_entity,
    draft_organization,
    draft_process,
    draft_publication,
    draft_sample,
    draft_study,
)
from builder.tools.hitl import HumanInterface
from builder.tools.lookups import (
    lookup_cell_line,
    lookup_cell_line_by_name,
    lookup_compound,
    lookup_doi,
    lookup_dtxsid,
    lookup_orcid,
    warm_compound_cache,
)

# One source of truth with the link tool for "where does the build read this
# entity type from" — imported rather than restated so the two cannot drift.
from builder.tools.provenance import _PROCESS_LINK_HOMES as _BUILD_HONOURED_HOMES
from builder.tools.verification import verify_identifier
from lookups.crossref import search_works_by_title
from lookups.orcid import lookup_orcid_by_name
from lookups.ror import fetch_ror_by_id

logger = logging.getLogger(__name__)


def _first_of_type(state: CrateState, type_name: str) -> Entity | None:
    """Return the first existing entity of *type_name*, or None."""
    return next((e for e in state.list_entities() if e.type == type_name), None)


def _merge_hints_into(entity: Entity, hints: Mapping[str, Any] | None) -> None:
    """Fill an existing entity's EMPTY fields from *hints* (fill-don't-clobber).

    Makes :func:`scaffold_isa_backbone` idempotent-WITH-MERGE: re-scaffolding a
    reused backbone layer no longer silently drops the supplied hints — a hint
    fills a field the entity is missing or carries an empty value for, but a value
    the entity already holds is never overwritten. Only non-empty hint values are
    applied. The merge is recorded as ``source="llm"`` (the same provenance the
    drafters use for hint-supplied fields). It is purely additive, so the call
    stays a no-op on a fully-populated entity.
    """
    if not hints:
        return
    to_apply: dict[str, Any] = {}
    for key, value in hints.items():
        if value is None or not str(value).strip():
            continue
        current = entity.fields.get(key)
        if current is None or not str(current).strip():
            to_apply[key] = value
    if to_apply:
        entity.set_fields_from_dict(to_apply, source="llm")


def scaffold_isa_backbone(
    state: CrateState,
    investigation: dict | None = None,
    study: dict | None = None,
    assay: dict | None = None,
    validate_base: bool | None = None,
) -> dict[str, Any]:
    """Create (or reuse) a linked Investigation -> Study -> Assay backbone.

    Chains the pure drafters in one call, wiring ``investigation_id`` /
    ``study_id`` so the result is a BASE-passing ISA backbone. It is
    **idempotent**: an existing entity of each type is reused rather than
    duplicated, and a missing layer is created and linked to the reused (or
    freshly created) parent. It deliberately creates **no File entities** — the
    scan inventory carries no role, so binding files here would manufacture
    ISA-layer orphans; wire data files explicitly with ``draft_file`` + ``link``.

    Args:
        state: The crate state to scaffold into.
        investigation: Optional field hints for the Investigation.
        study: Optional field hints for the Study.
        assay: Optional field hints for the Assay.
        validate_base: When true, also run a scoped ``build_and_validate(profile="base")``
            and return it under ``"validation"`` (one round-trip for scaffold +
            check). Weak models may pass ``None`` for this optional arg; that is
            treated as false. Named ``validate_base`` (not ``validate``) to avoid
            shadowing ``pydantic.BaseModel.validate`` in the generated arg schema.

    Returns:
        ``{"investigation_id", "study_id", "assay_id", "created", "reused"}``
        (entity ids plus which types were newly created vs reused), and
        ``"validation"`` when ``validate`` is true.
    """
    created: list[str] = []
    reused: list[str] = []

    def _ensure(type_name: str, make, hints: dict | None) -> Entity:
        existing = _first_of_type(state, type_name)
        if existing is not None:
            reused.append(type_name)
            # Idempotent-with-merge: fill the reused entity's empty fields from the
            # supplied hints instead of silently dropping them (fill-don't-clobber).
            _merge_hints_into(existing, hints)
            return existing
        created.append(type_name)
        return make()

    inv = _ensure(
        "Investigation", lambda: draft_investigation(state, investigation or {}), investigation
    )
    study_entity = _ensure("Study", lambda: draft_study(state, inv.entity_id, study or {}), study)
    assay_entity = _ensure(
        "Assay", lambda: draft_assay(state, study_entity.entity_id, assay or {}), assay
    )

    result: dict[str, Any] = {
        "investigation_id": inv.entity_id,
        "study_id": study_entity.entity_id,
        "assay_id": assay_entity.entity_id,
        "created": created,
        "reused": reused,
    }

    if validate_base:
        from builder.tools.validation import build_and_validate

        result["validation"] = build_and_validate(state, profile="base")

    return result


# ---------------------------------------------------------------------------
# LabProcess derivation chain (Issue #179, task 3)
# ---------------------------------------------------------------------------

# The canonical order of the gold S-VHPS21 derivation chain. A supplied chain may
# be any *subset* of these (partial chains work) but is always wired in this
# order so the provenance flows the right way.
_CHAIN_ORDER: tuple[str, ...] = (
    "CellCulture",
    "Exposure",
    "EndpointReadout",
    "DataAnalysis",
)

# Subtypes that produce *material* (a Sample) vs *data* (a File). Determines
# which kind of placeholder entity is synthesized to carry a step's output so the
# next step has something concrete to consume. EndpointReadout / DataAnalysis are
# the two subtypes with NO build-time output fallback (AGENTS.md §14.3); a missing
# schema:result on them fires a tox Violation — closing that trap is this tool's
# load-bearing job, and synthesizing an output for *every* producing step keeps
# the whole derivation chain connected and referenceable.
_SAMPLE_PRODUCERS = frozenset({"CellCulture"})

# Subtypes whose build-time output fallback is the *semantically-correct* output
# entity, so synthesizing a generic placeholder here would PRE-EMPT it (Issue
# #285). The Exposure's build fallback (``_crate_mapping._synth_condition_table``)
# is the CSVW **condition table** — the per-well design table that
# ``schema:about``-references the test MolecularEntities (the substances + doses
# the cells were exposed to). That ``table --about--> MolecularEntity`` edge is
# the TRUE ISA-Tox link, but it only fires when the Exposure has NO explicit
# ``result``. If ``draft_process_chain`` eagerly synthesized a generic result
# File, ``result`` would be populated, the condition table would never build, and
# the compounds would ride only on the weaker Study ``schema:mentions`` backstop.
# So we leave these steps' output to the build: the chain still flows downstream
# via the step's inputs (``upstream_output = outputs or inputs``).
_BUILD_SYNTHESIZES_OUTPUT = frozenset({"Exposure"})


def _ref_ids(value: Any) -> set[str]:
    """Normalize a reference value (id / {@id} / list thereof) to bare ids."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key:
            out.add(str(key).lstrip("#"))
    return out


def _explicit_ids(step: dict[str, Any], *keys: str) -> list[str]:
    """Collect the union of caller-supplied reference ids under ``keys``."""
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for rid in _ref_ids(step.get(key)):
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
    return out


def draft_process_chain(
    state: CrateState,
    assay_id: str,
    chain: list[dict[str, Any]] | None = None,
    validate_after: bool | None = None,
) -> dict[str, Any]:
    """Create and wire a LabProcess derivation chain in ONE idempotent call.

    Fuses the recurring ``draft_process`` + ``link`` sequence that wires the gold
    S-VHPS21 chain::

        Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
               →[EndpointReadout]→ raw/result →[DataAnalysis]→ figures

    into a single call. ``chain`` is an ordered list of step dicts; each step has
    a ``process_type`` (a subset of CellCulture / Exposure / EndpointReadout /
    DataAnalysis — **partial chains are allowed**), optional ``hints`` (passed
    straight to :func:`draft_process`), and optional explicit ``object`` /
    ``result`` reference id(s) (or their ``input`` / ``output`` aliases). Steps
    are always wired in the canonical chain order regardless of input order, so a
    weak model cannot mis-sequence the provenance.

    **The load-bearing job — output synthesis (AGENTS.md §14.3).** Unlike
    CellCulture / Exposure, ``EndpointReadout`` and ``DataAnalysis`` have **no
    build-time output fallback**: a process with no explicit ``result`` (and, for
    DataAnalysis, no ``object``) fires a tox REQUIRED Violation and the chain
    dangles. This composite closes that trap:

    - It threads each producing step's output into the next step's input, so a
      downstream process always *consumes* what the previous step produced.
    - For any step that still lacks a required output (``result`` for both,
      ``object`` for DataAnalysis), it **synthesizes an explicit placeholder data
      entity** — a :func:`~builder.tools.drafters.draft_sample` Sample for a
      material producer (CellCulture) or a
      :func:`~builder.tools.provenance.draft_file` File for a data producer — and
      wires it with :func:`~builder.tools.provenance.link`.

    Synthesized placeholders carry **only** structural metadata (a name, a crate
    path, a role); they NEVER fabricate measurement values or identifiers (D5).
    They are intentionally header-less stubs — use ``populate_condition_table`` /
    ``attach_files`` / ``set_fields`` to fill real content. What this tool
    **requires vs synthesizes**:

    - **Requires:** an existing ``assay_id``; the per-step ``process_type``.
    - **Synthesizes (only when needed):** the produced output entity for a step
      whose output is required but neither supplied nor derivable from the chain.
    - **Respects:** any explicit ``object`` / ``result`` you pass — those win over
      synthesis, and the build still appends the typed CSVW condition table
      (Exposure) / raw-measurements table (EndpointReadout) on top.

    It is **idempotent**: process / placeholder ids are derived deterministically
    from the step, so re-running reuses (overwrites in place) the same entities
    rather than minting duplicates.

    Args:
        state: The crate state to wire the chain into.
        assay_id: entity_id of the parent Assay every process belongs to.
        chain: Ordered list of step dicts (see above). ``None``/empty is a no-op.
        validate_after: When true, also run a full ``build_and_validate`` and
            return it under ``"validation"`` (weak models may pass ``None`` for
            this optional arg; that is treated as false). Named
            ``validate_after`` (not ``validate``) to avoid shadowing
            ``pydantic.BaseModel.validate`` in the generated arg schema; the
            suffix differs from the sibling ``scaffold_isa_backbone`` whose
            ``validate_base`` runs only the base profile, whereas this runs the
            full three-pass ``build_and_validate``.

    Returns:
        ``{"assay_id", "process_ids", "steps", "synthesized"}`` — the ordered
        process entity ids, a per-step summary (``{process_id, process_type,
        object, result}``), and the list of placeholder entity ids synthesized.
        ``"validation"`` is added when ``validate_after`` is true.

    Raises:
        ValueError: If ``assay_id`` is missing/not an Assay, or a step has an
            invalid ``process_type``.
    """
    from builder.tools.provenance import draft_file, link

    assay = state.get_entity(assay_id)
    if assay is None:
        raise ValueError(f"draft_process_chain assay not found: {assay_id!r}.")
    if assay.type != "Assay":
        raise ValueError(
            f"draft_process_chain assay_id must be an Assay; {assay_id!r} is a {assay.type}."
        )

    steps_in = list(chain or [])
    for step in steps_in:
        ptype = step.get("process_type")
        if ptype not in VALID_PROCESS_TYPES:
            raise ValueError(
                f"Invalid process_type {ptype!r} in chain step. Must be one of: "
                f"{', '.join(sorted(VALID_PROCESS_TYPES))}."
            )

    # Wire in canonical order regardless of input order so the model cannot
    # mis-sequence the provenance. Within a type, preserve the caller's order.
    ordered = sorted(steps_in, key=lambda s: _CHAIN_ORDER.index(s["process_type"]))

    process_ids: list[str] = []
    step_summaries: list[dict[str, Any]] = []
    synthesized: list[str] = []
    # The most recent step's produced output id(s) — fed to the next step's input.
    upstream_output: list[str] = []

    for step in ordered:
        ptype = str(step["process_type"])
        hints = dict(step.get("hints") or {})
        proc = draft_process(state, assay_id, ptype, hints)

        # --- inputs: explicit wins; otherwise inherit the upstream output ---
        inputs = _explicit_ids(step, "object", "input", "samples")
        if not inputs and upstream_output:
            inputs = list(upstream_output)
        for tid in inputs:
            if state.get_entity(tid) is not None:
                link(state, proc.entity_id, "object", tid)

        # --- outputs: explicit wins; else synthesize a concrete, referenceable
        # output so the chain connects AND the §14.3 "no output fallback" trap is
        # closed. Every producing step gets a real output entity in state (a
        # Sample for a material producer, a File for a data producer) — so the
        # next step can consume it by id and EndpointReadout / DataAnalysis never
        # dangle into a tox Violation. The EXCEPTION is a subtype whose build-time
        # fallback is the semantically-correct output (the Exposure's CSVW
        # condition table; #285): synthesizing a generic placeholder there would
        # pre-empt the ``table --about--> MolecularEntity`` link, so we leave its
        # output to the build (the chain still flows downstream via its inputs). ---
        outputs = _explicit_ids(step, "result", "output")
        if not outputs and ptype not in _BUILD_SYNTHESIZES_OUTPUT:
            placeholder = _synthesize_output(state, proc, ptype, draft_sample, draft_file)
            synthesized.append(placeholder.entity_id)
            outputs = [placeholder.entity_id]
        for tid in outputs:
            if state.get_entity(tid) is not None:
                link(state, proc.entity_id, "result", tid)

        # DataAnalysis MUST also carry schema:object (its raw/condition input).
        # If nothing was wired as input, synthesize a placeholder input File too.
        if ptype == "DataAnalysis" and not inputs:
            placeholder_in = _synthesize_input(state, proc, draft_file)
            synthesized.append(placeholder_in.entity_id)
            link(state, proc.entity_id, "object", placeholder_in.entity_id)
            inputs = [placeholder_in.entity_id]

        process_ids.append(proc.entity_id)
        step_summaries.append(
            {
                "process_id": proc.entity_id,
                "process_type": ptype,
                "object": inputs,
                "result": outputs,
            }
        )
        # The next step consumes what this one produced (its explicit/synthesized
        # output), falling back to this step's inputs so a no-output step (e.g. a
        # bare Exposure relying on the build's typed-table fallback) still hands
        # the material flow downstream.
        upstream_output = outputs or inputs

    result: dict[str, Any] = {
        "assay_id": assay_id,
        "process_ids": process_ids,
        "steps": step_summaries,
        "synthesized": synthesized,
    }

    if validate_after:
        from builder.tools.validation import build_and_validate

        result["validation"] = build_and_validate(state)

    return result


# Which minimal table a synthesized placeholder should carry (#438). An
# EndpointReadout emits per-well measurements; a DataAnalysis emits summarised
# results. The columns themselves live in ``_crate_mapping`` next to the existing
# CSVW table contracts, so every table the crate ships is typed the same way.
_PROVISIONAL_TABLE_KIND: dict[str, str] = {
    "EndpointReadout": "measurements",
    "DataAnalysis": "analysis",
}


def _synthesize_output(
    state: CrateState,
    proc: Entity,
    ptype: str,
    draft_sample_fn: Any,
    draft_file_fn: Any,
) -> Entity:
    """Create (deterministically) the placeholder output entity for ``proc``.

    A material producer (CellCulture) yields a placeholder Sample; a data
    producer (EndpointReadout / DataAnalysis) yields a placeholder File. The id
    is derived from the process so re-running reuses it (idempotent). The
    placeholder carries only structural metadata — never a fabricated value (D5).
    """
    if ptype in _SAMPLE_PRODUCERS:
        name = f"{proc.entity_id} output sample"
        existing = state.get_entity(f"sample_{_slug(name)}")
        if existing is not None:
            return existing
        sample_hints: dict[str, Any] = {"name": name}
        upstream = _input_ref(proc)
        if upstream is not None:
            sample_hints["derives_from"] = upstream
        return draft_sample_fn(state, sample_hints)
    # Data producer: a placeholder result File. It is marked PROVISIONAL so the
    # build materialises a minimal typed table for it (#438) — an entity alone
    # left the exported crate claiming a file that was never written.
    name = f"{proc.entity_id}_result.csv"
    file_id = f"file_{_slug(name)}"
    existing = state.get_entity(file_id)
    if existing is not None:
        return existing
    placeholder = draft_file_fn(
        state,
        name=name,
        path=f"data/{name}",
        role="processed_data",
    )
    placeholder.fields["provisional"] = True
    placeholder.fields["table_kind"] = _PROVISIONAL_TABLE_KIND.get(ptype, "measurements")
    return placeholder


def _synthesize_input(state: CrateState, proc: Entity, draft_file_fn: Any) -> Entity:
    """Create the placeholder *input* File a DataAnalysis needs (schema:object).

    Only used when a DataAnalysis has no upstream output and no explicit object —
    it must still declare its analysed input, so we mint a header-less stub.
    """
    name = f"{proc.entity_id}_input.csv"
    file_id = f"file_{_slug(name)}"
    existing = state.get_entity(file_id)
    if existing is not None:
        return existing
    placeholder = draft_file_fn(state, name=name, path=f"data/{name}", role="raw_data")
    # A DataAnalysis consumes measurements, so its provisional input carries the
    # per-well measurement shape rather than the analysis-result shape.
    placeholder.fields["provisional"] = True
    placeholder.fields["table_kind"] = "measurements"
    return placeholder


def _input_ref(proc: Entity) -> Any:
    """First wired input of a process (for a Sample's derives_from), or None."""
    for fld in ("object", "input", "samples", "cell_line"):
        ids = _ref_ids(proc.fields.get(fld))
        if ids:
            return next(iter(ids))
    return None


def _slug(text: str) -> str:
    """Mirror drafters._make_entity_id's name normalization for stable ids."""
    base = text.lower().replace(" ", "_").replace("-", "_")
    base = "".join(c for c in base if c.isalnum() or c == "_")
    return base or "unnamed"


# ---------------------------------------------------------------------------
# AOP-Wiki subgraph materialisation (Issue #180)
# ---------------------------------------------------------------------------

# AOP-Wiki @type string -> CrateState EntityType. The three classes share one
# collection (state.ENTITY_TYPE_MAP); the build types each node by its own class.
_AOP_NODE_TYPES: dict[str, EntityType] = {
    "AdverseOutcomePathway": "AdverseOutcomePathway",
    "KeyEvent": "KeyEvent",
    "KeyEventRelationship": "KeyEventRelationship",
}


def _materialize_aop_node(state: CrateState, node: dict[str, Any]) -> Entity | None:
    """Persist one AOP-Wiki node dict into CrateState as a typed entity.

    The node's resolvable AOP-Wiki IRI (``@id``) becomes the entity_id, so
    :func:`builder.tools._crate_mapping._mint_id` keeps it verbatim as the built
    node's ``@id`` and the subgraph's ``has_*`` / ``upstream_event`` /
    ``downstream_event`` reference objects (which already point at sibling IRIs)
    cross-link without any id resolution. Idempotent: a node whose IRI is already
    in state is left untouched (no duplicate, no clobber).

    Returns the materialised (or pre-existing) Entity, or ``None`` for a
    malformed node missing its ``@id`` / ``@type``.
    """
    iri = node.get("@id")
    node_type = _AOP_NODE_TYPES.get(str(node.get("@type")))
    if not iri or node_type is None:
        return None
    existing = state.get_entity(str(iri))
    if existing is not None:
        return existing
    fields = {k: v for k, v in node.items() if k not in ("@id", "@type")}
    entity = Entity(
        entity_id=str(iri),
        type=node_type,
        _provenance=EntityProvenance(created_by="lookup"),
    )
    entity.set_fields_from_dict(fields, source="lookup")
    state.add_entity(entity)
    return entity


def materialize_aop_subgraph(
    state: CrateState,
    aop_id: str,
    study_id: str | None = None,
) -> dict[str, Any]:
    """Turn ONE AOP-Wiki id into the full, cross-linked crate subgraph.

    Looks the AOP up via :func:`builder.tools.lookups.lookup_aop` and
    deterministically materialises its complete subgraph into ``state``:

    - one ``AdverseOutcomePathway`` node carrying its ``name`` / ``identifier`` /
      ``url`` (and ``alternateName`` when present) plus the
      ``has_molecular_initiating_event`` / ``has_key_event`` /
      ``has_adverse_outcome`` / ``has_key_event_relationship`` link arrays;
    - one ``KeyEvent`` node per molecular-initiating-event / key-event /
      adverse-outcome — all share ``@type KeyEvent`` and are discriminated only
      by their ``eventType`` string;
    - one ``KeyEventRelationship`` node per relation, linking its
      ``upstream_event`` and ``downstream_event`` by ``@id``.

    The ONLY model-supplied input is the numeric ``aop_id``; every link and id
    comes straight from the AOP-Wiki graph, so nothing is fabricated (D5). The
    nodes are keyed by their resolvable AOP-Wiki IRI, so re-running is idempotent.

    When ``study_id`` names an existing Study, the AOP is wired onto it via the
    ``aop`` reference (an alias of ``schema:mentions``), connecting the study to
    the pathway it investigates — mirroring the gold crate (Issue #180).

    Args:
        state: The crate state to materialise into.
        aop_id: Numeric AOP-Wiki identifier, e.g. ``"610"``.
        study_id: Optional entity_id of a Study to wire the AOP onto.

    Returns:
        On success, ``{"aop_id", "aop_entity_id", "events", "relationships",
        "wired_to_study"}``. On a lookup miss, ``{"ok": False, "error": ...}``.
    """
    from builder.tools.lookups import lookup_aop

    result = lookup_aop(str(aop_id))
    if not result.get("found"):
        return {
            "ok": False,
            "error": result.get("error", f"AOP '{aop_id}' not found"),
        }

    data = result["data"]
    aop_node = data.get("aop") or {}
    aop_entity = _materialize_aop_node(state, aop_node)

    events = 0
    events_detail: list[dict[str, Any]] = []
    for ev in data.get("events", []):
        entity = _materialize_aop_node(state, ev)
        if entity is not None:
            events += 1
            # Surfaced so a caller can pick the Key Event an Assay measures
            # WITHOUT a second lookup. Every value is read off the entity just
            # persisted — the @id is the AOP-Wiki IRI, never minted (D5).
            events_detail.append(
                {
                    "@id": entity.entity_id,
                    "name": entity.fields.get("name"),
                    "eventType": entity.fields.get("eventType"),
                }
            )

    relationships = 0
    for rel in data.get("relationships", []):
        if _materialize_aop_node(state, rel) is not None:
            relationships += 1

    wired_to_study: str | None = None
    if study_id and aop_entity is not None:
        study = state.get_entity(study_id)
        if study is not None and study.type == "Study":
            existing_refs = study.fields.get("aop") or []
            if not isinstance(existing_refs, list):
                existing_refs = [existing_refs]
            ref = {"@id": aop_entity.entity_id}
            ids = {r.get("@id") if isinstance(r, dict) else r for r in existing_refs}
            if aop_entity.entity_id not in ids:
                existing_refs = [*existing_refs, ref]
            study.fields["aop"] = existing_refs
            study.set_field_status("aop", "filled", "lookup")
            wired_to_study = study_id

    return {
        "aop_id": str(aop_id),
        "aop_entity_id": aop_entity.entity_id if aop_entity else None,
        "events": events,
        # Additive: the int count stays for existing callers/tests.
        "events_detail": events_detail,
        "relationships": relationships,
        "wired_to_study": wired_to_study,
    }


# ---------------------------------------------------------------------------
# Assay -> AOP Key Event (#382)
# ---------------------------------------------------------------------------
# `keyEvent` is a fully declared Assay reference field: it is in the draft
# schema, mapped by `_ASSAY_MENTION_FIELDS`, and consumed by the build — and it
# has ZERO writers anywhere in builder/. No crate either arm has produced has
# ever linked an Assay to the Key Event it measures, so the biological meaning of
# the measurement is absent from every crate while the field sits there looking
# supported.
#
# Matching is by NAME because that is what a depositor writes ("mitochondrial
# dysfunction"), but the reference committed is always the in-state AOP-Wiki IRI
# — never an id minted from the name (D5). An ambiguous or absent match writes
# nothing and returns the candidates, because guessing which Key Event an assay
# measures is a scientific claim, not a string operation.


def _event_tokens(text: str) -> frozenset[str]:
    """Comparable token set for a Key Event name (case/punctuation-insensitive)."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in str(text).casefold())
    return frozenset(cleaned.split())


def link_assay_to_key_event(state: CrateState, assay_id: str, event_name: str) -> dict[str, Any]:
    """Link an Assay to the AOP Key Event it measures, by Key Event name.

    Args:
        state: The crate state holding the Assay and the materialized Key Events.
        assay_id: ``entity_id`` of the Assay.
        event_name: The Key Event's name as written by the depositor.

    Returns:
        ``{"ok": True, "assay_id", "key_event_id", "matched_name"}`` on a single
        unambiguous match, else ``{"ok": False, "error", "candidates"}`` with
        nothing written.
    """
    from builder.tools.management import set_fields

    assay = state.get_entity(assay_id)
    if assay is None or assay.type != "Assay":
        return {"ok": False, "error": f"Assay not found: {assay_id!r}", "candidates": []}

    events = state.list_entities("KeyEvent")
    if not events:
        return {
            "ok": False,
            "error": (
                "no KeyEvent in the crate — run materialize_aop_subgraph first so "
                "the event can be referenced by its AOP-Wiki id"
            ),
            "candidates": [],
        }

    wanted = _event_tokens(event_name)
    matches = [
        event
        for event in events
        if any(
            _event_tokens(alias) == wanted
            for alias in (
                event.fields.get("name"),
                event.fields.get("short_name"),
                event.fields.get("alternateName"),
            )
            if alias
        )
    ]
    candidates = [{"@id": e.entity_id, "name": e.fields.get("name")} for e in events]
    if len(matches) != 1:
        return {
            "ok": False,
            "error": (
                f"{len(matches)} Key Events match {event_name!r}; refusing to guess "
                "which one this assay measures"
            ),
            "candidates": candidates,
        }

    event = matches[0]
    set_fields(state, assay_id, {"keyEvent": {"@id": event.entity_id}}, source="lookup")
    return {
        "ok": True,
        "assay_id": assay_id,
        "key_event_id": event.entity_id,
        "matched_name": event.fields.get("name"),
    }


# ---------------------------------------------------------------------------
# Publication authors + ORCID harmonization (Issue #180, deferred item)
#
# A citation author who is not already an in-crate Person used to get a
# synthesized blank id (#CitationAuthor_<Given>_<Family>). This composite
# resolves each author's @id to their ORCID when it can be determined — with
# strict verification (D5) and bounded HITL only on genuine ambiguity.
# ---------------------------------------------------------------------------


def _norm(text: Any) -> str:
    """Lowercase + collapse whitespace + drop trailing dots (for name matching)."""
    return " ".join(str(text or "").lower().replace(".", " ").split())


def _given_tokens(given: Any) -> list[str]:
    """Tokenise a given name into comparable parts ('F.M.A.' -> ['f','m','a'])."""
    return [t for t in _norm(given).split() if t]


def _is_initial(token: str) -> bool:
    return len(token) == 1


def _given_match(a: Any, b: Any) -> str:
    """Strength of a given-name match: 'full', 'initial', or '' (no match).

    'full' requires the leading given tokens to share a non-initial first name
    (e.g. 'Fabian' vs 'Fabian Marinus'); 'initial' requires only the first
    initial to agree (e.g. 'F.' / 'F.M.A.' vs 'Fabian'). An empty given on
    either side is treated as an initial-strength match (family-only signal).
    """
    ta, tb = _given_tokens(a), _given_tokens(b)
    if not ta or not tb:
        return "initial"
    fa, fb = ta[0], tb[0]
    if not _is_initial(fa) and not _is_initial(fb):
        return "full" if fa == fb else ""
    # At least one side is an initial — compare first letters.
    return "initial" if fa[0] == fb[0] else ""


def _names_match(given_a: Any, family_a: Any, given_b: Any, family_b: Any) -> str:
    """Match strength between two (given, family) names: 'full' | 'initial' | ''."""
    if not _norm(family_a) or _norm(family_a) != _norm(family_b):
        return ""
    return _given_match(given_a, given_b)


def _bare_orcid(value: Any) -> str:
    """Strip any URL prefix from an ORCID, returning the bare 0000-... id."""
    return str(value or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _verify_orcid(orcid_id: str, family: str, lookup_orcid_fn: Any) -> dict | None:
    """Resolve an ORCID and return its record IFF the family name roughly matches.

    D5: an ORCID is only trusted once :func:`lookup_orcid` resolves it AND the
    resolved family name matches the author's. Returns the resolved data dict on
    success, else ``None`` (a transient outage also yields ``None`` — we never
    attach an unverified ORCID).
    """
    bare = _bare_orcid(orcid_id)
    if not bare:
        return None
    result = lookup_orcid_fn(bare)
    if not result.get("found"):
        return None
    data = result.get("data") or {}
    resolved_family = data.get("familyName", "")
    if _norm(resolved_family) and _norm(resolved_family) == _norm(family):
        return data
    return None


# Authors are verified against ORCID one network round-trip at a time, and a paper
# has as many of those as it has authors. `_prefetch_orcid_verifications` runs
# that step for every author at once instead, under the same bounded gate
# `resolve_compound` uses so ORCID is not hammered.
#
# Only step (a) of the cascade moves. Everything else stays exactly where it was,
# in order, on the calling thread: the in-crate match and the person entities
# touch CrateState, and the name-search branch can ask the human. Neither belongs
# on a worker thread, and interleaving prompts from several at once would be
# worse than the wait.
_AUTHOR_VERIFY_TIMEOUT = 25.0


def _prefetch_orcid_verifications(
    authors: list[dict[str, Any]], lookup_orcid_fn: Any
) -> dict[int, dict | None]:
    """Verify every author's Crossref ORCID up front, concurrently.

    Returns ``{author index: verified record or None}``. An index missing from
    the mapping was never attempted (no Crossref ORCID to check).

    A verification that runs past :data:`_AUTHOR_VERIFY_TIMEOUT` yields ``None``,
    which the cascade already understands as "not verified" and handles by moving
    to its next step. That is the point: one ORCID sitting on the retry ladder
    used to hold up every author behind it. A profiled run spent 405 seconds in a
    single `draft_publication_with_authors` call — 22% of that session's machine
    time — resolving one DOI.
    """
    from concurrent.futures import ThreadPoolExecutor

    pending = {
        index: str(author.get("identifier"))
        for index, author in enumerate(authors)
        if author.get("identifier")
    }
    if not pending:
        return {}

    def verify(index: int, orcid_id: str) -> tuple[int, dict | None]:
        family = str(authors[index].get("familyName", ""))
        with resolve_concurrency.slot():
            ok, value = run_with_timeout(
                lambda: _verify_orcid(orcid_id, family, lookup_orcid_fn),
                _AUTHOR_VERIFY_TIMEOUT,
            )
        if not ok:
            logger.info(
                "ORCID verification for author %d ran past %.0fs; falling through "
                "to the rest of the cascade",
                index,
                _AUTHOR_VERIFY_TIMEOUT,
            )
            return index, None
        return index, value

    verified: dict[int, dict | None] = {}
    # Bounded by the gate inside `verify`, so the pool only has to be wide enough
    # not to be the narrower limit.
    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        for index, value in pool.map(lambda kv: verify(*kv), list(pending.items())):
            verified[index] = value
    return verified


def _find_in_crate_person(
    state: CrateState, given: str, family: str, affiliation: str | None
) -> Entity | None:
    """An in-crate Person with a VERIFIED ORCID matching this author, or None.

    Family must match and the given name must match at least at initial strength
    (this resolves 'Fabian Wagenaars' -> root 'F.M.A. Wagenaars'). When several
    qualify, an affiliation match is preferred, then a full-given match.
    """
    candidates: list[tuple[int, Entity]] = []
    aff_norm = _norm(affiliation)
    for person in state.list_entities("Person"):
        if not person.fields.get("orcid"):
            continue
        status = person.get_field_status("orcid")
        if status is None or status.status != "verified":
            continue
        strength = _names_match(
            given, family, person.fields.get("givenName"), person.fields.get("familyName")
        )
        if not strength:
            continue
        score = 0
        if strength == "full":
            score += 1
        if aff_norm and aff_norm == _norm(person.fields.get("affiliation")):
            score += 2
        candidates.append((score, person))
    if not candidates:
        return None
    candidates.sort(key=lambda sc: sc[0], reverse=True)
    return candidates[0][1]


def _pick_from_human(
    response: Mapping[str, Any] | None, candidates: list[dict], options: list[str]
) -> str | None:
    """Extract a chosen bare ORCID from a HITL ``present`` response, or None.

    Accepts a pick expressed as an explicit ORCID in ``comments``/``edits`` or as
    an option label/index. A ``skipped``/``rejected`` action yields ``None``.
    """
    if not response or response.get("action") in ("skipped", "rejected"):
        return None
    by_orcid = {_bare_orcid(c["orcid"]): c for c in candidates}
    # 1. An edits dict naming the orcid.
    edits = response.get("edits") or {}
    for value in edits.values():
        bare = _bare_orcid(value)
        if bare in by_orcid:
            return bare
    # 2. Free-text comments containing an orcid or an option label.
    comments = str(response.get("comments") or "").strip()
    if comments:
        bare = _bare_orcid(comments)
        if bare in by_orcid:
            return bare
        for idx, label in enumerate(options):
            if comments == label or comments == str(idx) or comments == str(idx + 1):
                if idx < len(candidates):
                    return _bare_orcid(candidates[idx]["orcid"])
    return None


def _resolve_via_search(
    given: str,
    family: str,
    affiliation: str | None,
    human: HumanInterface | None,
    lookup_orcid_fn: Any,
    lookup_by_name_fn: Any,
) -> str | None:
    """Search ORCID and resolve to a verified bare ORCID, escalating if ambiguous.

    Auto-accepts iff there is exactly ONE candidate that is a STRONG match
    (family + full given name). Anything else — multiple candidates, a weak /
    initial-only match, or a single match that fails name verification — is
    escalated to HITL (``present`` the candidates + a none/skip option, then
    optionally ``request_input`` for a pasted ORCID). Returns a verified bare
    ORCID, or ``None`` (no confident answer; caller falls back to synthesis).
    """
    candidates = list(lookup_by_name_fn(given, family, affiliation) or [])
    if not candidates:
        return None

    strong = [
        c
        for c in candidates
        if _names_match(given, family, c.get("given"), c.get("family")) == "full"
    ]

    # Auto-accept ONLY a single, strong, name-verified candidate.
    if len(candidates) == 1 and len(strong) == 1:
        verified = _verify_orcid(strong[0]["orcid"], family, lookup_orcid_fn)
        if verified is not None:
            return _bare_orcid(strong[0]["orcid"])
        # Fall through to HITL — a sole strong match that fails verification is
        # ambiguous, not confidently absent.

    # Genuine ambiguity (or a single weak / unverifiable match): escalate.
    if human is None:
        return None

    options = [
        f"{c.get('given', '')} {c.get('family', '')} — {c.get('orcid')}"
        + (f" ({c['affiliation']})" if c.get("affiliation") else "")
        for c in candidates
    ]
    options.append("None of these / skip")
    context = (
        f"Multiple ORCID candidates for citation author '{given} {family}'. "
        "Pick the correct one (or skip to leave it unresolved):"
    )
    chosen = _pick_from_human(human.present(context, options), candidates, options)
    if chosen is None:
        # Last chance: let the user paste an ORCID directly.
        resp = human.request_input(
            f"Paste the ORCID for '{given} {family}' (or skip):", "identifier"
        )
        if not resp.get("skipped"):
            chosen = _bare_orcid(resp.get("value"))
    if not chosen:
        return None

    # An HITL-chosen ORCID is still verified before use (D5).
    verified = _verify_orcid(chosen, family, lookup_orcid_fn)
    return _bare_orcid(chosen) if verified is not None else None


def _find_or_draft_organization(state: CrateState, name: str, ror: str | None = None) -> str | None:
    """Return the entity_id of an Organization for ``name``, drafting one if absent.

    The ISA shape requires ``Person.affiliation`` to reference a
    ``schema:Organization`` — a literal string is a Violation (Issue #179). This
    find-or-drafts the Organization an author's affiliation should reference.

    De-dup by name: an existing in-state Organization with the same (stripped)
    ``name`` is reused so two authors sharing an affiliation yield ONE Organization
    (its ``ror`` is back-filled if this call carries one and it was missing).
    Otherwise a new one is minted via the pure :func:`draft_organization` drafter
    (never hand-rolled JSON-LD). D5-safe: a ``ror`` is set ONLY when supplied by
    the lookup — never fabricated — so the build resolves the Organization's @id to
    the ROR IRI when known, else a name-derived id. Returns ``None`` on an empty
    name (a name-less affiliation cannot become an Organization reference).
    """
    name = (name or "").strip()
    if not name:
        return None
    ror_value = (ror or "").strip()
    for org in state.list_entities("Organization"):
        if str(org.fields.get("name") or "").strip() == name:
            patch: dict[str, Any] = {}
            if ror_value and not org.fields.get("ror"):
                patch["ror"] = ror_value
            known_ror = ror_value or str(org.fields.get("ror") or "")
            if known_ror and not org.fields.get("url"):
                patch.update(_ror_website(known_ror))
            if patch:
                org.set_fields_from_dict(patch, source="lookup")
            return org.entity_id
    hints: dict[str, Any] = {"ror": ror_value} if ror_value else {}
    if ror_value:
        hints.update(_ror_website(ror_value))
    org = draft_organization(state, name, hints)
    return org.entity_id


def _ror_website(ror_id: str) -> dict[str, str]:
    """``{"url": ...}`` for a known ROR id, or ``{}`` — never raises.

    ROR states the organization's website on the record the id already names, so
    the profile's "organization SHOULD have a URL" is answerable from a registry
    rather than from a human. This is an exact by-id fetch, not a name search:
    the id is already established (ORCID's employment record, or a human), so no
    guess is involved and D5 holds.

    Failure is silent by design. An organization without a website is a
    recommendation-level finding; an author cascade that dies because ROR is
    briefly down is a broken build.
    """
    try:
        return {"url": url} if (url := (fetch_ror_by_id(ror_id) or {}).get("url")) else {}
    except Exception:
        logger.debug("ROR website lookup failed for %s", ror_id, exc_info=True)
        return {}


def _ensure_person_for_orcid(state: CrateState, orcid: str, data: dict) -> Entity:
    """Find-or-create a Person whose @id is the ORCID URL, with a verified ORCID."""
    bare = _bare_orcid(orcid)
    orcid_url = f"https://orcid.org/{bare}"
    existing = state.get_entity(orcid_url) or state.get_entity(bare)
    if existing is not None and existing.type == "Person":
        person = existing
    else:
        person = Entity(
            entity_id=orcid_url,
            type="Person",
            _provenance=EntityProvenance(created_by="lookup"),
        )
        state.add_entity(person)
    fields: dict[str, Any] = {"orcid": bare}
    given = data.get("givenName")
    family = data.get("familyName")
    name = data.get("name") or (f"{given or ''} {family or ''}".strip())
    if name:
        fields["name"] = name
    if given:
        fields["givenName"] = given
    if family:
        fields["familyName"] = family
    # Affiliation MUST reference a schema:Organization, not a literal string
    # (ISA shape, Issue #179). Find-or-draft the Organization (preferring the
    # ORCID-provided ROR so its @id resolves to the ROR IRI; D5: never fabricated)
    # and wire the Person's `affiliation` to that Organization's reference id —
    # the build's `_wire_reference` then resolves it to the Organization node.
    affiliation_name = data.get("affiliation_name")
    if affiliation_name:
        org_id = _find_or_draft_organization(state, affiliation_name, data.get("affiliation_ror"))
        if org_id is not None:
            fields["affiliation"] = {"@id": org_id}
    # The ISA profile asks every Person for a job title, and ORCID publishes one
    # on the same employment record the affiliation above comes from — so this
    # answers the finding from data already fetched, rather than by asking a
    # human for something a registry already states. Written only when ORCID has
    # it: many researchers leave the role blank, and an invented title is worse
    # than a missing one.
    job_title = str(data.get("job_title") or "").strip()
    if job_title:
        fields["jobTitle"] = job_title
    person.set_fields_from_dict(fields, source="lookup")
    person.set_field_status("orcid", "verified", "lookup")
    return person


def _synthesize_citation_author(state: CrateState, given: str, family: str) -> Entity:
    """Create (or reuse) the fallback #CitationAuthor_<Given>_<Family> Person."""
    parts = [p for p in (str(given or "").strip(), str(family or "").strip()) if p]
    label = "_".join(parts).replace(" ", "_") or "Unknown"
    entity_id = f"#CitationAuthor_{label}"
    existing = state.get_entity(entity_id)
    if existing is not None:
        return existing
    person = Entity(
        entity_id=entity_id,
        type="Person",
        _provenance=EntityProvenance(created_by="llm"),
    )
    person.set_fields_from_dict(
        {
            "name": " ".join(parts) or "Unknown Author",
            **({"givenName": given} if given else {}),
            **({"familyName": family} if family else {}),
        },
        source="llm",
    )
    state.add_entity(person)
    return person


def _ensure_publication(state: CrateState, doi: str, data: dict) -> Entity:
    """Find-or-create the ScholarlyArticle Publication for a DOI (no author wiring)."""
    bare_doi = data.get("identifier") or doi
    for pub in state.list_entities("Publication"):
        ident = str(pub.fields.get("identifier") or "")
        if ident and (ident == str(bare_doi) or _bare_orcid(ident) == _bare_orcid(doi)):
            return pub
        if pub.fields.get("doi") and _norm(pub.fields["doi"]) == _norm(doi):
            return pub
    hints: dict[str, Any] = {}
    if data.get("name"):
        hints["name"] = data["name"]
    if data.get("headline"):
        hints["headline"] = data["headline"]
    if data.get("datePublished"):
        hints["datePublished"] = data["datePublished"]
    if data.get("url"):
        hints["url"] = data["url"]
    return draft_publication(state, str(bare_doi), hints)


def _wire_author(pub: Entity, person: Entity) -> None:
    """Append a Person reference onto the publication's ``author`` list (deduped)."""
    refs = pub.fields.get("author") or []
    if not isinstance(refs, list):
        refs = [refs]
    ids = {(r.get("@id") if isinstance(r, dict) else r) for r in refs}
    if person.entity_id not in ids:
        refs = [*refs, {"@id": person.entity_id}]
    pub.fields["author"] = refs
    pub.set_field_status("author", "filled", "lookup")


def draft_publication_with_authors(
    state: CrateState,
    doi: str,
    human_interface: HumanInterface | None = None,
) -> dict[str, Any]:
    """Draft a publication and wire each author, harmonizing @ids to ORCIDs (#180).

    Looks the DOI up via Crossref, ensures the ``ScholarlyArticle`` exists in
    state, and for EACH author creates/reuses a ``Person`` wired as the article's
    ``author``. Each author's ``@id`` is resolved by this cascade (first hit wins):

    1. **Crossref ORCID** on the author — verified via :func:`lookup_orcid`
       (resolved family name must match) — used as ``https://orcid.org/<id>``.
    2. **In-crate Person** with a verified ORCID matching the author's family +
       given/initial (affiliation-preferred) — reused (e.g. citation
       'Fabian Wagenaars' → root 'F.M.A. Wagenaars').
    3. **Public ORCID search** (:func:`lookups.orcid.lookup_orcid_by_name`): a
       single STRONG (family + full given) match is verified and used; anything
       ambiguous (multiple candidates, or a weak/initial-only match) is escalated
       to HITL via ``human_interface`` (pick a candidate / paste an ORCID / skip).
    4. **Fallback**: a synthesized ``#CitationAuthor_<Given>_<Family>`` Person.

    D5: an ORCID from (1) or (3) — and an HITL-chosen one — is only attached after
    it resolves and the name roughly matches; (2) is already verified. HITL fires
    ONLY on genuine ambiguity, never when an author is confidently resolved or
    confidently absent.

    Args:
        state: The crate state to draft into.
        doi: The DOI to resolve (with or without a URL prefix).
        human_interface: HITL adapter injected by the engine; when ``None`` the
            search step cannot escalate, so an ambiguous author falls back to a
            synthesized id rather than guessing (D5).

    Returns:
        On success ``{"publication_id", "doi", "authors": [{name, person_id,
        orcid, resolution}], "hitl": int}``. On a DOI miss
        ``{"ok": False, "error": ...}``.
    """
    lookup = lookup_doi(doi)
    if not lookup.get("found"):
        return {"ok": False, "error": lookup.get("error", f"DOI '{doi}' not found")}

    data = lookup.get("data") or {}
    pub = _ensure_publication(state, doi, data)

    authors_out: list[dict[str, Any]] = []
    hitl_count = 0
    # Step (a) for every author at once, before the loop — see
    # `_prefetch_orcid_verifications`. The loop below is otherwise unchanged: the
    # cascade, its order, the HITL prompts and every write to CrateState still
    # happen here, one author at a time, on this thread.
    author_list = list(data.get("author", []))
    prefetched = _prefetch_orcid_verifications(author_list, lookup_orcid)
    for index, author in enumerate(author_list):
        given = author.get("givenName", "")
        family = author.get("familyName", "")
        affiliation = author.get("affiliation")
        resolution = "synthesized"
        person: Entity | None = None

        # (a) Crossref ORCID on the author, verified before the loop started.
        crossref_orcid = author.get("identifier")
        if crossref_orcid:
            verified = prefetched.get(index)
            if verified is not None:
                person = _ensure_person_for_orcid(state, crossref_orcid, verified)
                resolution = "crossref_orcid"

        # (b) In-crate Person with a verified ORCID.
        if person is None:
            match = _find_in_crate_person(state, given, family, affiliation)
            if match is not None:
                person = match
                resolution = "in_crate"

        # (c) Public ORCID search (auto-accept ONE strong match; else HITL).
        if person is None:
            prompts_before = _hitl_prompt_count(human_interface)
            searched = _resolve_via_search(
                given,
                family,
                affiliation,
                human_interface,
                lookup_orcid,
                lookup_orcid_by_name,
            )
            if _hitl_prompt_count(human_interface) > prompts_before:
                hitl_count += 1
            if searched:
                verified = _verify_orcid(searched, family, lookup_orcid)
                if verified is not None:
                    person = _ensure_person_for_orcid(state, searched, verified)
                    resolution = "orcid_search"

        # (d) Fallback synthesized author.
        if person is None:
            person = _synthesize_citation_author(state, given, family)
            resolution = "synthesized"

        _wire_author(pub, person)
        authors_out.append(
            {
                "name": f"{given} {family}".strip(),
                "person_id": person.entity_id,
                "orcid": _bare_orcid(person.fields.get("orcid")) or None,
                "resolution": resolution,
            }
        )

    return {
        "publication_id": pub.entity_id,
        "doi": data.get("identifier") or doi,
        "authors": authors_out,
        "hitl": hitl_count,
    }


def _hitl_prompt_count(human: HumanInterface | None) -> int:
    """Best-effort count of prompts a recording interface has made (for hitl stat)."""
    if human is None:
        return 0
    return len(getattr(human, "present_calls", []) or []) + len(
        getattr(human, "input_calls", []) or []
    )


# ---------------------------------------------------------------------------
# Publication resolution: title -> Crossref -> DOI -> publication (Issue #179)
#
# Closes the gap PR #217 deferred: a plan carries a publication *title* only
# (D5 — no DOI), but ISA REQUIRES a ScholarlyArticle with an identifier. This
# composite resolves the title to a DOI via a Crossref title-search, gated by a
# strict confidence rule so a DOI is only ever committed when it is genuinely the
# titled work — never fabricated (D5) — then reuses draft_publication_with_authors
# (#192) to build the ScholarlyArticle + authors from that DOI.
# ---------------------------------------------------------------------------

# D5 confidence gate. A Crossref title-search hit is only trusted as the DOI for a
# title when BOTH hold:
#   1. Crossref's relevance ``score`` clears _MIN_CROSSREF_SCORE, AND
#   2. the candidate's normalized title is a near-exact match for the query
#      (exact after normalization, OR a containment match with a high token
#      overlap) — so a high score on a *different* paper is rejected.
# Either alone is too weak (a high score can rank an unrelated work first; a title
# match alone says nothing about Crossref's own confidence), so the gate is the
# AND of the two. A miss creates NO entity and reports the reason — never a guess.
_MIN_CROSSREF_SCORE: float = 50.0
_MIN_TITLE_TOKEN_OVERLAP: float = 0.9


def _norm_title(text: Any) -> str:
    """Lowercase + collapse whitespace + drop punctuation (for title matching)."""
    lowered = str(text or "").lower()
    kept = "".join(c if (c.isalnum() or c.isspace()) else " " for c in lowered)
    return " ".join(kept.split())


def _title_overlap(query: str, candidate: str) -> float:
    """Fraction of the query's title tokens present in the candidate's (0..1).

    A symmetric near-exact signal: 1.0 on an exact normalized match, and high
    when one title is contained in / shares almost all tokens with the other
    (e.g. a trailing subtitle on the Crossref side). 0.0 when either is empty.
    """
    q = set(_norm_title(query).split())
    c = set(_norm_title(candidate).split())
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    # Token overlap against the *smaller* set, so a subtitle on one side does not
    # penalise an otherwise-exact match.
    return len(q & c) / min(len(q), len(c))


def _confident_match(title: str, candidates: list[dict] | tuple[dict, ...]) -> dict | None:
    """Return the single confident Crossref candidate for *title*, or None (D5).

    Confidence requires the top-scoring candidate to clear BOTH the Crossref
    score floor and the normalized-title near-exact threshold. Anything weaker —
    a low score, a title mismatch, or no candidates — yields ``None`` so the
    caller commits no DOI and fabricates nothing.
    """
    best: dict | None = None
    best_overlap = 0.0
    for cand in candidates:
        if float(cand.get("score") or 0.0) < _MIN_CROSSREF_SCORE:
            continue
        overlap = _title_overlap(title, cand.get("title", ""))
        if overlap >= _MIN_TITLE_TOKEN_OVERLAP and overlap > best_overlap:
            best, best_overlap = cand, overlap
    return best


def resolve_publication(
    state: CrateState,
    title: str,
    verify: bool | None = None,
) -> dict[str, Any]:
    """Resolve a publication TITLE to a DOI-backed ``ScholarlyArticle`` in ONE call.

    Closes the gap PR #217 deferred: a plan carries a publication *title* only
    (D5 — no DOI), but ISA REQUIRES a ``ScholarlyArticle`` to have an identifier
    and BASE requires the auto-wired root ``citation`` ``@id`` to be an absolute
    URI — both unreachable from a title alone without a DOI. This composite is the
    citation counterpart of :func:`resolve_compound`: the ONLY model-supplied
    input is the ``title``; the identifier comes straight from Crossref, gated so
    nothing is fabricated (D5):

    1. :func:`~lookups.crossref.search_works_by_title` runs a Crossref
       ``query.bibliographic`` search and returns candidate works ranked by
       Crossref's relevance ``score``.
    2. **D5 confidence gate** (:func:`_confident_match`): a candidate is accepted
       ONLY when it clears BOTH the Crossref score floor (``_MIN_CROSSREF_SCORE``)
       AND a normalized-title near-exact match (``_MIN_TITLE_TOKEN_OVERLAP``).
       A high score on a *different* paper, a weak score on the right title, or no
       candidate all fail the gate — in which case this returns
       ``{ok: False, reason: "no confident DOI match", title}`` and creates NO
       entity. A DOI is never invented from a title.
    3. On a confident match it delegates to
       :func:`draft_publication_with_authors` with the resolved DOI, which builds
       the ``ScholarlyArticle`` and wires every author as a ``Person`` (the ORCID
       cascade is already handled there).

    It is **idempotent**, keyed by the resolved DOI: re-running reuses the existing
    ``Publication`` (``draft_publication_with_authors`` find-or-creates it by DOI)
    rather than minting a duplicate, consistent with the other composites.

    This is called by code (materialize / guidance), not chosen by the weak model;
    it is registered four-place for consistency with :func:`resolve_compound`.

    Args:
        state: The crate state to resolve into.
        title: The publication title to resolve (e.g. from an extracted plan).
        verify: Reserved for parity with :func:`resolve_compound`; the DOI is
            implicitly verified by the Crossref resolution
            (:func:`draft_publication_with_authors` re-looks up the DOI, so an
            unresolvable DOI yields no publication). Accepted and ignored.

    Returns:
        On a confident match ``{"ok": True, "doi", "entity_id", "title",
        "score"}``. On no confident match ``{"ok": False, "reason": "no confident
        DOI match", "title"}`` (and no entity is created).
    """
    del verify  # parity-only with resolve_compound; see docstring.
    candidates = list(search_works_by_title(title) or [])
    match = _confident_match(title, candidates)
    if match is None:
        return {"ok": False, "reason": "no confident DOI match", "title": title}

    doi = str(match["doi"])
    drafted = draft_publication_with_authors(state, doi)
    # A DOI that survived the confidence gate but fails the Crossref re-lookup in
    # the drafter (e.g. a transient outage) yields no publication — surface it as
    # an unconfident result rather than a fabricated entity (D5).
    entity_id = drafted.get("publication_id")
    if not entity_id:
        return {"ok": False, "reason": "no confident DOI match", "title": title}

    return {
        "ok": True,
        "doi": drafted.get("doi", doi),
        "entity_id": entity_id,
        "title": title,
        "score": float(match.get("score") or 0.0),
    }


# ---------------------------------------------------------------------------
# Compound resolution: lookup -> draft -> verify (Issue #179, task 3)
# ---------------------------------------------------------------------------

# The order matters: CAS first, then PubChem CID — this mirrors the build's
# _identifier_pv path which emits [CAS, PubChem CID] identifier PropertyValues in
# exactly this order for a MolecularEntity. ``identifier`` is the generic field
# verify_identifier also accepts; we verify the concrete source fields instead.
_COMPOUND_IDENTIFIER_FIELDS: tuple[str, ...] = ("cas", "pubchem_cid")

# Lookup-data keys copied onto the drafted MolecularEntity. ``cas``/``pubchem_cid``
# are the verifiable identifiers; the rest are descriptive structure that needs no
# verification. ``iupac_name`` is intentionally NOT copied onto ``name`` (the
# user-supplied name wins) but is exposed in the return for the caller.
_COMPOUND_DATA_FIELDS: tuple[str, ...] = (
    "cas",
    "pubchem_cid",
    "smiles",
    "inchikey",
    "inchi",
    "formula",
    "mass",
    # ChEBI fallback identity, in context-declared keys (Issue #243): ``chebiId``
    # (the CURIE, schema:identifier) and ``sameAs`` (the dereferenceable ontology
    # IRI as an @id node). The legacy bare ``chebi_id`` / ``chebi_iri`` keys are
    # gone — they were absent from the @context and failed base-profile validation.
    "chebiId",
    "sameAs",
)

# Chemical-identity fields, in priority order, used to dedup a MolecularEntity by
# the resolved MOLECULE rather than by its (display) name (Issue #179). Two
# different names that resolve to the same compound (e.g. 'Indocyanine green' and
# 'ICG' -> same CID / InChIKey) must collapse to ONE node — minting a second
# ``chem_<name>`` node duplicates the molecule, and two names resolving to the
# SAME ``pubchem_cid`` would mint the SAME ``@id`` at build time, which
# ro-crate-py silently overwrites (data loss).
_COMPOUND_IDENTITY_FIELDS: tuple[str, ...] = ("pubchem_cid", "inchikey", "cas", "chebiId")


def _identity_key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return a ``(field, value)`` chemical-identity key from a resolved record.

    Picks the first non-empty field in :data:`_COMPOUND_IDENTITY_FIELDS` priority
    order (``pubchem_cid`` -> ``inchikey`` -> ``cas`` -> ``chebiId``). Returns
    ``None`` when the record carries no identity field (a name-only hit cannot be
    deduped by identity — it falls back to name-keyed reuse).
    """
    for field in _COMPOUND_IDENTITY_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            return field, str(value).strip()
    return None


def _find_entity_by_identity(state: CrateState, key: tuple[str, str]) -> Entity | None:
    """An existing MolecularEntity whose same identity field matches ``key``.

    Scans ``state.list_entities("MolecularEntity")`` for one whose value for the
    key's field equals the key's value, so a compound resolved under a second name
    reuses the node minted for the first instead of duplicating the molecule
    (Issue #179). Returns the first match, or ``None``.
    """
    field, value = key
    for entity in state.list_entities("MolecularEntity"):
        existing = entity.fields.get(field)
        if existing not in (None, "") and str(existing).strip() == value:
            return entity
    return None


def _append_alternate_name(entity: Entity, name: str) -> None:
    """Record ``name`` as a ``schema:alternateName`` on a reused entity.

    A no-op when ``name`` is empty, already the entity's primary ``name``, or
    already present in ``alternateName`` (deduped). Keeps the field a list so an
    entity resolved under several synonyms accumulates them — a molecule reused
    across two chemical names (:func:`resolve_compound`) and a cell line reused
    across two source spellings or its Cellosaurus label
    (:func:`resolve_cell_line`) both rely on that.
    """
    candidate = " ".join(str(name).split())
    if not candidate or candidate == str(entity.fields.get("name") or ""):
        return
    existing = entity.fields.get("alternateName") or []
    aliases = list(existing) if isinstance(existing, list) else [existing]
    if candidate in aliases:
        return
    aliases.append(candidate)
    entity.set_fields_from_dict({"alternateName": aliases}, source="lookup")


def _verify_compound_identifier(
    state: CrateState,
    entity: Entity,
    field: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one MolecularEntity identifier, trusting PubChem's own primary key.

    A PubChem CID is the **primary key of the PubChem record itself**: it is what
    the authoritative name→CID lookup just returned for this compound. PubChem's
    ``/compound/name`` endpoint (which ``verify_identifier`` re-queries) resolves
    *names* and CAS synonyms, but NOT a bare numeric CID — so routing the CID back
    through it always misses and D5 then clears the very identifier the authority
    handed us (Issue #261). Re-verifying the authority's own primary key against
    the wrong endpoint is the bug.

    So when the entity's ``pubchem_cid`` is exactly the CID the primary lookup
    returned (``data["pubchem_cid"]``), it is already confirmed by that
    resolution: we mark it ``verified`` directly instead of clearing it. Every
    other case — a different / hint-supplied / stale CID, or any non-CID field
    such as ``cas`` — falls through to the normal :func:`verify_identifier`, which
    still confirms against source and clears an unconfirmable value (D5 preserved,
    CAS verification unchanged).
    """
    if field.lower() == "pubchem_cid":
        lookup_cid = str(data.get("pubchem_cid") or "").strip()
        entity_cid = str(entity.fields.get(field) or "").strip()
        if lookup_cid and entity_cid and entity_cid == lookup_cid:
            entity.set_field_status(field, "verified", "lookup")
            if "pubchem" not in entity._provenance.lookups_used:
                entity._provenance.lookups_used.append("pubchem")
            return {
                "verified": True,
                "entity_id": entity.entity_id,
                "field": field,
                "message": (
                    f"Verified {field} for {entity.type} via pubchem "
                    "(authoritative name→CID lookup)"
                ),
                "suggested_fix": None,
            }
    return verify_identifier(state, entity.entity_id, field)


def resolve_compound(
    state: CrateState,
    name: str,
    hints: dict[str, Any] | None = None,
    verify: bool | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Resolve a chemical name to a verified ``MolecularEntity`` in ONE call.

    Fuses the recurring ``lookup_compound`` -> ``draft_molecular_entity`` ->
    ``verify_identifier`` sequence into a single deterministic composite (the
    chemistry counterpart of ``scaffold_isa_backbone``). The ONLY model-supplied
    input is the compound ``name`` (plus optional descriptive ``hints``); every
    identifier comes straight from PubChem/ChEBI, so nothing is fabricated (D5):

    1. :func:`~builder.tools.lookups.lookup_compound` resolves the chemical from
       its name (PubChem, then a ChEBI fallback). On a miss it returns
       ``{"ok": False, "error": ...}`` and creates no entity.
    2. :func:`~builder.tools.drafters.draft_molecular_entity` mints (or reuses) the
       ``MolecularEntity``, carrying the looked-up ``cas`` / ``pubchem_cid`` (and
       ``smiles`` / ``inchikey`` / …) as fields. At build time the shared
       ``_identifier_pv`` path turns ``cas`` + ``pubchem_cid`` into the
       ``[CAS, PubChem CID]`` identifier PropertyValues — this composite does not
       hand-roll that wiring.
    3. :func:`~builder.tools.verification.verify_identifier` confirms each minted
       identifier against its source (D5). ``verify_identifier`` **clears** any
       value that does not resolve, so a failed identifier never lingers on the
       entity as a fabricated id; the per-field verdicts are surfaced in the
       return value (``verified`` is the AND of all of them).

    **Performance (Issue #252).** A single resolve used to fan out to up to SIX
    PubChem round-trips — name->JSON + synonyms for the lookup, then a *fresh*
    re-resolution of the same compound for each of the CAS and PubChem-CID
    verifications — and under a concurrent burst a 429 storm multiplied the
    retry/backoff across all of them (30-66s per compound observed). Three
    in-process levers (all in :mod:`builder.tools._resolve_cache`) close that gap
    without weakening D5:

    * the lookup is warmed into a shared cache under the name AND the resolved
      CAS / ``CID <cid>`` alias keys, so the two verify re-resolutions read the
      already-fetched authoritative record instead of re-hitting PubChem
      (6 round-trips -> ~2; a repeat name -> 0);
    * a bounded client-side concurrency gate admits only a few resolves at once,
      so a burst does not all storm PubChem and trip its rate limiter;
    * a per-compound ``timeout`` bounds the lookup; on expiry it returns a
      graceful ``{"ok": False, ...}`` partial result rather than hanging ~60s.

    It is **idempotent**: the entity id is derived deterministically from the
    name, so an existing ``MolecularEntity`` for this name is reused (its
    descriptive fields refreshed) rather than duplicated, consistent with the
    other composites.

    Args:
        state: The crate state to resolve into.
        name: The compound name to resolve (e.g. ``"Silychristin A"``).
        hints: Optional extra descriptive field values for the MolecularEntity
            (e.g. ``{"description": ...}``). Looked-up identifier fields win over
            same-named hints so the verified source value is never overwritten.
        verify: When ``None``/``True`` (the default), verify the minted
            identifiers against source. Pass ``False`` to skip verification (the
            return then reports ``verified`` as ``None``); use only when you will
            verify later, never to attach an unverified id.
        timeout: Per-compound wall-clock budget (seconds) for the lookup. ``None``
            uses :data:`~builder.tools._resolve_cache.DEFAULT_RESOLVE_TIMEOUT`;
            ``<= 0`` disables the bound. On expiry the call returns a graceful
            ``{"ok": False, "error": "...timeout..."}`` and creates no entity.

    Returns:
        On success ``{"entity_id", "name", "identifiers": {cas?, pubchem_cid?,
        ...}, "verifications": [{field, verified, message}], "verified": bool |
        None, "source"}``. On a lookup miss / timeout ``{"ok": False, "error":
        ...}``.
    """
    budget = DEFAULT_RESOLVE_TIMEOUT if timeout is None else timeout

    # Canonical display name: strip + collapse internal whitespace (casing is
    # preserved for display; ``_make_entity_id`` lowercases for the id). Used for
    # entity-id derivation and the entity's ``name`` so the same compound under
    # different whitespace resolves to ONE MolecularEntity (idempotency).
    display_name = " ".join(str(name).split()) or name

    # Fast path: an already-resolved compound (any casing/whitespace) is served
    # straight from the shared in-process cache — no lookup, no throttle, no
    # timeout. This makes a repeated compound instant across resolve_compound
    # calls (Issue #252), the common case in a multi-compound run.
    cache_key = normalize_compound_name(name)
    cached = compound_cache.get(cache_key) if cache_key else None
    if cached is not None and cached.get("found"):
        lookup = cached
    else:
        # Bound the (network-bound) lookup and cap how many resolves run it at
        # once, so a slow compound returns gracefully and a burst does not
        # 429-storm PubChem.
        def _do_lookup() -> dict[str, Any]:
            with resolve_concurrency.slot():
                return lookup_compound(name)

        try:
            completed, lookup = run_with_timeout(_do_lookup, budget)
        except Exception as exc:  # a lookup that raised — fail gracefully
            logger.exception("resolve_compound lookup failed for '%s'", name)
            return {"ok": False, "error": f"Compound '{name}' lookup failed: {exc}"}
        if not completed:
            return {
                "ok": False,
                "error": (
                    f"Compound '{name}' resolution exceeded its {budget:g}s "
                    "timeout; skipped to avoid stalling the run."
                ),
            }

    if not lookup.get("found"):
        return {
            "ok": False,
            "error": lookup.get("error", f"Compound '{name}' not found"),
        }

    # Warm the shared cache (name + resolved CAS / CID alias keys) so the CAS and
    # PubChem-CID verifications below reuse this authoritative record instead of
    # firing two more PubChem round-trips (Issue #252). Keyed by normalized name,
    # so a later resolve of the same compound (any casing) is an instant hit.
    warm_compound_cache(name, lookup)

    data = lookup.get("data") or {}

    # Identifier/source fields win over caller hints so a verified value from the
    # source is never clobbered by a (possibly stale) hint.
    merged_hints: dict[str, Any] = dict(hints or {})
    # Tracked separately from the merged hints so the values the AUTHORITY
    # supplied can be recorded as such. Everything else in ``merged_hints`` is
    # the caller's (a plan value, a model-drafted hint) and keeps that origin.
    looked_up: dict[str, Any] = {}
    for key in _COMPOUND_DATA_FIELDS:
        value = data.get(key)
        if value not in (None, ""):
            merged_hints[key] = value
            looked_up[key] = value

    # Dedup by chemical IDENTITY first (Issue #179): two DIFFERENT names that
    # resolve to the SAME molecule (e.g. 'Indocyanine green' / 'ICG' -> same CID /
    # InChIKey) must collapse to ONE MolecularEntity. Minting a second
    # ``chem_<name>`` node duplicates the molecule, and two names resolving to the
    # same ``pubchem_cid`` would mint the SAME ``@id`` at build time (silently
    # overwritten by ro-crate-py). Reuse the node already minted for this molecule
    # under another name — refresh its looked-up fields and record the new name as
    # an ``alternateName`` — rather than its (different) name-derived id.
    identity_key = _identity_key(data)
    by_identity = _find_entity_by_identity(state, identity_key) if identity_key else None
    if by_identity is not None:
        entity = by_identity
        # Do NOT clobber the entity's existing display ``name`` (the first name
        # under which the molecule was minted) — keep it stable and record this
        # call's name as an additional alias so the synonym is not lost.
        refreshed = dict(merged_hints)
        refreshed.pop("name", None)
        entity.set_fields_from_dict(refreshed, source="lookup")
        _append_alternate_name(entity, display_name)
    else:
        # Idempotent: reuse the deterministically-keyed MolecularEntity if present,
        # refreshing its looked-up fields, rather than minting a duplicate. The id
        # is derived from the whitespace-normalized display name, so the same
        # compound under different spacing maps to ONE entity.
        entity_id = _make_entity_id("chem", display_name, merged_hints)
        existing = state.get_entity(entity_id)
        if existing is not None and existing.type == "MolecularEntity":
            entity = existing
            refreshed = {**merged_hints, "name": display_name}
            entity.set_fields_from_dict(refreshed, source="lookup")
        else:
            entity = draft_molecular_entity(state, display_name, merged_hints)
            # ``draft_molecular_entity`` stamps every hint it is handed with
            # ``source="llm"`` — correct for a drafted hint, wrong for the PubChem
            # response we just merged in. Re-record that subset as looked-up, so a
            # D5 audit reading ``_completion`` does not see four fabricated
            # structural identifiers per compound. Matches the two reuse branches
            # above, which already record these as ``"lookup"`` (#424).
            if looked_up:
                entity.set_fields_from_dict(looked_up, source="lookup")

    # Best-effort EPA DTXSID enrichment (#179). DTXSID is a first-class ISA-Tox
    # chemical identifier that the deterministic pipeline otherwise never
    # produces — ``lookup_dtxsid`` had NO pipeline caller (it was reachable only
    # from the legacy ReAct loop). Query CompTox by the strongest EXACT key
    # available (CAS -> InChIKey -> name) so the match is unambiguous, and store
    # the DTXSID only when the lookup returns one. D5-safe: the value comes
    # straight from CompTox (the authority), never fabricated. A miss or outage
    # is NON-FATAL — DTXSID is enrichment, not a precondition — so a CompTox
    # failure never sinks an already-resolved compound (mirrors the graceful
    # handling of the primary lookup above).
    if not entity.fields.get("dtxsid"):
        dtxsid_query = data.get("cas") or data.get("inchikey") or display_name
        try:
            dtxsid_hit = lookup_dtxsid(dtxsid_query)
        except Exception:
            logger.warning(
                "DTXSID lookup errored for '%s'; skipping (non-fatal)",
                dtxsid_query,
                exc_info=True,
            )
            dtxsid_hit = {}
        if dtxsid_hit.get("found"):
            dtxsid = (dtxsid_hit.get("data") or {}).get("dtxsid")
            if dtxsid:
                entity.set_fields_from_dict({"dtxsid": dtxsid}, source="lookup")

    identifiers = {
        key: data[key] for key in _COMPOUND_IDENTIFIER_FIELDS if data.get(key) not in (None, "")
    }
    # Surface the looked-up DTXSID in the return (pipeline provenance/logging).
    if entity.fields.get("dtxsid"):
        identifiers["dtxsid"] = entity.fields["dtxsid"]

    verifications: list[dict[str, Any]] = []
    do_verify = verify is None or verify
    if do_verify:
        for field in _COMPOUND_IDENTIFIER_FIELDS:
            if entity.fields.get(field) in (None, ""):
                continue
            # The CID is PubChem's own primary key for the record this lookup just
            # returned — confirm it against that authoritative answer rather than
            # re-querying the name endpoint (which cannot resolve a bare CID and so
            # wrongly clears it — Issue #261). Every other field/value still goes
            # through verify_identifier (clears the unconfirmable; D5 preserved).
            verdict = _verify_compound_identifier(state, entity, field, data)
            verifications.append(
                {
                    "field": field,
                    "verified": bool(verdict.get("verified")),
                    "message": verdict.get("message", ""),
                }
            )

    verified: bool | None
    if not do_verify:
        verified = None
    elif not verifications:
        verified = False
    else:
        verified = all(v["verified"] for v in verifications)

    return {
        "entity_id": entity.entity_id,
        "name": display_name,
        "identifiers": identifiers,
        "verifications": verifications,
        "verified": verified,
        "source": data.get("source", "pubchem"),
    }


# ---------------------------------------------------------------------------
# resolve_cell_line (#372)
# ---------------------------------------------------------------------------

# A catalogue NAME is a name, not an identifier, which is what makes the plan's
# `catalog_name` D5-clean. This refuses the one thing that is NOT a name: a
# Cellosaurus accession smuggled through a field the plan's identifier strip
# deliberately leaves alone. Case-insensitive and tolerant of the `CVCL-`
# spelling, because a model routing an id around the strip would not be expected
# to use the canonical form.
_ACCESSION_SHAPED = re.compile(r"^CVCL[_-]?\w+$", re.IGNORECASE)

# Hint keys refused outright before anything is written (D5). A caller — the
# ReAct model, or a plan field that slipped the strip — must never be able to
# hand this composite an accession: every id it commits has to come back from
# Cellosaurus in THIS call. `rrid` and `identifier` are in here because
# `_crate_mapping._mint_id` reads `accession`/`rrid` to build the cell line's
# resolvable `@id`, so a hint on either would fabricate the node's identity, and
# the profile model promotes the accession to `schema:identifier` itself.
_CELL_LINE_REFUSED_HINTS: frozenset[str] = frozenset(
    {"accession", "identifier", "rrid", "cellosaurus", "cellosaurus_accession", "@id", "id"}
)

# Fields copied from the `lookup_cell_line` record onto the CellLineSample.
# Deliberately short — see `_CELL_LINE_DROPPED_FIELDS` for what the record also
# offers and why each of those stays off the entity.
_CELL_LINE_DATA_FIELDS: tuple[str, ...] = ("alternateName", "url", "sameAs")

# Everything the Cellosaurus record carries that must NOT be persisted, each with
# the failure it causes. Written out rather than left implicit because every one
# of these looks harmless and two of them are silently destructive.
_CELL_LINE_DROPPED_FIELDS: tuple[tuple[str, str], ...] = (
    (
        "identifier",
        "lookup_cellosaurus sets `identifier` to the record's full URL, not the "
        "bare accession. Persisting it would make verify_all_identifiers re-query "
        "Cellosaurus with that URL percent-encoded into the cell-line path, miss, "
        "and POP the field — D5 destroying a value the authority actually gave us. "
        "The profile model derives schema:identifier from `accession` anyway.",
    ),
    (
        "name",
        "The entity's `name` is the name as the SOURCE DOCUMENTS word it. The "
        "Cellosaurus label is a different string for the same line and belongs on "
        "alternateName; clobbering `name` would rewrite the study's own wording.",
    ),
    (
        "taxonomicRange",
        "A DefinedTerm NODE object. _scalar_props emits it inline, producing an "
        "un-flattened nested entity that fails base conformance. Promoting these "
        "to real DefinedTerm entities is its own lane.",
    ),
    ("disease", "A list of DefinedTerm node objects — same un-flattening failure."),
    ("anatomicalSite", "A DefinedTerm node object — same un-flattening failure."),
    (
        "donorSex",
        "Not a term in the crate's JSON-LD context, so emitting it fails base "
        "conformance ('not allowed in the compacted JSON-LD context'). It belongs "
        "in a Sample characteristic, which needs the additionalProperty lane.",
    ),
    ("donorAge", "Not a context term — same base-conformance failure as donorSex."),
    (
        "category",
        "Cellosaurus's own line-category vocabulary ('Cancer cell line', …), not a "
        "schema.org value. Same characteristic lane as donorSex/donorAge.",
    ),
)


def _cell_line_candidates(display_name: str, catalog_name: str | None) -> list[tuple[str, str]]:
    """The ``(tier, query)`` name candidates to try against Cellosaurus, in order.

    The full normalized display name first — it is what the documents call the
    line, so a hit on it is the strongest claim available. Then ``catalog_name``,
    the short catalogue name the extract plan may report ("FRTL-5" for "FRTL-5
    TPO-overexpressing rat thyroid follicular cells"), which is the ONLY way a
    descriptive phrase ever reaches its record: the D5 gate in
    :func:`~builder.tools.lookups.lookup_cell_line_by_name` requires an exact
    match against a primary identifier or synonym and is not relaxed here.

    Two candidates are refused rather than queried: one shaped like an accession
    (see :data:`_ACCESSION_SHAPED`), and one that merely re-spells the display
    name, which would spend a second network round-trip to ask the same question.
    """
    candidates: list[tuple[str, str]] = []
    if display_name.strip():
        candidates.append(("exact", display_name))
    catalog = " ".join(str(catalog_name or "").split())
    if not catalog or _ACCESSION_SHAPED.match(catalog):
        return candidates
    if any(catalog.casefold() == query.casefold() for _tier, query in candidates):
        return candidates
    candidates.append(("catalog", catalog))
    return candidates


def _find_cell_line_by_accession(state: CrateState, accession: str) -> Entity | None:
    """An existing ``CellLineSample`` already carrying ``accession``, or ``None``.

    The cell-line counterpart of :func:`_find_entity_by_identity` (Issue #179).
    One line routinely appears under two names in one submission — the shipped
    S-VHPS22 fixture calls it "…rat thyroid follicular cells" in the README and
    "…overexpressing cells" in both CSVs — and each name mints its own
    ``cell_<name>`` id. Once the accession drives the ``@id``
    (``_crate_mapping._mint_id``), two such entities collide onto ONE node at
    build time and ro-crate-py silently keeps whichever was written last.
    """
    target = accession.strip()
    for entity in state.list_entities("CellLineSample"):
        existing = entity.fields.get("accession")
        if existing not in (None, "") and str(existing).strip() == target:
            return entity
    return None


def _search_cell_line_accession(
    candidates: list[tuple[str, str]], budget: float
) -> tuple[str, str, str]:
    """Step 1: the first candidate name with a confident Cellosaurus accession.

    Returns ``(accession, match_tier, query)`` — all empty strings when no
    candidate resolved. Each candidate goes through the UNMODIFIED exact+unique
    gate, so a "catalog"-tier hit is not a weaker claim about the record, only a
    weaker claim that the record is the line the documents mean.

    A transient outage or a timeout **stops the walk** rather than falling
    through to the next candidate: the remaining candidates are weaker, and
    committing one because the strongest could not be asked would turn an outage
    into a quietly different answer.
    """
    for tier, query in candidates:

        def _do_lookup(query: str = query) -> dict[str, Any]:
            with resolve_concurrency.slot():
                return lookup_cell_line_by_name(query)

        try:
            completed, hit = run_with_timeout(_do_lookup, budget)
        except Exception:  # a lookup that raised — enrichment, never fatal
            logger.exception("resolve_cell_line name search failed for %r", query)
            break
        if not completed:
            logger.warning(
                "resolve_cell_line name search for %r exceeded its %gs timeout; "
                "continuing without an accession",
                query,
                budget,
            )
            break
        if hit.get("found"):
            accession = str((hit.get("data") or {}).get("accession") or "").strip()
            if accession:
                return accession, tier, query
        elif hit.get("transient"):
            break
    return "", "", ""


def resolve_cell_line(
    state: CrateState,
    name: str,
    hints: dict[str, Any] | None = None,
    catalog_name: str | None = None,
    verify: bool | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Resolve a cell-line name to a ``CellLineSample`` + Cellosaurus accession.

    The cell-line counterpart of :func:`resolve_compound`, and the deterministic
    arm's ONLY name→Cellosaurus path: before this existed the arm minted every
    cell line through ``draft_cell_line_sample`` with empty hints, so
    ``lookup_cell_line_by_name`` had no caller outside the ReAct arm and no
    default-arm crate ever carried an accession (#372, #386).

    Two steps, both against Cellosaurus:

    1. :func:`~builder.tools.lookups.lookup_cell_line_by_name` on each candidate
       name in turn (see :func:`_cell_line_candidates`) → a bare ``CVCL_*``
       accession, committed only on that lookup's exact+unique D5 gate.
    2. :func:`~builder.tools.lookups.lookup_cell_line` on that accession → the
       record. **Step 2 IS the verification.**
       ``_select_verifier("CellLineSample", "accession")`` already resolves to
       exactly this function, so following it with ``verify_identifier`` would
       re-issue the same ``lru_cache``d call to reach the same verdict; the
       status is set directly instead, mirroring
       :func:`_verify_compound_identifier`. A *transient* step-2 failure keeps
       the accession unverified; a *definitive* step-2 miss clears it, because a
       search that produced an accession the record endpoint denies is not
       evidence of anything.

    **A miss is NOT a failure.** The one deliberate divergence from
    :func:`resolve_compound`, which returns ``{"ok": False}`` and mints nothing.
    A ``CellLineSample`` carrying only a name is a valid ISA Sample and is
    exactly what the arm produced before this composite existed, so refusing to
    mint would delete the cell line from every crate whose line is not
    catalogued — taking the ``CellCulture.cell_line`` input and the Study's
    ``cell_lines`` mention with it. **Always mint; the accession is enrichment.**
    There is therefore no ``ok`` key: read ``accession``/``match`` instead.

    Idempotency is handled HERE, not in the drafter (which stays the plain
    ReAct-callable primitive): ``draft_cell_line_sample`` is not idempotent and
    ``state.add_entity`` silently *replaces* under ``CellLineSample:<eid>``,
    which would wipe the accession, its verified status and its provenance on a
    re-resolve. Reuse is tried by accession first (:func:`_find_cell_line_by_accession`)
    and then by the deterministic name-derived id.

    Args:
        state: The crate state to resolve into.
        name: The cell-line name **as the source documents word it** — kept
            verbatim as the entity's ``name``. The Cellosaurus label, when it
            differs, is recorded as an ``alternateName``, never as the name.
        hints: Optional extra descriptive field values (e.g. ``passage``).
            Identifier-bearing keys are refused (:data:`_CELL_LINE_REFUSED_HINTS`)
            — an accession may only come back from Cellosaurus in this call.
        catalog_name: Optional short catalogue name for the same line, e.g.
            ``"FRTL-5"`` for a descriptive phrase naming it. A catalogue *name*
            is a name, so it is D5-clean; one shaped like an accession is refused.
        verify: When ``None``/``True`` (the default), run step 2. Passing
            ``False`` skips it — and with it the record, since step 2 is the only
            fetch: the accession is then kept unverified and no enrichment
            fields land. ``verified`` is reported as ``None``.
        timeout: Per-request wall-clock budget (seconds). ``None`` uses
            :data:`~builder.tools._resolve_cache.DEFAULT_RESOLVE_TIMEOUT`;
            ``<= 0`` disables the bound. On expiry the cell line is still minted,
            without an accession.

    Returns:
        ``{"entity_id", "name", "accession", "match": "exact"|"catalog"|"none",
        "query", "verifications": [{field, verified, message}], "verified":
        bool | None, "source"}``. ``match`` is the tier of the candidate that
        hit and ``query`` the string that hit it, so a caller (interactive build)
        can surface a ``catalog``-tier commit for confirmation — that prompt is
        deliberately NOT here and NOT in ``run_pipeline``, whose contract is to
        stay non-blocking so ``--arch pipeline`` and the corpus eval run headless.
    """
    budget = DEFAULT_RESOLVE_TIMEOUT if timeout is None else timeout

    # Canonical display name: strip + collapse internal whitespace. Casing is
    # preserved for display; `_make_entity_id` lowercases for the id. Keeps
    # "Hep G2" and "Hep  G2" one entity, and is what gets searched.
    display_name = " ".join(str(name).split()) or name

    accession, match, query = _search_cell_line_accession(
        _cell_line_candidates(display_name, catalog_name), budget
    )

    # --- step 2: the record IS the verification -----------------------------
    record: dict[str, Any] = {}
    verifications: list[dict[str, Any]] = []
    do_verify = verify is None or verify
    verified: bool | None = None
    denied_accession = ""
    if accession and do_verify:

        def _do_record() -> dict[str, Any]:
            with resolve_concurrency.slot():
                return lookup_cell_line(accession)

        # A raised lookup and an expired budget are both "could not ask", which
        # is a transient verdict — the accession stays, unverified. Only a
        # definitive answer from the endpoint may clear it.
        confirmation: dict[str, Any] = {"found": False, "transient": True}
        try:
            completed, fetched = run_with_timeout(_do_record, budget)
            if completed and isinstance(fetched, dict):
                confirmation = fetched
        except Exception:
            logger.exception("resolve_cell_line record fetch failed for %r", accession)

        if confirmation.get("found"):
            record = confirmation.get("data") or {}
            verified = True
            message = f"Verified accession for CellLineSample via cellosaurus ({accession})"
        elif confirmation.get("transient"):
            verified = False
            message = (
                f"accession {accession} could not be confirmed right now — cellosaurus "
                "is temporarily unavailable; value kept."
            )
        else:
            # The name search handed us an accession the record endpoint denies.
            # Nothing about that pair is trustworthy, so drop it rather than
            # publish a CVCL id that does not dereference (D5).
            message = (
                f"accession {accession} did not resolve at cellosaurus; cleared "
                "rather than published as an unresolvable id."
            )
            denied_accession = accession
            accession, match, query = "", "none", ""
            verified = False
        verifications.append({"field": "accession", "verified": bool(verified), "message": message})

    # --- fields -------------------------------------------------------------
    merged_hints: dict[str, Any] = {
        key: value for key, value in (hints or {}).items() if key not in _CELL_LINE_REFUSED_HINTS
    }
    # Tracked separately so the AUTHORITY's values can be recorded as such; the
    # rest of `merged_hints` is the caller's and keeps that origin.
    looked_up: dict[str, Any] = {}
    for key in _CELL_LINE_DATA_FIELDS:
        value = record.get(key)
        if value not in (None, ""):
            looked_up[key] = value
    if accession:
        looked_up["accession"] = accession
    merged_hints.update(looked_up)

    # --- mint or reuse ------------------------------------------------------
    by_accession = _find_cell_line_by_accession(state, accession) if accession else None
    if by_accession is not None:
        entity = by_accession
        # Keep the name the line was first minted under and record this call's
        # name as an alias, so the second spelling in the submission is not lost.
        refreshed = dict(merged_hints)
        refreshed.pop("name", None)
        entity.set_fields_from_dict(refreshed, source="lookup")
        _append_alternate_name(entity, display_name)
    else:
        entity_id = _make_entity_id("cell", display_name, merged_hints)
        existing = state.get_entity(entity_id)
        if existing is not None and existing.type == "CellLineSample":
            entity = existing
            entity.set_fields_from_dict({**merged_hints, "name": display_name}, source="lookup")
        else:
            entity = draft_cell_line_sample(state, display_name, merged_hints)
            # The drafter stamps every hint it is handed `source="llm"` — right
            # for a drafted hint, wrong for the Cellosaurus record. Re-record
            # that subset as looked-up so a D5 audit reading `_completion` does
            # not see a fabricated accession on every cell line (#424).
            if looked_up:
                entity.set_fields_from_dict(looked_up, source="lookup")

    # The Cellosaurus label is an alias for the same line, never the entity's
    # name (which stays the documents' wording). Skipped when the record carried
    # no name-list: `lookup_cellosaurus` then echoes the accession back as the
    # name, and publishing "CVCL_0265" as an alternateName would read as a name
    # the line is actually known by.
    label = str(record.get("name") or "")
    if label != accession:
        _append_alternate_name(entity, label)

    if accession:
        if verified:
            entity.set_field_status("accession", "verified", "lookup")
        if "cellosaurus" not in entity._provenance.lookups_used:
            entity._provenance.lookups_used.append("cellosaurus")
    elif denied_accession and str(entity.fields.get("accession") or "") == denied_accession:
        # A re-resolve whose step 2 came back definitively 404: an id the record
        # endpoint denies must not survive on the entity just because an earlier
        # call wrote it. Mirrors `verify_identifier`'s pop-on-definitive-miss —
        # the return would otherwise report no accession while the crate still
        # published one, and `_mint_id` would still key the node on it.
        entity.fields.pop("accession", None)
        entity.set_field_status("accession", "missing", "lookup")

    return {
        "entity_id": entity.entity_id,
        "name": display_name,
        "accession": accession,
        "match": match or "none",
        "query": query,
        "verifications": verifications,
        "verified": verified,
        "source": "cellosaurus",
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("scaffold_isa_backbone", scaffold_isa_backbone, takes_state=True)
TOOL_REGISTRY.register("draft_process_chain", draft_process_chain, takes_state=True)
TOOL_REGISTRY.register("resolve_compound", resolve_compound, takes_state=True)
TOOL_REGISTRY.register("resolve_cell_line", resolve_cell_line, takes_state=True)
TOOL_REGISTRY.register("resolve_publication", resolve_publication, takes_state=True)
TOOL_REGISTRY.register("materialize_aop_subgraph", materialize_aop_subgraph, takes_state=True)
TOOL_REGISTRY.register("link_assay_to_key_event", link_assay_to_key_event, takes_state=True)
TOOL_REGISTRY.register(
    "draft_publication_with_authors",
    draft_publication_with_authors,
    takes_state=True,
    takes_human=True,
)


# ---------------------------------------------------------------------------
# Deterministic wiring backstop (#438 / #273)
# ---------------------------------------------------------------------------
# A MolecularEntity or CellLineSample is minted by whichever path resolved it,
# but it stays an ORPHAN until something references it: ISA forbids a compound as
# a process object, so a compound reaches the experiment only through the
# Exposure's `chemicals` field (which the build turns into the condition table's
# schema:about + the compound column's valueUrl). The pipeline arm wires this at
# one moment, conditional on holding the ids then; the ReAct arm relies on the
# model remembering to pass `hints={'chemicals': ...}`. Neither is a backstop, and
# a real 22-compound crate shipped with `chemicals=None` on every process.
#
# This is the OBVIOUS half of that problem, so it is done without asking: one
# Exposure and N compounds that nothing references have exactly one sensible
# wiring. Anything genuinely ambiguous — two Exposures, so which compound went
# where — is refused and reported for a human, never guessed.

# (entity type, process type, field on the process, multi-valued, field on the
# ISA container to fall back to). The process is the RICHER link — it says the
# compound was actually dosed — but it is not the only valid one: a compound
# belongs to the Study via schema:mentions whether or not an experiment was ever
# recorded. A crate with no process chain still has 21 compounds that must be
# reachable, so falling back to the container is not a consolation prize, it is
# the correct statement when there is no process to point at.
_DOMAIN_WIRING: tuple[tuple[str, str, str, bool, str], ...] = (
    ("MolecularEntity", "Exposure", "chemicals", True, "chemicals"),
    ("CellLineSample", "CellCulture", "cell_line", False, "cell_lines"),
)

# Where a domain entity attaches when no suitable process exists. The STUDY,
# specifically: only `_crate_mapping._STUDY_MENTION_FIELDS` maps `chemicals` /
# `cell_lines` onto schema:mentions / biologicalModels. The Assay's mention map
# carries key-event fields only, so setting `chemicals` there is silently dropped
# at build time — the state looks wired and the exported crate is not.
_CONTAINER_FALLBACK: tuple[str, ...] = ("Study",)


def _is_referenced(state: CrateState, target_id: str) -> bool:
    """True when any OTHER entity references *target_id* in any of its fields."""
    for ent in state.list_entities():
        if ent.entity_id == target_id:
            continue
        for value in ent.fields.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                ref = item.get("@id") if isinstance(item, dict) else item
                if isinstance(ref, str) and ref.lstrip("#") == target_id.lstrip("#"):
                    return True
    return False


# The fields through which a process actually CONSUMES or PRODUCES something.
# Deliberately not every reference field: a Study listing a cell line under
# `cell_lines` says the study is about it, and a placeholder Sample whose
# `derives_from` points at it says where it came from. Neither is an experiment
# using it, and treating them as equivalent is what let a crate ship with its
# one cell line attached to nothing that ever cultured it.
_PROCESS_IO_FIELDS: tuple[str, ...] = (
    "object",
    "input",
    "samples",
    "cell_line",
    "chemicals",
    "result",
    "output",
)


def _is_consumed_by_process(state: CrateState, target_id: str) -> bool:
    """True when some LabProcess takes *target_id* as an input or output.

    The stricter half of :func:`_is_referenced`. A domain entity earns its place
    in the provenance graph by being used by a process — mentions and derivation
    notes describe it, but only a process records that the experiment touched it.
    """
    wanted = target_id.lstrip("./").lstrip("#")
    target = state.get_entity(target_id)
    target_type = str(target.type) if target is not None else ""
    for proc in state.list_entities("LabProcess"):
        # Narrow to the build's field ONLY for the (entity type, process type)
        # pair that has one. A compound under an Exposure's `input` is read by
        # nothing — the ISA shape allows only File/Sample/BioSample there, so
        # `_build_process` takes compounds from `chemicals` — and counting it as
        # consumed makes this backstop skip the exact case it exists for.
        #
        # The process type is half of that key and cannot be dropped. A
        # CellLineSample has a build home under a CellCulture (`cell_line`), but
        # under an Exposure it is an ordinary `samples` participant that the
        # build does read. Narrowing it everywhere marked it permanently loose,
        # so `wire_unreferenced_domain_entities` re-wired it on every call —
        # which is a mutation, and cost the export its idempotency.
        process_type = str(
            proc.fields.get("process_type") or proc.fields.get("additionalType") or ""
        )
        home = _BUILD_HONOURED_HOMES.get((target_type, process_type))
        fields = (home,) if home else _PROCESS_IO_FIELDS
        for field in fields:
            value = proc.fields.get(field)
            if value is None:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                ref = item.get("@id") if isinstance(item, dict) else item
                if isinstance(ref, str) and ref.lstrip("./").lstrip("#") == wanted:
                    return True
    return False


def wire_unreferenced_domain_entities(state: CrateState) -> dict[str, Any]:
    """Attach domain entities that nothing references to the process that used them.

    Idempotent and deterministic: it only ever ADDS references for entities that
    are currently referenced by nothing, and unions with whatever the field
    already holds, so re-running changes nothing.

    Returns:
        ``{"wired": {field: [entity_id, ...]}, "ambiguous": [reason, ...]}`` —
        ``ambiguous`` names each case a human has to settle (no process of the
        required type, or more than one, so the pairing cannot be derived).
    """
    from builder.tools.management import set_fields

    wired: dict[str, list[str]] = {}
    ambiguous: list[str] = []

    processes = state.list_entities("LabProcess")
    for entity_type, process_type, field, multi, container_field in _DOMAIN_WIRING:
        # "Loose" means NO PROCESS USES IT, not "nothing mentions it". A cell
        # line listed under the Study's `cell_lines` and pointed at by a
        # placeholder Sample's `derives_from` satisfied the old reference test
        # while the CellCulture that supposedly grew it consumed neither — so
        # the backstop skipped exactly the case it exists for. Wiring is
        # idempotent and unions with what is already there, so re-stating a
        # container mention costs nothing when there is no process to point at.
        loose = [
            e.entity_id
            for e in state.list_entities(entity_type)
            if not _is_consumed_by_process(state, e.entity_id)
        ]
        if not loose:
            continue
        targets = [
            p
            for p in processes
            if str(p.fields.get("process_type") or p.fields.get("additionalType") or "")
            == process_type
        ]
        if len(targets) > 1:
            # Two Exposures and N loose compounds: which went where is a real
            # question, not a derivable fact. Refuse and report for a human.
            ambiguous.append(
                f"{len(loose)} unreferenced {entity_type} but {len(targets)} "
                f"{process_type} process(es) — cannot derive which belongs to which"
            )
            continue
        if targets:
            target, field = targets[0], field
        else:
            # No process to attach to — say what IS true: the Study mentions it.
            container = next(
                (c for kind in _CONTAINER_FALLBACK for c in state.list_entities(kind)),
                None,
            )
            if container is None:
                ambiguous.append(
                    f"{len(loose)} unreferenced {entity_type} and no {process_type} "
                    "process or Study to attach them to"
                )
                continue
            target, field, multi = container, container_field, True
        existing = target.fields.get(field)
        current = [
            (v.get("@id") if isinstance(v, dict) else v)
            for v in (existing if isinstance(existing, list) else [existing])
            if v
        ]
        merged = [*current, *[c for c in loose if c not in current]]
        value: Any = merged if multi else merged[0]
        try:
            set_fields(state, target.entity_id, {field: value})
        except Exception as exc:  # noqa: BLE001 — wiring never breaks an export
            logger.warning("wiring %s onto %s failed: %s", field, target.entity_id, exc)
            continue
        wired.setdefault(field, []).extend(loose)

    return {"wired": wired, "ambiguous": ambiguous}
