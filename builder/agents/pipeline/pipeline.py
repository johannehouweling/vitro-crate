"""The deterministic pipeline spine (Issue #179 — AGENTS.md §14.5).

This is the **code-driven** orchestrator that the §14 architecture promotes the
Priority 1-4 heuristic (§4) into: a pure, deterministic sequence with **no LLM
deciding control flow**. It operates on an already-:meth:`initialize`-d
:class:`~builder.engine.AgentEngine` (so scanning + approved-roots happened in the
engine) and drives the existing toolbox tools *through the engine* — it never
re-implements tool logic, only orchestrates it.

The sequence mirrors AGENTS.md §14.2::

    scaffold ISA backbone ─ draft entities ─ build_and_validate ─ fix loop

1. **Scaffold** the ISA backbone (``scaffold_isa_backbone``) — always. §14.3
   documents this as the deterministic path to a BASE/ISA/TOX-passing
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
import re
from typing import TYPE_CHECKING, Any, Callable

from builder.agents.llm import ModelOverrides
from builder.config import get_provider

# Deterministic given/family split lives in the pure drafter module so the
# materialize path here and every direct ``draft_person`` call share ONE contract
# (comma-form inverted; a lone token kept as a family-name candidate, never
# mis-placed into givenName). Re-exported under the legacy private name so the
# materialize-path callsite and tests can keep using ``_split_person_name``.
from builder.tools.drafters import split_person_name as _split_person_name
from builder.tools.field_kinds import drafter_visible_fields

if TYPE_CHECKING:
    from builder.engine import AgentEngine
    from builder.state import Entity

logger = logging.getLogger(__name__)

# A usage sink receives one leaf call's token usage as
# ``(input_tokens, output_tokens, model_name)``. The spine passes a sink that
# logs each leaf call's usage to the engine profiler so the eval harness records
# real per-case token counts for the ``--arch pipeline`` arm (Issue #221).
UsageSink = Callable[[int | None, int | None, str | None], None]

# A progress sink receives one concise human-readable line per pipeline phase
# (Issue #241). It defaults to a strict no-op so the eval and the determinism
# tests stay silent; the interactive build path threads in the CLI's `output`
# channel so a real user sees the deterministic spine is making progress rather
# than a terminal that looks frozen for ~tens of seconds.
ProgressSink = Callable[[str], Any]

# A session-save callback persists CrateState at a phase boundary (Issue #242).
# It mirrors :func:`builder.tools.session.save_session`'s call shape
# ``save(state, *, always_write=...)`` and defaults to the real function. The
# spine saves after each phase so a concurrent ``--dashboard`` (which watches
# ``sessions/<id>/crate_state.json``) reflects pipeline progress live instead of
# showing "No CrateState data available". Injected so the wiring is unit-testable
# with no disk I/O.
SaveFn = Callable[..., dict[str, Any]]


def _noop_progress(_message: str) -> None:
    """Default progress sink: discard. Keeps the spine silent under tests/eval."""


def _default_save() -> SaveFn:
    """The real session writer, imported lazily so the spine stays light.

    :func:`builder.tools.session.save_session` performs an atomic write of
    ``crate_state.json`` and is change-detecting (it skips identical content
    unless ``always_write=True``). Deferred so a test injecting its own ``save``
    is independent of the writer / disk.
    """
    from builder.tools.session import save_session

    return save_session


def draft_entity_fields(
    entity_type: str,
    context: str,
    *,
    overrides: ModelOverrides | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Lazy, no-op-safe shim over :func:`builder.agents.pipeline.leaves.draft_entity_fields`.

    The real leaf lives in :mod:`builder.agents.pipeline.leaves`, which imports
    ``langchain_core`` at module load. The deterministic spine, however, must stay
    importable (and runnable) in the **default environment without the
    ``langchain`` extra** — that is how the eval ``--arch pipeline`` path and CI run
    it with zero tokens. So we import the leaf lazily, *inside* this shim, and only
    ever after :func:`_draft_entities` has confirmed an LLM provider is configured
    (an unconfigured provider short-circuits before this is ever called).

    Defining the leaf as a module-level attribute here also gives tests a stable
    monkeypatch target (``builder.agents.pipeline.pipeline.draft_entity_fields``). The
    ``usage_sink`` is forwarded so the leaf can report its token usage (#221).
    """
    from builder.agents.pipeline.leaves import draft_entity_fields as _leaf

    return _leaf(entity_type, context, overrides=overrides, usage_sink=usage_sink)


def extract_plan(
    context: str,
    *,
    overrides: ModelOverrides | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Lazy, no-op-safe shim over :func:`builder.agents.pipeline.leaves.extract_plan`.

    Stage A of the §14 hybrid loop: the whole-document candidate-plan extractor.
    Mirrors the :func:`draft_entity_fields` shim — the real leaf imports
    ``langchain_core`` at module load, so the deterministic spine must import it
    lazily (inside this shim) and only ever after :func:`_materialize_plan` has
    confirmed an LLM provider is configured. Defining the leaf as a module-level
    attribute here also gives tests a stable monkeypatch target
    (``builder.agents.pipeline.pipeline.extract_plan``). The ``usage_sink`` is forwarded so
    the leaf can report its token usage (#221).
    """
    from builder.agents.pipeline.leaves import extract_plan as _leaf

    return _leaf(context, overrides=overrides, usage_sink=usage_sink)


def _as_int(value: Any) -> int:
    """Coerce a possibly-missing/None token count to a non-negative int."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _make_usage_logger(engine: AgentEngine, totals: dict[str, int]) -> UsageSink:
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
        # Also accumulate onto the crate's generator record so the exported crate
        # carries what the run cost. Independent of the profiler below: cost
        # accounting must not depend on instrumentation being enabled.
        try:
            engine.state.record_llm_usage({"input_tokens": in_t, "output_tokens": out_t})
        except Exception:  # noqa: BLE001 — accounting never breaks a leaf call
            logger.debug("Could not record leaf LLM usage", exc_info=True)
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
_DESCRIPTIVE_APPLY_FIELDS: frozenset[str] = frozenset({"name", "description"})

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
# extraction/drafter leaf (#231). `_gather_context` reads file BODIES — not just
# filenames — so it caps how much disk content reaches the leaf and the one
# bounded call stays affordable. EVERY emitted slice is charged against this
# total, previews included (#378); before that only body reads decremented it, so
# preview bytes were spent outside the arithmetic and the documented ceiling was
# silently exceeded.
#
# Raised 8000 -> 14000 with #378. That is roughly +1,300 tokens on one bounded
# cheap-model call, and it is a real if small regression in the "one bounded
# affordable call" property this block exists to protect. It buys the whole
# metadata workbook: compaction pays most of it back (-41% on the grid, -73% on
# the BioStudies descriptor), but chemicals 2-5 of the real S-VHPS26 deposit sit
# past 2,600 compacted chars and cannot be reached at 8000 under any weighting.
#
# Raised 14000 -> 16000 with #419. The two new compaction rules cut the S-VHPS26
# workbook from 22,007 chars to 8,675, but a tier's share is granted PER FILE, so
# seating the whole chemical table in tier 0 left the second tier-2 document (the
# README) only 1,000 chars — below the offset its own regression token sits at.
# The +2,000 restores that second document. Net against the old ceiling: 4x the
# chemicals for ~500 extra tokens on one bounded cheap-model call.
_MAX_CONTEXT_CHARS = 16000

# Per-file share by `_metadata_read_priority` tier (#378). Metadata-first was
# previously an ORDERING only: every file got an equal `_MAX_CONTEXT_CHARS // n`
# slice, so on the real S-VHPS26 deposit the priority-0 workbook that holds the
# cell line, RRID, author and all five chemicals emitted 298 chars while a
# priority-3 GraphPad file emitted 2,049. Equal shares provably cannot carry that
# workbook, so the budget is now weighted, not merely sorted.
#
# A tier claims its share only if it HAS files; an absent or under-spending tier
# flows its headroom down to the next (see `_gather_context`). That flow-down is
# what keeps a bulk-data-only deposit safe — with no metadata/doc files, the
# first priority-3 file inherits its own share plus every unclaimed one, rather
# than being held to 500.
#
# Tier 0 was raised 6000 -> 9000 with #419. At 6000 it cut the real S-VHPS26
# chemical table at chemical 15 of 19 — and because carry flows DOWNWARD only
# (tier 0 is weighted first, with `carry = 0`), no other tier's headroom could
# ever reach it. Most of the raise is paid for by the two new compaction rules,
# which took that workbook from 22,007 chars to 8,675; the rest is paid in
# tokens, `_MAX_CONTEXT_CHARS` having moved 14000 -> 16000 in the same change.
# The shares total 13,500 and the real deposit's emitted context grows 13,242 ->
# 16,224 chars, about +750 tokens, for 5 chemicals -> 19.
#
# A share is granted PER FILE, not per tier, so raising this one starves the
# tiers below on a multi-metadata deposit unless their shares are reserved
# first — see the `reserved` computation in `_gather_context`.
_TIER_SHARES: dict[int, int] = {0: 9000, 1: 2000, 2: 2000, 3: 500}

# Tiers whose body is worth a full `read_file` with compaction rather than the
# scanner's 20-rows-per-sheet preview (#378). The preview cap loses chemicals 3-5
# of the real workbook even with an unlimited char budget, because it truncates by
# ROW before any of this budgeting runs. `_EXCEL_PREVIEW_ROWS` deliberately stays
# at 20 — the binding constraint is chars, and raising it slows every scan.
_COMPACTED_READ_TIERS = frozenset({0, 1})

# `read_file` defaults to 100 rows per sheet, which is a *row* cut made before any
# char budgeting runs — so it cannot be traded against the budget and leaves no
# trace in the emitted slice. The real S-VHPS26 chemical sheet needs ~240 rows to
# state its 19 chemicals, and the 100-row default silently cut it at chemical 8
# (#419). Compacted tiers read to `read_excel`'s own ceiling instead; series
# folding, not a row cut, is what keeps the result affordable.
_COMPACTED_READ_MAX_ROWS = 500
_DEFAULT_READ_MAX_ROWS = 100


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


def _metadata_read_priority(filename: str) -> int:
    """Read-ordering rank for *filename* — lower sorts FIRST (Issue #179).

    The real S-VHPS26 run showed a large early *bulk-data* file starved the body
    budget so the structured metadata files read later got only their filename.
    Rank metadata-bearing files ahead of bulk data so they reach the leaf even
    under a tight budget. Ties keep the caller's original (stable) scan order.

    Ranks (case-insensitive on the basename):

    * ``0`` — an explicit ``*metadata*`` file (highest signal);
    * ``1`` — a BioStudies-style accession export / structured ``*.json``;
    * ``2`` — a README / SOP / protocol document or any ``.docx``/``.md``/``.txt``;
    * ``3`` — everything else (bulk data: ``.xlsx``/``.csv``/binary/…).
    """
    name = filename.lower()
    if "metadata" in name:
        return 0
    if name.endswith(".json"):
        return 1
    doc_suffixes = (".docx", ".md", ".txt", ".rtf", ".odt")
    doc_stems = ("readme", "sop", "protocol")
    if name.endswith(doc_suffixes) or any(stem in name for stem in doc_stems):
        return 2
    return 3


def _read_body_excerpt(
    path: str,
    approved_roots: set[str],
    remaining: int,
    *,
    compact: bool = False,
) -> str | None:
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

    ``compact`` opts into the shared boilerplate compactors (#378) — see
    :func:`builder.tools.file_readers.compact_grid_text`. The spine passes True
    for high-priority tiers, where the same signal then fits in far fewer chars.
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
        body = read_file(
            path,
            compact=compact,
            max_lines=_COMPACTED_READ_MAX_ROWS if compact else _DEFAULT_READ_MAX_ROWS,
        )
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


def _file_slice(f: Any, approved_roots: set[str], allowance: int, tier: int) -> str:
    """The single content slice emitted for one scanned file (#378).

    One path, one cap. A body read is preferred because it clears the scanner's
    20-rows-per-sheet preview limit; the ``first_rows`` preview is the fallback
    for a file that is unreadable or outside an approved root, and it is emitted
    in FULL up to *allowance*.

    Previously these were mutually exclusive — a previewed file could never be
    body-read and was pinned to three rows regardless of budget, which is what
    starved the highest-priority file in the inventory.
    """
    if allowance <= 0:
        return ""

    excerpt = _read_body_excerpt(
        f.path, approved_roots, allowance, compact=tier in _COMPACTED_READ_TIERS
    )
    if excerpt:
        return excerpt

    if f.first_rows:
        preview = " | ".join(str(r) for r in f.first_rows).strip()
        if len(preview) > allowance:
            preview = preview[:allowance].rstrip() + " […]"
        return preview
    return ""


def _gather_context(engine: AgentEngine) -> str:
    """Assemble a free-text context string for the drafter/extraction leaf (#231).

    Pulls from what an initialized engine actually carries: the crate title and
    description (``state.metadata``) and the scanned-file inventory
    (``state.scanned_files``). For each scanned file it prefers the cheap tabular
    ``first_rows`` preview the scanner already captured; for non-tabular rich files
    that lack a preview (``.json`` / ``.docx`` / ``.pdf`` …) it reads a
    **bounded body excerpt** from disk via :func:`_read_body_excerpt` so document
    BODIES — study titles, abstracts, SOP headings — reach the single bounded leaf
    rather than filenames alone. Without this the leaf saw only filenames + tiny
    previews and ``extract_plan`` returned an empty plan, so the backbone fell back
    to the literal default names (#231).

    **Metadata-first, priority-weighted budget (#179, corrected by #378).** Files
    are sorted by :func:`_metadata_read_priority` — ``*metadata*``, BioStudies
    ``*.json``, README/SOP docs, then bulk data — with scan order preserved within
    a tier. Each file then gets **one** content slice under **one** cap, sized by
    its tier's :data:`_TIER_SHARES` entry.

    Ordering alone was not enough, and that was the #378 bug: every file drew an
    equal slice, so the priority-0 workbook holding the cell line, RRID, author
    and five chemicals emitted 298 chars while a priority-3 GraphPad file emitted
    2,049. Priority now decides **chars**, not merely position.

    A tier that has no files, or whose files do not spend their share, flows the
    headroom down to the next tier — so a deposit of nothing but bulk CSVs still
    gives its first file a generous slice rather than the bare 500-char tier
    share. Priority-0/1 bodies are read with compaction
    (:data:`_COMPACTED_READ_TIERS`), which is what lets the whole workbook fit.

    The slice source is a single path: :func:`_read_body_excerpt` when the file is
    readable inside an approved root, otherwise the file's ``first_rows`` preview
    in FULL. The preview is a fallback, never a 3-row ceiling — the scanner has
    already paid to read it, and truncating it to three rows discarded 96% of
    what was in memory. **Every** emitted slice is charged against
    :data:`_MAX_CONTEXT_CHARS`, previews included, so the ceiling is honest.

    Body reads are **fail-closed to ``state.approved_scan_roots``** and never raise
    out of the spine (see :func:`_read_body_excerpt`). Output is bounded both per
    file AND in total, so the one bounded leaf call stays token-safe regardless of
    how many large files were scanned.

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

        # Order the WHOLE inventory metadata-first (stable within ties) so the
        # highest-signal files lead both the disk reads and the emitted digest.
        by_tier: dict[int, list[Any]] = {}
        for f in state.scanned_files:
            by_tier.setdefault(_metadata_read_priority(f.filename), []).append(f)

        budget = _MAX_CONTEXT_CHARS  # total emitted content across all files
        carry = 0  # headroom flowing down from absent / under-spending tiers
        file_lines: list[str] = []

        for tier in sorted(_TIER_SHARES):
            files = by_tier.get(tier)
            if not files:
                # Nobody claimed this tier's share; hand it to the tiers below.
                carry += _TIER_SHARES[tier]
                continue
            # Reserve the shares of the POPULATED tiers below this one before
            # sizing each file (#419). A tier's share is granted per FILE, so
            # without this a deposit holding two `*metadata*` workbooks let them
            # claim the whole ceiling between them and the BioStudies descriptor,
            # SOP and README all emitted nothing — the same silent starvation
            # this issue exists to remove, only pointed at a different document.
            # Counted PER FILE, because the share is granted per file: a tier
            # holding two documents needs two shares, and reserving only one
            # still left the second (the README, behind the SOP) at zero.
            reserved = sum(
                share * len(by_tier.get(lower) or ())
                for lower, share in _TIER_SHARES.items()
                if lower > tier
            )
            for f in files:
                allowance = min(_TIER_SHARES[tier] + carry, max(0, budget - reserved))
                slice_text = _file_slice(f, approved_roots, allowance, tier)
                # Unused headroom flows down rather than being forfeited.
                carry = max(0, allowance - len(slice_text))
                budget -= len(slice_text)

                line = f"- {f.filename}"
                if slice_text:
                    sep = "\n" if "\n" in slice_text else " "
                    line += f":{sep}{slice_text}"
                file_lines.append(line)

        if file_lines:
            parts.append("Scanned files:\n" + "\n".join(file_lines))

    # Document discovery context (#179): ranked, role-labelled documentation
    # discovered by the engine after scanning. This is stored as a list of
    # compact dicts on state and rendered as a concise summary line per doc.
    documents = getattr(state, "documents", [])
    if documents:
        doc_lines: list[str] = []
        for doc in documents[:20]:
            role = doc.get("role", "document")
            name = doc.get("filename", doc.get("relative_path", "?"))
            score = doc.get("score", 0.0)
            reasons = doc.get("reasons", [])
            reason_str = "; ".join(reasons[:2]) if reasons else ""
            line = f"[{role}] {name} (score: {score:.2f})"
            if reason_str:
                line += f" — {reason_str}"
            preview = str(doc.get("preview") or "").strip()
            if preview:
                line += f"\n{preview[:2000]}"
            doc_lines.append(line)
        if doc_lines:
            parts.append("Discovered documentation:\n" + "\n".join(doc_lines))

    return "\n\n".join(parts).strip()


# Per-field cap on the entity digest folded into the drafter prompt. Enough for a
# cell line's genetic-modification note, short enough that a long scanned blob on
# one entity cannot crowd out the shared crate context.
_ENTITY_CONTEXT_FIELD_CHARS = 300

# Cap on the whole per-entity block. Per-field truncation alone does not bound it
# — a field-heavy entity would spend back what the skip guard (§14.5 step 2)
# saves. Identity comes first, so what a cap drops is the field tail, never which
# entity the model is being asked about.
_ENTITY_CONTEXT_MAX_CHARS = 1500


def _entity_draft_context(entity: Entity, shared: str) -> str:
    """The drafter prompt's context for *entity*: the crate digest + its identity.

    The leaf's signature is ``(entity_type, context)`` — the entity itself never
    reaches it. Passing only the crate-wide *shared* digest means every entity of
    one type sends a byte-identical prompt, so the model cannot tell which one it
    is describing and returns the same text for all of them. Observed on a real
    build as the parental cell line ``cho_k1`` being described as "stably
    transfected with ... OATP1C1" — its transfected sibling's description (#423).

    So fold the entity's own id, type and already-known field values in. Only
    *this* entity's fields are included: naming a sibling here would reintroduce
    the same confusion from the other direction. Mirrors
    :func:`builder.agents.pipeline.guidance._draft_context`, which already varies
    the context per gap.
    """
    parts = [shared] if shared else []
    identity = [f"Entity id: {entity.entity_id}", f"Entity type: {entity.type}"]
    budget = _ENTITY_CONTEXT_MAX_CHARS - sum(len(line) for line in identity)
    for field, value in sorted(entity.fields.items()):
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > _ENTITY_CONTEXT_FIELD_CHARS:
            text = text[:_ENTITY_CONTEXT_FIELD_CHARS].rstrip() + "…"
        line = f"{field}: {text}"
        # Checked BEFORE appending: a cap that can be overshot by a whole field
        # is not a cap. Fields are sorted, so what survives is stable per entity.
        if len(line) > budget:
            break
        identity.append(line)
        budget -= len(line)
    parts.append(
        "The entity you are describing, and what is already known about it "
        "(describe THIS entity only, not any related one):\n" + "\n".join(identity)
    )
    return "\n\n".join(parts)


def _draft_entities(
    engine: AgentEngine,
    usage_sink: UsageSink | None = None,
    overrides: ModelOverrides | None = None,
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

        # Gate 3 — the model must actually be OFFERED a field we could apply.
        # The leaf binds the model to a D5-pruned schema, and we apply only what
        # is both descriptive and missing; when those sets are disjoint the call
        # cannot change state no matter what comes back, so don't pay for it.
        # On the S-VHPS26 fixture this is 21 of 26 calls (#423) — every named
        # compound, plus Person and Organization, whose schemas expose no
        # `description`.
        if not set(missing) & drafter_visible_fields(entity.type):
            logger.debug(
                "drafter-leaf skipped for %s (%s): missing %s, none offered by its schema",
                entity.entity_id,
                entity.type,
                missing,
            )
            continue

        try:
            leaf_fields = draft_entity_fields(
                entity.type,
                _entity_draft_context(entity, context),
                usage_sink=usage_sink,
                **({"overrides": overrides} if overrides is not None else {}),
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


def _find_or_draft_organization(engine: AgentEngine, name: str) -> str | None:
    """Return the entity_id of an Organization named *name*, minting one if absent.

    De-dup by name: an existing in-state Organization with the same (stripped)
    ``name`` is reused so two affiliations sharing a name yield ONE Organization
    rather than duplicates. Otherwise a new one is minted via the ``draft_organization``
    tool (never hand-rolled JSON-LD). D5-safe: only the plan's affiliation *name* is
    used — no ROR / IRI is fabricated here. Returns ``None`` on an empty name or if
    drafting fails (logged), so a failure never breaks the spine.
    """
    name = name.strip()
    if not name:
        return None
    existing = next(
        (
            e.entity_id
            for e in engine.state.list_entities()
            if e.type == "Organization" and str(e.fields.get("name") or "").strip() == name
        ),
        None,
    )
    if existing is not None:
        return existing
    try:
        org = engine.run_tool("draft_organization", name=name, hints={})
    except Exception as exc:  # noqa: BLE001 - a drafting failure must not break the spine
        logger.warning("draft_organization failed for %r: %s", name, exc)
        return None
    return getattr(org, "entity_id", None)


def _set_ref_field(
    engine: AgentEngine,
    entity_id: str,
    field: str,
    value: str | list[str],
) -> None:
    """Set a single reference *field* on *entity_id* via the ``set_fields`` tool.

    Thin wrapper used by the #273 entity→process/Study wiring: *field* is one of
    the crate mapping's reference fields (``chemicals`` / ``cell_line`` /
    ``cell_lines``) so the build resolves it to ``{"@id"}`` reference(s) rather than
    a literal. Goes through the consolidated mutation tool (never hand-rolled
    JSON-LD); a wiring failure is logged and never breaks the spine.
    """
    try:
        engine.run_tool("set_fields", entity_id=entity_id, fields={field: value})
    except Exception as exc:  # noqa: BLE001 - a wiring failure must not break the spine
        logger.warning("linking %s=%r onto %r failed: %s", field, value, entity_id, exc)


# The generic placeholder names the scaffold leaves on a backbone layer when no
# title was available (kept in sync with the `_DEFAULT_*` constants above). A
# field still carrying its layer's placeholder is treated as "empty" by the
# fill-don't-clobber merge below, so a real plan name overwrites it.
_GENERIC_BACKBONE_NAMES: frozenset[str] = frozenset(
    {_DEFAULT_INVESTIGATION_NAME, _DEFAULT_STUDY_NAME, _DEFAULT_ASSAY_NAME}
)


def _merge_backbone_layer(engine: AgentEngine, entity_type: str, hints: dict[str, str]) -> bool:
    """Fill the plan's name/description onto an existing backbone layer.

    The backbone is scaffolded BEFORE the plan is materialized, and
    ``scaffold_isa_backbone`` *reuses* an existing entity (its hints reach the
    drafter only on creation). So the plan's Study name would otherwise be
    dropped — the scaffolded Study keeps its generic placeholder. This applies the
    plan's descriptive fields directly onto the already-scaffolded entity, via the
    entity model (``set_fields_from_dict``, source ``"llm"``), **fill-don't-clobber**:
    a field is overwritten only when it is empty or still carries the layer's
    generic placeholder name, so a real, specific scaffolded name is never lost.

    D5-safe: only descriptive ``name`` / ``description`` are merged — never an
    identifier. Returns ``True`` iff at least one field was applied.
    """
    entity = next((e for e in engine.state.list_entities() if e.type == entity_type), None)
    if entity is None:
        return False

    to_apply: dict[str, str] = {}
    for field, value in hints.items():
        new_value = str(value or "").strip()
        if not new_value:
            continue
        current = str(entity.fields.get(field) or "").strip()
        # Fill when empty or still the generic placeholder; otherwise keep what's
        # there (the scaffold derived a real name from the crate title — it wins).
        if not current or current in _GENERIC_BACKBONE_NAMES:
            to_apply[field] = new_value

    if to_apply:
        entity.set_fields_from_dict(to_apply, source="llm")
    return bool(to_apply)


# The conservative default process a protocol governs when the plan gives no (or
# an unmatched) hint: the central exposure/assay step, then a measurement readout.
_PROTOCOL_DEFAULT_PROCESS_TYPES: tuple[str, ...] = (
    "Exposure",
    "EndpointReadout",
    "DataAnalysis",
    "CellCulture",
)


def _select_process_for_protocol(steps: list[dict[str, Any]], process_hint: str) -> str | None:
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


# --- Deterministic standard process chain + file attachment (#262) ----------
#
# Before #262 the pipeline only drafted a process chain (and only attached files)
# when an LLM provider returned a candidate plan with a `process_chain` section,
# so the no-provider crate was structurally hollow: `lab_processes: []`,
# `files: []`. Both shapes are deterministic given the Assay + the scanned-file
# inventory the engine already carries, so the spine now drafts them in *code*,
# regardless of whether a provider is configured — the legacy ReAct path did
# both, and the deterministic spine must too.

# The standard in-vitro derivation chain (AGENTS.md §14.3 / the gold S-VHPS21
# shape): CellCulture → Exposure → EndpointReadout → DataAnalysis. `process_type`
# is the load-bearing field (it drives `draft_process_chain`'s wiring + the §14.3
# output synthesis); the `name` is a stable default so the process `@id`s are
# deterministic with NO provider, and is overlaid with a plan step name only when
# the plan actually supplies one (idempotent: the id stays keyed to the name).
_STANDARD_CHAIN: tuple[dict[str, str], ...] = (
    {"process_type": "CellCulture", "name": "Cell culture"},
    {"process_type": "Exposure", "name": "Exposure"},
    {"process_type": "EndpointReadout", "name": "Endpoint readout"},
    {"process_type": "DataAnalysis", "name": "Data analysis"},
)


def _merge_plan_chain_names(
    plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build the chain spec: the standard 4 steps, overlaid with plan step names
    and the experimental parameters the plan states (#379).

    Always returns the full canonical chain (so the no-provider crate is never
    hollow), but when *plan* carries a ``process_chain`` each plan step's ``name``
    is overlaid onto the matching ``process_type`` so a provider's richer step
    names drive the process ``@id``s. Plan process_types outside the standard four
    are ignored here — ``draft_process_chain`` only wires the valid subtypes and a
    bogus type would raise; the standard chain is the deterministic backbone.

    Each step's ``parameters`` are overlaid alongside ``name`` and flow on through
    ``draft_process_chain`` -> ``draft_process`` -> ``_build_process``, replacing
    the ``"unknown"`` / ``"NA"`` / ``"Standard medium"`` placeholders that the
    crate otherwise publishes as ontology-typed ParameterValues nobody asserted.

    Three guards, each load-bearing:

    * **Whitelisted** to ``LABPROCESS_PARAMETER_FIELDS``, never splatted. Plan
      items are ``additionalProperties: True``, so a splat would turn
      ``object_hint`` into a LabProcess state field and an ``entity_id`` key would
      hijack the process ``@id`` through ``drafters._make_entity_id``.
    * **Non-empty stripped strings only.** ``_build_process`` does
      ``f.get("duration", "unknown")`` — a default that applies only when the key
      is ABSENT, so an empty overlaid value would ship an *empty* ParameterValue.
    * A plan with no parameters yields exactly the previous name-only hints, so
      the #262 "no-provider crate is byte-identical" guarantee is untouched.
    """
    from builder.tools._crate_mapping import LABPROCESS_PARAMETER_FIELDS

    plan_names: dict[str, str] = {}
    plan_parameters: dict[str, dict[str, str]] = {}
    for step in (plan or {}).get("process_chain") or []:
        if not isinstance(step, dict):
            continue
        ptype = str(step.get("process_type") or "").strip()
        if not ptype:
            continue
        name = str(step.get("name") or "").strip()
        if name:
            plan_names[ptype] = name

        raw = step.get("parameters")
        if not isinstance(raw, dict):
            continue
        kept = {
            key: value.strip()
            for key, value in raw.items()
            if key in LABPROCESS_PARAMETER_FIELDS
            and isinstance(value, str)
            and value.strip()
        }
        if kept:
            plan_parameters[ptype] = kept

    chain: list[dict[str, Any]] = []
    for std in _STANDARD_CHAIN:
        ptype = std["process_type"]
        hints: dict[str, Any] = {"name": plan_names.get(ptype, std["name"])}
        hints.update(plan_parameters.get(ptype, {}))
        chain.append({"process_type": ptype, "hints": hints})
    return chain


def _draft_standard_chain(
    engine: AgentEngine, plan: dict[str, Any] | None
) -> tuple[int, list[dict[str, Any]]]:
    """Deterministically draft the standard process chain onto the scaffolded Assay.

    Calls the existing idempotent ``draft_process_chain`` composite (NO hand-rolled
    JSON-LD) with the full CellCulture → Exposure → EndpointReadout → DataAnalysis
    chain, so EndpointReadout/DataAnalysis get the §14.3 result/object output
    synthesis (the "no output" Violation trap) and the whole chain is wired under
    the Assay. Plan step names (when a provider supplied them) are overlaid onto
    the standard chain so the two paths share ONE chain rather than minting
    duplicates. Runs regardless of provider. Returns
    ``(process_count, step_summary)`` — the summary feeds protocol linking.
    """
    assay_id = _first_entity_id(engine, "Assay")
    if not assay_id:
        logger.warning("No Assay scaffolded; skipping process-chain materialization (#262).")
        return 0, []

    chain = _merge_plan_chain_names(plan)
    try:
        chain_result = engine.run_tool("draft_process_chain", assay_id=assay_id, chain=chain)
    except Exception as exc:  # noqa: BLE001 - a chain failure must not break the spine
        logger.warning("draft_process_chain failed: %s", exc)
        return 0, []

    process_ids = list(chain_result.get("process_ids") or [])
    logger.info(
        "Materialized %d-step process chain under %s: %s (#262).",
        len(process_ids),
        assay_id,
        ", ".join(s["process_type"] for s in _STANDARD_CHAIN),
    )
    return len(process_ids), list(chain_result.get("steps") or [])


# Filename / MIME signals that a scanned file is *processed/analysed* output
# (figures, prism/pzfx analysis files, results) rather than *raw* measurements.
# Used only to stamp a descriptive `role` on the attached File — the structural
# link is the same for both (under the Assay's hasPart), so a misclassification
# never drops a file; it only mislabels its role.
_PROCESSED_NAME_HINTS: tuple[str, ...] = (
    "result",
    "analysis",
    "analy",
    "processed",
    "figure",
    "plot",
    "ic50",
    "summary",
)
# GraphPad Prism writes the same project — fitted curves and analyses — under
# three interchangeable extensions. `.pzf` is the legacy binary spelling of
# `.pzfx`; deposits built with older tooling (S-VHPS21) use it throughout their
# study-wide analysis folder, and omitting it exported that analysis into the
# crate's raw_data tree.
_PROCESSED_EXT_HINTS: tuple[str, ...] = (".prism", ".pzfx", ".pzf")


def _file_role(filename: str, mime: str) -> str:
    """Deterministic raw-vs-processed role for a scanned file (descriptive only)."""
    lowered = (filename or "").lower()
    if lowered.endswith(_PROCESSED_EXT_HINTS):
        return "processed_data"
    if any(hint in lowered for hint in _PROCESSED_NAME_HINTS):
        return "processed_data"
    return "raw_data"


def _attach_scanned_files(engine: AgentEngine) -> int:
    """Add every scanned file to the crate as a File and link it under the Assay.

    Deterministic and provider-independent (#262): the engine already carries the
    scanned-file inventory (``state.scanned_files``), so the spine adds each one as
    a ``File`` entity and places it under the scaffolded Assay's ``hasPart`` via the
    existing idempotent ``attach_files`` composite (NO hand-rolled JSON-LD). Files
    are grouped by a deterministic raw/processed ``role`` so each batch is stamped
    appropriately; nothing is skipped silently — what is attached is logged. Falls
    back to the Study when no Assay exists. ``attach_files`` dedups by on-disk
    source, so re-running mints no duplicates. Returns the number of File entities
    attached.
    """
    if not engine.state.scanned_files:
        return 0

    target_id = _first_entity_id(engine, "Assay") or _first_entity_id(engine, "Study")
    if not target_id:
        logger.warning("No Assay/Study scaffolded; cannot attach scanned files (#262).")
        return 0

    # Group the scanned paths by deterministic role so each batch is stamped with
    # the right descriptive role. The structural placement (under the Assay) is the
    # same for every role, so a role misclassification never drops a file.
    by_role: dict[str, list[str]] = {}
    for fc in engine.state.scanned_files:
        role = _file_role(fc.filename or "", fc.mime_type or "")
        by_role.setdefault(role, []).append(fc.path)

    attached_ids: set[str] = set()
    for role in sorted(by_role):  # sorted ⇒ deterministic call order
        paths = by_role[role]
        try:
            result = engine.run_tool("attach_files", to=target_id, paths=paths, role=role)
        except Exception as exc:  # noqa: BLE001 - one bad batch must not break the spine
            logger.warning("attach_files (%s) failed for %d file(s): %s", role, len(paths), exc)
            continue
        ids = list(result.get("file_ids") or [])
        attached_ids.update(ids)
        logger.info(
            "Attached %d %s file(s) under %s (#262): %s",
            len(ids),
            role,
            target_id,
            ", ".join(ids) or "(none)",
        )
    return len(attached_ids)


# --- Publication recovery from PDF text (#245) ------------------------------
#
# A plan publication carries a TITLE only (D5 — no DOI), but when the source
# document IS a PDF the bounded extractor (`extract_plan`) frequently hands back
# the PDF *filename* (e.g. `Wagenaars_etal_2025_OATP1C1.pdf`) as that title.
# Looking a DOI up by filename essentially always fails the Crossref confidence
# gate. So when a candidate publication maps to a PDF under an approved scan root
# we read the PDF *text* and recover a real query — a DOI (regex first; most
# reliable) or the article title (first non-trivial heading) — and resolve with
# THAT, never the bare filename. When neither is recoverable we skip rather than
# query Crossref with a filename.

# DOI regex (Crossref's recommended pattern): `10.<registrant>/<suffix>`. The
# suffix runs to the first whitespace/quote/closing bracket; trailing sentence
# punctuation is trimmed by the caller. Case-insensitive — DOIs are.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)

# Strip the structured section markers `extract_pdf_text` emits (`[Page N]`,
# `[Text] `, `[Table …]`, `[Image]`) so the title heuristic sees plain lines.
_PDF_MARKER_RE = re.compile(r"^\[(?:Page \d+|Table[^\]]*|Image[^\]]*)\]\s*$")
_PDF_TEXT_PREFIX = "[Text] "

# A recovered title must look like a real heading, not a fragment or boilerplate.
_MIN_TITLE_WORDS = 3
_MIN_TITLE_CHARS = 12


def _extract_doi_from_text(text: str) -> str | None:
    """Recover the first DOI from PDF *text* via regex (most reliable — #245).

    Returns the bare DOI (``10.…/…``, any ``doi:``/URL prefix dropped, trailing
    sentence punctuation trimmed) or ``None`` when the text carries no DOI.
    """
    match = _DOI_RE.search(text or "")
    if match is None:
        return None
    doi = match.group(0).rstrip(".,;)]}>\"'")
    return doi or None


def _extract_title_from_pdf_text(text: str) -> str | None:
    """Recover an article title from PDF *text* — its first non-trivial line (#245).

    Walks the lines of :func:`~builder.tools.scanner.extract_pdf_text` output,
    stripping its ``[Page N]`` / ``[Text] `` / ``[Table …]`` markers, and returns
    the first line that reads like a heading (at least :data:`_MIN_TITLE_WORDS`
    words and :data:`_MIN_TITLE_CHARS` characters, and not itself a DOI/URL). This
    is descriptive parsing of the document body, not identifier fabrication
    (D5-safe). Returns ``None`` when no plausible title line is found.
    """
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or _PDF_MARKER_RE.match(line):
            continue
        if line.startswith(_PDF_TEXT_PREFIX):
            line = line[len(_PDF_TEXT_PREFIX) :].strip()
        if not line:
            continue
        # A DOI/URL line is not a title; keep scanning.
        if _DOI_RE.search(line) or line.lower().startswith(("http://", "https://", "doi:")):
            continue
        if len(line) >= _MIN_TITLE_CHARS and len(line.split()) >= _MIN_TITLE_WORDS:
            return line
    return None


def _scanned_path_for_name(
    engine: AgentEngine, name: str, *, suffix: str | None = None
) -> str | None:
    """Path of the scanned file *name* refers to, matched by BASENAME, or ``None``.

    The single resolver for "the plan named a file; which scanned file is that?"
    — shared by the #245 publication-PDF path and the #408 condition table.

    **Basename, deliberately.** :func:`_gather_context` shows the extraction leaf
    only ``f.filename``, never ``f.path``, so a plan can only ever name a bare
    basename. Comparing full paths would make any caller look wired and silently
    never fire.

    The result is **fail-closed to ``approved_scan_roots``** via the same
    :func:`builder.tools.scanner._contain` guard the rest of the spine uses: plan
    paths are LLM free text, so resolving one must never widen filesystem access.

    Args:
        engine: The engine whose ``state.scanned_files`` is the inventory.
        name: The plan's file reference (a basename, possibly with directories).
        suffix: When given, require *name* to end with it (case-insensitive) —
            e.g. ``".pdf"`` for the publication case. ``None`` accepts any name.

    Returns:
        The scanned file's path, or ``None`` when *name* is empty, fails the
        *suffix* test, matches no scanned file, or resolves outside the roots.
    """
    candidate = (name or "").strip()
    if not candidate:
        return None
    if suffix is not None and not candidate.lower().endswith(suffix.lower()):
        return None

    from pathlib import PurePath

    from builder.tools.scanner import _contain

    wanted = PurePath(candidate).name.lower()
    roots = engine.state.approved_scan_roots
    for f in engine.state.scanned_files:
        fname = (f.filename or PurePath(f.path).name or "").lower()
        if fname != wanted:
            continue
        # Fail-closed: only read a file that resolves inside an approved root.
        if _contain(f.path, roots) is None:
            logger.debug("Scanned file %s is outside approved scan roots — refusing.", f.path)
            return None
        return f.path
    return None


def _pdf_path_for_publication(engine: AgentEngine, title: str) -> str | None:
    """Path of the scanned PDF a plan publication *title* refers to, or ``None``.

    A plan publication is treated as PDF-backed when its ``title`` itself names a
    PDF (ends in ``.pdf``) and a scanned file matches it by basename — the common
    #245 case where the extractor returned the filename as the title. Returns
    ``None`` when the title is an ordinary article title (resolve it directly) or
    names no scanned PDF inside the approved roots.
    """
    return _scanned_path_for_name(engine, title, suffix=".pdf")


def _propose_condition_table(engine: AgentEngine, exposure_id: str) -> dict[str, Any]:
    """Write a best-effort design table for an Exposure with no supplied plate map.

    Writes only the rows the crate can justify and returns the questions a human
    must settle, so the caller can surface them. Never raises: a proposal failure
    leaves the header-only table exactly as before.
    """
    from builder.tools.data_content import propose_condition_rows

    try:
        proposal = propose_condition_rows(engine.state, exposure_id)
    except Exception as exc:  # noqa: BLE001 — a proposal must not break the spine
        logger.warning("condition-table proposal failed for %s: %s", exposure_id, exc)
        return {"populated": False, "reason": f"proposal raised: {exc}"}
    if not proposal.get("ok"):
        return {"populated": False, "reason": str(proposal.get("error") or "declined")}

    rows = proposal.get("rows") or []
    if not rows:
        return {"populated": False, "reason": "nothing known to propose"}
    try:
        outcome = engine.run_tool(
            "populate_condition_table", exposure_id=exposure_id, rows_or_csv_path=rows
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("populate_condition_table failed for a proposal: %s", exc)
        return {"populated": False, "reason": f"populate_condition_table raised: {exc}"}

    outcome = outcome if isinstance(outcome, dict) else {}
    if not outcome.get("ok"):
        return {"populated": False, "reason": str(outcome.get("error") or "declined")}
    return {
        "populated": True,
        "proposed": True,
        "reason": "proposed from crate entities — awaiting confirmation",
        "rows": outcome.get("rows"),
        "path": outcome.get("path"),
        "known_columns": proposal.get("known"),
        "blank_columns": proposal.get("blank"),
        "questions": proposal.get("questions"),
    }


def _populate_condition_table_from_plan(
    engine: AgentEngine, plan: dict[str, Any], exposure_id: str
) -> dict[str, Any]:
    """Write the plan's ``condition_table`` file into the Exposure's CSV (#408).

    The extraction leaf classifies every plan file into
    ``raw``/``processed``/``condition_table``/``other``, but the spine re-derived a
    role from the filename (:func:`_file_role`, which only returns
    ``raw_data``/``processed_data``) and dropped the answer — so ``condition_table``
    was unreachable and every exported table shipped header-only while the per-well
    payload sat beside it as an untyped ``File``.

    Deterministic and conservative:

    * exactly ONE ``condition_table`` entry is actionable — zero or several and the
      spine records why and does nothing rather than guess which plate map is the
      design table;
    * the path is resolved through :func:`_scanned_path_for_name` (basename match,
      fail-closed to ``approved_scan_roots``);
    * the write goes through ``engine.run_tool("populate_condition_table", ...)``,
      never a hand-rolled CSV, so it lands on the exact path
      :func:`~builder.tools._crate_mapping._synth_condition_table` types as a
      ``csvw:Table`` and the #94/#180 schema stays attached. ``output_dir`` is left
      to the tool, which resolves it the same way ``export_crate`` does (#381).

    Never raises: a tool failure is logged and reported as a reason, so one bad
    plate map cannot break the spine.

    Returns:
        ``{"populated": bool, "reason": str}`` plus, on success, the tool's
        ``rows`` / ``path`` / ``unmapped_source_columns``. Surfaced in
        :func:`_materialize_plan`'s result so it reaches ``run_pipeline``.
    """
    entries = [f for f in (plan.get("files") or []) if isinstance(f, dict)]
    candidates = [e for e in entries if str(e.get("role") or "").strip() == "condition_table"]
    if not candidates:
        # The user supplied no plate map. Rather than shipping a header-only
        # table beside compounds it should have named, PROPOSE the design from
        # what the crate already knows (#438) — compound identity, cell line and
        # assay are entities, so restating them asserts nothing new. Anything the
        # crate never states stays blank and comes back as a question for the
        # human; no concentration is ever invented.
        return _propose_condition_table(engine, exposure_id)
    if len(candidates) > 1:
        names = ", ".join(sorted(str(c.get("path") or "?") for c in candidates))
        return {
            "populated": False,
            "reason": f"{len(candidates)} condition_table candidates ({names}) — refusing to guess",
        }

    named = str(candidates[0].get("path") or "").strip()
    if not named:
        return {"populated": False, "reason": "the condition_table entry carries no path"}

    path = _scanned_path_for_name(engine, named)
    if path is None:
        return {
            "populated": False,
            "reason": f"{named!r} matches no scanned file inside the approved scan roots",
        }

    try:
        outcome = engine.run_tool(
            "populate_condition_table",
            exposure_id=exposure_id,
            rows_or_csv_path=path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("populate_condition_table failed for %s: %s", path, exc)
        return {"populated": False, "reason": f"populate_condition_table raised: {exc}"}

    outcome = outcome if isinstance(outcome, dict) else {}
    if not outcome.get("ok"):
        reason = str(outcome.get("error") or "populate_condition_table declined")
        logger.info("Condition table not populated from %s: %s", named, reason)
        return {"populated": False, "reason": reason}

    logger.info(
        "Populated condition table from %s: %s row(s) (#408).", named, outcome.get("rows")
    )
    return {
        "populated": True,
        "reason": "",
        "source": named,
        "rows": outcome.get("rows"),
        "path": outcome.get("path"),
        "unmapped_source_columns": outcome.get("unmapped_source_columns") or [],
    }


def _recover_publication_query(engine: AgentEngine, title: str) -> tuple[str, str] | None:
    """Resolve a plan publication *title* to a real Crossref query (#245).

    Returns one of:

    * ``("doi", <doi>)`` — a DOI recovered from the PDF text (most reliable);
    * ``("title", <title>)`` — a real title (the ordinary case: the plan title is
      already an article title; or one extracted from the PDF text when no DOI was
      found);
    * ``None`` — the title is a PDF filename and neither a DOI nor a plausible
      title could be recovered from the PDF text, so the caller must SKIP rather
      than query Crossref with the bare filename.

    The PDF read is fail-closed to ``approved_scan_roots`` and never raises out of
    the spine: any reader error/missing dependency is logged and yields ``None``.
    """
    pdf_path = _pdf_path_for_publication(engine, title)
    if pdf_path is None:
        # An ordinary article title — resolve it directly (existing behaviour).
        return ("title", title)

    # Lazy import: keep the spine importable in the default env; only touch the
    # PDF reader when there is actually a PDF-backed publication.
    from builder.tools.scanner import extract_pdf_text

    try:
        text = extract_pdf_text(pdf_path)
    except Exception as exc:  # noqa: BLE001 - a malformed PDF must not break the spine
        logger.warning("Reading publication PDF %s failed; skipping: %s", pdf_path, exc)
        return None

    if not text or not text.strip():
        return None

    doi = _extract_doi_from_text(text)
    if doi:
        return ("doi", doi)

    real_title = _extract_title_from_pdf_text(text)
    if real_title:
        return ("title", real_title)

    # Neither recoverable — never query Crossref with the filename.
    return None


def _materialize_plan(
    engine: AgentEngine,
    usage_sink: UsageSink | None = None,
    overrides: ModelOverrides | None = None,
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
    * **entity→provenance wiring (#273).** Resolving a compound / cell line MINTS
      the entity but leaves it a graph ORPHAN unless something references it, so the
      collected ids are wired deterministically via ``set_fields`` (never
      hand-rolled JSON-LD) with the canonical ISA-Tox reference fields: every
      resolved ``MolecularEntity`` → the **Exposure** LabProcess via ``chemicals``
      (ISA forbids a MolecularEntity as a process object, so the build connects the
      compound THROUGH the Exposure's CSVW condition table — ``schema:about`` →
      MolecularEntity + the compound column ``valueUrl``), the resolved
      ``CellLineSample`` → the **CellCulture** LabProcess via ``cell_line`` (its
      consumed input, replacing the synthesized generic ``..._input``), and BOTH
      onto the scaffolded Study via ``schema:mentions`` (``chemicals`` /
      ``cell_lines``→``biologicalModels``) so every resolved entity — PubChem- AND
      ChEBI-backed — is reachable from the backbone (orphan count → 0). Idempotent:
      ``set_fields`` writes the same deterministic ids, so re-running mints no dups.
    * each ``protocols[]`` → ``draft_protocol`` (a ``LabProtocol`` from the
      name/description only — D5: no identifier) which is then linked to the
      ``LabProcess`` it governs via the ``labprotocol`` reference field (resolved
      to ``executesLabProtocol`` at build time, isa_tox.md). The plan's optional
      free-text ``process_hint`` is matched conservatively (by ``process_type``,
      then by step name) to choose the process; with no match it attaches to the
      central exposure/assay step, and an unresolvable link is left for the
      guidance loop rather than guessed (:func:`_select_process_for_protocol`).
    * **process chain — ALWAYS, regardless of provider (#262).** ONE
      :func:`draft_process_chain` lays the standard in-vitro chain (CellCulture →
      Exposure → EndpointReadout → DataAnalysis) onto the scaffolded Assay
      (:func:`_draft_standard_chain`). The composite synthesizes the
      EndpointReadout / DataAnalysis outputs the build has no fallback for (the
      §14.3 "no output" Violation trap). When a provider supplied a
      ``process_chain``, each plan step's ``name`` is overlaid onto the matching
      ``process_type`` so the two paths share ONE chain (no duplicate processes).
      This runs even with NO provider, so the crate is never structurally hollow.
    * **condition table (#408).** The ONE plan file classified
      ``role == "condition_table"`` is written into the Exposure's typed CSVW table
      via ``populate_condition_table``
      (:func:`_populate_condition_table_from_plan`) — the leaf already emits that
      role and the spine used to discard it, so every crate declared a ten-column
      ``csvw:Table`` and shipped it header-only. Zero or several candidates → the
      spine records why and writes nothing rather than guess. The outcome is
      surfaced on this function's result.
    * **scanned files — ALWAYS, regardless of provider (#262).** Every
      ``engine.state.scanned_files`` entry is added as a ``File`` entity and placed
      under the scaffolded Assay (else Study) ``hasPart`` via the idempotent
      :func:`attach_files` composite (:func:`_attach_scanned_files`), grouped by a
      deterministic raw/processed ``role``. Nothing is skipped silently — what is
      attached is logged. This runs even with NO provider.
    * each ``aops[]`` → :func:`materialize_aop_subgraph` onto the scaffolded
      Study (the only model input is the numeric ``aop_id``; every node id comes
      from AOP-Wiki — D5).
    * each ``people[]`` → ``draft_person`` with the name plus a deterministic
      ``givenName`` / ``familyName`` split of that name (ISA REQUIRES a non-empty
      given name; splitting a name is descriptive parsing, not identifier
      fabrication, so it is D5-safe). ORCID stays empty for a later lookup.
    * each ``publications[]`` → :func:`resolve_publication` /
      :func:`draft_publication_with_authors` (#219/#224/#245). A plan carries a
      title ONLY (D5 — no DOI), but when the source was a PDF that "title" is
      often the PDF *filename* (which Crossref can never match). So each candidate
      is first mapped (:func:`_recover_publication_query`) to a real Crossref
      query: a **DOI** extracted from the PDF text (regex; most reliable) is
      resolved via :func:`draft_publication_with_authors`; otherwise a **real
      title** (an ordinary plan title, or one extracted from the PDF text) is
      resolved via :func:`resolve_publication`, which commits a DOI-backed
      ``ScholarlyArticle`` (+ authors) ONLY on a confident match (counted under
      ``publications``) and otherwise leaves the title under
      ``publications_deferred``. A PDF filename with no recoverable DOI/title is
      **skipped** — never queried by filename. A DOI is never fabricated from a
      title; the identifier always comes from the Crossref lookup, never the plan
      (D5).

    Guarantees:

    * **The process chain + file attachment ALWAYS run (#262)** — they are pure,
      deterministic, code-driven steps over the scaffolded Assay + the engine's
      scanned-file inventory, so the crate is never structurally hollow even with
      no provider. Stage A (``extract_plan``) and every *plan-driven* section below
      are still **no-ops when no LLM provider is configured** (the same
      :func:`builder.config.get_provider` gate :func:`_draft_entities` uses) and
      when there is no usable context — Stage A is never called, so the
      deterministic spine and its tests are unchanged (the chain/file steps stay
      deterministic, so the no-provider graph-hash is identical across repeats).
    * **D5 (Verify, Don't Trust).** Only plan *names/titles* reach the composites;
      identifiers are produced by the composites' own lookups/verification and
      are never set or overwritten from the plan. ``extract_plan`` already strips
      identifiers from the plan; this step never re-introduces them.
    * **Idempotent.** Every composite reuses an existing entity (deterministic
      ids), so re-running the spine mints no duplicates.

    Returns ``{"study", "compounds", "cell_lines", "protocols", "processes",
    "files", "aops", "people", "publications", "publications_deferred"}`` —
    per-section counts of what was materialized (``processes`` is the standard
    chain's length, ``files`` the number of scanned files attached), plus the
    titles of publications that found no confident DOI match and were deferred for
    a later resolution.
    """
    result: dict[str, Any] = {
        "study": 0,
        "compounds": 0,
        "cell_lines": 0,
        "protocols": 0,
        "processes": 0,
        "files": 0,
        "aops": 0,
        "people": 0,
        "publications": 0,
        "publications_deferred": [],
        # #408: whether the plan's condition_table file reached the Exposure's CSV,
        # and when it did not, why — never a silent skip.
        "condition_table": {"populated": False, "reason": "not attempted"},
    }

    # Extract the candidate plan FIRST (when there is a provider + usable context),
    # so the deterministic steps below can overlay the plan's richer step names onto
    # the standard chain. With no provider / no context this is a strict no-op and
    # `plan` stays None — the deterministic process-chain + file-attachment steps
    # still run, so the crate is never structurally hollow (#262).
    plan: dict[str, Any] | None = None
    if get_provider() is not None:
        context = _gather_context(engine)
        if context:
            try:
                extract_kwargs: dict[str, Any] = {"usage_sink": usage_sink}
                if overrides is not None:
                    extract_kwargs["overrides"] = overrides
                extracted = extract_plan(context, **extract_kwargs)
            except Exception as exc:  # noqa: BLE001 - a flaky extractor must not break the spine
                logger.warning("extract_plan failed; skipping plan materialization: %s", exc)
                extracted = None
            if isinstance(extracted, dict):
                plan = extracted

    # --- process chain (#262): ALWAYS draft the standard 4-step in-vitro chain
    # (CellCulture → Exposure → EndpointReadout → DataAnalysis) under the scaffolded
    # Assay, deterministically and regardless of provider, so `lab_processes` is
    # never empty. Plan step names (when a provider supplied them) are overlaid onto
    # the standard chain so the two paths share ONE chain (no duplicates). The
    # composite synthesizes EndpointReadout/DataAnalysis outputs (§14.3 trap). ---
    n_processes, chain_steps_summary = _draft_standard_chain(engine, plan)
    result["processes"] = n_processes

    # --- file attachment (#262): ALWAYS add the scanned data files as File entities
    # and link them under the Assay/Study, deterministically and regardless of
    # provider, so `files` is never empty. ---
    result["files"] = _attach_scanned_files(engine)

    # Everything below is plan-driven and therefore provider-gated: with no plan
    # (no provider / no context / extractor failure) the deterministic chain + file
    # steps above are the whole of materialization and we return here.
    if plan is None:
        return result

    # --- study: merge the plan's name/description onto the scaffolded backbone ---
    # The backbone is scaffolded BEFORE this step, and scaffold_isa_backbone reuses
    # the existing Study (dropping its hints), so re-calling it here would be a
    # no-op on the name. Apply the plan's descriptive fields directly onto the
    # already-scaffolded Study, fill-don't-clobber, so a generic "Study"
    # placeholder is replaced by the plan name while a real (title-derived) name is
    # preserved. D5-safe: only name/description are merged, never identifiers.
    study_plan = plan.get("study")
    if isinstance(study_plan, dict) and (study_plan.get("name") or study_plan.get("description")):
        study_hints = {
            key: study_plan[key]
            for key in ("name", "description")
            if str(study_plan.get(key) or "").strip()
        }
        try:
            if _merge_backbone_layer(engine, "Study", study_hints):
                result["study"] = 1
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("merge plan study name failed: %s", exc)

    # --- compounds: resolve_compound mints the MolecularEntity + verified ids ---
    # Collect each resolved MolecularEntity id so the test compounds can be WIRED
    # into the Exposure below (#273) — without this they are graph orphans.
    compound_ids: list[str] = []
    for compound in plan.get("compounds") or []:
        name = str((compound or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            # D5: only the NAME is passed; identifiers come from the lookup.
            resolved = engine.run_tool("resolve_compound", name=name)
            result["compounds"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_compound failed for %r: %s", name, exc)
            continue
        # resolve_compound returns {"entity_id", ...} on a hit and {"ok": False}
        # on a miss (which mints no entity, so there is nothing to wire). Capture
        # the id (PubChem AND ChEBI hits alike) for the Exposure/Study linking.
        cid = resolved.get("entity_id") if isinstance(resolved, dict) else None
        if cid:
            compound_ids.append(str(cid))

    # --- cell lines: a CellLineSample from the name only (accession is a lookup) ---
    # Collect each minted CellLineSample id so the cell line can be wired into the
    # CellCulture (and Study) below (#273) rather than left orphaned.
    cell_line_ids: list[str] = []
    for cell_line in plan.get("cell_lines") or []:
        name = str((cell_line or {}).get("name") or "").strip()
        if not name:
            continue
        try:
            cell = engine.run_tool("draft_cell_line_sample", name=name, hints={})
            result["cell_lines"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_cell_line_sample failed for %r: %s", name, exc)
            continue
        # draft_cell_line_sample returns the Entity it created/reused.
        cid = getattr(cell, "entity_id", None)
        if cid:
            cell_line_ids.append(str(cid))

    # --- wire resolved compounds + cell line into the provenance (#273) ---------
    # The resolver/drafter above MINT the right domain entities, but they are
    # orphans until something references them. Wire them deterministically with the
    # canonical ISA-Tox reference fields (NEVER hand-rolled JSON-LD):
    #   * each MolecularEntity → the Exposure LabProcess via `chemicals`. ISA
    #     forbids a MolecularEntity as a process object (objects MUST be
    #     File/Sample/BioSample), so the build connects the compound THROUGH the
    #     Exposure's CSVW condition table (schema:about → MolecularEntity), with the
    #     compound column's valueUrl resolving to its id (_crate_mapping
    #     ._build_process / _synth_condition_table).
    #   * the CellLineSample → the CellCulture LabProcess via `cell_line` (its
    #     consumed input), replacing the synthesized generic `..._input` placeholder.
    #   * both also surface on the Study via schema:mentions (`chemicals` /
    #     `cell_lines`→biologicalModels) so every resolved entity is reachable at a
    #     glance even when (e.g. a ChEBI-only compound) it is not the condition
    #     table's first valueUrl column. Idempotent: `set_fields` overwrites the ref
    #     field with the same deterministic ids, so re-running mints no duplicates.
    chain_by_type = {str(s.get("process_type") or ""): s for s in chain_steps_summary}
    if compound_ids:
        exposure_step = chain_by_type.get("Exposure")
        exposure_id = exposure_step.get("process_id") if exposure_step else None
        if exposure_id:
            _set_ref_field(engine, str(exposure_id), "chemicals", compound_ids)

    # --- condition table (#408): the plan already classified one file as the
    # per-well design table; write it into the Exposure's typed CSVW table rather
    # than leaving the header-only placeholder beside an untyped payload File.
    # Runs regardless of `compound_ids` — a plate map is worth populating even when
    # no compound resolved. ---
    exposure_for_table = (chain_by_type.get("Exposure") or {}).get("process_id")
    if exposure_for_table:
        result["condition_table"] = _populate_condition_table_from_plan(
            engine, plan, str(exposure_for_table)
        )
    else:
        result["condition_table"] = {
            "populated": False,
            "reason": "no Exposure in the process chain to attach a condition table to",
        }
    if cell_line_ids:
        culture_step = chain_by_type.get("CellCulture")
        culture_id = culture_step.get("process_id") if culture_step else None
        if culture_id:
            # CellCulture consumes ONE cell line; use the first resolved one.
            _set_ref_field(engine, str(culture_id), "cell_line", cell_line_ids[0])

    # Surface both on the scaffolded Study via schema:mentions so every resolved
    # entity is reachable from the backbone (no orphan), regardless of which
    # condition-table column resolves to which id.
    study_mention_id = _first_entity_id(engine, "Study")
    if study_mention_id:
        if compound_ids:
            _set_ref_field(engine, study_mention_id, "chemicals", compound_ids)
        if cell_line_ids:
            _set_ref_field(engine, study_mention_id, "cell_lines", cell_line_ids)

    # NOTE: the process chain is now drafted unconditionally ABOVE (#262) — the
    # standard 4-step chain with the plan's step names overlaid — so there is no
    # separate plan-driven chain step here; `chain_steps_summary` (from that call)
    # is what the protocol linking below uses.

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

    # --- people: a Person from the name + a deterministic given/family split,
    # plus an Organization minted (or reused) from `affiliation_name` and wired
    # onto the Person's `affiliation` reference (#179 Lane 1). The plan carries a
    # name only — no ROR/IRI — so the Organization is identifier-free (D5); the
    # build's `_wire_reference` resolves the affiliation to the Organization's
    # `@id`. Organizations are de-duplicated by name so two people sharing an
    # affiliation reference ONE Organization. ---
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
            drafted = engine.run_tool("draft_person", name=name, hints=person_hints)
            result["people"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("draft_person failed for %r: %s", name, exc)
            continue

        affiliation_name = str((person or {}).get("affiliation_name") or "").strip()
        person_id = getattr(drafted, "entity_id", None)
        if not affiliation_name or not person_id:
            continue
        org_id = _find_or_draft_organization(engine, affiliation_name)
        if org_id is not None:
            # `affiliation` is resolved to the Organization `@id` at build time
            # (_crate_mapping._wire_reference); set_fields stores the ref id.
            _set_ref_field(engine, person_id, "affiliation", org_id)

    # --- publications: materialize each via resolve_publication (#219/#224), with
    # a PDF-text recovery step (#245). A plan publication carries a TITLE only
    # (D5 — no DOI), but when the source was a PDF that "title" is frequently the
    # PDF *filename* (e.g. `Wagenaars_etal_2025_OATP1C1.pdf`), which Crossref can
    # never match. `_recover_publication_query` therefore maps each candidate to a
    # real Crossref query:
    #   * a DOI extracted from the PDF text (regex; most reliable) → resolve via
    #     draft_publication_with_authors(doi=…), which looks the DOI up and commits
    #     a DOI-backed ScholarlyArticle (+ authors). The identifier is the LOOKED-UP
    #     DOI, never fabricated (D5).
    #   * a real title (an ordinary plan title, or one extracted from the PDF text
    #     when no DOI was found) → resolve via resolve_publication(title=…), which
    #     commits a DOI-backed ScholarlyArticle ONLY on a confident Crossref match
    #     and otherwise leaves the title deferred (D5).
    #   * nothing recoverable from a PDF filename → SKIP (never query Crossref with
    #     a bare filename); the publication is left as a gap rather than deferred
    #     under an unmatchable filename.
    for publication in plan.get("publications") or []:
        title = str((publication or {}).get("title") or "").strip()
        if not title:
            continue
        try:
            query = _recover_publication_query(engine, title)
        except Exception as exc:  # noqa: BLE001 - recovery must never break the spine
            logger.warning("publication query recovery failed for %r: %s", title, exc)
            query = None
        if query is None:
            # A PDF filename with no recoverable DOI/title — skip (do NOT query
            # Crossref with the filename, and do NOT defer the filename as a
            # "title" to retry; a filename can never become a confident match).
            logger.info(
                "Skipping publication %r — no DOI/title recoverable from its PDF (#245).",
                title,
            )
            continue

        kind, value = query
        try:
            if kind == "doi":
                # The DOI extracted from the PDF text drives the resolution; the
                # composite re-looks it up, so an unresolvable DOI mints nothing.
                pub_result = engine.run_tool("draft_publication_with_authors", doi=value)
                ok = isinstance(pub_result, dict) and bool(pub_result.get("publication_id"))
            else:
                pub_result = engine.run_tool("resolve_publication", title=value)
                ok = isinstance(pub_result, dict) and bool(pub_result.get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("publication resolution failed for %r: %s", value, exc)
            # Only a real (non-filename) title is worth deferring for a retry.
            if kind == "title":
                result["publications_deferred"].append(value)
            continue

        if ok:
            result["publications"] += 1
        elif kind == "title":
            # No confident DOI match — keep the (real) title for later (D5). A DOI
            # that failed its own lookup is not deferred (nothing to retry by).
            result["publications_deferred"].append(value)

    return result


def _run_fix_loop(
    engine: AgentEngine,
    *,
    progress: ProgressSink = _noop_progress,
    save: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """Step 4 — bounded deterministic fix loop.

    Runs ``fix_required_issues`` up to ``_MAX_FIX_ROUNDS`` times, stopping early
    when it reports ``ok`` (no REQUIRED issue remains) or a round makes no
    progress (nothing newly fixed). Returns the last ``build_and_validate`` result
    and the number of fix rounds actually run. All dispatch is deterministic — the
    repair module never calls an LLM or the network.

    A round "makes progress" when it fixes at least one issue; a round that fixes
    nothing means the remaining issues are not deterministically repairable, so
    spinning further is wasted SHACL work.

    ``progress`` receives one concise line per validate/fix phase (#241) and
    ``save`` (when supplied) is called after each ``build_and_validate`` so a
    concurrent dashboard sees the crate converge round by round (#242).
    """
    from builder.tools.validation import build_and_validate

    rounds = 0
    progress("Validating base→ISA→ISA-Tox…")
    last_validation = engine.run_tool("build_and_validate", profile="all", severity="required")
    if save is not None:
        save()
    if last_validation.get("ok"):
        return last_validation, rounds

    for _ in range(_MAX_FIX_ROUNDS):
        rounds += 1
        progress("Resolving gaps…")
        fix_result = engine.run_tool("fix_required_issues", profile="all", severity="required")
        # Re-validate to get the authoritative conformance after this round.
        progress("Validating base→ISA→ISA-Tox…")
        last_validation = engine.run_tool("build_and_validate", profile="all", severity="required")
        # Persist after each fix-loop validate so the dashboard live-updates (#242).
        if save is not None:
            save()
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


def run_pipeline(
    engine: AgentEngine,
    *,
    progress: ProgressSink | None = None,
    save: SaveFn | None = None,
    overrides: ModelOverrides | None = None,
) -> dict[str, Any]:
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

    Progress + persistence (#241 / #242). The spine emits a concise line per phase
    through *progress* and persists ``CrateState`` to ``sessions/<id>/`` via *save*
    after each phase boundary (scaffold, materialize, and every fix-loop validate),
    so a concurrent ``--dashboard`` reflects the build converging live instead of
    showing "No CrateState data available". Persisting CrateState never perturbs
    the built ``@graph`` (the change-detected, on-disk write touches only
    ``crate_state.json``), so the no-provider determinism guarantee is preserved.

    Args:
        engine: An initialized headless :class:`~builder.engine.AgentEngine`. The
            spine mutates ``engine.state`` in place and routes all tool calls
            through the engine (so they are profiled and validation is cached).
        progress: Optional per-phase progress sink (Issue #241). Defaults to a
            strict no-op so the eval and determinism tests stay silent; the
            interactive build threads the CLI ``output`` channel in.
        save: Optional ``save(state, *, always_write=...)`` callback (Issue #242).
            Defaults to the real :func:`builder.tools.session.save_session`. The
            spine calls it (incrementally, change-detected) at each phase boundary
            so a concurrent dashboard live-updates. Injected so the wiring is
            unit-testable with no disk I/O.

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
    emit: ProgressSink = progress or _noop_progress
    save_fn: SaveFn = save or _default_save()

    def _persist() -> None:
        """Persist CrateState at a phase boundary — never let a save failure break
        the spine (the build itself is the load-bearing work)."""
        try:
            save_fn(engine.state)
        except Exception as exc:  # noqa: BLE001 - a save failure must not abort the build
            logger.warning("Pipeline session save failed: %s", exc)

    # Accumulate per-run leaf token usage; the sink also logs each call to the
    # profiler so eval/runner.py mines it via the same path as the ReAct arm.
    totals = {"input_tokens": 0, "output_tokens": 0}
    usage_sink = _make_usage_logger(engine, totals)

    emit("Scaffolding ISA backbone…")
    scaffold = _scaffold_backbone(engine)
    _persist()

    emit("Extracting plan…")
    materialized = _materialize_plan(engine, usage_sink, overrides)
    materialized_count = sum(v for v in materialized.values() if isinstance(v, int))
    if materialized_count:
        emit(f"Materializing {materialized_count} entities…")
    _persist()

    drafted = _draft_entities(engine, usage_sink, overrides)

    validation, fix_rounds = _run_fix_loop(engine, progress=emit, save=_persist)

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
