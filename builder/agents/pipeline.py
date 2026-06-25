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


def extract_plan(context: str, *, model: str | None = None) -> dict[str, Any]:
    """Lazy, no-op-safe shim over :func:`builder.agents.leaves.extract_plan`.

    Stage A of the §14 hybrid loop: the whole-document candidate-plan extractor.
    Mirrors the :func:`draft_entity_fields` shim — the real leaf imports
    ``langchain_core`` at module load, so the deterministic spine must import it
    lazily (inside this shim) and only ever after :func:`_materialize_plan` has
    confirmed an LLM provider is configured. Defining the leaf as a module-level
    attribute here also gives tests a stable monkeypatch target
    (``builder.agents.pipeline.extract_plan``).
    """
    from builder.agents.leaves import extract_plan as _leaf

    return _leaf(context, model=model)


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


# The id of the scaffolded Assay / Study the materialized chain / AOP wire onto.
# Resolved from state (the backbone is scaffolded first) so the chain and AOP
# subgraph hang off the real backbone rather than minting an orphan one.
def _first_entity_id(engine: AgentEngine, entity_type: str) -> str | None:
    """entity_id of the first state entity of *entity_type*, or ``None``."""
    return next(
        (e.entity_id for e in engine.state.list_entities() if e.type == entity_type),
        None,
    )


def _split_person_name(name: str) -> tuple[str, str]:
    """Split a full name into ``(givenName, familyName)`` deterministically.

    A purely descriptive parse of the plan's name (no identifier involved, so
    D5-safe): the last whitespace-separated token is the family name and the rest
    is the given name. ISA REQUIRES a non-empty given name on a Person, so a
    single-token name maps wholesale to the given name (family left empty). Used
    to make a plan-materialized Person ISA-conformant without an external lookup.
    """
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _materialize_plan(engine: AgentEngine) -> dict[str, Any]:
    """Stage B (§14) — materialize the extracted candidate plan via composites.

    Bridges the bounded whole-document extractor (:func:`extract_plan`, Stage A)
    to the deterministic materialization composites: it gathers the same
    free-text ``context`` the drafter-leaf uses, asks the leaf for a CANDIDATE
    PLAN (names only — no identifiers), then deterministically turns each plan
    section into real ISA-Tox entities by calling the **idempotent** composites
    *through the engine*. It never re-implements tool logic and never hand-rolls
    JSON-LD.

    Plan → composite mapping (each call wrapped in ``try/except`` so one failing
    section never breaks the spine — the failure is logged and the next section
    proceeds):

    * ``study`` → :func:`scaffold_isa_backbone` (idempotent; reuses the backbone
      scaffolded in step 1, only filling a Study name/description from the plan).
    * each ``compounds[]`` → :func:`resolve_compound` (mints the
      ``MolecularEntity`` and its **verified** identifiers; the plan supplies the
      NAME only — D5).
    * each ``cell_lines[]`` → ``draft_cell_line_sample`` (a ``CellLineSample``
      from the name only; the Cellosaurus accession is a later lookup, not the
      plan).
    * ``process_chain[]`` → ONE :func:`draft_process_chain` onto the scaffolded
      Assay, mapping each step's ``process_type`` / ``name`` / hints (the
      composite synthesizes EndpointReadout / DataAnalysis outputs).
    * each ``aops[]`` → :func:`materialize_aop_subgraph` onto the scaffolded
      Study (the only model input is the numeric ``aop_id``; every node id comes
      from AOP-Wiki — D5).
    * each ``people[]`` → ``draft_person`` with the name plus a deterministic
      ``givenName`` / ``familyName`` split of that name (ISA REQUIRES a non-empty
      given name; splitting a name is descriptive parsing, not identifier
      fabrication, so it is D5-safe). ORCID stays empty for a later lookup.
    * each ``publications[]`` is **deferred, not materialized.** A plan carries a
      title ONLY (D5 — no DOI), but ISA REQUIRES a ScholarlyArticle to have an
      identifier and BASE requires the auto-wired root ``citation`` @id to be an
      absolute URI — both unreachable from a title alone without fabricating a
      DOI. Per D5 (Verify, Don't Trust) we never invent that DOI here; the title
      is surfaced under ``publications_deferred`` for a later
      ``draft_publication_with_authors(doi=...)`` once a DOI is resolved. This
      follows the project's design docs over the literal task wording, which
      conflict on this one point (see ``.claude/CLAUDE.md``: "follow the
      documents and say so").

    Guarantees:

    * **No-op when no LLM provider is configured** (the same
      :func:`builder.config.get_provider` gate :func:`_draft_entities` uses) and
      **no-op when there is no usable context** — Stage A is never called, so the
      deterministic spine and its tests are unchanged.
    * **D5 (Verify, Don't Trust).** Only plan *names/titles* reach the composites;
      identifiers are produced by the composites' own lookups/verification and
      are never set or overwritten from the plan. ``extract_plan`` already strips
      identifiers from the plan; this step never re-introduces them.
    * **Idempotent.** Every composite reuses an existing entity (deterministic
      ids), so re-running the spine mints no duplicates.

    Returns ``{"study", "compounds", "cell_lines", "processes", "aops",
    "people", "publications", "publications_deferred"}`` — per-section counts of
    what was materialized, plus the titles of publications deferred for a later
    DOI lookup (``publications`` is therefore always ``0`` in this stage).
    """
    result: dict[str, Any] = {
        "study": 0,
        "compounds": 0,
        "cell_lines": 0,
        "processes": 0,
        "aops": 0,
        "people": 0,
        "publications": 0,
        "publications_deferred": [],
    }

    # Gate 1 — provider must be configured, else strict no-op (deterministic spine).
    if get_provider() is None:
        return result

    # Gate 2 — there must be usable context to plan from, else strict no-op.
    context = _gather_context(engine)
    if not context:
        return result

    try:
        plan = extract_plan(context)
    except Exception as exc:  # noqa: BLE001 - a flaky extractor must not break the spine
        logger.warning("extract_plan failed; skipping materialization: %s", exc)
        return result
    if not isinstance(plan, dict):
        return result

    # --- study: merge the plan's name/description into the scaffolded backbone ---
    study_plan = plan.get("study")
    if isinstance(study_plan, dict) and (study_plan.get("name") or study_plan.get("description")):
        study_hints = {
            key: study_plan[key]
            for key in ("name", "description")
            if str(study_plan.get(key) or "").strip()
        }
        try:
            engine.run_tool("scaffold_isa_backbone", study=study_hints)
            result["study"] = 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("scaffold_isa_backbone (plan study) failed: %s", exc)

    # --- compounds: resolve_compound mints the MolecularEntity + verified ids ---
    for compound in plan.get("compounds") or []:
        name = str((compound or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            # D5: only the NAME is passed; identifiers come from the lookup.
            engine.run_tool("resolve_compound", name=name)
            result["compounds"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_compound failed for %r: %s", name, exc)

    # --- cell lines: a CellLineSample from the name only (accession is a lookup) ---
    for cell_line in plan.get("cell_lines") or []:
        name = str((cell_line or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            engine.run_tool("draft_cell_line_sample", name=name, hints={})
            result["cell_lines"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_cell_line_sample failed for %r: %s", name, exc)

    # --- process chain: ONE draft_process_chain onto the scaffolded Assay ---
    assay_id = _first_entity_id(engine, "Assay")
    chain_steps = [
        {
            "process_type": step["process_type"],
            **(
                {"hints": {"name": step["name"]}}
                if str((step or {}).get("name") or "").strip()
                else {}
            ),
        }
        for step in (plan.get("process_chain") or [])
        if isinstance(step, dict) and step.get("process_type")
    ]
    if assay_id and chain_steps:
        try:
            chain_result = engine.run_tool(
                "draft_process_chain", assay_id=assay_id, chain=chain_steps
            )
            result["processes"] = len(chain_result.get("process_ids") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_process_chain failed: %s", exc)

    # --- AOPs: materialize each subgraph and wire it onto the scaffolded Study ---
    study_id = _first_entity_id(engine, "Study")
    for aop in plan.get("aops") or []:
        aop_id = str((aop or {}).get("aop_id") or "").strip()
        if not aop_id:
            continue
        try:
            aop_result = engine.run_tool(
                "materialize_aop_subgraph", aop_id=aop_id, study_id=study_id
            )
            if aop_result.get("aop_entity_id"):
                result["aops"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("materialize_aop_subgraph failed for %r: %s", aop_id, exc)

    # --- people: a Person from the name + a deterministic given/family split ---
    for person in plan.get("people") or []:
        name = str((person or {}).get("name") or "").strip()
        if not name:
            continue
        given, family = _split_person_name(name)
        # ISA REQUIRES a non-empty given name; the split is descriptive parsing of
        # the plan's name, not identifier fabrication (D5-safe). ORCID stays empty.
        person_hints = {"givenName": given}
        if family:
            person_hints["familyName"] = family
        try:
            engine.run_tool("draft_person", name=name, hints=person_hints)
            result["people"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_person failed for %r: %s", name, exc)

    # --- publications: DEFERRED, not materialized (D5). A plan carries a title
    # only, but ISA REQUIRES a ScholarlyArticle identifier and BASE requires the
    # auto-wired root citation @id to be an absolute URI — neither reachable from
    # a title without fabricating a DOI, which D5 forbids. Surface the titles for a
    # later draft_publication_with_authors(doi=...) once a DOI is resolved. ---
    for publication in plan.get("publications") or []:
        title = str((publication or {}).get("title") or "").strip()
        if title:
            result["publications_deferred"].append(title)

    return result


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
    scaffold backbone → materialize plan → draft entities → build_and_validate →
    bounded fix loop.

    No LLM decides control flow. With **no LLM provider configured** every step is
    deterministic (the materialize-plan and drafter-leaf steps are strict no-ops),
    so the same input
    state yields the same built crate — the determinism guarantee the deterministic
    A/B path of the eval harness asserts. When a provider is configured, the
    bounded drafter-leaf (step 2) enriches entities with descriptive content.

    Args:
        engine: An initialized headless :class:`~builder.engine.AgentEngine`. The
            spine mutates ``engine.state`` in place and routes all tool calls
            through the engine (so they are profiled and validation is cached).

    Returns:
        ``{"ok", "conformance", "issues", "scaffold", "materialized", "drafted",
        "fix_rounds"}`` — the final ``build_and_validate`` verdict (``ok`` /
        per-layer ``conformance`` / routed ``issues``) plus a small trace of what
        each step did. ``conformance`` always carries the ``base`` / ``isa`` /
        ``tox`` keys.
    """
    scaffold = _scaffold_backbone(engine)
    materialized = _materialize_plan(engine)
    drafted = _draft_entities(engine)
    validation, fix_rounds = _run_fix_loop(engine)

    return {
        "ok": bool(validation.get("ok")),
        "conformance": validation.get("conformance", {}),
        "issues": validation.get("issues", []),
        "scaffold": scaffold,
        "materialized": materialized,
        "drafted": drafted,
        "fix_rounds": fix_rounds,
    }
