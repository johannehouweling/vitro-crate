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
from typing import TYPE_CHECKING, Any, Callable

from builder.config import get_provider

# Deterministic given/family split lives in the pure drafter module so the
# materialize path here and every direct ``draft_person`` call share ONE contract
# (comma-form inverted; a lone token kept as a family-name candidate, never
# mis-placed into givenName). Re-exported under the legacy private name so the
# materialize-path callsite and tests can keep using ``_split_person_name``.
from builder.tools.drafters import split_person_name as _split_person_name

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)

# A usage sink receives one leaf call's token usage as
# ``(input_tokens, output_tokens, model_name)``. The spine passes a sink that
# logs each leaf call's usage to the engine profiler so the eval harness records
# real per-case token counts for the ``--arch pipeline`` arm (Issue #221).
UsageSink = Callable[[int | None, int | None, str | None], None]


def draft_entity_fields(
    entity_type: str,
    context: str,
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
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
    monkeypatch target (``builder.agents.pipeline.draft_entity_fields``). The
    ``usage_sink`` is forwarded so the leaf can report its token usage (#221).
    """
    from builder.agents.leaves import draft_entity_fields as _leaf

    return _leaf(entity_type, context, model=model, usage_sink=usage_sink)


def extract_plan(
    context: str,
    *,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Lazy, no-op-safe shim over :func:`builder.agents.leaves.extract_plan`.

    Stage A of the §14 hybrid loop: the whole-document candidate-plan extractor.
    Mirrors the :func:`draft_entity_fields` shim — the real leaf imports
    ``langchain_core`` at module load, so the deterministic spine must import it
    lazily (inside this shim) and only ever after :func:`_materialize_plan` has
    confirmed an LLM provider is configured. Defining the leaf as a module-level
    attribute here also gives tests a stable monkeypatch target
    (``builder.agents.pipeline.extract_plan``). The ``usage_sink`` is forwarded so
    the leaf can report its token usage (#221).
    """
    from builder.agents.leaves import extract_plan as _leaf

    return _leaf(context, model=model, usage_sink=usage_sink)


def _as_int(value: Any) -> int:
    """Coerce a possibly-missing/None token count to a non-negative int."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _make_usage_logger(
    engine: AgentEngine, totals: dict[str, int]
) -> UsageSink:
    """Build a :data:`UsageSink` that records one leaf call's token usage (#221).

    For each leaf call it (1) accumulates ``input``/``output`` tokens into
    *totals* (the running per-run sum the spine surfaces in ``run_pipeline``'s
    result) and (2) logs a ``node_end``/``node="model"`` event to the engine
    profiler — the SAME profile-event shape the ReAct model node emits — so
    :func:`eval.metrics.mine_profile_metrics` mines pipeline tokens identically to
    the ReAct arm with no runner/factory changes. When no profiler is active (e.g.
    an engine that was never initialized) the accumulation still happens; only the
    profile write is skipped.
    """

    def _sink(
        input_tokens: int | None,
        output_tokens: int | None,
        model_name: str | None,
    ) -> None:
        in_t = _as_int(input_tokens)
        out_t = _as_int(output_tokens)
        totals["input_tokens"] += in_t
        totals["output_tokens"] += out_t
        profiler = getattr(engine, "profiler", None)
        if profiler is not None:
            profiler.log_event(
                event="node_end",
                node="model",
                iteration=engine.state.iteration_count,
                input_tokens=in_t,
                output_tokens=out_t,
                model_name=model_name,
            )

    return _sink


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

# Token-safety budget for the free-text context fed to the single bounded
# extraction/drafter leaf (#231). `_gather_context` now reads non-tabular rich
# file BODIES (`.json` / `.docx` / `.pdf` …) — not just filenames — so it must
# cap how much disk content reaches the leaf so the one bounded call stays
# affordable. The cap is applied BOTH per-file (each body excerpt is truncated to
# `_MAX_CONTEXT_CHARS`) AND to the total accumulated body content (the running sum
# of body excerpts never exceeds `_MAX_CONTEXT_CHARS`), so a folder of many large
# files cannot blow the budget. ~8k chars is roughly a couple thousand tokens —
# enough for a study title / abstract / SOP heading to survive, small enough to
# stay cheap.
_MAX_CONTEXT_CHARS = 8000


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


def _read_body_excerpt(path: str, approved_roots: set[str], remaining: int) -> str | None:
    """Read a bounded BODY excerpt of *path*, fail-closed to *approved_roots* (#231).

    Mirrors the engine's fail-closed containment guard (``engine.py`` /
    :func:`builder.tools.scanner._contain`): the read is refused unless *path*
    resolves inside an approved scan root, so the spine never widens filesystem
    access on the leaf's say-so. Dispatches to the existing
    :func:`builder.tools.file_readers.read_file` (so ``.json`` / ``.docx`` /
    ``.xlsx`` / ``.pdf`` / text are all handled by code that already exists — no
    hand-rolled parsing) and never lets a reader error escape: any
    :class:`OSError`, missing optional dependency, or malformed-file error is
    logged and yields ``None`` (skip), so a single unreadable file can never break
    the deterministic spine.

    The returned excerpt is truncated to ``min(remaining, _MAX_CONTEXT_CHARS)`` so
    both the per-file cap and the caller's total budget are honoured. Returns
    ``None`` when nothing readable was produced (so the caller emits no body line).
    """
    if remaining <= 0:
        return None

    # Lazy imports: keep the spine importable in the default env, and only touch
    # the readers / containment primitive when there is actually a body to read.
    from builder.tools.file_readers import read_file
    from builder.tools.scanner import _contain

    # Fail-closed containment: refuse any path not inside an approved scan root.
    if _contain(path, approved_roots) is None:
        logger.debug("Skipping body read of %s — outside approved scan roots (#231).", path)
        return None

    try:
        body = read_file(path)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("Body read failed for %s; skipping: %s", path, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a malformed file must not break the spine
        logger.warning("Unexpected body-read error for %s; skipping: %s", path, exc)
        return None

    if not body or not body.strip():
        return None

    cap = min(remaining, _MAX_CONTEXT_CHARS)
    excerpt = body.strip()
    if len(excerpt) > cap:
        excerpt = excerpt[:cap].rstrip() + " […]"
    return excerpt


def _gather_context(engine: AgentEngine) -> str:
    """Assemble a free-text context string for the drafter/extraction leaf (#231).

    Pulls from what an initialized engine actually carries: the crate title and
    description (``state.metadata``) and the scanned-file inventory
    (``state.scanned_files``). For each scanned file it prefers the cheap tabular
    ``first_rows`` preview the scanner already captured; for non-tabular rich files
    that lack a preview (``.json`` / ``.docx`` / ``.pdf`` …) it now reads a
    **bounded body excerpt** from disk via :func:`_read_body_excerpt` so document
    BODIES — study titles, abstracts, SOP headings — reach the single bounded leaf
    rather than filenames alone. Without this the leaf saw only filenames + tiny
    previews and ``extract_plan`` returned an empty plan, so the backbone fell back
    to the literal default names (#231).

    Body reads are **fail-closed to ``state.approved_scan_roots``** and never raise
    out of the spine (see :func:`_read_body_excerpt`). Output is bounded by
    :data:`_MAX_CONTEXT_CHARS` per file AND in total, so the one bounded leaf call
    stays token-safe regardless of how many large files were scanned.

    Returns ``""`` when nothing usable is available, which the caller treats as a
    strict no-op (no provider call is made) — preserving the no-context strict-noop
    and no-provider determinism guarantees.
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
        approved_roots = state.approved_scan_roots
        file_lines: list[str] = []
        body_budget = _MAX_CONTEXT_CHARS  # total body content across all files
        for f in state.scanned_files:
            line = f"- {f.filename}"
            if f.first_rows:
                # Prefer the cheap preview the scanner already captured — no disk read.
                preview = " | ".join(str(r) for r in f.first_rows[:3])
                if preview.strip():
                    line += f": {preview}"
            elif body_budget > 0:
                # No tabular preview: read a bounded body excerpt (fail-closed to
                # approved roots; never raises) so the leaf sees the document body.
                excerpt = _read_body_excerpt(f.path, approved_roots, body_budget)
                if excerpt:
                    body_budget -= len(excerpt)
                    line += f":\n{excerpt}"
            file_lines.append(line)
        if file_lines:
            parts.append("Scanned files:\n" + "\n".join(file_lines))

    return "\n\n".join(parts).strip()


def _draft_entities(
    engine: AgentEngine, usage_sink: UsageSink | None = None
) -> dict[str, Any]:
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
            leaf_fields = draft_entity_fields(
                entity.type, context, usage_sink=usage_sink
            )
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


# The conservative default process a protocol governs when the plan gives no (or
# an unmatched) hint: the central exposure/assay step, then a measurement readout.
_PROTOCOL_DEFAULT_PROCESS_TYPES: tuple[str, ...] = (
    "Exposure",
    "EndpointReadout",
    "DataAnalysis",
    "CellCulture",
)


def _select_process_for_protocol(
    steps: list[dict[str, Any]], process_hint: str
) -> str | None:
    """Pick the LabProcess id a plan protocol governs (D5-conservative).

    ``steps`` is :func:`draft_process_chain`'s per-step summary (each a dict with
    ``process_id`` / ``process_type``). The free-text ``process_hint`` from the
    plan is matched, in order, against (1) a step's ``process_type`` and (2) a
    substring of its name; on no match we fall back to the central
    exposure/assay step (:data:`_PROTOCOL_DEFAULT_PROCESS_TYPES`). Returns the
    chosen ``process_id`` or ``None`` when there are no processes to link to —
    the protocol is still minted; only the (uncertain) link is left for the
    guidance loop. No identifier is ever fabricated.
    """
    if not steps:
        return None

    hint = process_hint.strip().lower()
    if hint:
        # (1) match the hint against a step's process_type (case-insensitive).
        for step in steps:
            ptype = str(step.get("process_type") or "")
            if ptype.lower() == hint and step.get("process_id"):
                return str(step["process_id"])
        # (2) match the hint as a substring of a step name.
        for step in steps:
            sname = str(step.get("name") or "").lower()
            if sname and (hint in sname or sname in hint) and step.get("process_id"):
                return str(step["process_id"])

    # No usable hint / no match — conservatively attach to the central step.
    by_type = {str(s.get("process_type") or ""): s for s in steps}
    for ptype in _PROTOCOL_DEFAULT_PROCESS_TYPES:
        step = by_type.get(ptype)
        if step and step.get("process_id"):
            return str(step["process_id"])

    # Fall back to the first process that has an id.
    for step in steps:
        if step.get("process_id"):
            return str(step["process_id"])
    return None


def _materialize_plan(
    engine: AgentEngine, usage_sink: UsageSink | None = None
) -> dict[str, Any]:
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
    * each ``protocols[]`` → ``draft_protocol`` (a ``LabProtocol`` from the
      name/description only — D5: no identifier) which is then linked to the
      ``LabProcess`` it governs via the ``labprotocol`` reference field (resolved
      to ``executesLabProtocol`` at build time, isa_tox.md). The plan's optional
      free-text ``process_hint`` is matched conservatively (by ``process_type``,
      then by step name) to choose the process; with no match it attaches to the
      central exposure/assay step, and an unresolvable link is left for the
      guidance loop rather than guessed (:func:`_select_process_for_protocol`).
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
    * each ``publications[]`` → :func:`resolve_publication` (#219/#224). A plan
      carries a title ONLY (D5 — no DOI); the composite searches Crossref by title
      and commits a DOI-backed ``ScholarlyArticle`` (+ authors) ONLY on a
      confident match (counted under ``publications``). On no confident match it
      returns ``ok=False`` and creates nothing, so the title is kept under
      ``publications_deferred`` for a later resolution. A DOI is never fabricated
      from a title — the identifier always comes from the Crossref lookup, never
      the plan (D5).

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

    Returns ``{"study", "compounds", "cell_lines", "protocols", "processes",
    "aops", "people", "publications", "publications_deferred"}`` — per-section
    counts of what was materialized, plus the titles of publications that found no
    confident DOI match and were deferred for a later resolution.
    """
    result: dict[str, Any] = {
        "study": 0,
        "compounds": 0,
        "cell_lines": 0,
        "protocols": 0,
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
        plan = extract_plan(context, usage_sink=usage_sink)
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
    chain_steps_summary: list[dict[str, Any]] = []
    if assay_id and chain_steps:
        try:
            chain_result = engine.run_tool(
                "draft_process_chain", assay_id=assay_id, chain=chain_steps
            )
            result["processes"] = len(chain_result.get("process_ids") or [])
            chain_steps_summary = list(chain_result.get("steps") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_process_chain failed: %s", exc)

    # --- protocols: draft a LabProtocol from the name/description (D5 — no id) and
    # link the LabProcess(es) it governs via the `labprotocol` ref field, which the
    # crate mapping resolves to `executesLabProtocol` (isa_tox.md). The plan may
    # carry a free-text `process_hint`; we map it to a process conservatively (by
    # process_type, then by step name) and, when nothing matches, fall back to the
    # central exposure/assay step. An unresolvable hint links nothing and is left
    # for the guidance loop rather than guessed at. ---
    for protocol in plan.get("protocols") or []:
        name = str((protocol or {}).get("name") or "").strip()
        if not name:
            continue
        proto_hints: dict[str, Any] = {"name": name}
        description = str((protocol or {}).get("description") or "").strip()
        if description:
            proto_hints["description"] = description
        try:
            proto = engine.run_tool("draft_protocol", hints=proto_hints)
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_protocol failed for %r: %s", name, exc)
            continue
        result["protocols"] += 1

        proto_id = getattr(proto, "entity_id", None)
        if not proto_id:
            continue
        target_process_id = _select_process_for_protocol(
            chain_steps_summary, str((protocol or {}).get("process_hint") or "")
        )
        if target_process_id is None:
            continue
        try:
            # `labprotocol` is a reference field on LabProcess (-> executesLabProtocol);
            # set_fields wires it, the crate mapping resolves it at build time.
            engine.run_tool(
                "set_fields",
                entity_id=target_process_id,
                fields={"labprotocol": proto_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "linking protocol %r to process %r failed: %s",
                proto_id,
                target_process_id,
                exc,
            )

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

    # --- publications: resolve each title via resolve_publication (#219/#224). A
    # plan carries a title ONLY (D5 — no DOI). resolve_publication searches Crossref
    # by title and commits a DOI-backed ScholarlyArticle (+ authors) ONLY on a
    # confident match; on no confident match it returns ok=False and creates
    # nothing, so the title is kept under `publications_deferred`. A DOI is never
    # fabricated from a title here — the identifier always comes from the Crossref
    # lookup, never the plan. ---
    for publication in plan.get("publications") or []:
        title = str((publication or {}).get("title") or "").strip()
        if not title:
            continue
        try:
            pub_result = engine.run_tool("resolve_publication", title=title)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_publication failed for %r: %s", title, exc)
            result["publications_deferred"].append(title)
            continue
        if isinstance(pub_result, dict) and pub_result.get("ok"):
            result["publications"] += 1
        else:
            # No confident DOI match — keep the title for a later resolution (D5).
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
        "fix_rounds", "usage"}`` — the final ``build_and_validate`` verdict
        (``ok`` / per-layer ``conformance`` / routed ``issues``) plus a small
        trace of what each step did. ``conformance`` always carries the ``base`` /
        ``isa`` / ``tox`` keys. ``usage`` is the accumulated token usage across all
        leaf LLM calls (``{"input_tokens", "output_tokens", "total_tokens"}``,
        all 0 when no provider is configured) — additive (#221), and ALSO written
        to ``profile.ndjson`` as ``node_end``/``node="model"`` events so the eval
        harness mines it exactly as it does the ReAct arm.
    """
    # Accumulate per-run leaf token usage; the sink also logs each call to the
    # profiler so eval/runner.py mines it via the same path as the ReAct arm.
    totals = {"input_tokens": 0, "output_tokens": 0}
    usage_sink = _make_usage_logger(engine, totals)

    scaffold = _scaffold_backbone(engine)
    materialized = _materialize_plan(engine, usage_sink)
    drafted = _draft_entities(engine, usage_sink)
    validation, fix_rounds = _run_fix_loop(engine)

    return {
        "ok": bool(validation.get("ok")),
        "conformance": validation.get("conformance", {}),
        "issues": validation.get("issues", []),
        "scaffold": scaffold,
        "materialized": materialized,
        "drafted": drafted,
        "fix_rounds": fix_rounds,
        "usage": {
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        },
    }
