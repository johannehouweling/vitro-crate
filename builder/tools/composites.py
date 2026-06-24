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

from builder.state import CrateState, Entity
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
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("scaffold_isa_backbone", scaffold_isa_backbone, takes_state=True)
