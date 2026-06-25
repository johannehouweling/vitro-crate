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

import logging
from collections.abc import Mapping
from typing import Any

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.drafters import (
    VALID_PROCESS_TYPES,
    _make_entity_id,
    draft_assay,
    draft_investigation,
    draft_molecular_entity,
    draft_process,
    draft_publication,
    draft_sample,
    draft_study,
)
from builder.tools.hitl import HumanInterface
from builder.tools.lookups import lookup_compound, lookup_doi, lookup_orcid
from builder.tools.verification import verify_identifier
from lookups.crossref import search_works_by_title
from lookups.orcid import lookup_orcid_by_name

logger = logging.getLogger(__name__)


def _first_of_type(state: CrateState, type_name: str) -> Entity | None:
    """Return the first existing entity of *type_name*, or None."""
    return next((e for e in state.list_entities() if e.type == type_name), None)


def _merge_hints_into(entity: Entity, hints: Mapping[str, Any] | None) -> None:
    """Fill an existing entity's EMPTY fields from *hints* (fill-don't-clobber).

    Makes :func:`scaffold_isa_backbone` idempotent-WITH-MERGE: re-scaffolding a
    reused backbone layer no longer silently drops the supplied hints — a hint
    fills a field the entity is missing or carries an empty value for, but a value
    the entity already holds is never overwritten. Only non-empty hint values are
    applied. The merge is recorded as ``source="llm"`` (the same provenance the
    drafters use for hint-supplied fields). It is purely additive, so the call
    stays a no-op on a fully-populated entity.
    """
    if not hints:
        return
    to_apply: dict[str, Any] = {}
    for key, value in hints.items():
        if value is None or not str(value).strip():
            continue
        current = entity.fields.get(key)
        if current is None or not str(current).strip():
            to_apply[key] = value
    if to_apply:
        entity.set_fields_from_dict(to_apply, source="llm")


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

    def _ensure(type_name: str, make, hints: dict | None) -> Entity:
        existing = _first_of_type(state, type_name)
        if existing is not None:
            reused.append(type_name)
            # Idempotent-with-merge: fill the reused entity's empty fields from the
            # supplied hints instead of silently dropping them (fill-don't-clobber).
            _merge_hints_into(existing, hints)
            return existing
        created.append(type_name)
        return make()

    inv = _ensure(
        "Investigation", lambda: draft_investigation(state, investigation or {}), investigation
    )
    study_entity = _ensure(
        "Study", lambda: draft_study(state, inv.entity_id, study or {}), study
    )
    assay_entity = _ensure(
        "Assay", lambda: draft_assay(state, study_entity.entity_id, assay or {}), assay
    )

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
# LabProcess derivation chain (Issue #179, task 3)
# ---------------------------------------------------------------------------

# The canonical order of the gold S-VHPS21 derivation chain. A supplied chain may
# be any *subset* of these (partial chains work) but is always wired in this
# order so the provenance flows the right way.
_CHAIN_ORDER: tuple[str, ...] = (
    "CellCulture",
    "Exposure",
    "EndpointReadout",
    "DataAnalysis",
)

# Subtypes that produce *material* (a Sample) vs *data* (a File). Determines
# which kind of placeholder entity is synthesized to carry a step's output so the
# next step has something concrete to consume. EndpointReadout / DataAnalysis are
# the two subtypes with NO build-time output fallback (AGENTS.md §14.3); a missing
# schema:result on them fires a tox Violation — closing that trap is this tool's
# load-bearing job, and synthesizing an output for *every* producing step keeps
# the whole derivation chain connected and referenceable.
_SAMPLE_PRODUCERS = frozenset({"CellCulture"})


def _ref_ids(value: Any) -> set[str]:
    """Normalize a reference value (id / {@id} / list thereof) to bare ids."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for v in items:
        key = v.get("@id") if isinstance(v, dict) else v
        if key:
            out.add(str(key).lstrip("#"))
    return out


def _explicit_ids(step: dict[str, Any], *keys: str) -> list[str]:
    """Collect the union of caller-supplied reference ids under ``keys``."""
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for rid in _ref_ids(step.get(key)):
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
    return out


def draft_process_chain(
    state: CrateState,
    assay_id: str,
    chain: list[dict[str, Any]] | None = None,
    validate_after: bool | None = None,
) -> dict[str, Any]:
    """Create and wire a LabProcess derivation chain in ONE idempotent call.

    Fuses the recurring ``draft_process`` + ``link`` sequence that wires the gold
    S-VHPS21 chain::

        Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
               →[EndpointReadout]→ raw/result →[DataAnalysis]→ figures

    into a single call. ``chain`` is an ordered list of step dicts; each step has
    a ``process_type`` (a subset of CellCulture / Exposure / EndpointReadout /
    DataAnalysis — **partial chains are allowed**), optional ``hints`` (passed
    straight to :func:`draft_process`), and optional explicit ``object`` /
    ``result`` reference id(s) (or their ``input`` / ``output`` aliases). Steps
    are always wired in the canonical chain order regardless of input order, so a
    weak model cannot mis-sequence the provenance.

    **The load-bearing job — output synthesis (AGENTS.md §14.3).** Unlike
    CellCulture / Exposure, ``EndpointReadout`` and ``DataAnalysis`` have **no
    build-time output fallback**: a process with no explicit ``result`` (and, for
    DataAnalysis, no ``object``) fires a tox REQUIRED Violation and the chain
    dangles. This composite closes that trap:

    - It threads each producing step's output into the next step's input, so a
      downstream process always *consumes* what the previous step produced.
    - For any step that still lacks a required output (``result`` for both,
      ``object`` for DataAnalysis), it **synthesizes an explicit placeholder data
      entity** — a :func:`~builder.tools.drafters.draft_sample` Sample for a
      material producer (CellCulture) or a
      :func:`~builder.tools.provenance.draft_file` File for a data producer — and
      wires it with :func:`~builder.tools.provenance.link`.

    Synthesized placeholders carry **only** structural metadata (a name, a crate
    path, a role); they NEVER fabricate measurement values or identifiers (D5).
    They are intentionally header-less stubs — use ``populate_condition_table`` /
    ``attach_files`` / ``set_fields`` to fill real content. What this tool
    **requires vs synthesizes**:

    - **Requires:** an existing ``assay_id``; the per-step ``process_type``.
    - **Synthesizes (only when needed):** the produced output entity for a step
      whose output is required but neither supplied nor derivable from the chain.
    - **Respects:** any explicit ``object`` / ``result`` you pass — those win over
      synthesis, and the build still appends the typed CSVW condition table
      (Exposure) / raw-measurements table (EndpointReadout) on top.

    It is **idempotent**: process / placeholder ids are derived deterministically
    from the step, so re-running reuses (overwrites in place) the same entities
    rather than minting duplicates.

    Args:
        state: The crate state to wire the chain into.
        assay_id: entity_id of the parent Assay every process belongs to.
        chain: Ordered list of step dicts (see above). ``None``/empty is a no-op.
        validate_after: When true, also run a full ``build_and_validate`` and
            return it under ``"validation"`` (weak models may pass ``None`` for
            this optional arg; that is treated as false). Named
            ``validate_after`` (not ``validate``) to avoid shadowing
            ``pydantic.BaseModel.validate`` in the generated arg schema; the
            suffix differs from the sibling ``scaffold_isa_backbone`` whose
            ``validate_base`` runs only the base profile, whereas this runs the
            full three-pass ``build_and_validate``.

    Returns:
        ``{"assay_id", "process_ids", "steps", "synthesized"}`` — the ordered
        process entity ids, a per-step summary (``{process_id, process_type,
        object, result}``), and the list of placeholder entity ids synthesized.
        ``"validation"`` is added when ``validate_after`` is true.

    Raises:
        ValueError: If ``assay_id`` is missing/not an Assay, or a step has an
            invalid ``process_type``.
    """
    from builder.tools.provenance import draft_file, link

    assay = state.get_entity(assay_id)
    if assay is None:
        raise ValueError(f"draft_process_chain assay not found: {assay_id!r}.")
    if assay.type != "Assay":
        raise ValueError(
            f"draft_process_chain assay_id must be an Assay; {assay_id!r} is a "
            f"{assay.type}."
        )

    steps_in = list(chain or [])
    for step in steps_in:
        ptype = step.get("process_type")
        if ptype not in VALID_PROCESS_TYPES:
            raise ValueError(
                f"Invalid process_type {ptype!r} in chain step. Must be one of: "
                f"{', '.join(sorted(VALID_PROCESS_TYPES))}."
            )

    # Wire in canonical order regardless of input order so the model cannot
    # mis-sequence the provenance. Within a type, preserve the caller's order.
    ordered = sorted(steps_in, key=lambda s: _CHAIN_ORDER.index(s["process_type"]))

    process_ids: list[str] = []
    step_summaries: list[dict[str, Any]] = []
    synthesized: list[str] = []
    # The most recent step's produced output id(s) — fed to the next step's input.
    upstream_output: list[str] = []

    for step in ordered:
        ptype = str(step["process_type"])
        hints = dict(step.get("hints") or {})
        proc = draft_process(state, assay_id, ptype, hints)

        # --- inputs: explicit wins; otherwise inherit the upstream output ---
        inputs = _explicit_ids(step, "object", "input", "samples")
        if not inputs and upstream_output:
            inputs = list(upstream_output)
        for tid in inputs:
            if state.get_entity(tid) is not None:
                link(state, proc.entity_id, "object", tid)

        # --- outputs: explicit wins; else synthesize a concrete, referenceable
        # output so the chain connects AND the §14.3 "no output fallback" trap is
        # closed. Every producing step gets a real output entity in state (a
        # Sample for a material producer, a File for a data producer) — so the
        # next step can consume it by id and EndpointReadout / DataAnalysis never
        # dangle into a tox Violation. ---
        outputs = _explicit_ids(step, "result", "output")
        if not outputs:
            placeholder = _synthesize_output(
                state, proc, ptype, draft_sample, draft_file
            )
            synthesized.append(placeholder.entity_id)
            outputs = [placeholder.entity_id]
        for tid in outputs:
            if state.get_entity(tid) is not None:
                link(state, proc.entity_id, "result", tid)

        # DataAnalysis MUST also carry schema:object (its raw/condition input).
        # If nothing was wired as input, synthesize a placeholder input File too.
        if ptype == "DataAnalysis" and not inputs:
            placeholder_in = _synthesize_input(state, proc, draft_file)
            synthesized.append(placeholder_in.entity_id)
            link(state, proc.entity_id, "object", placeholder_in.entity_id)
            inputs = [placeholder_in.entity_id]

        process_ids.append(proc.entity_id)
        step_summaries.append(
            {
                "process_id": proc.entity_id,
                "process_type": ptype,
                "object": inputs,
                "result": outputs,
            }
        )
        # The next step consumes what this one produced (its explicit/synthesized
        # output), falling back to this step's inputs so a no-output step (e.g. a
        # bare Exposure relying on the build's typed-table fallback) still hands
        # the material flow downstream.
        upstream_output = outputs or inputs

    result: dict[str, Any] = {
        "assay_id": assay_id,
        "process_ids": process_ids,
        "steps": step_summaries,
        "synthesized": synthesized,
    }

    if validate_after:
        from builder.tools.validation import build_and_validate

        result["validation"] = build_and_validate(state)

    return result


def _synthesize_output(
    state: CrateState,
    proc: Entity,
    ptype: str,
    draft_sample_fn: Any,
    draft_file_fn: Any,
) -> Entity:
    """Create (deterministically) the placeholder output entity for ``proc``.

    A material producer (CellCulture) yields a placeholder Sample; a data
    producer (EndpointReadout / DataAnalysis) yields a placeholder File. The id
    is derived from the process so re-running reuses it (idempotent). The
    placeholder carries only structural metadata — never a fabricated value (D5).
    """
    if ptype in _SAMPLE_PRODUCERS:
        name = f"{proc.entity_id} output sample"
        existing = state.get_entity(f"sample_{_slug(name)}")
        if existing is not None:
            return existing
        sample_hints: dict[str, Any] = {"name": name}
        upstream = _input_ref(proc)
        if upstream is not None:
            sample_hints["derives_from"] = upstream
        return draft_sample_fn(state, sample_hints)
    # Data producer: a placeholder result File.
    name = f"{proc.entity_id}_result.csv"
    file_id = f"file_{_slug(name)}"
    existing = state.get_entity(file_id)
    if existing is not None:
        return existing
    return draft_file_fn(
        state,
        name=name,
        path=f"data/{name}",
        role="processed_data",
    )


def _synthesize_input(state: CrateState, proc: Entity, draft_file_fn: Any) -> Entity:
    """Create the placeholder *input* File a DataAnalysis needs (schema:object).

    Only used when a DataAnalysis has no upstream output and no explicit object —
    it must still declare its analysed input, so we mint a header-less stub.
    """
    name = f"{proc.entity_id}_input.csv"
    file_id = f"file_{_slug(name)}"
    existing = state.get_entity(file_id)
    if existing is not None:
        return existing
    return draft_file_fn(state, name=name, path=f"data/{name}", role="raw_data")


def _input_ref(proc: Entity) -> Any:
    """First wired input of a process (for a Sample's derives_from), or None."""
    for fld in ("object", "input", "samples", "cell_line"):
        ids = _ref_ids(proc.fields.get(fld))
        if ids:
            return next(iter(ids))
    return None


def _slug(text: str) -> str:
    """Mirror drafters._make_entity_id's name normalization for stable ids."""
    base = text.lower().replace(" ", "_").replace("-", "_")
    base = "".join(c for c in base if c.isalnum() or c == "_")
    return base or "unnamed"


# ---------------------------------------------------------------------------
# AOP-Wiki subgraph materialisation (Issue #180)
# ---------------------------------------------------------------------------

# AOP-Wiki @type string -> CrateState EntityType. The three classes share one
# collection (state.ENTITY_TYPE_MAP); the build types each node by its own class.
_AOP_NODE_TYPES: dict[str, EntityType] = {
    "AdverseOutcomePathway": "AdverseOutcomePathway",
    "KeyEvent": "KeyEvent",
    "KeyEventRelationship": "KeyEventRelationship",
}


def _materialize_aop_node(state: CrateState, node: dict[str, Any]) -> Entity | None:
    """Persist one AOP-Wiki node dict into CrateState as a typed entity.

    The node's resolvable AOP-Wiki IRI (``@id``) becomes the entity_id, so
    :func:`builder.tools._crate_mapping._mint_id` keeps it verbatim as the built
    node's ``@id`` and the subgraph's ``has_*`` / ``upstream_event`` /
    ``downstream_event`` reference objects (which already point at sibling IRIs)
    cross-link without any id resolution. Idempotent: a node whose IRI is already
    in state is left untouched (no duplicate, no clobber).

    Returns the materialised (or pre-existing) Entity, or ``None`` for a
    malformed node missing its ``@id`` / ``@type``.
    """
    iri = node.get("@id")
    node_type = _AOP_NODE_TYPES.get(str(node.get("@type")))
    if not iri or node_type is None:
        return None
    existing = state.get_entity(str(iri))
    if existing is not None:
        return existing
    fields = {k: v for k, v in node.items() if k not in ("@id", "@type")}
    entity = Entity(
        entity_id=str(iri),
        type=node_type,
        _provenance=EntityProvenance(created_by="lookup"),
    )
    entity.set_fields_from_dict(fields, source="lookup")
    state.add_entity(entity)
    return entity


def materialize_aop_subgraph(
    state: CrateState,
    aop_id: str,
    study_id: str | None = None,
) -> dict[str, Any]:
    """Turn ONE AOP-Wiki id into the full, cross-linked crate subgraph.

    Looks the AOP up via :func:`builder.tools.lookups.lookup_aop` and
    deterministically materialises its complete subgraph into ``state``:

    - one ``AdverseOutcomePathway`` node carrying its ``name`` / ``identifier`` /
      ``url`` (and ``alternateName`` when present) plus the
      ``has_molecular_initiating_event`` / ``has_key_event`` /
      ``has_adverse_outcome`` / ``has_key_event_relationship`` link arrays;
    - one ``KeyEvent`` node per molecular-initiating-event / key-event /
      adverse-outcome — all share ``@type KeyEvent`` and are discriminated only
      by their ``eventType`` string;
    - one ``KeyEventRelationship`` node per relation, linking its
      ``upstream_event`` and ``downstream_event`` by ``@id``.

    The ONLY model-supplied input is the numeric ``aop_id``; every link and id
    comes straight from the AOP-Wiki graph, so nothing is fabricated (D5). The
    nodes are keyed by their resolvable AOP-Wiki IRI, so re-running is idempotent.

    When ``study_id`` names an existing Study, the AOP is wired onto it via the
    ``aop`` reference (an alias of ``schema:mentions``), connecting the study to
    the pathway it investigates — mirroring the gold crate (Issue #180).

    Args:
        state: The crate state to materialise into.
        aop_id: Numeric AOP-Wiki identifier, e.g. ``"610"``.
        study_id: Optional entity_id of a Study to wire the AOP onto.

    Returns:
        On success, ``{"aop_id", "aop_entity_id", "events", "relationships",
        "wired_to_study"}``. On a lookup miss, ``{"ok": False, "error": ...}``.
    """
    from builder.tools.lookups import lookup_aop

    result = lookup_aop(str(aop_id))
    if not result.get("found"):
        return {
            "ok": False,
            "error": result.get("error", f"AOP '{aop_id}' not found"),
        }

    data = result["data"]
    aop_node = data.get("aop") or {}
    aop_entity = _materialize_aop_node(state, aop_node)

    events = 0
    for ev in data.get("events", []):
        if _materialize_aop_node(state, ev) is not None:
            events += 1

    relationships = 0
    for rel in data.get("relationships", []):
        if _materialize_aop_node(state, rel) is not None:
            relationships += 1

    wired_to_study: str | None = None
    if study_id and aop_entity is not None:
        study = state.get_entity(study_id)
        if study is not None and study.type == "Study":
            existing_refs = study.fields.get("aop") or []
            if not isinstance(existing_refs, list):
                existing_refs = [existing_refs]
            ref = {"@id": aop_entity.entity_id}
            ids = {r.get("@id") if isinstance(r, dict) else r for r in existing_refs}
            if aop_entity.entity_id not in ids:
                existing_refs = [*existing_refs, ref]
            study.fields["aop"] = existing_refs
            study.set_field_status("aop", "filled", "lookup")
            wired_to_study = study_id

    return {
        "aop_id": str(aop_id),
        "aop_entity_id": aop_entity.entity_id if aop_entity else None,
        "events": events,
        "relationships": relationships,
        "wired_to_study": wired_to_study,
    }


# ---------------------------------------------------------------------------
# Publication authors + ORCID harmonization (Issue #180, deferred item)
#
# A citation author who is not already an in-crate Person used to get a
# synthesized blank id (#CitationAuthor_<Given>_<Family>). This composite
# resolves each author's @id to their ORCID when it can be determined — with
# strict verification (D5) and bounded HITL only on genuine ambiguity.
# ---------------------------------------------------------------------------


def _norm(text: Any) -> str:
    """Lowercase + collapse whitespace + drop trailing dots (for name matching)."""
    return " ".join(str(text or "").lower().replace(".", " ").split())


def _given_tokens(given: Any) -> list[str]:
    """Tokenise a given name into comparable parts ('F.M.A.' -> ['f','m','a'])."""
    return [t for t in _norm(given).split() if t]


def _is_initial(token: str) -> bool:
    return len(token) == 1


def _given_match(a: Any, b: Any) -> str:
    """Strength of a given-name match: 'full', 'initial', or '' (no match).

    'full' requires the leading given tokens to share a non-initial first name
    (e.g. 'Fabian' vs 'Fabian Marinus'); 'initial' requires only the first
    initial to agree (e.g. 'F.' / 'F.M.A.' vs 'Fabian'). An empty given on
    either side is treated as an initial-strength match (family-only signal).
    """
    ta, tb = _given_tokens(a), _given_tokens(b)
    if not ta or not tb:
        return "initial"
    fa, fb = ta[0], tb[0]
    if not _is_initial(fa) and not _is_initial(fb):
        return "full" if fa == fb else ""
    # At least one side is an initial — compare first letters.
    return "initial" if fa[0] == fb[0] else ""


def _names_match(given_a: Any, family_a: Any, given_b: Any, family_b: Any) -> str:
    """Match strength between two (given, family) names: 'full' | 'initial' | ''."""
    if not _norm(family_a) or _norm(family_a) != _norm(family_b):
        return ""
    return _given_match(given_a, given_b)


def _bare_orcid(value: Any) -> str:
    """Strip any URL prefix from an ORCID, returning the bare 0000-... id."""
    return str(value or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _verify_orcid(orcid_id: str, family: str, lookup_orcid_fn: Any) -> dict | None:
    """Resolve an ORCID and return its record IFF the family name roughly matches.

    D5: an ORCID is only trusted once :func:`lookup_orcid` resolves it AND the
    resolved family name matches the author's. Returns the resolved data dict on
    success, else ``None`` (a transient outage also yields ``None`` — we never
    attach an unverified ORCID).
    """
    bare = _bare_orcid(orcid_id)
    if not bare:
        return None
    result = lookup_orcid_fn(bare)
    if not result.get("found"):
        return None
    data = result.get("data") or {}
    resolved_family = data.get("familyName", "")
    if _norm(resolved_family) and _norm(resolved_family) == _norm(family):
        return data
    return None


def _find_in_crate_person(
    state: CrateState, given: str, family: str, affiliation: str | None
) -> Entity | None:
    """An in-crate Person with a VERIFIED ORCID matching this author, or None.

    Family must match and the given name must match at least at initial strength
    (this resolves 'Fabian Wagenaars' -> root 'F.M.A. Wagenaars'). When several
    qualify, an affiliation match is preferred, then a full-given match.
    """
    candidates: list[tuple[int, Entity]] = []
    aff_norm = _norm(affiliation)
    for person in state.list_entities("Person"):
        if not person.fields.get("orcid"):
            continue
        status = person.get_field_status("orcid")
        if status is None or status.status != "verified":
            continue
        strength = _names_match(
            given, family, person.fields.get("givenName"), person.fields.get("familyName")
        )
        if not strength:
            continue
        score = 0
        if strength == "full":
            score += 1
        if aff_norm and aff_norm == _norm(person.fields.get("affiliation")):
            score += 2
        candidates.append((score, person))
    if not candidates:
        return None
    candidates.sort(key=lambda sc: sc[0], reverse=True)
    return candidates[0][1]


def _pick_from_human(
    response: Mapping[str, Any] | None, candidates: list[dict], options: list[str]
) -> str | None:
    """Extract a chosen bare ORCID from a HITL ``present`` response, or None.

    Accepts a pick expressed as an explicit ORCID in ``comments``/``edits`` or as
    an option label/index. A ``skipped``/``rejected`` action yields ``None``.
    """
    if not response or response.get("action") in ("skipped", "rejected"):
        return None
    by_orcid = {_bare_orcid(c["orcid"]): c for c in candidates}
    # 1. An edits dict naming the orcid.
    edits = response.get("edits") or {}
    for value in edits.values():
        bare = _bare_orcid(value)
        if bare in by_orcid:
            return bare
    # 2. Free-text comments containing an orcid or an option label.
    comments = str(response.get("comments") or "").strip()
    if comments:
        bare = _bare_orcid(comments)
        if bare in by_orcid:
            return bare
        for idx, label in enumerate(options):
            if comments == label or comments == str(idx) or comments == str(idx + 1):
                if idx < len(candidates):
                    return _bare_orcid(candidates[idx]["orcid"])
    return None


def _resolve_via_search(
    given: str,
    family: str,
    affiliation: str | None,
    human: HumanInterface | None,
    lookup_orcid_fn: Any,
    lookup_by_name_fn: Any,
) -> str | None:
    """Search ORCID and resolve to a verified bare ORCID, escalating if ambiguous.

    Auto-accepts iff there is exactly ONE candidate that is a STRONG match
    (family + full given name). Anything else — multiple candidates, a weak /
    initial-only match, or a single match that fails name verification — is
    escalated to HITL (``present`` the candidates + a none/skip option, then
    optionally ``request_input`` for a pasted ORCID). Returns a verified bare
    ORCID, or ``None`` (no confident answer; caller falls back to synthesis).
    """
    candidates = list(lookup_by_name_fn(given, family, affiliation) or [])
    if not candidates:
        return None

    strong = [
        c
        for c in candidates
        if _names_match(given, family, c.get("given"), c.get("family")) == "full"
    ]

    # Auto-accept ONLY a single, strong, name-verified candidate.
    if len(candidates) == 1 and len(strong) == 1:
        verified = _verify_orcid(strong[0]["orcid"], family, lookup_orcid_fn)
        if verified is not None:
            return _bare_orcid(strong[0]["orcid"])
        # Fall through to HITL — a sole strong match that fails verification is
        # ambiguous, not confidently absent.

    # Genuine ambiguity (or a single weak / unverifiable match): escalate.
    if human is None:
        return None

    options = [
        f"{c.get('given', '')} {c.get('family', '')} — {c.get('orcid')}"
        + (f" ({c['affiliation']})" if c.get("affiliation") else "")
        for c in candidates
    ]
    options.append("None of these / skip")
    context = (
        f"Multiple ORCID candidates for citation author '{given} {family}'. "
        "Pick the correct one (or skip to leave it unresolved):"
    )
    chosen = _pick_from_human(human.present(context, options), candidates, options)
    if chosen is None:
        # Last chance: let the user paste an ORCID directly.
        resp = human.request_input(
            f"Paste the ORCID for '{given} {family}' (or skip):", "identifier"
        )
        if not resp.get("skipped"):
            chosen = _bare_orcid(resp.get("value"))
    if not chosen:
        return None

    # An HITL-chosen ORCID is still verified before use (D5).
    verified = _verify_orcid(chosen, family, lookup_orcid_fn)
    return _bare_orcid(chosen) if verified is not None else None


def _ensure_person_for_orcid(state: CrateState, orcid: str, data: dict) -> Entity:
    """Find-or-create a Person whose @id is the ORCID URL, with a verified ORCID."""
    bare = _bare_orcid(orcid)
    orcid_url = f"https://orcid.org/{bare}"
    existing = state.get_entity(orcid_url) or state.get_entity(bare)
    if existing is not None and existing.type == "Person":
        person = existing
    else:
        person = Entity(
            entity_id=orcid_url,
            type="Person",
            _provenance=EntityProvenance(created_by="lookup"),
        )
        state.add_entity(person)
    fields: dict[str, Any] = {"orcid": bare}
    given = data.get("givenName")
    family = data.get("familyName")
    name = data.get("name") or (f"{given or ''} {family or ''}".strip())
    if name:
        fields["name"] = name
    if given:
        fields["givenName"] = given
    if family:
        fields["familyName"] = family
    affiliation = data.get("affiliation_name")
    if affiliation:
        fields["affiliation"] = affiliation
    person.set_fields_from_dict(fields, source="lookup")
    person.set_field_status("orcid", "verified", "lookup")
    return person


def _synthesize_citation_author(state: CrateState, given: str, family: str) -> Entity:
    """Create (or reuse) the fallback #CitationAuthor_<Given>_<Family> Person."""
    parts = [p for p in (str(given or "").strip(), str(family or "").strip()) if p]
    label = "_".join(parts).replace(" ", "_") or "Unknown"
    entity_id = f"#CitationAuthor_{label}"
    existing = state.get_entity(entity_id)
    if existing is not None:
        return existing
    person = Entity(
        entity_id=entity_id,
        type="Person",
        _provenance=EntityProvenance(created_by="llm"),
    )
    person.set_fields_from_dict(
        {
            "name": " ".join(parts) or "Unknown Author",
            **({"givenName": given} if given else {}),
            **({"familyName": family} if family else {}),
        },
        source="llm",
    )
    state.add_entity(person)
    return person


def _ensure_publication(state: CrateState, doi: str, data: dict) -> Entity:
    """Find-or-create the ScholarlyArticle Publication for a DOI (no author wiring)."""
    bare_doi = data.get("identifier") or doi
    for pub in state.list_entities("Publication"):
        ident = str(pub.fields.get("identifier") or "")
        if ident and (ident == str(bare_doi) or _bare_orcid(ident) == _bare_orcid(doi)):
            return pub
        if pub.fields.get("doi") and _norm(pub.fields["doi"]) == _norm(doi):
            return pub
    hints: dict[str, Any] = {}
    if data.get("name"):
        hints["name"] = data["name"]
    if data.get("headline"):
        hints["headline"] = data["headline"]
    if data.get("datePublished"):
        hints["datePublished"] = data["datePublished"]
    if data.get("url"):
        hints["url"] = data["url"]
    return draft_publication(state, str(bare_doi), hints)


def _wire_author(pub: Entity, person: Entity) -> None:
    """Append a Person reference onto the publication's ``author`` list (deduped)."""
    refs = pub.fields.get("author") or []
    if not isinstance(refs, list):
        refs = [refs]
    ids = {(r.get("@id") if isinstance(r, dict) else r) for r in refs}
    if person.entity_id not in ids:
        refs = [*refs, {"@id": person.entity_id}]
    pub.fields["author"] = refs
    pub.set_field_status("author", "filled", "lookup")


def draft_publication_with_authors(
    state: CrateState,
    doi: str,
    human_interface: HumanInterface | None = None,
) -> dict[str, Any]:
    """Draft a publication and wire each author, harmonizing @ids to ORCIDs (#180).

    Looks the DOI up via Crossref, ensures the ``ScholarlyArticle`` exists in
    state, and for EACH author creates/reuses a ``Person`` wired as the article's
    ``author``. Each author's ``@id`` is resolved by this cascade (first hit wins):

    1. **Crossref ORCID** on the author — verified via :func:`lookup_orcid`
       (resolved family name must match) — used as ``https://orcid.org/<id>``.
    2. **In-crate Person** with a verified ORCID matching the author's family +
       given/initial (affiliation-preferred) — reused (e.g. citation
       'Fabian Wagenaars' → root 'F.M.A. Wagenaars').
    3. **Public ORCID search** (:func:`lookups.orcid.lookup_orcid_by_name`): a
       single STRONG (family + full given) match is verified and used; anything
       ambiguous (multiple candidates, or a weak/initial-only match) is escalated
       to HITL via ``human_interface`` (pick a candidate / paste an ORCID / skip).
    4. **Fallback**: a synthesized ``#CitationAuthor_<Given>_<Family>`` Person.

    D5: an ORCID from (1) or (3) — and an HITL-chosen one — is only attached after
    it resolves and the name roughly matches; (2) is already verified. HITL fires
    ONLY on genuine ambiguity, never when an author is confidently resolved or
    confidently absent.

    Args:
        state: The crate state to draft into.
        doi: The DOI to resolve (with or without a URL prefix).
        human_interface: HITL adapter injected by the engine; when ``None`` the
            search step cannot escalate, so an ambiguous author falls back to a
            synthesized id rather than guessing (D5).

    Returns:
        On success ``{"publication_id", "doi", "authors": [{name, person_id,
        orcid, resolution}], "hitl": int}``. On a DOI miss
        ``{"ok": False, "error": ...}``.
    """
    lookup = lookup_doi(doi)
    if not lookup.get("found"):
        return {"ok": False, "error": lookup.get("error", f"DOI '{doi}' not found")}

    data = lookup.get("data") or {}
    pub = _ensure_publication(state, doi, data)

    authors_out: list[dict[str, Any]] = []
    hitl_count = 0
    for author in data.get("author", []):
        given = author.get("givenName", "")
        family = author.get("familyName", "")
        affiliation = author.get("affiliation")
        resolution = "synthesized"
        person: Entity | None = None

        # (a) Crossref ORCID on the author.
        crossref_orcid = author.get("identifier")
        if crossref_orcid:
            verified = _verify_orcid(crossref_orcid, family, lookup_orcid)
            if verified is not None:
                person = _ensure_person_for_orcid(state, crossref_orcid, verified)
                resolution = "crossref_orcid"

        # (b) In-crate Person with a verified ORCID.
        if person is None:
            match = _find_in_crate_person(state, given, family, affiliation)
            if match is not None:
                person = match
                resolution = "in_crate"

        # (c) Public ORCID search (auto-accept ONE strong match; else HITL).
        if person is None:
            prompts_before = _hitl_prompt_count(human_interface)
            searched = _resolve_via_search(
                given,
                family,
                affiliation,
                human_interface,
                lookup_orcid,
                lookup_orcid_by_name,
            )
            if _hitl_prompt_count(human_interface) > prompts_before:
                hitl_count += 1
            if searched:
                verified = _verify_orcid(searched, family, lookup_orcid)
                if verified is not None:
                    person = _ensure_person_for_orcid(state, searched, verified)
                    resolution = "orcid_search"

        # (d) Fallback synthesized author.
        if person is None:
            person = _synthesize_citation_author(state, given, family)
            resolution = "synthesized"

        _wire_author(pub, person)
        authors_out.append(
            {
                "name": f"{given} {family}".strip(),
                "person_id": person.entity_id,
                "orcid": _bare_orcid(person.fields.get("orcid")) or None,
                "resolution": resolution,
            }
        )

    return {
        "publication_id": pub.entity_id,
        "doi": data.get("identifier") or doi,
        "authors": authors_out,
        "hitl": hitl_count,
    }


def _hitl_prompt_count(human: HumanInterface | None) -> int:
    """Best-effort count of prompts a recording interface has made (for hitl stat)."""
    if human is None:
        return 0
    return len(getattr(human, "present_calls", []) or []) + len(
        getattr(human, "input_calls", []) or []
    )


# ---------------------------------------------------------------------------
# Publication resolution: title -> Crossref -> DOI -> publication (Issue #179)
#
# Closes the gap PR #217 deferred: a plan carries a publication *title* only
# (D5 — no DOI), but ISA REQUIRES a ScholarlyArticle with an identifier. This
# composite resolves the title to a DOI via a Crossref title-search, gated by a
# strict confidence rule so a DOI is only ever committed when it is genuinely the
# titled work — never fabricated (D5) — then reuses draft_publication_with_authors
# (#192) to build the ScholarlyArticle + authors from that DOI.
# ---------------------------------------------------------------------------

# D5 confidence gate. A Crossref title-search hit is only trusted as the DOI for a
# title when BOTH hold:
#   1. Crossref's relevance ``score`` clears _MIN_CROSSREF_SCORE, AND
#   2. the candidate's normalized title is a near-exact match for the query
#      (exact after normalization, OR a containment match with a high token
#      overlap) — so a high score on a *different* paper is rejected.
# Either alone is too weak (a high score can rank an unrelated work first; a title
# match alone says nothing about Crossref's own confidence), so the gate is the
# AND of the two. A miss creates NO entity and reports the reason — never a guess.
_MIN_CROSSREF_SCORE: float = 50.0
_MIN_TITLE_TOKEN_OVERLAP: float = 0.9


def _norm_title(text: Any) -> str:
    """Lowercase + collapse whitespace + drop punctuation (for title matching)."""
    lowered = str(text or "").lower()
    kept = "".join(c if (c.isalnum() or c.isspace()) else " " for c in lowered)
    return " ".join(kept.split())


def _title_overlap(query: str, candidate: str) -> float:
    """Fraction of the query's title tokens present in the candidate's (0..1).

    A symmetric near-exact signal: 1.0 on an exact normalized match, and high
    when one title is contained in / shares almost all tokens with the other
    (e.g. a trailing subtitle on the Crossref side). 0.0 when either is empty.
    """
    q = set(_norm_title(query).split())
    c = set(_norm_title(candidate).split())
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    # Token overlap against the *smaller* set, so a subtitle on one side does not
    # penalise an otherwise-exact match.
    return len(q & c) / min(len(q), len(c))


def _confident_match(
    title: str, candidates: list[dict] | tuple[dict, ...]
) -> dict | None:
    """Return the single confident Crossref candidate for *title*, or None (D5).

    Confidence requires the top-scoring candidate to clear BOTH the Crossref
    score floor and the normalized-title near-exact threshold. Anything weaker —
    a low score, a title mismatch, or no candidates — yields ``None`` so the
    caller commits no DOI and fabricates nothing.
    """
    best: dict | None = None
    best_overlap = 0.0
    for cand in candidates:
        if float(cand.get("score") or 0.0) < _MIN_CROSSREF_SCORE:
            continue
        overlap = _title_overlap(title, cand.get("title", ""))
        if overlap >= _MIN_TITLE_TOKEN_OVERLAP and overlap > best_overlap:
            best, best_overlap = cand, overlap
    return best


def resolve_publication(
    state: CrateState,
    title: str,
    verify: bool | None = None,
) -> dict[str, Any]:
    """Resolve a publication TITLE to a DOI-backed ``ScholarlyArticle`` in ONE call.

    Closes the gap PR #217 deferred: a plan carries a publication *title* only
    (D5 — no DOI), but ISA REQUIRES a ``ScholarlyArticle`` to have an identifier
    and BASE requires the auto-wired root ``citation`` ``@id`` to be an absolute
    URI — both unreachable from a title alone without a DOI. This composite is the
    citation counterpart of :func:`resolve_compound`: the ONLY model-supplied
    input is the ``title``; the identifier comes straight from Crossref, gated so
    nothing is fabricated (D5):

    1. :func:`~lookups.crossref.search_works_by_title` runs a Crossref
       ``query.bibliographic`` search and returns candidate works ranked by
       Crossref's relevance ``score``.
    2. **D5 confidence gate** (:func:`_confident_match`): a candidate is accepted
       ONLY when it clears BOTH the Crossref score floor (``_MIN_CROSSREF_SCORE``)
       AND a normalized-title near-exact match (``_MIN_TITLE_TOKEN_OVERLAP``).
       A high score on a *different* paper, a weak score on the right title, or no
       candidate all fail the gate — in which case this returns
       ``{ok: False, reason: "no confident DOI match", title}`` and creates NO
       entity. A DOI is never invented from a title.
    3. On a confident match it delegates to
       :func:`draft_publication_with_authors` with the resolved DOI, which builds
       the ``ScholarlyArticle`` and wires every author as a ``Person`` (the ORCID
       cascade is already handled there).

    It is **idempotent**, keyed by the resolved DOI: re-running reuses the existing
    ``Publication`` (``draft_publication_with_authors`` find-or-creates it by DOI)
    rather than minting a duplicate, consistent with the other composites.

    This is called by code (materialize / guidance), not chosen by the weak model;
    it is registered four-place for consistency with :func:`resolve_compound`.

    Args:
        state: The crate state to resolve into.
        title: The publication title to resolve (e.g. from an extracted plan).
        verify: Reserved for parity with :func:`resolve_compound`; the DOI is
            implicitly verified by the Crossref resolution
            (:func:`draft_publication_with_authors` re-looks up the DOI, so an
            unresolvable DOI yields no publication). Accepted and ignored.

    Returns:
        On a confident match ``{"ok": True, "doi", "entity_id", "title",
        "score"}``. On no confident match ``{"ok": False, "reason": "no confident
        DOI match", "title"}`` (and no entity is created).
    """
    del verify  # parity-only with resolve_compound; see docstring.
    candidates = list(search_works_by_title(title) or [])
    match = _confident_match(title, candidates)
    if match is None:
        return {"ok": False, "reason": "no confident DOI match", "title": title}

    doi = str(match["doi"])
    drafted = draft_publication_with_authors(state, doi)
    # A DOI that survived the confidence gate but fails the Crossref re-lookup in
    # the drafter (e.g. a transient outage) yields no publication — surface it as
    # an unconfident result rather than a fabricated entity (D5).
    entity_id = drafted.get("publication_id")
    if not entity_id:
        return {"ok": False, "reason": "no confident DOI match", "title": title}

    return {
        "ok": True,
        "doi": drafted.get("doi", doi),
        "entity_id": entity_id,
        "title": title,
        "score": float(match.get("score") or 0.0),
    }


# ---------------------------------------------------------------------------
# Compound resolution: lookup -> draft -> verify (Issue #179, task 3)
# ---------------------------------------------------------------------------

# The order matters: CAS first, then PubChem CID — this mirrors the build's
# _identifier_pv path which emits [CAS, PubChem CID] identifier PropertyValues in
# exactly this order for a MolecularEntity. ``identifier`` is the generic field
# verify_identifier also accepts; we verify the concrete source fields instead.
_COMPOUND_IDENTIFIER_FIELDS: tuple[str, ...] = ("cas", "pubchem_cid")

# Lookup-data keys copied onto the drafted MolecularEntity. ``cas``/``pubchem_cid``
# are the verifiable identifiers; the rest are descriptive structure that needs no
# verification. ``iupac_name`` is intentionally NOT copied onto ``name`` (the
# user-supplied name wins) but is exposed in the return for the caller.
_COMPOUND_DATA_FIELDS: tuple[str, ...] = (
    "cas",
    "pubchem_cid",
    "smiles",
    "inchikey",
    "inchi",
    "formula",
    "mass",
    # ChEBI fallback identity, in context-declared keys (Issue #243): ``chebiId``
    # (the CURIE, schema:identifier) and ``sameAs`` (the dereferenceable ontology
    # IRI as an @id node). The legacy bare ``chebi_id`` / ``chebi_iri`` keys are
    # gone — they were absent from the @context and failed base-profile validation.
    "chebiId",
    "sameAs",
)


def resolve_compound(
    state: CrateState,
    name: str,
    hints: dict[str, Any] | None = None,
    verify: bool | None = None,
) -> dict[str, Any]:
    """Resolve a chemical name to a verified ``MolecularEntity`` in ONE call.

    Fuses the recurring ``lookup_compound`` -> ``draft_molecular_entity`` ->
    ``verify_identifier`` sequence into a single deterministic composite (the
    chemistry counterpart of ``scaffold_isa_backbone``). The ONLY model-supplied
    input is the compound ``name`` (plus optional descriptive ``hints``); every
    identifier comes straight from PubChem/ChEBI, so nothing is fabricated (D5):

    1. :func:`~builder.tools.lookups.lookup_compound` resolves the chemical from
       its name (PubChem, then a ChEBI fallback). On a miss it returns
       ``{"ok": False, "error": ...}`` and creates no entity.
    2. :func:`~builder.tools.drafters.draft_molecular_entity` mints (or reuses) the
       ``MolecularEntity``, carrying the looked-up ``cas`` / ``pubchem_cid`` (and
       ``smiles`` / ``inchikey`` / …) as fields. At build time the shared
       ``_identifier_pv`` path turns ``cas`` + ``pubchem_cid`` into the
       ``[CAS, PubChem CID]`` identifier PropertyValues — this composite does not
       hand-roll that wiring.
    3. :func:`~builder.tools.verification.verify_identifier` confirms each minted
       identifier against its source (D5). ``verify_identifier`` **clears** any
       value that does not resolve, so a failed identifier never lingers on the
       entity as a fabricated id; the per-field verdicts are surfaced in the
       return value (``verified`` is the AND of all of them).

    It is **idempotent**: the entity id is derived deterministically from the
    name, so an existing ``MolecularEntity`` for this name is reused (its
    descriptive fields refreshed) rather than duplicated, consistent with the
    other composites.

    Args:
        state: The crate state to resolve into.
        name: The compound name to resolve (e.g. ``"Silychristin A"``).
        hints: Optional extra descriptive field values for the MolecularEntity
            (e.g. ``{"description": ...}``). Looked-up identifier fields win over
            same-named hints so the verified source value is never overwritten.
        verify: When ``None``/``True`` (the default), verify the minted
            identifiers against source. Pass ``False`` to skip verification (the
            return then reports ``verified`` as ``None``); use only when you will
            verify later, never to attach an unverified id.

    Returns:
        On success ``{"entity_id", "name", "identifiers": {cas?, pubchem_cid?,
        ...}, "verifications": [{field, verified, message}], "verified": bool |
        None, "source"}``. On a lookup miss ``{"ok": False, "error": ...}``.
    """
    lookup = lookup_compound(name)
    if not lookup.get("found"):
        return {
            "ok": False,
            "error": lookup.get("error", f"Compound '{name}' not found"),
        }

    data = lookup.get("data") or {}

    # Identifier/source fields win over caller hints so a verified value from the
    # source is never clobbered by a (possibly stale) hint.
    merged_hints: dict[str, Any] = dict(hints or {})
    for key in _COMPOUND_DATA_FIELDS:
        value = data.get(key)
        if value not in (None, ""):
            merged_hints[key] = value

    # Idempotent: reuse the deterministically-keyed MolecularEntity if present,
    # refreshing its looked-up fields, rather than minting a duplicate.
    entity_id = _make_entity_id("chem", name, merged_hints)
    existing = state.get_entity(entity_id)
    if existing is not None and existing.type == "MolecularEntity":
        entity = existing
        refreshed = {**merged_hints, "name": name}
        entity.set_fields_from_dict(refreshed, source="lookup")
    else:
        entity = draft_molecular_entity(state, name, merged_hints)

    identifiers = {
        key: data[key]
        for key in _COMPOUND_IDENTIFIER_FIELDS
        if data.get(key) not in (None, "")
    }

    verifications: list[dict[str, Any]] = []
    do_verify = verify is None or verify
    if do_verify:
        for field in _COMPOUND_IDENTIFIER_FIELDS:
            if entity.fields.get(field) in (None, ""):
                continue
            verdict = verify_identifier(state, entity.entity_id, field)
            verifications.append(
                {
                    "field": field,
                    "verified": bool(verdict.get("verified")),
                    "message": verdict.get("message", ""),
                }
            )

    verified: bool | None
    if not do_verify:
        verified = None
    elif not verifications:
        verified = False
    else:
        verified = all(v["verified"] for v in verifications)

    return {
        "entity_id": entity.entity_id,
        "name": name,
        "identifiers": identifiers,
        "verifications": verifications,
        "verified": verified,
        "source": data.get("source", "pubchem"),
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("scaffold_isa_backbone", scaffold_isa_backbone, takes_state=True)
TOOL_REGISTRY.register("draft_process_chain", draft_process_chain, takes_state=True)
TOOL_REGISTRY.register("resolve_compound", resolve_compound, takes_state=True)
TOOL_REGISTRY.register("resolve_publication", resolve_publication, takes_state=True)
TOOL_REGISTRY.register(
    "materialize_aop_subgraph", materialize_aop_subgraph, takes_state=True
)
TOOL_REGISTRY.register(
    "draft_publication_with_authors",
    draft_publication_with_authors,
    takes_state=True,
    takes_human=True,
)
