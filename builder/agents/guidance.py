"""The guidance agent — HITL gap-resolution loop (Issue #179, task 2b-G; #244).

This is the **code-driven** loop that consumes the gap engine's prioritized
:class:`~builder.tools.gap_analysis.GapReport` and resolves gaps with the human in
the loop. It is the §14 hybrid architecture's "human-confirmed enrichment" half:
**CODE owns control flow** (NOT a ReAct / LLM-orchestrated agent) and the **user
is the authority** — every commit of uncertain content is confirmed before it
lands (D5: Verify, Don't Trust).

The LLM is used only at bounded **leaves**: the drafter (:func:`draft_entity_fields`)
suggests a value the user confirms, and — the #179 hybrid's "small guidance agent"
(#244) — two more leaves make the *ask-user* step a small bounded exchange that
**phrases** a cryptic gap as one clear question and **interprets** the free-text
reply into a structured decision (commit / skip / clarify / from_file), so a
musing like "no idea which file you mean" can never be stored verbatim as a field
value. With no provider configured (or a leaf unavailable/flaky) the exchange
degrades to the original deterministic ask-and-set, keeping offline runs
deterministic.

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
   - **ask-user** (``fix_hint == "ask-user"``) -> the LLM-mediated
     phrase -> ask -> interpret exchange (#244, :func:`_resolve_ask_user`): one
     clear phrased question via the :class:`~builder.tools.hitl.HumanInterface`,
     the reply interpreted into a structured decision, and only a clean ``commit``
     value applied through the existing ``set_fields`` / ``set_crate_metadata``
     tools (never hand-rolled JSON-LD). ``skip``/``from_file`` commit nothing;
     ``clarify`` asks at most one bounded follow-up. With no provider this is the
     deterministic ask-and-set.

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
* **Every LLM call is a bounded leaf.** The drafter (:func:`draft_entity_fields`)
  and the guidance leaves (:func:`phrase_gap_question` / :func:`interpret_gap_reply`)
  are single bounded calls gated on ``get_provider()``; the drafter's output is
  confirmed before commit, and the interpreter's output is a *structured* decision
  — a free-text reply is never stored verbatim.
* **HITL is never removed.** ask-user and draft-confirm both route through the
  injected :class:`HumanInterface`; the loop cannot silently fabricate content.
* **D5 at the leaf.** Identifier-bearing fields are never committed from the user's
  prose (those come from lookups); the interpreter refuses such a commit.

This is a clean library entrypoint — the CLI / spine wiring is a later PR.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

# Re-exported at module scope so the spine, tests, and the eval harness have a
# single stable monkeypatch target — and so a flaky/absent LLM drafter / guidance
# leaf can be stubbed without importing langchain.
from builder.agents.pipeline import draft_entity_fields
from builder.config import get_provider
from builder.tools.gap_analysis import REPORT_ONLY, Gap, assess_gaps

# (#275) The ORCID lookup, re-exported at module scope as the single stable
# monkeypatch target for tests. A person/agent-typed gap (creator/author/…)
# answered with a name + ORCID verifies the ORCID through this before attaching
# it (D5: an ORCID is only trusted once a lookup confirms the family name). It is
# called only when an ORCID is actually present in the user's prose, so the
# offline / no-provider path stays network-free unless the user supplied one.
from builder.tools.lookups import lookup_orcid

# The guidance leaves (#244, #257): PHRASE the gap into one human question,
# INTERPRET the free-text reply into a structured decision, and EXTRACT a field
# value from a file the user pointed at (#257). Imported at module scope as the
# single monkeypatch target for tests, but guarded so that with langchain absent
# the names resolve to ``None`` and the LLM path is simply skipped (it is gated on
# ``get_provider()`` regardless). Typed as optional callables so the ``None``
# fallback type-checks cleanly.
phrase_gap_question: Callable[..., str] | None
interpret_gap_reply: Callable[..., dict[str, Any]] | None
extract_field_from_file: Callable[..., str] | None
try:  # pragma: no cover — exercised by both branches across the test matrix
    from builder.agents.leaves import (
        extract_field_from_file as _extract_file_leaf,
    )
    from builder.agents.leaves import (
        interpret_gap_reply as _interpret_leaf,
    )
    from builder.agents.leaves import (
        phrase_gap_question as _phrase_leaf,
    )

    phrase_gap_question = _phrase_leaf
    interpret_gap_reply = _interpret_leaf
    extract_field_from_file = _extract_file_leaf
except Exception:  # pragma: no cover — langchain absent: LLM path is gated off anyway
    phrase_gap_question = None
    interpret_gap_reply = None
    extract_field_from_file = None

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

# (#257, fix C) Max characters of a pointed-at file's text fed to the extraction
# leaf. The file readers already cap their own output (Excel rows, the 64 KiB text
# budget); this is a final per-gap belt-and-braces cap so one big file can't blow
# the bounded leaf's token budget. One read per gap.
_MAX_FILE_EXTRACT_CHARS = 32 * 1024

# Cap on clarifying follow-ups within a SINGLE ask-user turn (#244). The interpret
# leaf may ask for clarification, but a vague user could otherwise loop forever;
# after this many follow-ups with no committable value the turn skips the gap.
_MAX_CLARIFY_FOLLOW_UPS = 1

# Descriptive context fields used for the draftable path. Mirrors the spine's
# `_DESCRIPTIVE_APPLY_FIELDS`: the drafter leaf is only trusted for free-text
# descriptive fields (identifiers come from lookups, D5).
_DESCRIPTIVE_FIELDS: frozenset[str] = frozenset({"name", "description"})

# D5: identifier-bearing field names the deterministic interpret fallback must
# NEVER commit from the user's prose (those come from lookups). Mirrors
# `builder.agents.leaves._IDENTIFIER_SCALAR_FIELDS`; kept local so the no-provider
# / offline path stays free of the (langchain-importing) leaves module.
_IDENTIFIER_FIELDS: frozenset[str] = frozenset(
    {
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


def _is_identifier_field(field: str) -> bool:
    """Whether ``field`` (a local property name) is identifier-bearing (D5)."""
    return field in _IDENTIFIER_FIELDS

# (#275) Person/agent-typed fields whose ISA value MUST be a Person (or
# Organization) ENTITY REFERENCE, never a literal string. Answering one of these
# with prose and committing it via ``set_fields`` (a string) leaves the ISA
# "creator MUST be of type Person" SHACL shape unsatisfied, so the gap re-emits
# every round and ``isa=fail`` — the #275 re-ask loop. These are instead routed
# to ``draft_person`` and linked as a reference (see :func:`_apply_person_value`).
_PERSON_FIELDS: frozenset[str] = frozenset(
    {"creator", "author", "publisher", "editor", "contributor"}
)


def _is_person_field(field: str) -> bool:
    """Whether ``field`` (a local property name) is a person/agent-typed field."""
    return field in _PERSON_FIELDS


# An ORCID iD anywhere in free text — the 16-digit, dash-grouped form (final
# group may end in X), with an optional ``https://orcid.org/`` URL prefix so the
# whole token (URL and all) is captured and can be stripped from the name.
_ORCID_RE = re.compile(
    r"(?:https?://orcid\.org/)?\b(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])\b",
    re.IGNORECASE,
)
# An "ORCID:" / "ORCID iD:" label that precedes the id in free text — stripped
# (with any leading comma/semicolon) so only the person's NAME remains.
_ORCID_LABEL_RE = re.compile(
    r"\b[,;]?\s*orcid(?:\s*id)?\s*[:=]?\s*",
    re.IGNORECASE,
)
# An affiliation introduced by a leading "(" or an "@"/"affiliation:" marker.
_AFFILIATION_RE = re.compile(
    r"(?:\baffiliation\s*[:=]\s*|\s+@\s*|\s*\(\s*)(?P<aff>[^()]+?)\s*\)?$",
    re.IGNORECASE,
)

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


def _parse_person_value(value: str) -> tuple[str, str, str]:
    """Parse a free-text person answer into ``(name, bare_orcid, affiliation)``.

    The user's reply to a creator/author gap is prose like
    ``"Fabian Wagenaars"``, ``"Fabian Wagenaars, University Utrecht"`` or
    ``"Fabian Wagenaars, ORCID: 0000-0003-4766-7358"``. This pulls out, in order:

    * a **bare ORCID** anywhere in the text (the dash-grouped 16-digit id),
      reusing the same id shape ``builder.tools.composites`` recognises;
    * an **affiliation** only when it is unambiguously marked — a trailing
      ``(…)``, ``affiliation: …`` or ``… @ org`` — so an inverted ``Last, First``
      name is never mis-read as an affiliation;

    and returns the remaining text as the person's **name** (with the ORCID
    label/id and any marked affiliation stripped). Returns ``("", …)`` for an
    empty/whitespace-only value. This is purely descriptive parsing of prose — no
    identifier is *fabricated*; a parsed ORCID is only trusted after a lookup
    confirms it (D5, see :func:`_verified_orcid_for`).
    """
    text = (value or "").strip()
    if not text:
        return "", "", ""

    orcid_match = _ORCID_RE.search(text)
    bare_orcid = orcid_match.group(1).upper() if orcid_match else ""

    # Strip the ORCID id and its preceding label ("…, ORCID: <id>") from the name.
    if orcid_match:
        text = text[: orcid_match.start()] + text[orcid_match.end() :]
    text = _ORCID_LABEL_RE.sub(" ", text).strip(" ,;")

    affiliation = ""
    aff_match = _AFFILIATION_RE.search(text)
    if aff_match:
        affiliation = aff_match.group("aff").strip()
        text = text[: aff_match.start()].strip(" ,;")

    return text.strip(" ,;"), bare_orcid, affiliation


def _verified_orcid_for(family: str, bare_orcid: str) -> str | None:
    """Return ``bare_orcid`` IFF a lookup confirms it belongs to ``family`` (D5).

    Reuses the composite drafter's verification contract (an ORCID is only
    trusted once :func:`lookup_orcid` resolves it AND the resolved family name
    matches). Returns ``None`` — never an unverified id — on any mismatch, a
    failed/empty lookup, or a transient outage, so the guidance loop attaches
    only verified ORCIDs and never fabricates an identifier from the user's prose.
    """
    if not bare_orcid:
        return None
    try:
        from builder.tools.composites import _verify_orcid
    except ImportError:  # pragma: no cover — composites is a sibling module
        return None
    try:
        verified = _verify_orcid(bare_orcid, family, lookup_orcid)
    except Exception as exc:  # noqa: BLE001 — a flaky lookup must not break the loop
        logger.warning("guidance: ORCID verification failed for %s: %s", bare_orcid, exc)
        return None
    return bare_orcid if verified is not None else None


def _apply_person_value(engine: AgentEngine, gap: Gap, value: str) -> bool:
    """Mint a Person for a person/agent-typed gap and link it by reference (#275).

    Person/agent fields (``creator``/``author``/…) require an ISA Person ENTITY
    reference, not a literal string. This parses the user's prose into a name
    (plus an optional ORCID / affiliation), drafts a Person via the existing
    ``draft_person`` tool (which splits the name ISA-conformantly), and links the
    resulting Person ``@id`` onto the gap entity's field as a ``{"@id": …}``
    reference through ``set_fields`` — never hand-rolled JSON-LD (AGENTS.md §4.7).

    D5: a supplied ORCID is attached only after :func:`_verified_orcid_for`
    confirms it; an unverified one is dropped (the name still mints a Person).

    A crate-level / root person gap (no resolvable state entity, e.g. the
    Investigation's ``creator``) is satisfied by minting the Person alone: the
    crate builder wires every Person onto the Root Data Entity as an author, so
    no explicit field link is needed (and there is no state field to set).
    Returns ``True`` iff a Person was minted, else ``False`` (no usable name).
    """
    name, bare_orcid, affiliation = _parse_person_value(value)
    if not name:
        return False

    from builder.tools.drafters import split_person_name

    given, family = split_person_name(name)
    hints: dict[str, Any] = {}
    if given:
        hints["givenName"] = given
    if family:
        hints["familyName"] = family
    if affiliation:
        hints["affiliation"] = affiliation
    verified = _verified_orcid_for(family, bare_orcid)
    if verified:
        hints["orcid"] = verified

    person = engine.run_tool("draft_person", name=name, hints=hints)
    person_id = getattr(person, "entity_id", None)
    if not person_id:
        return False

    state_id = _resolve_entity_id(engine, gap)
    if state_id is None:
        # Root / crate-level person gap: the Person is auto-wired onto the Root
        # Data Entity as an author by the builder, so minting it suffices.
        return True

    # Link the Person as a REFERENCE on the gap entity's field. The reference @id
    # is the builder's MINTED node id (the ORCID URL for a verified ORCID, else a
    # ``#Person_…`` fragment) so it resolves to the Person node at build time.
    from builder.tools._crate_mapping import _mint_id

    field = _local_name(gap.property) or (gap.property or "")
    ref_id = _mint_id(person)
    engine.run_tool(
        "set_fields", entity_id=state_id, fields={field: {"@id": ref_id}}
    )
    return True


def _apply_value(engine: AgentEngine, gap: Gap, value: str) -> bool:
    """Commit ``value`` for ``gap`` via the existing set tools. Returns success.

    Uses ``set_fields`` for an entity-scoped gap and ``set_crate_metadata`` for a
    crate-level one — never hand-rolled JSON-LD (AGENTS.md §4.7). The target field
    is the local name of the gap's ``property``. Returns ``False`` (committing
    nothing) when the gap names no usable field or its entity cannot be resolved,
    so the caller treats it as "no progress" rather than a silent partial write.

    A **person/agent-typed** field (``creator``/``author``/…) is routed to
    :func:`_apply_person_value` instead: those require an ISA Person ENTITY
    reference, so committing the prose as a literal string would leave the
    "creator MUST be of type Person" SHACL shape unsatisfied and the gap would
    re-emit every round (the #275 re-ask loop). Drafting a Person and linking it
    by reference closes the gap properly.
    """
    field = _local_name(gap.property) or (gap.property or "")
    if not field:
        return False

    # (#275) Person/agent fields need a Person reference, not a literal string.
    if _is_person_field(field):
        return _apply_person_value(engine, gap, value)

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


def _ask_user_prompt(gap: Gap, engine: AgentEngine | None = None) -> str:
    """Build a human-readable ask-user prompt for ``gap``.

    The raw ``gap.message`` is a description of a *failed check* (e.g. "Study MUST
    have a description"), not a question with a field label and expected format —
    surfaced verbatim it reads as a cryptic "What?" box. Instead we assemble a
    clear, multi-line prompt: a direct question naming the field (and the entity /
    tier it applies to), the gap's own explanation, any suggestion, and the
    expected input format. This keeps the human genuinely in the loop (D5).

    When ``engine`` is given and the gap resolves to a concrete named entity
    (#257, fix A), the prompt NAMES that entity ("…for 'Silychristin A' (a
    MolecularEntity)…") so even the deterministic / no-provider path never asks
    about a bare "this chemical / this protocol".
    """
    field = _local_name(gap.property) or (gap.property or "this field")
    entity_name = _gap_entity_name(engine, gap) if engine is not None else None
    if entity_name:
        target = f" for '{entity_name}'"
        if gap.entity_type:
            target += f" (a {gap.entity_type})"
    elif gap.entity_type:
        target = f" on the {gap.entity_type}"
    else:
        target = ""
    lines: list[str] = [f"Please provide a value for '{field}'{target}."]
    if gap.message:
        lines.append(f"Why: {gap.message}")
    if gap.suggestion:
        lines.append(f"Suggestion: {gap.suggestion}")
    lines.append("Expected: free text (leave blank or skip to defer this field).")
    return "\n".join(lines)


def _gap_entity_name(engine: AgentEngine | None, gap: Gap) -> str | None:
    """Resolve the gap's concrete entity name from state, or ``None`` (#257)."""
    if engine is None:
        return None
    state_id = _resolve_entity_id(engine, gap)
    if state_id is None:
        return None
    entity = engine.state.get_entity(state_id)
    if entity is None:
        return None
    return _entity_display_name(entity)


def _ask_user(engine: AgentEngine, human: HumanInterface, gap: Gap) -> str | None:
    """Deterministic ask-and-set: prompt with the canned prompt, return the reply.

    This is the **no-provider / offline** behavior (#244): the user's non-empty
    reply is returned verbatim (the caller commits it as-is) and an empty/skipped
    reply returns ``None``. The LLM-mediated phrase/interpret exchange wraps this
    only when a provider is configured (see :func:`_resolve_ask_user`).
    """
    response = human.request_input(_ask_user_prompt(gap, engine))
    if response.get("skipped"):
        return None
    value = response.get("value")
    if value is None or not str(value).strip():
        return None
    return str(value)


def _reply_text(response: Any) -> str | None:
    """Extract a non-empty reply string from a HumanInterface input response.

    Returns the trimmed text, or ``None`` for a skip / empty reply (which the
    interpret step would treat as a skip anyway).
    """
    if response.get("skipped"):
        return None
    value = response.get("value")
    if value is None or not str(value).strip():
        return None
    return str(value)


# Fields, in preference order, that carry a human-meaningful entity name (#257).
# Person splits the name into givenName/familyName, so those are combined.
_NAME_FIELDS: tuple[str, ...] = ("name", "title")

# Descriptive fields surfaced to the phrase leaf as the entity's KNOWN context
# (#257) so a question can be grounded ("…for Silychristin A (a test compound)…").
# Identifier fields are deliberately excluded — they are never the grounding.
_KNOWN_CONTEXT_FIELDS: tuple[str, ...] = ("name", "title", "description")


def _entity_display_name(entity: Any) -> str | None:
    """A human-readable name for ``entity``, or ``None`` (#257).

    Prefers ``name``/``title``; for a Person assembles ``givenName familyName``.
    Returns ``None`` when nothing usable is recorded — the caller then never claims
    a specific entity exists by a fabricated name.
    """
    fields = getattr(entity, "fields", {}) or {}
    for key in _NAME_FIELDS:
        value = str(fields.get(key) or "").strip()
        if value:
            return value
    # Person: combine given/family when no ``name`` is set.
    given = str(fields.get("givenName") or "").strip()
    family = str(fields.get("familyName") or "").strip()
    combined = " ".join(p for p in (given, family) if p).strip()
    return combined or None


def _known_fields(entity: Any) -> dict[str, str]:
    """The entity's already-known descriptive fields, for grounding the question."""
    fields = getattr(entity, "fields", {}) or {}
    known: dict[str, str] = {}
    for key in _KNOWN_CONTEXT_FIELDS:
        value = str(fields.get(key) or "").strip()
        if value:
            known[key] = value
    return known


def _gap_context(engine: AgentEngine, gap: Gap) -> dict[str, Any]:
    """Assemble the per-gap context dict the guidance leaves consume (#244, #257).

    A compact, jargon-light digest of the gap plus the crate's title/description,
    so :func:`phrase_gap_question` can rephrase it and :func:`interpret_gap_reply`
    can interpret a reply in context. The ``property`` is the **local field name**
    (not the raw IRI) so the interpret leaf's D5 identifier guard sees the same
    token the loop would commit to.

    When the gap has a concrete ``entity_id`` that resolves to a real state entity
    (#257, fix A), the entity's **name** (``entity_name``) and **known descriptive
    fields** (``known_fields``) are threaded in so the phrased question references
    the entity BY NAME — never a bare "this chemical / this protocol / this cell
    line". The entity's own type takes precedence over the gap's coarser
    ``entity_type`` when they differ.
    """
    field = _local_name(gap.property) or (gap.property or "")
    context: dict[str, Any] = {
        "property": field,
        "entity_type": gap.entity_type,
        "tier": gap.tier,
        "message": gap.message,
        "suggestion": gap.suggestion,
    }
    # (#257, fix A) Resolve the concrete entity so the question names it.
    state_id = _resolve_entity_id(engine, gap)
    if state_id is not None:
        entity = engine.state.get_entity(state_id)
        if entity is not None:
            context["entity_type"] = entity.type or gap.entity_type
            name = _entity_display_name(entity)
            if name:
                context["entity_name"] = name
            known = _known_fields(entity)
            if known:
                context["known_fields"] = known
    title = (engine.state.metadata.title or "").strip()
    if title:
        context["crate_title"] = title
    description = (engine.state.metadata.description or "").strip()
    if description:
        context["crate_description"] = description
    return context


def _phrase_question(engine: AgentEngine, gap: Gap) -> str:
    """Phrase ``gap`` as one human question via the LLM leaf, with a safe fallback.

    Calls :func:`phrase_gap_question` (the drafter-tier leaf); on any failure or an
    empty result it falls back to the deterministic :func:`_ask_user_prompt`, so a
    flaky leaf never produces a blank question and never breaks the loop.
    """
    if phrase_gap_question is not None:
        try:
            question = phrase_gap_question(_gap_context(engine, gap))
        except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
            logger.warning("guidance: phrase leaf failed: %s", exc)
            question = ""
        if question and question.strip():
            return question.strip()
    return _ask_user_prompt(gap, engine)


def _deterministic_decision(gap: Gap, reply: str) -> dict[str, Any]:
    """The deterministic interpret fallback: a non-empty reply is a commit (#244).

    Used when the interpret leaf is unavailable or fails (and as the documented
    no-provider behavior): treat a non-empty reply as a commit and an empty one as
    a skip — *except* for an identifier-bearing field, where the user's prose must
    NOT become an identifier value (D5), so it is skipped (identifiers come from
    lookups). This preserves today's ask-and-set behavior without ever storing a
    musing as an identifier.
    """
    if not reply or not reply.strip():
        return {"action": "skip"}
    field = _local_name(gap.property) or (gap.property or "")
    if _is_identifier_field(field):
        return {"action": "skip"}
    return {"action": "commit", "value": reply.strip()}


def _interpret_reply(
    engine: AgentEngine, gap: Gap, question: str, reply: str
) -> dict[str, Any]:
    """Interpret ``reply`` into a structured decision via the LLM leaf.

    Calls :func:`interpret_gap_reply` (the drafter-tier leaf) and returns its
    normalised decision. On any failure it falls back to the **deterministic**
    decision (:func:`_deterministic_decision`: non-empty reply -> commit, empty ->
    skip, identifier field -> skip) — the same offline behavior the no-provider
    path uses, so a flaky/unreachable leaf degrades to today's ask-and-set rather
    than silently dropping the user's answer.
    """
    if interpret_gap_reply is None:  # pragma: no cover — provider gated elsewhere
        return _deterministic_decision(gap, reply)
    try:
        return interpret_gap_reply(question, reply, _gap_context(engine, gap))
    except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
        logger.warning(
            "guidance: interpret leaf failed (%s); deterministic fallback", exc
        )
        return _deterministic_decision(gap, reply)


def _resolve_ask_user(
    engine: AgentEngine, human: HumanInterface, gap: Gap
) -> str | None:
    """Run the LLM-mediated ask-user exchange for ``gap``; return a clean value.

    The §14.6 "small guidance agent" (#244, #257). When a provider is configured
    this is a bounded PHRASE -> ask -> INTERPRET exchange:

    * **PHRASE** the gap into one clear human question (never raw SHACL/FAIR text),
      naming the concrete entity it is about (#257, fix A).
    * **INTERPRET** the free-text reply into a structured decision:
      ``commit`` -> return the clean value (the caller commits it via
      :func:`_apply_value`); ``skip`` (covers "I don't know"/empty) -> return
      ``None``; ``clarify`` -> ask ONE bounded follow-up
      (:data:`_MAX_CLARIFY_FOLLOW_UPS`) then skip; ``from_file`` -> **read the file
      the user pointed at and EXTRACT the requested value** (#257, fix C), then
      return it to commit. The reply prose is still NEVER stored verbatim — only a
      value extracted from the file's text by the bounded
      :func:`extract_field_from_file` leaf, and only when the file is inside an
      approved scan root. An unreadable / outside-root file gracefully skips.

    A free-text musing therefore can never become a field value (D5). With **no
    provider** configured this degrades to the deterministic :func:`_ask_user`
    (today's ask-and-set behavior), keeping offline runs deterministic.
    """
    # No provider -> deterministic ask-and-set (offline determinism, #244).
    if get_provider() is None:
        return _ask_user(engine, human, gap)

    question = _phrase_question(engine, gap)
    response = human.request_input(question)
    reply = _reply_text(response)
    if reply is None:
        # An explicit skip / empty reply is a skip — never interpreted.
        return None

    follow_ups = 0
    while True:
        decision = _interpret_reply(engine, gap, question, reply)
        action = decision.get("action")

        if action == "commit":
            value = decision.get("value")
            # Defensive: the leaf normalises this, but never commit empty.
            if isinstance(value, str) and value.strip():
                return value.strip()
            return None

        if action == "clarify" and follow_ups < _MAX_CLARIFY_FOLLOW_UPS:
            follow_ups += 1
            follow_up = decision.get("question") or _ask_user_prompt(gap, engine)
            question = str(follow_up)
            response = human.request_input(question)
            reply = _reply_text(response)
            if reply is None:
                return None
            continue

        if action == "from_file":
            # (#257, fix C) Actually READ the file and EXTRACT the value — instead
            # of logging the hint and skipping (the #244 behavior). The reply's
            # prose is never stored; only a value the extraction leaf pulls from
            # the file's text, gated to approved scan roots.
            return _resolve_from_file(engine, gap, reply, decision.get("filename"))

        # skip, an exhausted clarify budget, or any unrecognised action -> skip.
        return None


def _candidate_file_paths(
    engine: AgentEngine, filename: str | None, reply: str
) -> list[str]:
    """Likely on-disk paths the user pointed at, best-effort and bounded (#257).

    Combines, in priority order:

    1. the ``filename`` hint the interpret leaf returned (as-is, and resolved
       under each approved scan root / against the scanned-file inventory by base
       name);
    2. any scanned file whose base name appears verbatim in the user's ``reply``.

    The result is de-duplicated, order-preserving. Containment (the sandbox) is
    enforced by the caller via :func:`builder.tools.scanner._contain` — this only
    proposes candidates, it never widens access.
    """
    candidates: list[str] = []

    def _add(path: str | None) -> None:
        if path and path not in candidates:
            candidates.append(path)

    scanned = list(getattr(engine.state, "scanned_files", []) or [])

    if filename:
        hint = filename.strip()
        _add(hint)
        base = Path(hint).name
        # Resolve a bare base name against the approved roots and the inventory.
        for root in engine.state.approved_scan_roots:
            _add(str(Path(root) / base))
        for f in scanned:
            if Path(f.path).name == base or f.filename == base:
                _add(f.path)

    # Any scanned file the user named verbatim in their reply.
    if reply:
        for f in scanned:
            name = f.filename or Path(f.path).name
            if name and name in reply:
                _add(f.path)

    return candidates


def _read_pointed_file(engine: AgentEngine, candidates: list[str]) -> str | None:
    """Read the FIRST candidate inside an approved scan root, bounded (#257).

    One read per gap: returns the (size-capped) text of the first candidate that
    (a) resolves inside an approved scan root and (b) the readers can read.
    Returns ``None`` when no candidate is readable in-sandbox — a graceful skip.
    Never raises out: any reader/optional-dependency error is logged and skipped,
    so a single unreadable file can never break the loop.
    """
    from builder.tools.file_readers import read_file
    from builder.tools.scanner import _contain

    roots = engine.state.approved_scan_roots
    for candidate in candidates:
        resolved = _contain(candidate, roots)
        if resolved is None:
            logger.debug(
                "guidance: from_file refused %s — outside approved scan roots (#257).",
                candidate,
            )
            continue
        try:
            body = read_file(str(resolved))
        except (OSError, ValueError, ImportError) as exc:
            logger.warning("guidance: from_file read failed for %s: %s", candidate, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — a malformed file must not break the loop
            logger.warning(
                "guidance: unexpected from_file read error for %s: %s", candidate, exc
            )
            continue
        if body and body.strip():
            return body[:_MAX_FILE_EXTRACT_CHARS]
    return None


def _resolve_from_file(
    engine: AgentEngine, gap: Gap, reply: str, filename: str | None
) -> str | None:
    """Read the file the user pointed at and extract the gap's value (#257, fix C).

    Returns a clean value to commit, or ``None`` to skip. The flow:

    1. locate likely file paths (:func:`_candidate_file_paths`);
    2. read the first one INSIDE an approved scan root (:func:`_read_pointed_file`)
       — one bounded read; an unreadable / outside-root file gracefully skips;
    3. extract the requested field's value from the file text via the bounded
       :func:`extract_field_from_file` leaf (a flaky leaf -> skip, never crashes);
    4. D5: never return a value for an identifier-bearing field — those come from
       lookups, not file text — so an identifier gap always skips here.

    The user's reply prose is NEVER stored: only a value the leaf extracts from the
    file's text is returned.
    """
    field = _local_name(gap.property) or (gap.property or "")
    # D5: identifiers come from lookups, never extracted from a file's prose.
    if _is_identifier_field(field):
        logger.info(
            "guidance: from_file for identifier field %s -> skip (lookups only, D5).",
            field,
        )
        return None

    if extract_field_from_file is None:  # pragma: no cover — provider gated elsewhere
        return None

    candidates = _candidate_file_paths(engine, filename, reply)
    if not candidates:
        logger.info("guidance: from_file but no file could be located; skipping (#257).")
        return None

    file_text = _read_pointed_file(engine, candidates)
    if file_text is None:
        logger.info(
            "guidance: from_file file unreadable / outside scan roots; skipping (#257)."
        )
        return None

    try:
        value = extract_field_from_file(field, file_text, _gap_context(engine, gap))
    except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
        logger.warning("guidance: from_file extraction leaf failed: %s", exc)
        return None

    if isinstance(value, str) and value.strip():
        logger.info("guidance: extracted %s from a pointed-at file (#257).", field)
        return value.strip()
    logger.info(
        "guidance: pointed-at file did not yield a value for %s; skipping (#257).",
        field,
    )
    return None


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

    # --- ask-user: phrase -> interpret -> apply (LLM-mediated, #244) ----------
    asked.append(record)
    value = _resolve_ask_user(engine, human, gap)
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
