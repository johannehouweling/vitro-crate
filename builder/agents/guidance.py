"""The guidance agent — HITL gap-resolution loop (Issue #179, task 2b-G).

This is the **deterministic, code-driven** loop that consumes the gap engine's
prioritized :class:`~builder.tools.gap_analysis.GapReport` and resolves gaps with
the human in the loop. It is the §14 hybrid architecture's "human-confirmed
enrichment" half: **CODE owns control flow** (NOT a ReAct / LLM-orchestrated
agent), the LLM is used *only* to draft a suggested value for a draftable gap, and
the **user is the authority** — every commit of uncertain content is confirmed
before it lands (D5: Verify, Don't Trust).

The loop (:func:`run_guidance`) per round:

1. ``report = assess_gaps(engine.state)`` — re-assess from scratch each round so
   resolved gaps disappear and newly-surfaced ones appear.
2. **Terminate** when no MUST gaps remain AND (the user signalled done OR there
   are no actionable SHOULD/MAY gaps left).
3. Otherwise take the **highest-priority actionable gap** (the report is already
   sorted MUST -> SHOULD -> MAY) and resolve it by ``fix_hint`` / ``auto_fixable``:

   - **auto_fixable** -> run the deterministic repair (``fix_required_issues``).
     No user prompt — the correct value is already determined by state.
   - **draftable** (``fix_hint == "draft"``) -> draft a candidate value via
     :func:`draft_entity_fields`, **show it to the user and require confirmation
     before committing** (D5). On reject, fall through to *ask-user*.
   - **ask-user** (``fix_hint == "ask-user"``) -> prompt via the
     :class:`~builder.tools.hitl.HumanInterface` and apply the user's answer to
     the entity through the existing ``set_fields`` / ``set_crate_metadata`` tools
     (never hand-rolled JSON-LD).

4. Re-assess after each committed change; **never loop forever** — bounded by
   ``max_rounds`` and a per-report skip-set. A gap the loop cannot progress this
   round (e.g. the user skips it) is *skipped*, not fatal: the loop advances to the
   next actionable gap and only stops once the whole report is exhausted with no
   progress (#230). The skip-set is cleared on every commit (the re-assessed
   report is fresh). ``report-only`` gaps — FAIR indicators and crate-level MIT
   params with no settable target — are never drawn at all.

Determinism & safety contract:

* **Bounded.** At most ``max_rounds`` rounds; each round resolves at most one gap.
* **Explicit termination.** Two independent stop conditions (no actionable gap
  left / the whole report exhausted with no progress) plus the hard ``max_rounds``
  cap.
* **Every LLM call is a bounded leaf.** The drafter leaf
  (:func:`draft_entity_fields`) is the *only* model call, and its output is shown
  to the user for confirmation before it is ever committed.
* **HITL is never removed.** ask-user and draft-confirm both route through the
  injected :class:`HumanInterface`; the loop cannot silently fabricate content.

This is a clean library entrypoint — the CLI / spine wiring is a later PR.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# Re-exported at module scope so the spine, tests, and the eval harness have a
# single stable monkeypatch target — and so a flaky/absent LLM drafter can be
# stubbed without importing langchain.
from builder.agents.pipeline import draft_entity_fields
from builder.tools.gap_analysis import REPORT_ONLY, Gap, assess_gaps

if TYPE_CHECKING:
    from builder.engine import AgentEngine
    from builder.tools.gap_analysis import GapReport
    from builder.tools.hitl import HumanInterface

logger = logging.getLogger(__name__)

__all__ = ["run_guidance"]

# Default upper bound on rounds. Each round runs the SHACL-heavy gap engine once,
# so this caps the worst-case work and guarantees termination even if a resolved
# gap never clears (e.g. a user value the validator still rejects).
_DEFAULT_MAX_ROUNDS = 20

# Descriptive context fields used for the draftable path. Mirrors the spine's
# `_DESCRIPTIVE_APPLY_FIELDS`: the drafter leaf is only trusted for free-text
# descriptive fields (identifiers come from lookups, D5).
_DESCRIPTIVE_FIELDS: frozenset[str] = frozenset({"name", "description"})

# Local property names that map onto crate-level (Root Data Entity) metadata via
# `set_crate_metadata`, for a crate-level gap (entity_id is None). Anything else
# crate-level has no deterministic setter and is recorded as "asked" only.
_CRATE_METADATA_FIELDS: dict[str, str] = {
    "name": "title",
    "title": "title",
    "description": "description",
    "identifier": "accession",
    "accession": "accession",
}


def _local_name(iri: str | None) -> str:
    """Local part of a property IRI (after the last ``/`` or ``#``)."""
    if not iri:
        return ""
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _resolve_entity_id(engine: AgentEngine, gap: Gap) -> str | None:
    """Map a gap's ``entity_id`` back to a *state* entity_id, or ``None``.

    A gap's ``entity_id`` may be a state entity_id (MIT gaps, and the test
    doubles) or the built-graph node @id a SHACL issue reports (e.g.
    ``"./#LabProcess_er1"``). We try the direct lookup first, then invert the
    build's minting via the repair module's resolver — re-used read-only so the
    two modules cannot drift on how a focus node maps back to state.
    """
    if not gap.entity_id:
        return None
    if engine.state.get_entity(gap.entity_id) is not None:
        return gap.entity_id
    try:
        from builder.tools.repair import _resolve_state_entity
    except ImportError:  # pragma: no cover — repair is a sibling module
        return None
    resolved = _resolve_state_entity(engine.state, gap.entity_id)
    return resolved.entity_id if resolved is not None else None


def _apply_value(engine: AgentEngine, gap: Gap, value: str) -> bool:
    """Commit ``value`` for ``gap`` via the existing set tools. Returns success.

    Uses ``set_fields`` for an entity-scoped gap and ``set_crate_metadata`` for a
    crate-level one — never hand-rolled JSON-LD (AGENTS.md §4.7). The target field
    is the local name of the gap's ``property``. Returns ``False`` (committing
    nothing) when the gap names no usable field or its entity cannot be resolved,
    so the caller treats it as "no progress" rather than a silent partial write.
    """
    field = _local_name(gap.property) or (gap.property or "")
    if not field:
        return False

    state_id = _resolve_entity_id(engine, gap)
    if state_id is not None:
        engine.run_tool("set_fields", entity_id=state_id, fields={field: value})
        return True

    # Crate-level gap (no entity): route to the Root Data Entity metadata setter
    # when the field maps to a known root slot; otherwise we cannot commit it
    # deterministically (it was still surfaced to the user as "asked").
    crate_arg = _CRATE_METADATA_FIELDS.get(field)
    if gap.entity_id is None and crate_arg is not None:
        engine.run_tool("set_crate_metadata", **{crate_arg: value})
        return True

    logger.debug(
        "guidance: no deterministic target to commit gap (entity_id=%s, property=%s)",
        gap.entity_id,
        gap.property,
    )
    return False


def _draft_context(engine: AgentEngine, gap: Gap) -> str:
    """Free-text context for the drafter leaf, assembled from state + the gap.

    A bounded digest — crate title / description plus the gap's own message and
    suggestion — so the leaf has something to extract from. The leaf is a single
    bounded call; we never feed it file bodies here.
    """
    state = engine.state
    parts: list[str] = []
    title = (state.metadata.title or "").strip()
    if title:
        parts.append(f"Crate title: {title}")
    description = (state.metadata.description or "").strip()
    if description:
        parts.append(f"Crate description: {description}")
    if gap.message:
        parts.append(f"Gap: {gap.message}")
    if gap.suggestion:
        parts.append(f"Hint: {gap.suggestion}")
    return "\n".join(parts).strip()


def _drafted_value(engine: AgentEngine, gap: Gap) -> str | None:
    """Draft a candidate value for ``gap`` via the bounded drafter leaf, or None.

    Calls :func:`draft_entity_fields` for the gap's ``entity_type`` and returns the
    drafted value for the gap's target field (or the first descriptive field the
    leaf returned). Returns ``None`` when the leaf yields nothing usable or raises
    — a flaky leaf must never break the loop; the caller falls back to ask-user.
    """
    entity_type = gap.entity_type
    if not entity_type:
        return None
    field = _local_name(gap.property) or (gap.property or "")
    try:
        fields = draft_entity_fields(entity_type, _draft_context(engine, gap))
    except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
        logger.warning("guidance: drafter leaf failed for %s: %s", entity_type, exc)
        return None
    if not isinstance(fields, dict):
        return None

    # Prefer the gap's own field; else any descriptive field the leaf returned.
    candidate = fields.get(field)
    if candidate is None:
        for key in _DESCRIPTIVE_FIELDS:
            if str(fields.get(key) or "").strip():
                candidate = fields.get(key)
                break
    if candidate is None or not str(candidate).strip():
        return None
    return str(candidate)


def _ask_user_prompt(gap: Gap) -> str:
    """Build a human-readable ask-user prompt for ``gap``.

    The raw ``gap.message`` is a description of a *failed check* (e.g. "Study MUST
    have a description"), not a question with a field label and expected format —
    surfaced verbatim it reads as a cryptic "What?" box. Instead we assemble a
    clear, multi-line prompt: a direct question naming the field (and the entity /
    tier it applies to), the gap's own explanation, any suggestion, and the
    expected input format. This keeps the human genuinely in the loop (D5).
    """
    field = _local_name(gap.property) or (gap.property or "this field")
    target = f" on the {gap.entity_type}" if gap.entity_type else ""
    lines: list[str] = [f"Please provide a value for '{field}'{target}."]
    if gap.message:
        lines.append(f"Why: {gap.message}")
    if gap.suggestion:
        lines.append(f"Suggestion: {gap.suggestion}")
    lines.append("Expected: free text (leave blank or skip to defer this field).")
    return "\n".join(lines)


def _ask_user(engine: AgentEngine, human: HumanInterface, gap: Gap) -> str | None:
    """Prompt the human for ``gap`` and return their value, or ``None`` if skipped."""
    response = human.request_input(_ask_user_prompt(gap))
    if response.get("skipped"):
        return None
    value = response.get("value")
    if value is None or not str(value).strip():
        return None
    return str(value)


def _resolve_gap(
    engine: AgentEngine,
    human: HumanInterface,
    gap: Gap,
    *,
    resolved: list[dict[str, Any]],
    asked: list[dict[str, Any]],
) -> bool:
    """Resolve a single ``gap``; return ``True`` iff it committed a change.

    Dispatches on ``auto_fixable`` / ``fix_hint``:

    * **auto_fixable** -> the deterministic repair, no prompt.
    * **draft** -> draft a value, show it, require confirmation (D5); on reject,
      fall through to ask-user.
    * **ask-user** (or any non-auto fallback) -> prompt and apply the answer.

    Records the action in ``resolved`` (committed) or ``asked`` (surfaced to the
    user) for the run summary.
    """
    record = {
        "tier": gap.tier,
        "source": gap.source,
        "entity_id": gap.entity_id,
        "property": gap.property,
        "fix_hint": gap.fix_hint,
    }

    # --- auto-fixable: deterministic repair, no human prompt -------------------
    if gap.auto_fixable:
        result = engine.run_tool(
            "fix_required_issues", profile="all", severity="required"
        )
        fixed = bool(result.get("fixed"))
        if fixed:
            resolved.append({**record, "via": "fix_required_issues"})
        return fixed

    # --- draftable: draft -> confirm -> commit (D5) ---------------------------
    if gap.fix_hint == "draft":
        candidate = _drafted_value(engine, gap)
        if candidate is not None:
            decision = human.present(
                context=(
                    f"{gap.message}\n\nDrafted value:\n{candidate}\n\n"
                    "Approve to commit this drafted value, or reject to enter your own."
                ),
                options=["approve", "reject"],
            )
            if decision.get("action") == "approved":
                # An edited confirmation may carry the user's own value.
                edits = decision.get("edits") or {}
                value = str(edits.get("value")) if edits.get("value") else candidate
                if _apply_value(engine, gap, value):
                    resolved.append({**record, "via": "draft-confirmed"})
                    return True
                return False
        # No usable draft, or the user rejected it -> fall through to ask-user.

    # --- ask-user: prompt and apply -------------------------------------------
    asked.append(record)
    value = _ask_user(engine, human, gap)
    if value is None:
        return False
    if _apply_value(engine, gap, value):
        resolved.append({**record, "via": "ask-user"})
        return True
    return False


def _user_signals_done(human: HumanInterface) -> bool:
    """Whether the user wants to stop once all MUST gaps are cleared.

    Optional protocol extension: a HumanInterface may expose ``is_done()``; when
    absent we never treat the user as "done" and instead rely on the
    no-actionable-gap and no-progress termination guards. This keeps the loop's
    termination guarantees independent of any particular frontend.
    """
    is_done = getattr(human, "is_done", None)
    if callable(is_done):
        try:
            return bool(is_done())
        except Exception:  # noqa: BLE001 — a frontend hiccup must not hang the loop
            return False
    return False


def run_guidance(
    engine: AgentEngine,
    human: HumanInterface,
    *,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    """Run the deterministic HITL gap-resolution loop over the gap engine.

    Each round re-assesses gaps from scratch (:func:`assess_gaps`) after a commit,
    takes the highest-priority actionable gap (MUST -> SHOULD -> MAY), and resolves
    it by ``fix_hint`` / ``auto_fixable`` (auto-fix / draft-confirm-commit /
    ask-user). A gap it cannot progress this round is added to a **per-report
    skip-set** and the loop advances to the next actionable gap rather than
    aborting, so one un-committable gap never abandons the ones behind it (#230).
    The loop is bounded by ``max_rounds`` and terminates once the whole report is
    exhausted with no progress, or once no MUST gap remains and the user is done.
    CODE owns control flow; the LLM only drafts; the user confirms every uncertain
    commit (D5). HITL is never bypassed.

    Args:
        engine: The :class:`~builder.engine.AgentEngine` whose ``state`` is
            assessed and mutated in place (through the existing set / repair
            tools — never hand-rolled JSON-LD).
        human: The injected :class:`~builder.tools.hitl.HumanInterface` used for
            ask-user prompts and draft confirmations.
        max_rounds: Hard upper bound on rounds (default 20). Guarantees
            termination even if a resolved gap never clears.

    Returns:
        A summary dict::

            {
                "resolved": [ {tier, source, entity_id, property, fix_hint, via}, ... ],
                "asked":    [ {tier, source, entity_id, property, fix_hint}, ... ],
                "remaining_gaps": {must_open, should_open, may_open},
                "conformance":    {base, isa, tox},
                "rounds":         <int>,
            }
    """
    resolved: list[dict[str, Any]] = []
    asked: list[dict[str, Any]] = []
    rounds = 0
    report: GapReport = assess_gaps(engine.state)
    # Indices into the CURRENT report's gaps that were tried this round and could
    # not be progressed (e.g. the user skipped them). They are skipped so the loop
    # advances to the next actionable gap instead of re-offering the same one; the
    # set is cleared whenever a commit invalidates the report (a fresh re-assess).
    skipped: set[int] = set()

    for _ in range(max(0, max_rounds)):
        index = _next_actionable_index(report, skipped)

        # --- termination: the whole report is exhausted -----------------------
        # No actionable gap remains that we have not already tried this round —
        # either there are none, or every one was skipped (un-progressable). Either
        # way, re-assessing would only reproduce the same gaps, so we stop.
        if index is None:
            break
        gap = report.gaps[index]
        # Once MUST gaps are cleared, an actionable SHOULD/MAY only continues the
        # loop while the user wants to keep going.
        if report.counts.get("must_open", 0) == 0 and _user_signals_done(human):
            break

        rounds += 1
        progressed = _resolve_gap(
            engine, human, gap, resolved=resolved, asked=asked
        )

        if progressed:
            # State changed: re-assess from scratch and forget the skip-set (the
            # gap indices no longer refer to the same gaps).
            report = assess_gaps(engine.state)
            skipped = set()
        else:
            # This one gap is not resolvable right now (e.g. skipped); skip it and
            # let the next round draw the next actionable gap in the SAME report.
            # The loop only stops once EVERY gap in the report is exhausted, so a
            # single un-progressable gap never abandons the ones behind it. Still
            # bounded by ``max_rounds``.
            skipped.add(index)

    return {
        "resolved": resolved,
        "asked": asked,
        "remaining_gaps": dict(report.counts),
        "conformance": dict(report.conformance),
        "rounds": rounds,
    }


def _next_actionable_index(report: GapReport, skipped: set[int]) -> int | None:
    """Index of the highest-priority *actionable, not-yet-skipped* gap, or ``None``.

    The report is already sorted MUST -> SHOULD -> MAY (committable before
    ``report-only`` within a tier), so we walk it in order and return the index of
    the first gap that is BOTH actionable and not in ``skipped`` (indices into
    ``report.gaps`` the loop has already tried and could not progress this round).

    A gap is **actionable** when it has a resolution route the loop can drive:
    ``auto_fixable``, or a ``fix_hint`` of ``fix_required_issues`` / ``draft`` /
    ``ask-user`` (or an unknown/absent hint, which falls back to ask-user — the
    safe default that keeps the human in the loop). A ``report-only`` gap is
    **never** actionable: it has no deterministic settable target, so the loop
    surfaces it for context but never spends an ask-user turn on it.
    """
    for index, gap in enumerate(report.gaps):
        if index in skipped:
            continue
        if gap.fix_hint == REPORT_ONLY:
            continue
        # Auto-fixable, a known committable hint, or an unknown hint (ask-user
        # fallback) -> actionable.
        return index
    return None


def _next_actionable_gap(report: GapReport, *, skipped: set[int]) -> Gap | None:
    """The highest-priority *actionable, not-yet-skipped* gap, or ``None``.

    Thin wrapper over :func:`_next_actionable_index` for callers (and tests) that
    only need the gap, not its position. See that function for the actionability
    and skip-set rules.
    """
    index = _next_actionable_index(report, skipped)
    return report.gaps[index] if index is not None else None
