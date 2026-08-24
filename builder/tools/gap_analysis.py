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
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from builder.state import CrateState, Entity, MITReport
from builder.tools.field_kinds import (
    CITATION_FIELDS,
    PERSON_FIELDS,
    is_identifier_field,
)
from builder.tools.mit_assessment import (
    assess_mit_coverage,
    graph_nodes,
    iter_scorable_params,
    load_mit_yaml,
    scoring_graph,
    slot_matcher,
    slot_type_present,
)

logger = logging.getLogger(__name__)

Tier = Literal["MUST", "SHOULD", "MAY"]
Source = Literal["shacl", "mit", "fair", "air", "identity"]

# The MIT checklist path, loader and parameter traversal all live in
# builder.tools.mit_assessment (#357). This module had its own copy of each, with
# its own dedup, and its own comment saying it "mirrors mit_assessment" — three
# readers of one checklist, free to drift.


@dataclass(frozen=True)
class Gap:
    """One actionable, prioritized gap unified across the three assessors.

    Attributes:
        tier: Requirement tier — ``"MUST"`` (blocking, SHACL REQUIRED),
            ``"SHOULD"`` (recommended), or ``"MAY"`` (optional).
        source: Which assessor produced it — ``"shacl"`` / ``"mit"`` / ``"fair"`` /
            ``"air"`` (Bridge2AI AI-readiness).
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
        air_summary: The Bridge2AI seven-dimension profile plus criterion counts.
            Deliberately carries no aggregate — the instrument's authors refuse one.
        counts: ``{must_open, should_open, may_open}``.
    """

    gaps: list[Gap] = field(default_factory=list)
    conformance: dict[str, bool] = field(default_factory=dict)
    mit_overall: float = 0.0
    fair_summary: dict[str, Any] = field(default_factory=dict)
    air_summary: dict[str, Any] = field(default_factory=dict)
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

# Sentinel ``fix_hint`` for a gap the guidance loop can NOT commit deterministically
# from the gap's own fields (no settable entity field and no crate-level slot). It
# is recorded for reporting but never consumes an ask-user / draft turn — see
# ``builder.agents.pipeline.guidance._next_actionable_gap``.
REPORT_ONLY = "report-only"

# Local property names a crate-level gap (``entity_id is None``) can be committed
# to via the Root Data Entity metadata setter. MUST stay in sync with
# ``builder.agents.pipeline.guidance._CRATE_METADATA_FIELDS`` (the guidance loop's
# ``_apply_value`` is the single place these are actually written); kept here as a
# lower-module constant to avoid a circular import (guidance imports gap_analysis).
_CRATE_SETTABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "title", "description", "identifier", "accession"}
)


def _local_name(iri: str | None) -> str:
    """Local part of a property IRI (after the last ``/`` or ``#``)."""
    if not iri:
        return ""
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _is_committable(
    state: CrateState,
    entity_id: str | None,
    prop: str | None,
    entity_type: str | None = None,
) -> bool:
    """Whether the guidance loop could deterministically commit such a gap.

    Mirrors :func:`builder.agents.pipeline.guidance._apply_value`'s success
    conditions so the gap engine and the guidance loop can never drift on what
    "settable" means. A gap this returns ``False`` for stays in the report (honest
    counting) but is marked ``report-only``, so the loop never spends a human turn
    on a question whose answer it would then discard.

    Committable when the gap names a field the loop can actually write:

    * the field is a **person/agent** or **citation** field — those have composite
      routes of their own (``draft_person`` / the publication composites);
    * an **entity-scoped** gap whose ``entity_id`` really resolves to a state
      entity (``repair._resolve_state_entity``), or the root ``"./"`` with a field
      that maps to a Root Data Entity metadata slot;
    * a **typed** gap (no ``entity_id``, an ``entity_type``) whose field is a crate
      slot **and** whose type has exactly one instance in state — the target
      ``_apply_value`` now resolves;
    * a **crate-level** gap whose field maps to a Root Data Entity slot.

    Never committable, whatever the target:

    * a gap that names **no field** (a node shape) — there is nothing to set;
    * an **identifier-bearing** field (D5 — the value comes from a lookup, so the
      user's answer is refused whatever they type).

    A **reference-only** field stays committable on purpose. Prose cannot be
    committed to one, but naming an entity that already exists in the crate can —
    and that is precisely the useful question when a repair rule declined because
    two candidates were ambiguous ("which File is this analysis' input?"). #375
    makes the *prose* case honest (``_apply_value`` refuses it and the loop asks
    once) rather than removing the interaction.
    """
    field = _local_name(prop)
    if not field:
        return False

    if is_identifier_field(field):
        return False

    if entity_id is not None:
        # Composite routes (`_apply_person_value` / `_apply_citation_value`) commit
        # these without needing the focus node to resolve to a state entity. Scoped
        # to an entity-bearing gap on purpose: a crate-level MIT gap keeps the
        # field gate below, so this cannot flip the ~167 report-only MIT gaps.
        if field in PERSON_FIELDS or field in CITATION_FIELDS:
            return True
        from builder.tools.repair import _resolve_state_entity

        if _resolve_state_entity(state, entity_id) is not None:
            return True
        # The root "./" folds the Investigation and has no separate state entity.
        return entity_id == "./" and field in _CRATE_SETTABLE_FIELDS

    # Typed gap (MIT): committable when the FIELD is settable *and* its type has
    # exactly one named instance to write to. Gating on the field as well as the
    # instance count is load-bearing: dropping the field test would flip the ~150
    # pseudo-field MIT slots (``MolecularEntity:char``, ``LabProcessExposure:param``,
    # …) from report-only to ask-user, and ``set_fields`` would then write literal
    # ``"char"`` / ``"param"`` keys — a strictly worse regression.
    if entity_type not in (None, "Investigation"):
        return (
            field in _CRATE_SETTABLE_FIELDS
            and len(_instances_of(state, entity_type)) == 1
        )

    return field in _CRATE_SETTABLE_FIELDS


def _instances_of(state: CrateState, entity_type: str | None) -> list[Entity]:
    """In-state instances of ``entity_type``.

    The read-only counterpart of ``guidance._instances_for_commit`` — the same
    rule decides whether a typed gap has an unambiguous commit target. Note it
    counts ALL instances, not only named ones: a name is needed to *phrase* the
    question well, but the sole instance of a type is an unambiguous place to
    *write* whether or not it has one, and an unnamed sibling still makes the
    target ambiguous.
    """
    if not entity_type:
        return []
    try:
        return list(state.list_entities(entity_type))
    except (KeyError, ValueError):  # pragma: no cover — unknown type is rare
        return []


# ---------------------------------------------------------------------------
# Identity gaps
# ---------------------------------------------------------------------------

_PUNCT = re.compile(r"[^0-9a-z]+")


def _identity_key(name: str) -> str:
    """A cell-line name with case and punctuation removed, tokens intact.

    "H4", "H-4" and "h 4" collapse together; "CHO-K1" and "CHO-K1 hOATP1C1" do
    not, because the extra token survives. That distinction is the point: an
    engineered derivative is a different line, and
    :func:`~builder.tools.lookups.cell_line_names_match` already refuses to treat
    it as its parent.
    """
    return _PUNCT.sub("", (name or "").casefold())


def _identity_gaps(state: CrateState) -> list[Gap]:
    """Cell lines that may be one line under two spellings — a question, not a merge.

    S-VHPS22 carries "H4" and "H-4" as separate entities, neither resolved. They
    are probably one line typed twice, and the builder must not act on "probably":
    Cellosaurus registers H4 (``CVCL_1239``) and H-4 (``CVCL_6C19``) as DISTINCT
    records that each list the other's name as a synonym, and a third
    (``CVCL_HA56``) also answers to H4. All three are human, so neither
    punctuation nor species separates them. There is no general property that
    tells "one line typed twice" from "two lines sharing a synonym", so merging
    on a normalised name would fabricate an identity (D5).

    Reported only where NOTHING can settle it — every entity in the group lacks
    an accession. One accession anywhere in the group answers the question
    already, and two different accessions mean two lines however alike the names
    look; :func:`_find_cell_line_by_accession` then merges the ones that really
    are the same. This is the residue that identifier resolution could not reach.

    One gap per group, not per entity: three spellings of one name is one
    question.
    """
    groups: dict[str, list[Entity]] = {}
    resolved: set[str] = set()
    for entity in _instances_of(state, "CellLineSample"):
        key = _identity_key(str(entity.fields.get("name") or ""))
        if not key:
            continue
        groups.setdefault(key, []).append(entity)
        if entity.fields.get("accession") or entity.fields.get("identifier"):
            resolved.add(key)

    gaps: list[Gap] = []
    for key, members in sorted(groups.items()):
        if key in resolved or len(members) < 2:
            continue
        names = sorted({str(m.fields.get("name") or m.entity_id) for m in members})
        gaps.append(
            Gap(
                tier="SHOULD",
                source="identity",
                entity_id=members[0].entity_id,
                entity_type="CellLineSample",
                property="name",
                message=(
                    f"{len(members)} cell lines differ only in punctuation or case "
                    f"({', '.join(names)}) and none resolved to a Cellosaurus "
                    "accession. They may be one line under several spellings, or "
                    "genuinely different lines that share a synonym — Cellosaurus "
                    "registers such pairs. The crate cannot tell them apart."
                ),
                suggestion=(
                    "Confirm whether these name one cell line. If they do, give the "
                    "accession (CVCL_*) so the entities merge on identity; if they "
                    "do not, name them distinctly."
                ),
                fix_hint="ask-user",
                auto_fixable=False,
            )
        )
    return gaps


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
            _unique_unwired_input,
        )
    except ImportError:  # pragma: no cover — repair is a sibling module
        return False

    entity = _resolve_state_entity(state, issue.get("entity_id"))
    for rule in _RULES:
        if not rule.applies(issue, entity):
            continue
        # Mirror each rule's "would the repair decline?" check without mutating
        # state, rather than re-deriving each rule's internals:
        # missing_process_output is fixable iff a single un-wired File exists;
        # missing_process_input (its symmetric counterpart) iff a single
        # free-floating Sample/File candidate exists.
        if rule.name == "missing_process_output":
            return _unique_unwired_file(state) is not None
        if rule.name == "missing_process_input":
            return _unique_unwired_input(state) is not None
        # An unknown future rule that owns the shape is treated as auto-fixable;
        # the repair loop is the source of truth and re-validates after.
        return True
    return False


def _shacl_gaps(state: CrateState) -> tuple[list[Gap], dict[str, bool], dict[str, Any] | None]:
    """Build SHACL gaps (all severities) and the conformance dict.

    Runs ``build_and_validate`` at ``recommended`` severity so REQUIRED *and*
    RECOMMENDED issues are surfaced in one pass; OPTIONAL (MAY) issues are also
    swept so the guidance agent sees the full picture.
    """
    # "optional" gates in REQUIRED + RECOMMENDED + OPTIONAL (the widest sweep).
    # The assembled document is captured alongside the verdict so the MIT matcher
    # can score against the SAME assembly rather than building the crate twice.
    from builder.tools.validation import _assemble_and_validate, _synthesize_fix

    try:
        metadata_doc, results = _assemble_and_validate(
            state, severity="optional", profile="all"
        )
    except Exception as exc:  # noqa: BLE001 — mirror build_and_validate's contract
        logger.warning("gap engine: SHACL validation error: %s", exc)
        return [], {}, None
    conformance: dict[str, bool] = {r.profile: r.passed_required for r in results}
    result: dict[str, Any] = {
        "issues": [
            {
                "entity_id": issue.entity_id,
                "property": issue.property,
                "message": issue.message,
                "fix": _synthesize_fix(issue),
                "severity": issue.severity,
                "profile": issue.profile,
            }
            for r in results
            for issue in r.issues
        ],
    }

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
        # (#375) A non-auto-fixable SHACL issue was previously advertised
        # "ask-user" unconditionally, so the loop spent a human turn on gaps
        # `_apply_value` can only refuse — a node shape naming no field, an
        # unresolvable focus node, an identifier, a reference-only property. They
        # stay in the report (honest counting) but no longer consume a turn.
        fix_hint = (
            "fix_required_issues"
            if auto
            else (
                "ask-user"
                if _is_committable(state, issue.get("entity_id"), prop, entity_type)
                else REPORT_ONLY
            )
        )
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
    return gaps, conformance, metadata_doc


# ---------------------------------------------------------------------------
# MIT gaps
# ---------------------------------------------------------------------------




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


def _present_entity_types(state: CrateState) -> set[str]:
    """The set of entity TYPES that have at least one instance in ``state``.

    Used so a MIT parameter keyed solely on an entity type with NO instance (e.g.
    ``MolecularEntity:cas`` when there are zero MolecularEntities) is surfaced as
    a *creation prompt* / report-only gap rather than phrased as if a specific
    chemical/protocol/cell line already exists (#257, fix B).
    """
    return {entity.type for entity in state.list_entities()}


def _mit_gaps(state: CrateState, *, graph: Any | None = None) -> tuple[list[Gap], float]:
    """Unfilled MIT parameters as gaps; also return the overall MIT score.

    A parameter is a gap when *none* of its ``crate_slot`` targets is filled.
    Core parameters (``additional: false``) are ``SHOULD``; ``additional: true``
    ones are ``MAY``. The overall MIT score mirrors ``assess_mit_coverage``.

    A parameter keyed *only* on entity types with NO instance in state has no
    concrete entity to ask about: it is surfaced as a **creation-prompt**,
    ``report-only`` gap (#257, fix B) so the guidance loop never phrases it as a
    specific "this chemical / this protocol" that does not exist.
    """
    mit_data = load_mit_yaml()
    if mit_data is None:
        return [], 0.0

    # (#311) Resolve the graph through the SAME helper the scorer uses, so a
    # caller who holds none gets the same document — and therefore the same score
    # — from both readers. Skipping this is how the two would drift apart: the
    # scorer would assemble and the gap engine would fall back to the field match,
    # and one crate would carry two different coverage numbers.
    graph = scoring_graph(state, graph)

    # (#377) ONE matcher for the crate_slot vocabulary, shared with the scorer.
    # With a graph it resolves what a CrateState field scan structurally cannot:
    # LabProcess* subtypes (not EntityType members), the `char` characteristic
    # traversal, and fields the assembly PROMOTES (a compound's `cas` becomes the
    # node's `identifier`). Without one — only when the crate will not assemble at
    # all — it degrades to the legacy field match, because a degraded list of gaps
    # is still work the user can act on, where a degraded coverage *percentage*
    # would be a claim the scorer rightly refuses to make.
    matcher = slot_matcher(state, graph=graph)
    nodes = graph_nodes(graph) if graph is not None else None
    present_types = _present_entity_types(state)
    gaps: list[Gap] = []
    total_completed = 0
    total_required = 0

    # ONE traversal, shared with the scorer (#357): section walk, dedup by id and
    # the skip rule all live in `iter_scorable_params`, so "the gap engine emits a
    # gap" and "the scorer counts the slot unfilled" range over the same
    # parameters by construction rather than by two copies staying in step.
    for _module, param, slots in iter_scorable_params(mit_data):
        crate_slot = param.get("crate_slot", "")
        total_required += 1
        if any(matcher(et, f) for et, f in slots):
            total_completed += 1
            continue

        # Unfilled -> a gap. Tier from the `additional` flag.
        additional = bool(param.get("additional", False))
        tier: Tier = "MAY" if additional else "SHOULD"
        # The first slot drives the routed property/entity_type (the canonical
        # crate field the parameter maps to); the rest are alternatives.
        slot_entity_type, slot_field = slots[0] if slots else (None, None)
        param_name = param.get("name") or param.get("id") or slot_field or "parameter"
        # (#257, fix B) Does ANY slot reference an entity type that actually has
        # an instance? If not, the parameter is type-level only — there is no
        # concrete entity to ask about, so phrasing it as a specific entity is
        # exactly the bug (asking for "this chemical"'s CAS with zero chemicals).
        has_instance = any(
            slot_type_present(et, nodes) if nodes is not None else et in present_types
            for et, _ in slots
            if et
        )
        if not has_instance:
            # No instance of any of the parameter's entity types: surface a
            # creation-prompt, report-only gap (never a per-field ask on a
            # non-existent entity).
            message = (
                f"No {slot_entity_type or 'matching'} recorded yet; the MIT "
                f"profile expects '{param_name}' (crate_slot {crate_slot}). "
                "Add one to capture it."
            )
            fix_hint = REPORT_ONLY
        # MIT gaps are emitted crate-level (entity_id None). They are only
        # committable when their field maps to a Root Data Entity slot; the
        # rest have no deterministic settable target and are report-only, so
        # the guidance loop surfaces them for context without burning a turn.
        elif not _is_committable(state, None, slot_field, slot_entity_type):
            message = (
                f"MIT parameter '{param_name}' is not yet captured (crate_slot {crate_slot})."
            )
            fix_hint = REPORT_ONLY
        else:
            message = (
                f"MIT parameter '{param_name}' is not yet captured (crate_slot {crate_slot})."
            )
            # MIT enrichment needs content the user provides or a drafter
            # synthesizes; it is never a deterministic auto-fix (D5).
            fix_hint = "ask-user" if not additional else "draft"
        gaps.append(
            Gap(
                tier=tier,
                source="mit",
                entity_id=None,
                entity_type=slot_entity_type,
                property=slot_field,
                message=message,
                suggestion=_mit_suggestion(param),
                fix_hint=fix_hint,
                auto_fixable=False,
            )
        )

    overall = total_completed / total_required if total_required > 0 else 0.0
    return gaps, overall


# ---------------------------------------------------------------------------
# FAIR gaps
# ---------------------------------------------------------------------------


def _fair_gaps(
    state: CrateState, *, graph: Any | None = None, mit: MITReport | None = None
) -> tuple[list[Gap], dict[str, Any]]:
    """Failing FAIR indicators as gaps; also return a FAIR summary.

    A *failing* indicator (``passed is False``) becomes a gap — ``SHOULD`` for an
    essential indicator, ``MAY`` for an important / nice-to-have one. Out-of-scope
    indicators (``passed is None``) are never gaps. The summary carries the DSM
    level and pass/fail indicator counts.

    *graph* and *mit* are the same assembly and the same MIT report the SHACL and
    MIT passes already used. Without them every graph-aware DSM indicator answers
    "not assessed" here while the maturity report answers it properly, and the
    R1.3 coverage indicator reads the never-populated ``state.mit_assessment`` —
    one crate with two FAIR verdicts, of which the builder acts on the blind one.
    This is the same defect #377 fixed for MIT, in the neighbouring assessor.
    """
    from builder.tools.fair_assessment import assess_fair_maturity

    report = assess_fair_maturity(state, mit=mit, graph=graph)
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
                    f"FAIR indicator {indicator_id} not met: {indicator.get('text', '')}".strip()
                ),
                suggestion=f"Dimension {indicator.get('dimension', '')} "
                f"({priority or 'unrated'})".strip(),
                # A FAIR indicator id maps to no settable crate field, so the
                # guidance loop cannot commit it — surface it for context only.
                fix_hint=REPORT_ONLY,
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
# AI-readiness gaps
# ---------------------------------------------------------------------------


def _air_gaps(state: CrateState, *, graph: Any | None = None) -> tuple[list[Gap], dict[str, Any]]:
    """Failing Bridge2AI criteria as gaps; also return the AI-readiness profile.

    This is what makes AI-readiness a thing that changes crates rather than a tile
    that grades them. Three rules keep it honest:

    * **Only a real failure is a gap.** ``passed is None`` means the criterion is not
      assessable from a crate — ethics, governance, hosting — and reporting it as work
      the user could do would be a lie about both the crate and the instrument.
    * **The YAML states intent; :func:`_is_committable` states reality, and reality
      wins.** ``air/criteria.yaml`` declares a remedy per criterion, but a remedy
      naming an entity type with no instance in state has nothing to write to. Letting
      the declaration through would burn a human turn on an answer ``_apply_value``
      then discards — the bug #375 fixed.
    * **Never ``MUST``.** ``MUST`` is the SHACL build gate; no RO-Crate profile
      requires AI-readiness, so emitting one would assert a conformance failure that
      does not exist.

    The gap ``message`` is the criterion id plus its published practice text and
    nothing else. ``_gap_identity`` is ``(source, entity_id, property, message)`` and
    the loop's skip set depends on it, so a live count here would change a gap's
    identity whenever the crate changed and re-draw one the user already answered. The
    counts belong in ``suggestion``, which identity ignores.
    """
    from builder.tools.air_assessment import assess_air_readiness

    report = assess_air_readiness(state, graph=graph)
    gaps: list[Gap] = []

    for criterion in report.criterion_results:
        if criterion.get("passed") is not False:
            continue  # met, or never assessed — neither is work to do
        ident = str(criterion.get("id") or "")
        remedy = criterion.get("remedy") or {}
        prop = remedy.get("property")
        entity_type = remedy.get("entity_type")
        route = remedy.get("route") or REPORT_ONLY

        if route == REPORT_ONLY or not _is_committable(state, None, prop, entity_type):
            route, prop, entity_type = REPORT_ONLY, ident, None

        gaps.append(
            Gap(
                # Assessable and fixable is a recommendation; assessable and merely
                # reportable is optional context. Neither blocks a build.
                tier="SHOULD" if route != REPORT_ONLY else "MAY",
                source="air",
                entity_id=None,
                entity_type=entity_type,
                property=prop,
                message=f"AI-readiness {ident} not met: {criterion.get('text', '')}".strip(),
                suggestion=(
                    f"{criterion.get('label', '')} "
                    f"(dimension {criterion.get('dimension')}) — "
                    f"{criterion.get('evidence', '')}"
                ).strip(),
                fix_hint=route,
                # `auto_fixable` means precisely "fix_required_issues can clear it",
                # and no repair rule targets an AI-readiness criterion. Claiming it
                # would put a gap in front of the user that no tool can close.
                auto_fixable=False,
            )
        )

    summary: dict[str, Any] = {
        "dimensions": report.dimensions,
        "criteria_met": sum(1 for c in report.criterion_results if c.get("passed") is True),
        "criteria_assessed": sum(
            1 for c in report.criterion_results if c.get("passed") is not None
        ),
        "criteria_total": len(report.criterion_results),
    }
    return gaps, summary


# ---------------------------------------------------------------------------
# The unified gap engine
# ---------------------------------------------------------------------------


def _sort_key(gap: Gap) -> tuple:
    """Primary sort by tier, then committable-before-report-only, then stable
    secondary by ``(source, entity_id, property, message)``.

    Ordering committable gaps ahead of ``report-only`` ones within a tier means
    the guidance loop always reaches the gaps it can actually act on first, and
    never has to skip past a wall of un-committable FAIR/MIT gaps to find them.
    """
    return (
        _TIER_RANK[gap.tier],
        0 if gap.fix_hint != REPORT_ONLY else 1,
        gap.source,
        gap.entity_id or "",
        gap.property or "",
        gap.message,
    )


def assess_gaps(state: CrateState) -> GapReport:
    """Unify SHACL + MIT + FAIR + AI-readiness into ONE prioritized :class:`GapReport`.

    Pure and deterministic (no LLM, no network) and side-effect-free — ``state``
    is never mutated, so two calls on the same state return identical ordered
    output. See the module docstring for the tiering and ``auto_fixable`` rules.

    Args:
        state: The crate state to assess.

    Returns:
        A :class:`GapReport` whose ``gaps`` are sorted MUST -> SHOULD -> MAY with
        a stable secondary order by ``(source, entity_id, property)``.
    """
    shacl_gaps, conformance, metadata_doc = _shacl_gaps(state)
    # ONE assembly per call: the MIT matcher scores against the very document the
    # SHACL pass just validated, rather than re-assembling the crate (#377). The
    # FAIR and AI-readiness assessors read that same document, so all four sources
    # answer about one crate rather than four differently-informed views of it.
    mit_gaps, mit_overall = _mit_gaps(state, graph=metadata_doc)
    mit_report = assess_mit_coverage(state, graph=metadata_doc)
    fair_gaps, fair_summary = _fair_gaps(state, graph=metadata_doc, mit=mit_report)
    air_gaps, air_summary = _air_gaps(state, graph=metadata_doc)
    # Reads state, not the assembled document: the question is whether two
    # entities name one thing, which the crate answers identically either way.
    identity_gaps = _identity_gaps(state)

    gaps = sorted(
        [*shacl_gaps, *mit_gaps, *fair_gaps, *air_gaps, *identity_gaps], key=_sort_key
    )

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
        air_summary=air_summary,
        counts=counts,
    )
