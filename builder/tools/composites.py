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

from typing import Any

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.drafters import draft_assay, draft_investigation, draft_study


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
TOOL_REGISTRY.register(
    "materialize_aop_subgraph", materialize_aop_subgraph, takes_state=True
)
