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
from typing import Any

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.drafters import (
    VALID_PROCESS_TYPES,
    draft_assay,
    draft_investigation,
    draft_process,
    draft_sample,
    draft_study,
)

logger = logging.getLogger(__name__)


def _first_of_type(state: CrateState, type_name: str) -> Entity | None:
    """Return the first existing entity of *type_name*, or None."""
    return next((e for e in state.list_entities() if e.type == type_name), None)


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

    def _ensure(type_name: str, make) -> Entity:
        existing = _first_of_type(state, type_name)
        if existing is not None:
            reused.append(type_name)
            return existing
        created.append(type_name)
        return make()

    inv = _ensure("Investigation", lambda: draft_investigation(state, investigation or {}))
    study_entity = _ensure("Study", lambda: draft_study(state, inv.entity_id, study or {}))
    assay_entity = _ensure("Assay", lambda: draft_assay(state, study_entity.entity_id, assay or {}))

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
    validate: bool | None = None,
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
        validate: When true, also run a full ``build_and_validate`` and return it
            under ``"validation"`` (weak models may pass ``None`` for this
            optional arg; that is treated as false).

    Returns:
        ``{"assay_id", "process_ids", "steps", "synthesized"}`` — the ordered
        process entity ids, a per-step summary (``{process_id, process_type,
        object, result}``), and the list of placeholder entity ids synthesized.
        ``"validation"`` is added when ``validate`` is true.

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
            f"draft_process_chain assay_id must be an Assay; {assay_id!r} is a "
            f"{assay.type}."
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
        # dangle into a tox Violation. ---
        outputs = _explicit_ids(step, "result", "output")
        if not outputs:
            placeholder = _synthesize_output(
                state, proc, ptype, draft_sample, draft_file
            )
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

    if validate:
        from builder.tools.validation import build_and_validate

        result["validation"] = build_and_validate(state)

    return result


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
    # Data producer: a placeholder result File.
    name = f"{proc.entity_id}_result.csv"
    file_id = f"file_{_slug(name)}"
    existing = state.get_entity(file_id)
    if existing is not None:
        return existing
    return draft_file_fn(
        state,
        name=name,
        path=f"data/{name}",
        role="processed_data",
    )


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
    return draft_file_fn(state, name=name, path=f"data/{name}", role="raw_data")


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
    for ev in data.get("events", []):
        if _materialize_aop_node(state, ev) is not None:
            events += 1

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
        "relationships": relationships,
        "wired_to_study": wired_to_study,
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("scaffold_isa_backbone", scaffold_isa_backbone, takes_state=True)
TOOL_REGISTRY.register("draft_process_chain", draft_process_chain, takes_state=True)
TOOL_REGISTRY.register(
    "materialize_aop_subgraph", materialize_aop_subgraph, takes_state=True
)
