"""The gap engine (#179, Stage C of the hybrid ISA-Tox build loop).

``assess_gaps(state) -> GapReport`` unifies the three assessors into ONE
prioritized gap list the guidance agent consumes:

- **SHACL** — the in-memory three-pass validation (:func:`build_and_validate`).
  REQUIRED issues become ``MUST`` gaps; RECOMMENDED issues become ``SHOULD``
  gaps; OPTIONAL issues become ``MAY`` gaps.
- **MIT** — the OECD/ToxTemp coverage report. Every unfilled MIT parameter is a
  domain-enrichment gap: ``SHOULD`` for a core (``additional: false``) parameter,
  ``MAY`` for an ``additional: true`` one.
- **FAIR** — the crate-intrinsic FAIR/DSM indicators. Each *failing* indicator
  becomes a gap: ``SHOULD`` for an essential indicator, ``MAY`` for an important
  / nice-to-have one.

This module is **pure, deterministic, and idempotent**: NO LLM, NO network, and
it never mutates ``state`` (AGENTS.md §14, D5). It is a **library function**
consumed by the spine / guidance code — it is *not* registered as a four-place
LLM tool.

The single non-trivial classification is ``auto_fixable``: a SHACL MUST gap is
auto-fixable iff the deterministic repair loop (:mod:`builder.tools.repair`,
``fix_required_issues``) can resolve it from state alone. We decide this by
re-using the *same* predicates the repair rules use, so the gap engine and the
repair loop can never drift on what "deterministically fixable" means.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from builder.state import CrateState

logger = logging.getLogger(__name__)

Tier = Literal["MUST", "SHOULD", "MAY"]
Source = Literal["shacl", "mit", "fair"]

# Path to the MIT YAML (shared with builder.tools.mit_assessment).
MIT_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "mit" / "invitro_tox.yaml"


@dataclass(frozen=True)
class Gap:
    """One actionable, prioritized gap unified across the three assessors.

    Attributes:
        tier: Requirement tier — ``"MUST"`` (blocking, SHACL REQUIRED),
            ``"SHOULD"`` (recommended), or ``"MAY"`` (optional).
        source: Which assessor produced it — ``"shacl"`` / ``"mit"`` / ``"fair"``.
        entity_id: The affected entity (``None`` for a crate-level gap).
        entity_type: The affected entity's type, when known.
        property: The missing field / parameter (a property IRI for SHACL, a
            ``crate_slot`` field for MIT), or ``None``.
        message: Human-readable description of what's missing and why it matters.
        suggestion: A concrete hint — the expected propertyID IRI / ontology
            term / expected type from the profile, or the parameter description.
        fix_hint: How to resolve — a deterministic tool name
            (``"fix_required_issues"``), ``"draft"``, or ``"ask-user"``.
        auto_fixable: ``True`` iff ``fix_required_issues`` can resolve it
            deterministically from state alone.
    """

    tier: Tier
    source: Source
    entity_id: str | None
    entity_type: str | None
    property: str | None
    message: str
    suggestion: str | None
    fix_hint: str | None
    auto_fixable: bool


@dataclass
class GapReport:
    """The unified, prioritized gap list plus headline assessor summaries.

    Attributes:
        gaps: All gaps sorted MUST -> SHOULD -> MAY, with a stable secondary
            order by ``(source, entity_id, property)``.
        conformance: ``{base, isa, tox}`` REQUIRED-level pass/fail from
            ``build_and_validate`` (best-effort; ``{}`` on a validation error).
        mit_overall: ``MITReport.overall_score`` (0.0-1.0).
        fair_summary: ``{dsm_level, indicators_passed, indicators_failed}``.
        counts: ``{must_open, should_open, may_open}``.
    """

    gaps: list[Gap] = field(default_factory=list)
    conformance: dict[str, bool] = field(default_factory=dict)
    mit_overall: float = 0.0
    fair_summary: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Severity / tier mapping
# ---------------------------------------------------------------------------

_SEVERITY_TO_TIER: dict[str, Tier] = {
    "required": "MUST",
    "recommended": "SHOULD",
    "optional": "MAY",
}

# Stable tier rank for the primary sort.
_TIER_RANK: dict[Tier, int] = {"MUST": 0, "SHOULD": 1, "MAY": 2}


def _local_name(iri: str | None) -> str:
    """Local part of a property IRI (after the last ``/`` or ``#``)."""
    if not iri:
        return ""
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


# ---------------------------------------------------------------------------
# SHACL gaps
# ---------------------------------------------------------------------------


def _shacl_auto_fixable(state: CrateState, issue: dict[str, Any]) -> bool:
    """Whether ``fix_required_issues`` can deterministically clear this issue.

    Re-uses the repair loop's *own* rule predicates so the two cannot drift on
    what "deterministically fixable" means: a rule that ``applies`` to the issue
    AND whose repair would not decline (its target is unambiguously in state)
    makes the issue auto-fixable. We never mutate state — we only ask the rules
    whether they *would* act, by inspecting the same state they would read.
    """
    # Only REQUIRED (MUST) issues are in scope for the deterministic repair loop.
    if issue.get("severity") != "required":
        return False
    try:
        from builder.tools.repair import (
            _RULES,
            _resolve_state_entity,
            _unique_unwired_file,
        )
    except ImportError:  # pragma: no cover — repair is a sibling module
        return False

    entity = _resolve_state_entity(state, issue.get("entity_id"))
    for rule in _RULES:
        if not rule.applies(issue, entity):
            continue
        # The sole rule today (missing_process_output) is fixable iff a single
        # un-wired File exists. Mirror that "would the repair decline?" check
        # without mutating state, rather than re-deriving each rule's internals.
        if rule.name == "missing_process_output":
            return _unique_unwired_file(state) is not None
        # An unknown future rule that owns the shape is treated as auto-fixable;
        # the repair loop is the source of truth and re-validates after.
        return True
    return False


def _shacl_gaps(state: CrateState, build_and_validate) -> tuple[list[Gap], dict[str, bool]]:
    """Build SHACL gaps (all severities) and the conformance dict.

    Runs ``build_and_validate`` at ``recommended`` severity so REQUIRED *and*
    RECOMMENDED issues are surfaced in one pass; OPTIONAL (MAY) issues are also
    swept so the guidance agent sees the full picture.
    """
    # "optional" gates in REQUIRED + RECOMMENDED + OPTIONAL (the widest sweep).
    result = build_and_validate(state, severity="optional", profile="all")
    conformance = dict(result.get("conformance", {}))
    if "error" in result:
        logger.warning("gap engine: SHACL validation error: %s", result["error"])
        return [], conformance

    gaps: list[Gap] = []
    for issue in result.get("issues", []):
        severity = issue.get("severity", "required")
        tier = _SEVERITY_TO_TIER.get(severity, "MUST")
        prop = issue.get("property")
        auto = _shacl_auto_fixable(state, issue)
        # Resolve the entity type for context (best-effort).
        entity_type: str | None = None
        from builder.tools.repair import _resolve_state_entity

        resolved = _resolve_state_entity(state, issue.get("entity_id"))
        if resolved is not None:
            entity_type = resolved.type
        fix_hint = "fix_required_issues" if auto else "ask-user"
        gaps.append(
            Gap(
                tier=tier,
                source="shacl",
                entity_id=issue.get("entity_id"),
                entity_type=entity_type,
                property=prop,
                message=issue.get("message", ""),
                suggestion=issue.get("fix") or None,
                fix_hint=fix_hint,
                auto_fixable=auto,
            )
        )
    return gaps, conformance


# ---------------------------------------------------------------------------
# MIT gaps
# ---------------------------------------------------------------------------


def _load_mit_yaml() -> dict[str, Any] | None:
    """Load and parse the MIT YAML (mirrors mit_assessment._load_mit_yaml)."""
    try:
        import yaml

        with open(MIT_YAML_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001 — best-effort, never crash the engine
        logger.warning("gap engine: failed to load MIT YAML from %s: %s", MIT_YAML_PATH, e)
        return None


def _filled_fields(state: CrateState) -> set[tuple[str, str]]:
    """The ``(entity_type, field)`` pairs that are filled/verified in state.

    Mirrors ``mit_assessment._count_filled_fields`` so a parameter is considered
    "filled" by exactly the same rule the MIT assessor uses.
    """
    filled: set[tuple[str, str]] = set()
    for entity in state.list_entities():
        for field_name in entity.fields:
            fc = entity.get_field_status(field_name)
            if fc is not None and fc.status in ("filled", "verified"):
                filled.add((entity.type, field_name))
    return filled


def _parse_crate_slots(slot_str: str) -> list[tuple[str, str]]:
    """Parse ``"A:x;B:y"`` into ``[(A, x), (B, y)]`` (mirrors mit_assessment)."""
    slots: list[tuple[str, str]] = []
    for part in (p.strip() for p in slot_str.split(";")):
        if ":" in part:
            entity_type, field_name = part.split(":", 1)
            slots.append((entity_type.strip(), field_name.strip()))
    return slots


def _mit_suggestion(param: dict[str, Any]) -> str | None:
    """Build a suggestion hint from a MIT parameter's description + standards."""
    description = (param.get("description") or "").strip()
    standards = param.get("standards") or {}
    cited = sorted(k for k, v in standards.items() if v)
    parts: list[str] = []
    if description:
        parts.append(description)
    if cited:
        parts.append("Standards: " + ", ".join(cited))
    return " | ".join(parts) if parts else None


def _mit_gaps(state: CrateState) -> tuple[list[Gap], float]:
    """Unfilled MIT parameters as gaps; also return the overall MIT score.

    A parameter is a gap when *none* of its ``crate_slot`` targets is filled.
    Core parameters (``additional: false``) are ``SHOULD``; ``additional: true``
    ones are ``MAY``. The overall MIT score mirrors ``assess_mit_coverage``.
    """
    mit_data = _load_mit_yaml()
    if mit_data is None:
        return [], 0.0

    filled = _filled_fields(state)
    modules = mit_data.get("modules", [])
    gaps: list[Gap] = []
    total_completed = 0
    total_required = 0

    for module in modules:
        all_params: list[dict[str, Any]] = []
        for section in module.get("sections", []):
            all_params.extend(section.get("parameters", []))
        all_params.extend(module.get("parameters", []))

        # Deduplicate by parameter id (mirrors mit_assessment).
        seen: set[str] = set()
        unique_params: list[dict[str, Any]] = []
        for param in all_params:
            pid = param.get("id", "")
            if pid and pid not in seen:
                seen.add(pid)
                unique_params.append(param)

        for param in unique_params:
            crate_slot = param.get("crate_slot", "")
            if not crate_slot:
                continue
            slots = _parse_crate_slots(crate_slot)
            total_required += 1
            is_filled = any(slot in filled for slot in slots)
            if is_filled:
                total_completed += 1
                continue

            # Unfilled -> a gap. Tier from the `additional` flag.
            additional = bool(param.get("additional", False))
            tier: Tier = "MAY" if additional else "SHOULD"
            # The first slot drives the routed property/entity_type (the canonical
            # crate field the parameter maps to); the rest are alternatives.
            slot_entity_type, slot_field = slots[0] if slots else (None, None)
            param_name = param.get("name") or param.get("id") or slot_field or "parameter"
            gaps.append(
                Gap(
                    tier=tier,
                    source="mit",
                    entity_id=None,
                    entity_type=slot_entity_type,
                    property=slot_field,
                    message=(
                        f"MIT parameter '{param_name}' is not yet captured "
                        f"(crate_slot {crate_slot})."
                    ),
                    suggestion=_mit_suggestion(param),
                    # MIT enrichment needs content the user provides or a drafter
                    # synthesizes; it is never a deterministic auto-fix (D5).
                    fix_hint="ask-user" if not additional else "draft",
                    auto_fixable=False,
                )
            )

    overall = total_completed / total_required if total_required > 0 else 0.0
    return gaps, overall


# ---------------------------------------------------------------------------
# FAIR gaps
# ---------------------------------------------------------------------------


def _fair_gaps(state: CrateState) -> tuple[list[Gap], dict[str, Any]]:
    """Failing FAIR indicators as gaps; also return a FAIR summary.

    A *failing* indicator (``passed is False``) becomes a gap — ``SHOULD`` for an
    essential indicator, ``MAY`` for an important / nice-to-have one. Out-of-scope
    indicators (``passed is None``) are never gaps. The summary carries the DSM
    level and pass/fail indicator counts.
    """
    from builder.tools.fair_assessment import assess_fair_maturity

    report = assess_fair_maturity(state)
    passed = 0
    failed = 0
    gaps: list[Gap] = []

    for indicator in report.indicator_results:
        outcome = indicator.get("passed")
        if outcome is None:
            continue  # out_of_scope — not a gap, not a pass/fail
        if outcome:
            passed += 1
            continue
        failed += 1
        priority = (indicator.get("priority") or "").lower()
        # Essential FAIR indicators are recommended (SHOULD); the rest are MAY.
        tier: Tier = "SHOULD" if priority == "essential" else "MAY"
        indicator_id = indicator.get("id") or ""
        gaps.append(
            Gap(
                tier=tier,
                source="fair",
                entity_id=None,
                entity_type=None,
                property=indicator_id or None,
                message=(
                    f"FAIR indicator {indicator_id} not met: "
                    f"{indicator.get('text', '')}".strip()
                ),
                suggestion=f"Dimension {indicator.get('dimension', '')} "
                f"({priority or 'unrated'})".strip(),
                fix_hint="ask-user",
                auto_fixable=False,
            )
        )

    summary = {
        "dsm_level": report.dsm_level,
        "indicators_passed": passed,
        "indicators_failed": failed,
    }
    return gaps, summary


# ---------------------------------------------------------------------------
# The unified gap engine
# ---------------------------------------------------------------------------


def _sort_key(gap: Gap) -> tuple:
    """Primary sort by tier, stable secondary by (source, entity_id, property)."""
    return (
        _TIER_RANK[gap.tier],
        gap.source,
        gap.entity_id or "",
        gap.property or "",
        gap.message,
    )


def assess_gaps(state: CrateState) -> GapReport:
    """Unify SHACL + MIT + FAIR into ONE prioritized :class:`GapReport`.

    Pure and deterministic (no LLM, no network) and side-effect-free — ``state``
    is never mutated, so two calls on the same state return identical ordered
    output. See the module docstring for the tiering and ``auto_fixable`` rules.

    Args:
        state: The crate state to assess.

    Returns:
        A :class:`GapReport` whose ``gaps`` are sorted MUST -> SHOULD -> MAY with
        a stable secondary order by ``(source, entity_id, property)``.
    """
    from builder.tools.validation import build_and_validate

    shacl_gaps, conformance = _shacl_gaps(state, build_and_validate)
    mit_gaps, mit_overall = _mit_gaps(state)
    fair_gaps, fair_summary = _fair_gaps(state)

    gaps = sorted([*shacl_gaps, *mit_gaps, *fair_gaps], key=_sort_key)

    counts = {
        "must_open": sum(1 for g in gaps if g.tier == "MUST"),
        "should_open": sum(1 for g in gaps if g.tier == "SHOULD"),
        "may_open": sum(1 for g in gaps if g.tier == "MAY"),
    }

    return GapReport(
        gaps=gaps,
        conformance=conformance,
        mit_overall=mit_overall,
        fair_summary=fair_summary,
        counts=counts,
    )
