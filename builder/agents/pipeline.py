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
2. **Draft entities** — enrich entities via the bounded §14.2 "drafter-leaf"
   (``draft_entity_fields``, a cheap LLM). The spine gathers a free-text context
   from what the engine carries (crate title / description + scanned-file digest)
   and, for each draftable entity missing descriptive fields, applies only the
   leaf's NON-identifier fields. **This step is a strict no-op when no LLM provider
   is configured** (the deterministic spine, its tests, and the A/B path are
   unchanged) and when there is no usable context. It is D5-safe: identifiers are
   never set or overwritten — those come from lookups.
3. **build_and_validate** in memory (no disk write).
4. **Fix loop**: call ``fix_required_issues`` and re-validate, bounded to
   ``_MAX_FIX_ROUNDS`` rounds, stopping when no REQUIRED issue remains or a round
   makes no progress (deterministic dispatch only — see :mod:`builder.tools.repair`).
5. Return a result dict with the final per-layer conformance.

Determinism contract: with **no LLM provider configured** every step is
deterministic — the scaffold is idempotent, the drafter-leaf step is a strict
no-op (it never calls a model), and the fix loop uses only deterministic
dispatch — so the same input state ⇒ an identical built ``@graph`` (the headline
win the eval harness asserts on the deterministic A/B path). When a provider IS
configured the drafter-leaf (step 2) introduces a bounded, D5-safe extraction
call, trading strict graph-hash determinism for richer drafted content.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from builder.config import get_provider

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)


def draft_entity_fields(
    entity_type: str, context: str, *, model: str | None = None
) -> dict[str, Any]:
    """Lazy, no-op-safe shim over :func:`builder.agents.leaves.draft_entity_fields`.

    The real leaf lives in :mod:`builder.agents.leaves`, which imports
    ``langchain_core`` at module load. The deterministic spine, however, must stay
    importable (and runnable) in the **default environment without the
    ``langchain`` extra** — that is how the eval ``--arch pipeline`` path and CI run
    it with zero tokens. So we import the leaf lazily, *inside* this shim, and only
    ever after :func:`_draft_entities` has confirmed an LLM provider is configured
    (an unconfigured provider short-circuits before this is ever called).

    Defining the leaf as a module-level attribute here also gives tests a stable
    monkeypatch target (``builder.agents.pipeline.draft_entity_fields``).
    """
    from builder.agents.leaves import draft_entity_fields as _leaf

    return _leaf(entity_type, context, model=model)


# Fields the drafter-leaf result must NEVER write onto a state entity (D5: Verify,
# Don't Trust). The leaf already prunes identifier-bearing fields from the schema
# the model sees and strips them from its output; we defend in depth here so the
# spine never sets an identifier / `@id` / `entity_id` regardless of what the leaf
# returns. Identifiers come from lookups, never from extraction.
_FORBIDDEN_APPLY_FIELDS: frozenset[str] = frozenset(
    {
        "@id",
        "id",
        "entity_id",
        "identifier",
        "accession",
        "inchikey",
        "smiles",
        "molecular_formula",
        "pubchem_cid",
        "cas",
        "casrn",
        "cas_number",
        "orcid",
        "ror",
        "doi",
        "term_code",
        "in_defined_term_set",
        "property_id",
        "unit_code",
        "url",
    }
)

# Entity types the drafter-leaf enriches with descriptive fields. The backbone
# (Investigation / Study / Assay) is always present after the scaffold step; the
# domain types are enriched only when already seeded in state. Process / reference
# / contextual types are intentionally excluded — their content is wired
# deterministically (link / resolver), not extracted as free text.
_DRAFTABLE_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "Investigation",
        "Study",
        "Assay",
        "MolecularEntity",
        "CellLineSample",
        "LabProtocol",
        "Sample",
        "Person",
        "Organization",
        "Publication",
    }
)

# Descriptive fields the spine is willing to fill from the leaf. Restricting to a
# small, safe set keeps the enrichment bounded and predictable (the leaf may return
# a long open-schema tail). These are non-identifier free-text fields only.
_DESCRIPTIVE_APPLY_FIELDS: frozenset[str] = frozenset(
    {"name", "description"}
)

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


def _gather_context(engine: AgentEngine) -> str:
    """Assemble a free-text context string for the drafter-leaf from the engine.

    Pulls from what an initialized engine actually carries: the crate title and
    description (``state.metadata``) and the scanned-file inventory
    (``state.scanned_files`` — filenames plus any first-row previews the scanner
    captured). Returns ``""`` when nothing usable is available, which the caller
    treats as a strict no-op (no provider call is made).

    The context is intentionally a *digest*, not the full file bodies — the leaf is
    a bounded single call, and the spine never re-reads disk here.
    """
    state = engine.state
    parts: list[str] = []

    title = (state.metadata.title or "").strip()
    if title:
        parts.append(f"Title: {title}")
    description = (state.metadata.description or "").strip()
    if description:
        parts.append(f"Description: {description}")

    if state.scanned_files:
        file_lines: list[str] = []
        for f in state.scanned_files:
            line = f"- {f.filename}"
            if f.first_rows:
                preview = " | ".join(str(r) for r in f.first_rows[:3])
                if preview.strip():
                    line += f": {preview}"
            file_lines.append(line)
        if file_lines:
            parts.append("Scanned files:\n" + "\n".join(file_lines))

    return "\n\n".join(parts).strip()


def _draft_entities(engine: AgentEngine) -> dict[str, Any]:
    """Step 2 — enrich entities via the bounded drafter-leaf (§14.2).

    Wires the cheap-model drafter-leaf (:func:`draft_entity_fields`) into the
    spine: it gathers a free-text ``context`` from what the engine carries (crate
    title / description + scanned-file digest), and for each draftable entity that
    is missing descriptive fields it calls the leaf with ``(entity_type, context)``
    and applies only the returned **non-identifier descriptive** fields.

    Guarantees:

    * **No-op when no LLM provider is configured.** Detected via
      :func:`builder.config.get_provider`. With no provider the spine stays fully
      deterministic — nothing is mutated and the leaf is never imported/called, so
      the existing pipeline tests and the deterministic A/B path are unchanged.
    * **No-op when there is no usable context** (untitled, undescribed, unscanned
      crate) — there is nothing to extract from, so the leaf is not called.
    * **D5 (Verify, Don't Trust).** Identifier / ``@id`` / ``entity_id`` fields are
      never set or overwritten (:data:`_FORBIDDEN_APPLY_FIELDS`); the spine applies
      only what the leaf returns and never fabricates. The leaf itself already
      strips identifiers — this is defence in depth.
    * **Fill, don't clobber.** Only fields the entity is *missing* (or carries an
      empty value for) are filled; existing values are preserved.

    Returns ``{"drafted": [<entity ids enriched>], "fields_applied": <n>}``.
    """
    drafted: list[str] = []
    fields_applied = 0
    noop = {"drafted": drafted, "fields_applied": fields_applied}

    # Gate 1 — provider must be configured, else strict no-op (deterministic spine).
    if get_provider() is None:
        return noop

    # Gate 2 — there must be usable context to extract from, else strict no-op.
    context = _gather_context(engine)
    if not context:
        return noop

    for entity in engine.state.list_entities():
        if entity.type not in _DRAFTABLE_ENTITY_TYPES:
            continue

        # Only enrich entities that are missing at least one descriptive field;
        # nothing to do for already-complete ones.
        missing = [
            field
            for field in _DESCRIPTIVE_APPLY_FIELDS
            if not str(entity.fields.get(field) or "").strip()
        ]
        if not missing:
            continue

        try:
            leaf_fields = draft_entity_fields(entity.type, context)
        except Exception as exc:  # noqa: BLE001 - a flaky leaf must not break the spine
            logger.warning(
                "drafter-leaf failed for %s (%s); skipping enrichment: %s",
                entity.entity_id,
                entity.type,
                exc,
            )
            continue

        if not isinstance(leaf_fields, dict):
            continue

        applied: dict[str, Any] = {}
        for field, value in leaf_fields.items():
            # D5: never set an identifier / @id / entity_id.
            if field in _FORBIDDEN_APPLY_FIELDS:
                continue
            # Bound enrichment to the safe descriptive set, and only fields the
            # entity is actually missing (fill, don't clobber).
            if field not in missing:
                continue
            if value is None or not str(value).strip():
                continue
            applied[field] = value

        if applied:
            entity.set_fields_from_dict(applied, source="llm")
            drafted.append(entity.entity_id)
            fields_applied += len(applied)

    return {"drafted": drafted, "fields_applied": fields_applied}


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

    No LLM decides control flow. With **no LLM provider configured** every step is
    deterministic (the drafter-leaf step is a strict no-op), so the same input
    state yields the same built crate — the determinism guarantee the deterministic
    A/B path of the eval harness asserts. When a provider is configured, the
    bounded drafter-leaf (step 2) enriches entities with descriptive content.

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
