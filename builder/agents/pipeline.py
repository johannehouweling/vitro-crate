"""The deterministic pipeline spine (Issue #179, task 4 — AGENTS.md §14.2).

This is the **code-driven** orchestrator that the §14 architecture promotes the
Priority 1-4 heuristic (§4) into: a pure, deterministic sequence with **no LLM
deciding control flow**. It operates on an already-:meth:`initialize`-d
:class:`~builder.engine.AgentEngine` (so scanning + approved-roots happened in the
engine) and drives the existing toolbox tools *through the engine* — it never
re-implements tool logic, only orchestrates it.

The sequence mirrors AGENTS.md §14.2::

    scaffold ISA backbone ─ draft entities ─ build_and_validate ─ fix loop

1. **Scaffold** the ISA backbone (``scaffold_isa_backbone``) — always. The gate
   audit (§14.3) found this is the deterministic path to a BASE/ISA/TOX-passing
   backbone on an empty crate, *provided the Study carries a name* — a bare
   ``draft_study`` defaults only the entity_id, not the ``name`` field, so the
   spine supplies deterministic backbone names (derived from the crate title when
   present, else stable defaults) so ISA passes with zero LLM involvement.
2. **Draft entities** from what state already has. The existing drafters are pure
   state mutations, but turning scanned files / free-text into typed entities is
   the bounded *extraction* job the §14.2 "drafter-leaf" (a cheap LLM) will own —
   there is no deterministic file→entity path today. So this step is a deliberate
   no-op when there is nothing already-structured to draft; the backbone suffices
   for conformance. A future PR swaps in the LLM drafter-leaf here.
3. **build_and_validate** in memory (no disk write).
4. **Fix loop**: call ``fix_required_issues`` and re-validate, bounded to
   ``_MAX_FIX_ROUNDS`` rounds, stopping when no REQUIRED issue remains or a round
   makes no progress (deterministic dispatch only — see :mod:`builder.tools.repair`).
5. Return a result dict with the final per-layer conformance.

Determinism contract: the same input state ⇒ an identical built ``@graph`` (the
headline win the eval harness asserts). Every step is deterministic — the
scaffold is idempotent, drafting is pure, and the fix loop uses only
deterministic dispatch — so re-running on an equal state yields an equal crate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)

# Upper bound on deterministic fix-loop rounds. Each round runs
# fix_required_issues (which itself validates twice), so this caps the worst-case
# SHACL work and guarantees termination even if a repair never converges.
_MAX_FIX_ROUNDS = 3

# Stable default names for the backbone layers when neither hints nor the crate
# title supply one. A non-empty Study `name` is REQUIRED by the ISA profile, so
# these are load-bearing for ISA conformance — not cosmetic.
_DEFAULT_INVESTIGATION_NAME = "Investigation"
_DEFAULT_STUDY_NAME = "Study"
_DEFAULT_ASSAY_NAME = "Assay"


def _backbone_hints(engine: AgentEngine) -> dict[str, dict[str, str]]:
    """Deterministic name hints for the Investigation / Study / Assay backbone.

    Derives names from the crate title when present (so a titled crate gets a
    meaningful backbone) and otherwise falls back to stable defaults. The Study
    name is the load-bearing one — the ISA profile REQUIRES a non-empty Study
    ``name`` and the bare drafter does not populate it. Returns hints only for
    backbone layers that do not yet exist, so the call stays idempotent.
    """
    state = engine.state
    title = (state.metadata.title or "").strip()
    existing = {e.type for e in state.list_entities()}

    hints: dict[str, dict[str, str]] = {}
    if "Investigation" not in existing:
        hints["investigation"] = {"name": title or _DEFAULT_INVESTIGATION_NAME}
    if "Study" not in existing:
        hints["study"] = {"name": title or _DEFAULT_STUDY_NAME}
    if "Assay" not in existing:
        hints["assay"] = {"name": title or _DEFAULT_ASSAY_NAME}
    return hints


def _scaffold_backbone(engine: AgentEngine) -> dict[str, Any]:
    """Step 1 — scaffold (or reuse) the ISA backbone via the engine.

    Idempotent: ``scaffold_isa_backbone`` reuses an existing entity of each type,
    so re-running the spine never duplicates the backbone. Backbone name hints are
    supplied only for missing layers so the call is a strict no-op on a complete
    backbone.
    """
    hints = _backbone_hints(engine)
    return engine.run_tool("scaffold_isa_backbone", **hints)


def _draft_entities(engine: AgentEngine) -> dict[str, Any]:
    """Step 2 — draft entities from what state already carries (deterministic).

    There is no deterministic file→entity extraction today: turning scanned files
    / free text into typed entities is the bounded LLM "drafter-leaf" the §14.2
    architecture introduces in a later PR. To keep the spine fully deterministic
    we draft nothing speculatively here — the scaffolded backbone already reaches
    ``{base, isa, tox}`` conformance, and any pre-seeded entities in state are
    carried into the build as-is. This is an intentional, documented deferral.
    """
    # Report what was already present so the result is informative; mutate nothing.
    drafted: list[str] = []
    return {"drafted": drafted, "deferred_to_llm_leaf": True}


def _run_fix_loop(engine: AgentEngine) -> tuple[dict[str, Any], int]:
    """Step 4 — bounded deterministic fix loop.

    Runs ``fix_required_issues`` up to ``_MAX_FIX_ROUNDS`` times, stopping early
    when it reports ``ok`` (no REQUIRED issue remains) or a round makes no
    progress (nothing newly fixed). Returns the last ``build_and_validate`` result
    and the number of fix rounds actually run. All dispatch is deterministic — the
    repair module never calls an LLM or the network.

    A round "makes progress" when it fixes at least one issue; a round that fixes
    nothing means the remaining issues are not deterministically repairable, so
    spinning further is wasted SHACL work.
    """
    from builder.tools.validation import build_and_validate

    rounds = 0
    last_validation = engine.run_tool("build_and_validate", profile="all", severity="required")
    if last_validation.get("ok"):
        return last_validation, rounds

    for _ in range(_MAX_FIX_ROUNDS):
        rounds += 1
        fix_result = engine.run_tool("fix_required_issues", profile="all", severity="required")
        # Re-validate to get the authoritative conformance after this round.
        last_validation = engine.run_tool(
            "build_and_validate", profile="all", severity="required"
        )
        if last_validation.get("ok"):
            break
        if not fix_result.get("fixed"):
            # No deterministic repair landed this round — further rounds cannot
            # help (the loop is monotone over the deterministic rule set).
            break

    # Defensive: if the loop body never validated (it always does), fall back to a
    # final read so the caller always gets a real conformance map.
    if last_validation is None:  # pragma: no cover - belt-and-braces
        last_validation = build_and_validate(engine.state, profile="all", severity="required")
    return last_validation, rounds


def run_pipeline(engine: AgentEngine) -> dict[str, Any]:
    """Run the deterministic pipeline spine on *engine* and return its outcome.

    The engine MUST already be :meth:`~builder.engine.AgentEngine.initialize`-d
    (so scanning + approved-roots have happened). The spine then runs, in code:
    scaffold backbone → draft entities → build_and_validate → bounded fix loop.

    No LLM decides control flow and every step is deterministic, so the same input
    state yields the same built crate (the determinism guarantee the eval harness
    asserts).

    Args:
        engine: An initialized headless :class:`~builder.engine.AgentEngine`. The
            spine mutates ``engine.state`` in place and routes all tool calls
            through the engine (so they are profiled and validation is cached).

    Returns:
        ``{"ok", "conformance", "issues", "scaffold", "drafted", "fix_rounds"}`` —
        the final ``build_and_validate`` verdict (``ok`` / per-layer
        ``conformance`` / routed ``issues``) plus a small trace of what each step
        did. ``conformance`` always carries the ``base`` / ``isa`` / ``tox`` keys.
    """
    scaffold = _scaffold_backbone(engine)
    drafted = _draft_entities(engine)
    validation, fix_rounds = _run_fix_loop(engine)

    return {
        "ok": bool(validation.get("ok")),
        "conformance": validation.get("conformance", {}),
        "issues": validation.get("issues", []),
        "scaffold": scaffold,
        "drafted": drafted,
        "fix_rounds": fix_rounds,
    }
