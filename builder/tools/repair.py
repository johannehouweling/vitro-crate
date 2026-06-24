"""Deterministic issue->repair dispatch (Issue #179, task 1 — the keystone).

``build_and_validate`` (#87) already routes every SHACL violation to the
``{entity_id, property, message, fix, severity, profile}`` shape, and
``engine._order_required_issues`` orders them — but nothing maps an issue back to a
*repair*. This module closes that gap: :func:`fix_required_issues` consumes the
routed issues and attempts a **deterministic** repair per issue (no LLM, no
network), using only what already exists in :class:`CrateState`.

Scope (AGENTS.md §14.2, §14.3, D5):

- **In scope** — repairs whose correct value is already determined by state:
  a missing process output/input edge whose target is the *single, unambiguous*
  candidate already in state -> :func:`link`.
- **Out of scope** — anything that needs NEW content, a NEW entity, or a
  fabricated identifier. Those are classified as ``remaining`` for the bounded
  LLM leaf to handle later. We **never fabricate identifiers** (D5).

The dispatch is a small, ordered list of :class:`RepairRule`s (issue-shape ->
repair action), not a monolith, so each case is independently testable and new
cases drop in without touching the loop. It is idempotent and side-effect-safe:
if nothing is deterministically fixable it returns every issue unchanged in
``remaining`` and does not mutate state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from builder.state import CrateState, Entity
from builder.tools._crate_mapping import _mint_id
from builder.tools.provenance import _OUTPUT_FIELDS, _ref_ids
from builder.tools.provenance import link as _link

logger = logging.getLogger(__name__)

# A repair returns a short human-readable description of the mutation it made,
# or ``None`` when it declines (the issue is not its responsibility / not
# deterministically fixable). Declining must NOT mutate state.
RepairFn = Callable[[CrateState, dict[str, Any], "Entity | None"], "str | None"]


@dataclass(frozen=True)
class RepairRule:
    """One issue-shape -> repair-action mapping.

    Attributes:
        name: Stable identifier for the rule (recorded on the ``fixed`` item).
        applies: Predicate over ``(issue, entity)`` deciding if this rule owns
            the issue. ``entity`` is the resolved state :class:`Entity` (or None
            when the issue's focus node has no state counterpart, e.g. the root).
        repair: The deterministic mutation. Returns an action description on
            success, or ``None`` to decline (leaving state untouched).
    """

    name: str
    applies: Callable[[dict[str, Any], "Entity | None"], bool]
    repair: RepairFn


# ---------------------------------------------------------------------------
# Issue -> state-entity resolution
# ---------------------------------------------------------------------------


def _local_property(prop: str | None) -> str:
    """The local name of a property IRI (after the last ``/`` or ``#``)."""
    if not prop:
        return ""
    return prop.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _resolve_state_entity(state: CrateState, focus_id: str | None) -> Entity | None:
    """Map a validation focus-node @id back to the originating state :class:`Entity`.

    ``build_and_validate`` reports ``entity_id`` as the crate-relative @id of the
    built graph node (e.g. ``"./#LabProcess_er1"`` or ``"./"``), not the state
    ``entity_id``. The build mints each node's @id via ``_mint_id``, so we invert
    it: strip the base-relative ``"./"`` prefix the validator prepends, then match
    the remainder against each entity's minted @id. Returns ``None`` for the root
    (``"./"``) — which folds the single Investigation and has no separate node —
    or when no entity mints to that @id.
    """
    if not focus_id:
        return None
    candidate = focus_id[2:] if focus_id.startswith("./") else focus_id
    if candidate in ("", "."):
        return None
    for entity in state.list_entities():
        if _mint_id(entity) == candidate:
            return entity
    return None


# ---------------------------------------------------------------------------
# Repair rules
# ---------------------------------------------------------------------------

# Domain process types whose build mapping has NO output fallback (mirrors
# provenance._OUTPUT_REQUIRED_TYPES): a missing result/output on these is a real
# TOX REQUIRED violation, not a benign gap the build would synthesize.
_OUTPUT_REQUIRED_TYPES = frozenset({"EndpointReadout", "DataAnalysis"})


def _process_type(entity: Entity) -> str:
    return entity.fields.get("process_type") or entity.fields.get("additionalType") or ""


def _already_wired_outputs(state: CrateState) -> set[str]:
    """entity_ids already wired as some process's output (result/output)."""
    wired: set[str] = set()
    for proc in state.list_entities("LabProcess"):
        for fld in _OUTPUT_FIELDS:
            wired |= _ref_ids(proc.fields.get(fld))
    return wired


def _unique_unwired_file(state: CrateState) -> Entity | None:
    """The single File not yet wired as any process output, or None.

    Returns ``None`` when zero (needs new content) or 2+ (ambiguous) such Files
    exist — both are deferred to the LLM rather than guessed.
    """
    wired = _already_wired_outputs(state)
    candidates = [f for f in state.list_entities("File") if f.entity_id not in wired]
    return candidates[0] if len(candidates) == 1 else None


def _applies_missing_process_output(issue: dict[str, Any], entity: Entity | None) -> bool:
    """A REQUIRED missing-output issue on an EndpointReadout / DataAnalysis."""
    if entity is None or entity.type != "LabProcess":
        return False
    if _process_type(entity) not in _OUTPUT_REQUIRED_TYPES:
        return False
    return _local_property(issue.get("property")) in _OUTPUT_FIELDS


def _repair_missing_process_output(
    state: CrateState, issue: dict[str, Any], entity: Entity | None
) -> str | None:
    """Wire the unique un-wired File as this process's ``result`` (deterministic).

    Declines (returns ``None``, no mutation) when the target is not unambiguous —
    zero candidates (new content needed) or 2+ (genuine ambiguity) — so the LLM
    leaf decides. Never creates a File and never fabricates an id (D5).
    """
    assert entity is not None  # guarded by _applies_missing_process_output
    target = _unique_unwired_file(state)
    if target is None:
        return None
    _link(state, entity.entity_id, "result", target.entity_id)
    return f"link({entity.entity_id!r}, 'result', {target.entity_id!r})"


# The ordered dispatch table. First rule whose ``applies`` is true and whose
# ``repair`` does not decline wins; the rest are skipped for that issue.
_RULES: tuple[RepairRule, ...] = (
    RepairRule(
        name="missing_process_output",
        applies=_applies_missing_process_output,
        repair=_repair_missing_process_output,
    ),
)


# ---------------------------------------------------------------------------
# The deterministic repair loop
# ---------------------------------------------------------------------------


def _issue_key(issue: dict[str, Any]) -> tuple:
    """A stable identity for an issue, for set-difference across re-validation."""
    return (
        issue.get("entity_id"),
        issue.get("property"),
        issue.get("profile"),
        issue.get("severity"),
        issue.get("message"),
    )


def fix_required_issues(
    state: CrateState,
    severity: str | None = "required",
    profile: str | None = "all",
) -> dict[str, Any]:
    """Deterministically repair routed validation issues — no LLM, no network.

    The keystone of the deterministic pipeline (AGENTS.md §14): it runs
    ``build_and_validate(severity, profile)``, dispatches each issue to a
    deterministic :class:`RepairRule`, then re-validates to confirm which issues
    actually cleared. Repairs that would need new content, a new entity, or a
    fabricated identifier are left for the LLM leaf (classified ``remaining``).

    It is **side-effect-safe**: if no rule deterministically fixes any issue, it
    mutates nothing and returns every issue under ``remaining``. It is
    **idempotent**: a second call on an already-repaired (or unfixable) state is a
    no-op.

    Args:
        state: The crate state to repair in place.
        severity: Gate severity forwarded to ``build_and_validate`` ("required"
            by default). ``None`` is treated as the default (weak models emit
            explicit nulls).
        profile: Validation scope forwarded to ``build_and_validate`` ("all" by
            default). ``None`` is treated as the default.

    Returns:
        ``{"ok": bool, "fixed": [...], "remaining": [...]}`` where ``ok`` is True
        when no issues remain at the gate severity. Each ``fixed`` item is
        ``{"issue", "rule", "action"}`` (the original issue plus the repair made);
        each ``remaining`` item is ``{"issue", "reason"}`` (why it was not
        auto-fixed). An upstream validation error surfaces as
        ``{"ok": False, "fixed": [], "remaining": [], "error": ...}``.
    """
    severity = "required" if severity is None else severity
    profile = "all" if profile is None else profile

    from builder.tools.validation import build_and_validate

    before = build_and_validate(state, severity=severity, profile=profile)
    if "error" in before:
        return {"ok": False, "fixed": [], "remaining": [], "error": before["error"]}

    issues = before.get("issues", [])
    if not issues:
        return {"ok": before.get("ok", True), "fixed": [], "remaining": []}

    # Dispatch each issue to the first rule that owns it and does not decline.
    # Record the planned fix per issue-key; we do not trust the rule's verdict for
    # the final report — re-validation below decides what actually cleared.
    planned: dict[tuple, dict[str, Any]] = {}
    deferred: list[dict[str, Any]] = []
    any_repair = False

    for issue in issues:
        entity = _resolve_state_entity(state, issue.get("entity_id"))
        repaired = False
        for rule in _RULES:
            if not rule.applies(issue, entity):
                continue
            action = rule.repair(state, issue, entity)
            if action is None:
                continue  # rule owned the shape but declined (ambiguous/new content)
            planned[_issue_key(issue)] = {
                "issue": issue,
                "rule": rule.name,
                "action": action,
            }
            any_repair = True
            repaired = True
            break
        if not repaired:
            deferred.append(
                {
                    "issue": issue,
                    "reason": (
                        "No deterministic repair: needs new content, a new entity, "
                        "an unambiguous in-state target, or LLM judgement."
                    ),
                }
            )

    # Side-effect-safe: if nothing was deterministically fixable, state is
    # unchanged — return every issue as remaining without a (wasted) re-validate.
    if not any_repair:
        return {"ok": False, "fixed": [], "remaining": deferred}

    # Re-validate to confirm which issues truly cleared (a repair can clear or
    # uncover issues; trust the validator, not the rule's optimism).
    after = build_and_validate(state, severity=severity, profile=profile)
    if "error" in after:
        return {"ok": False, "fixed": [], "remaining": deferred, "error": after["error"]}
    remaining_keys = {_issue_key(i) for i in after.get("issues", [])}

    fixed: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = list(deferred)
    for key, record in planned.items():
        if key in remaining_keys:
            # We attempted a repair but the issue persists — surface it honestly.
            still_open.append(
                {
                    "issue": record["issue"],
                    "reason": (
                        f"Attempted {record['action']} ({record['rule']}) but the "
                        f"issue persists; needs further repair."
                    ),
                }
            )
        else:
            fixed.append(record)

    # Any newly-surfaced issue (not in the original set) also belongs in remaining.
    original_keys = {_issue_key(i) for i in issues}
    for issue in after.get("issues", []):
        if _issue_key(issue) not in original_keys:
            still_open.append(
                {
                    "issue": issue,
                    "reason": "Surfaced after a deterministic repair; not auto-fixable.",
                }
            )

    return {"ok": after.get("ok", not still_open), "fixed": fixed, "remaining": still_open}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("fix_required_issues", fix_required_issues, takes_state=True)
