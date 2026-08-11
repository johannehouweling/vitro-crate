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
     deterministic ask-and-set — still routed through
     :func:`_deterministic_decision`, so the D5 identifier skip applies offline too.

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
* **Every leaf call is accounted.** Each one reports its token usage to the run's
  :data:`UsageSink`, which logs the same profile event the spine and the ReAct
  model node log — so the status bar re-printed before every question, the
  ``--dashboard`` table and the eval all include the tail's spend (#384).
* **HITL is never removed.** ask-user and draft-confirm both route through the
  injected :class:`HumanInterface`; the loop cannot silently fabricate content.
* **D5 at the chokepoint.** Identifier-bearing fields are never committed from the
  user's prose (those come from lookups). The refusal lives in :func:`_apply_value`,
  which *every* commit funnels through — the interpreter refuses one too, but it is
  not on the no-provider path, so guarding only there left the guarantee unmet
  (#375).
* **A ``True`` commit is one the crate will carry.** :func:`_apply_value` writes a
  typed gap to its own instance rather than the Root Data Entity, and refuses a
  reference-only field a value that would not resolve — the build silently drops
  such a literal, so reporting success would be a lie (#375).

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
#
# ``UsageSink`` / ``make_usage_logger`` come from the SHARED llm module rather
# than from the spine (#384): the tail reuses the one implementation verbatim
# instead of reimplementing it, so the logger emits the same
# ``node_end``/``node="model"`` profile event the spine and the ReAct model node
# emit — the only surface the status bar, the ``--dashboard`` table and the eval
# read. Importing it from ``pipeline`` would work too, but would leave the tail
# depending on the spine for something neither of them owns.
from builder.agents.llm import ModelOverrides, UsageSink, make_usage_logger
from builder.agents.pipeline.pipeline import draft_entity_fields
from builder.config import get_provider
from builder.tools.field_kinds import (
    CITATION_FIELDS,
    IDENTIFIER_FIELDS,
    PERSON_FIELDS,
    is_citation_field,
    is_identifier_field,
    is_person_field,
    is_reference_field,
    is_resolvable_reference,
)
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
    from builder.agents.pipeline.leaves import (
        extract_field_from_file as _extract_file_leaf,
    )
    from builder.agents.pipeline.leaves import (
        interpret_gap_reply as _interpret_leaf,
    )
    from builder.agents.pipeline.leaves import (
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

# D5: identifier-bearing field names that must NEVER be committed from the user's
# prose (those come from lookups). The definition now lives in the shared
# :mod:`builder.tools.field_kinds` — one vocabulary for both build arms, replacing
# the byte-identical copy that also existed in ``leaves`` (#375). These aliases
# are kept so existing callers and monkeypatching tests are unaffected.
_IDENTIFIER_FIELDS = IDENTIFIER_FIELDS
_is_identifier_field = is_identifier_field


# (#275) Person/agent-typed fields whose ISA value MUST be a Person (or
# Organization) ENTITY REFERENCE, never a literal string. Answering one of these
# with prose and committing it via ``set_fields`` (a string) leaves the ISA
# "creator MUST be of type Person" SHACL shape unsatisfied, so the gap re-emits
# every round and ``isa=fail`` — the #275 re-ask loop. These are instead routed
# to ``draft_person`` and linked as a reference (see :func:`_apply_person_value`).
# Defined once in the shared :mod:`builder.tools.field_kinds` (#375) so the gap
# engine can ask the same question without importing guidance (which would be a
# cycle: guidance imports gap_analysis).
_PERSON_FIELDS = PERSON_FIELDS
_is_person_field = is_person_field


# (Commit 1, #179) Citation-typed fields whose ISA/BASE value MUST be a
# ``ScholarlyArticle`` ENTITY REFERENCE (with an absolute-URI ``@id``), never a
# literal string. The root Data Entity's ``citation`` MUST gap surfaces with
# ``entity_id == "./"``, which ``_resolve_entity_id`` cannot map to a state entity
# and which is not a crate-metadata slot — so committing the prose as a string did
# nothing and the always-highest-priority gap was re-asked every round. These are
# instead routed to the publication composites (see :func:`_apply_citation_value`).
_CITATION_FIELDS = CITATION_FIELDS
_is_citation_field = is_citation_field


# (#382) The Assay's AOP Key Event slot, in all three spellings the crate mapping
# accepts (``_ASSAY_MENTION_FIELDS``); all expand to ``schema:mentions``. Its value
# MUST be a reference to a ``KeyEvent`` ALREADY IN THE CRATE — ``_wire_mention``
# emits nothing for free text, so storing the user's reply as a literal string
# would report success while the answer vanished and the gap re-emitted: the same
# #275 / #179 re-ask class as the person and citation fields above. The reply is
# instead routed to ``link_assay_to_key_event`` (see
# :func:`_apply_key_event_value`), which resolves the name against the in-crate
# KeyEvents and commits their AOP-Wiki IRI.
#
# Kept local rather than in :mod:`builder.tools.field_kinds`: unlike the person and
# citation vocabularies, nothing outside this module asks the question — the gap
# engine has no key-event branch (emitting that gap is deliberately out of scope,
# #382 "Out of scope"), so a shared constant would advertise a contract no second
# reader has.
_KEY_EVENT_FIELDS: frozenset[str] = frozenset({"key_event", "keyEvent", "key_events"})


def _is_key_event_field(field: str) -> bool:
    """Whether ``field`` is the Assay's AOP Key Event reference slot (#382)."""
    return field in _KEY_EVENT_FIELDS


def _gap_is_root(engine: AgentEngine, gap: Gap) -> bool:
    """Whether ``gap`` targets the Root Data Entity (``./`` / no state entity).

    A root/crate-level gap is one whose ``entity_id`` is ``None`` or the literal
    ``"./"``, or one that ``_resolve_entity_id`` cannot map back to a real state
    entity (the build's root ``./`` node folds the Investigation and has no
    separate state node — see :func:`builder.tools.repair._resolve_state_entity`).
    """
    if gap.entity_id is None or gap.entity_id == "./":
        return True
    return _resolve_entity_id(engine, gap) is None


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
# A DOI anywhere in free text — the ``10.<registrant>/<suffix>`` form (mirrors the
# pipeline's ``_DOI_RE``). Used to route a root citation answer to the DOI-based
# publication composite rather than a title search (Commit 1, #179).
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
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


# A gap's stable IDENTITY, used by the per-RUN skip-set (Commit 1, #179). Two gaps
# from different reports are "the same gap" when these four fields match — the
# loop keys un-progressable gaps by identity (NOT by report index, which is reset
# on every commit) so an already-tried gap (e.g. the always-highest-priority root
# citation MUST gap) is never re-drawn even after a different gap commits and a
# fresh re-assess re-emits it.
GapIdentity = tuple[str | None, str | None, str | None, str]


def _gap_identity(gap: Gap) -> GapIdentity:
    """A stable identity tuple ``(source, entity_id, property, message)`` for ``gap``."""
    return (gap.source, gap.entity_id, gap.property, gap.message)


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


def _apply_measurement_method(
    engine: AgentEngine, gap: Gap, value: str, *, human: HumanInterface | None = None
) -> bool:
    """Resolve a human method description into a crate DefinedTerm reference."""
    state_id = _resolve_entity_id(engine, gap)
    if state_id is None:
        return False
    try:
        from builder.tools.lookups import lookup_bao_term

        result = lookup_bao_term(value.strip())
    except Exception as exc:  # noqa: BLE001 — a lookup failure is a guidance skip
        logger.warning("guidance: measurement method lookup failed: %s", exc)
        result = {"found": False}
    data = result.get("data") if isinstance(result, dict) else None
    if not result.get("found") or not isinstance(data, dict) or not data.get("@id"):
        _notify(
            human,
            f"I could not resolve '{value}' to a BioAssay Ontology method. "
            "Please provide a more specific method name or skip this field.",
        )
        return False
    hints = {
        "entity_id": data["@id"],
        "termCode": data.get("termCode") or data.get("term_code"),
        "inDefinedTermSet": data.get("inDefinedTermSet") or "http://bioassayontology.org/bao",
    }
    term = engine.run_tool("draft_defined_term", name=data.get("name") or value, hints=hints)
    term_id = getattr(term, "entity_id", None)
    if not term_id:
        return False
    field = _local_name(gap.property) or "measurementMethod"
    engine.run_tool("set_fields", entity_id=state_id, fields={field: {"@id": term_id}})
    return True


# Root-level agent properties and the ``CrateMetadata`` slot each is wired from.
# ``author`` is deliberately absent: the builder appends EVERY Person to the root
# as an author, so that gap closes on minting alone.
_ROOT_ATTRIBUTION_SLOTS: dict[str, str] = {
    "publisher": "publisher",
    "creator": "creator",
    "contactPoint": "contact",
}


def _record_root_attribution(engine: AgentEngine, gap: Gap, person_id: str) -> bool:
    """Record an answered root person gap against the property it answered (#337).

    The root Data Entity has no state node, so a root person gap used to be
    considered satisfied by minting the Person alone — on the assumption that the
    builder auto-wires every Person onto the root. It does, but only as
    ``author``. ``publisher`` / ``creator`` / ``contactPoint`` are wired
    exclusively from ``CrateMetadata`` by
    :func:`~builder.tools._crate_mapping._wire_root_attribution`, so answering
    "who should be credited as the publisher?" wrote nothing that could close
    ``./ schema:publisher``.

    The failure was self-sustaining rather than merely incomplete: ``_apply_value``
    returned True, so ``run_guidance`` counted it as progress and kept the gap out
    of ``tried_identities``; the same gap re-emerged the next round and was
    re-worded by ``phrase_gap_question``, which is why one run asked for the
    publisher twice in different words. The user's answer was discarded each time.

    Recording the user's own answer against the slot they answered is also why
    this belongs here rather than in the writer: making the writer promote an
    arbitrary Person to ``publisher`` would credit someone the user never named,
    on a crate where publisher is often the institution rather than the
    researcher. Here nothing is inferred — the value is what was asked for.
    """
    # SHACL reports a full IRI (``http://schema.org/publisher``); a CURIE is
    # accepted too so a differently-shaped gap source degrades to a no-op only
    # when the property genuinely is not a root attribution slot.
    local = (_local_name(gap.property) or "").rsplit(":", 1)[-1]
    slot = _ROOT_ATTRIBUTION_SLOTS.get(local)
    if slot is None:
        return True
    # The freshly answered value wins over any earlier one: this gap being open
    # is evidence whatever was there did not satisfy it.
    setattr(engine.state.metadata, slot, person_id)
    return True


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

    A crate-level / root person gap (no resolvable state entity) has no state
    field to set, so it is routed to :func:`_record_root_attribution`, which
    records the answer against the ``CrateMetadata`` slot the gap asked about.
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
        return _record_root_attribution(engine, gap, person_id)

    # Link the Person as a REFERENCE on the gap entity's field. The reference @id
    # is the builder's MINTED node id (the ORCID URL for a verified ORCID, else a
    # ``#Person_…`` fragment) so it resolves to the Person node at build time.
    from builder.tools._crate_mapping import _mint_id

    field = _local_name(gap.property) or (gap.property or "")
    ref_id = _mint_id(person)
    engine.run_tool("set_fields", entity_id=state_id, fields={field: {"@id": ref_id}})
    return True


def _apply_citation_value(engine: AgentEngine, gap: Gap, value: str) -> bool:
    """Resolve a root ``citation`` answer to a ``ScholarlyArticle`` (Commit 1, #179).

    The root Data Entity's ``citation`` requirement (BASE: the auto-wired root
    ``citation`` ``@id`` must be an absolute URI; ISA: a ``ScholarlyArticle`` with
    an identifier) cannot be satisfied by committing the user's prose as a literal
    string — ``set_fields`` has no resolvable state target for the ``./`` root, and
    ``citation`` is not a crate-metadata slot, so the OLD path silently dropped the
    answer and the always-highest-priority gap was re-asked every round.

    Instead the answer is routed to the existing publication composites (never
    hand-rolled JSON-LD, AGENTS.md §4.7), mirroring how :func:`_apply_person_value`
    special-cases person fields. The builder auto-wires the resulting
    ``ScholarlyArticle`` onto ``root_dataset.citation``:

    * an answer carrying a **DOI** (or a DOI inside a URL) -> ``draft_publication_
      with_authors(doi=…)`` — the DOI is re-looked-up, so an unresolvable DOI mints
      nothing (D5);
    * otherwise the answer is treated as a **title** ->
      ``resolve_publication(title=…)`` — which commits a DOI-backed
      ``ScholarlyArticle`` ONLY on a confident Crossref match (D5).

    Returns ``True`` iff a publication entity was created so the gap clears, else
    ``False`` (no usable answer, or no confident match — committing nothing).
    """
    text = (value or "").strip()
    if not text:
        return False

    doi_match = _DOI_RE.search(text)
    try:
        if doi_match:
            result = engine.run_tool("draft_publication_with_authors", doi=doi_match.group(0))
            return isinstance(result, dict) and bool(result.get("publication_id"))
        result = engine.run_tool("resolve_publication", title=text)
        return isinstance(result, dict) and bool(result.get("ok"))
    except Exception as exc:  # noqa: BLE001 — a flaky lookup must not break the loop
        logger.warning("guidance: citation resolution failed for %r: %s", text, exc)
        return False


def _apply_key_event_value(
    engine: AgentEngine, gap: Gap, value: str, *, human: HumanInterface | None = None
) -> bool:
    """Resolve an Assay's Key Event answer against the in-crate KeyEvents (#382).

    ``keyEvent`` is a reference-only property (an alias of ``schema:mentions``):
    the build strips it out of the node's scalar properties and ``_wire_mention``
    emits nothing for a value that is neither a resolvable IRI nor an index hit.
    So committing the user's prose ("mitochondrial dysfunction") as a literal
    string would return ``True`` while the crate carried nothing and the gap
    re-emitted next round — the #275 / #179 re-ask class.

    The answer is instead routed to ``link_assay_to_key_event`` (never hand-rolled
    JSON-LD, AGENTS.md §4.7), which matches the name against the ``KeyEvent``
    entities already in state and commits THEIR AOP-Wiki id.

    Returns ``True`` only when the link was actually written. A zero or ambiguous
    match returns ``False`` **and commits nothing**: which Key Event an assay
    measures is a scientific claim, and reporting a refusal as progress would be
    the same lie in the opposite direction. The candidate names come back on the
    tool result, so the user is shown what the crate can actually offer instead of
    being asked the identical question again.
    """
    text = (value or "").strip()
    if not text:
        return False

    # A MIT gap carries `entity_id=None` + `entity_type="Assay"`, a SHACL gap the
    # assay node; resolve both the way `_apply_value` resolves its own targets, so
    # the answer lands on the entity the question was phrased about (#375).
    assay_id = _resolve_entity_id(engine, gap)
    if assay_id is None:
        instances = _instances_for_commit(engine, "Assay")
        if len(instances) != 1:
            if len(instances) > 1:
                _notify(
                    human,
                    "there are several Assay entries, so it is not clear which one "
                    "measures that key event — your answer was not stored.",
                )
            return False
        assay_id = instances[0].entity_id

    try:
        result = engine.run_tool("link_assay_to_key_event", assay_id=assay_id, event_name=text)
    except Exception as exc:  # noqa: BLE001 — a tool failure is a guidance skip
        logger.warning("guidance: key event link failed for %r: %s", text, exc)
        return False

    if isinstance(result, dict) and result.get("ok"):
        return True

    offered = result.get("candidates") or [] if isinstance(result, dict) else []
    candidates = [str(c.get("name")) for c in offered if isinstance(c, dict) and c.get("name")]
    known = ""
    if candidates:
        known = f" The key events in this crate are: {', '.join(candidates)}."
    _notify(
        human,
        f"'{text}' does not name exactly one key event already in the crate, so "
        f"your answer was not stored.{known}",
    )
    return False


def _instances_for_commit(engine: AgentEngine, entity_type: str | None) -> list[Any]:
    """In-state instances of ``entity_type`` — the commit-target selection rule.

    Mirrors :func:`builder.tools.gap_analysis._instances_of`, so what the gap
    engine advertises as actionable and what :func:`_apply_value` can actually
    write are decided the same way.

    Counts ALL instances, not only named ones: a name is needed to *phrase* the
    question well (see :func:`_named_instances`), but the sole instance of a type
    is an unambiguous place to *write* whether or not it has one — and an unnamed
    sibling still makes the target ambiguous, so it must be counted.
    """
    if not entity_type:
        return []
    try:
        return list(engine.state.list_entities(entity_type))
    except (KeyError, ValueError) as exc:  # pragma: no cover — unknown type is rare
        logger.debug("guidance: cannot list %s instances: %s", entity_type, exc)
        return []


def _named_instances(engine: AgentEngine, entity_type: str | None) -> list[tuple[Any, str]]:
    """The NAMED in-state instances of ``entity_type`` as ``(entity, name)`` pairs.

    Used to decide what a question is *about* (:func:`_ground_entityless_gap`
    threads the real instance name into the phrase leaf). The commit target is
    chosen by :func:`_instances_for_commit` over the same type, so the prompt and
    the write can never point at different entities — that mismatch was the #375
    defect.
    """
    return [
        (entity, name)
        for entity in _instances_for_commit(engine, entity_type)
        if (name := _entity_display_name(entity))
    ]


def _notify(human: HumanInterface | None, message: str) -> None:
    """Tell the user something without asking them anything.

    ``HumanInterface`` has only blocking members (``present`` / ``request_input``),
    so this uses the same **optional-attribute** pattern as ``is_interactive`` and
    ``is_done``: a frontend that offers ``notify`` shows the message immediately;
    one that does not simply gets the log line. Never blocks and never consumes a
    scripted answer.
    """
    notify = getattr(human, "notify", None)
    if callable(notify):
        try:
            notify(message)
            return
        except Exception:  # noqa: BLE001 — a frontend hiccup must not break the loop
            pass
    logger.info("guidance: %s", message)


def _apply_value(
    engine: AgentEngine,
    gap: Gap,
    value: str,
    human: HumanInterface | None = None,
) -> bool:
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

    The root ``citation`` field (Commit 1, #179) is likewise routed to
    :func:`_apply_citation_value`: its value must be a ``ScholarlyArticle``
    reference resolved through the publication composites, not a literal string,
    so a string commit dropped the answer and the gap was re-asked every round.

    The Assay's ``keyEvent`` field (#382) is routed to
    :func:`_apply_key_event_value`, which resolves the answer against the
    ``KeyEvent`` entities in the crate: ``_wire_mention`` drops free text, so a
    literal commit would silently lose the one edge that says what the assay
    actually measures.
    """
    field = _local_name(gap.property) or (gap.property or "")
    if not field:
        return False

    # (#275) Person/agent fields need a Person reference, not a literal string.
    if _is_person_field(field):
        return _apply_person_value(engine, gap, value)

    # (#179) The root citation gap needs a resolved ScholarlyArticle, not a string.
    if _is_citation_field(field) and _gap_is_root(engine, gap):
        return _apply_citation_value(engine, gap, value)

    # (#382) The Assay's key event needs a reference to a KeyEvent already in the
    # crate. A value that ALREADY resolves (an AOP-Wiki IRI, or an in-state id the
    # user pasted) is a reference, not prose, so it falls through to the generic
    # reference path below — sending it to the name matcher would fail to match an
    # IRI against the event NAMES and reject a perfectly good answer.
    if _is_key_event_field(field) and not is_resolvable_reference(engine.state, value):
        return _apply_key_event_value(engine, gap, value, human=human)

    # Assay measurementMethod is a DefinedTerm reference, but the user naturally
    # answers it with a method name (for example, "Gamma counter"). Resolve that
    # prose through BAO/OLS, persist the verified term, and link the assay to it.
    # Otherwise the generic reference guard rejects a valid human answer because
    # no DefinedTerm existed yet.
    # A value that ALREADY names a crate entity (or is an IRI) is a reference, not
    # prose, so it falls through to the generic reference path below — sending it
    # to the BAO lookup would fail to resolve "term1" and reject a valid answer.
    if field == "measurementMethod" and not is_resolvable_reference(engine.state, value):
        return _apply_measurement_method(engine, gap, value, human=human)

    # (#375, D5) An identifier is never taken from prose, on ANY path. This is the
    # single chokepoint every commit funnels through, and it has two feeders that
    # are otherwise unguarded: the no-provider ask-user path and the draft-confirm
    # dialog's ``edits["value"]``. Guarding here closes both at once.
    if is_identifier_field(field):
        _notify(
            human,
            f"'{field}' is an identifier, so it is looked up rather than typed in — "
            "your answer was not stored.",
        )
        return False

    # (#375) A reference-only property must name an entity. The build strips every
    # `_REF_FIELDS` key out of an entity's scalar properties and `_wire_reference`
    # emits nothing for a non-resolvable literal, so storing prose here and
    # returning True is a lie: the value never reaches the crate.
    if is_reference_field(field) and not is_resolvable_reference(engine.state, value):
        _notify(
            human,
            f"'{field}' has to name something already in the crate, so your answer "
            "was not stored.",
        )
        return False

    state_id = _resolve_entity_id(engine, gap)
    if state_id is not None:
        engine.run_tool("set_fields", entity_id=state_id, fields={field: value})
        return True

    # (#375) A TYPED gap (MIT gaps carry `entity_id=None` + an `entity_type`) is
    # about a specific instance — the question was phrased about it by
    # `_ground_entityless_gap`. Resolve the same instance with the same rule so the
    # answer lands where the question promised, instead of falling through to the
    # Root Data Entity setter below and clobbering a Base MUST.
    if gap.entity_type not in (None, "Investigation"):
        instances = _instances_for_commit(engine, gap.entity_type)
        if len(instances) == 1:
            engine.run_tool(
                "set_fields", entity_id=instances[0].entity_id, fields={field: value}
            )
            return True
        # Zero or several instances: there is no unambiguous target, so commit
        # nothing rather than guess (D5).
        logger.debug(
            "guidance: %d %s instance(s); no unambiguous commit target",
            len(instances),
            gap.entity_type,
        )
        if len(instances) > 1:
            _notify(
                human,
                f"there are several {gap.entity_type} entries, so it is not clear which "
                "one you meant — your answer was not stored.",
            )
        return False

    # Crate-level gap: route to the Root Data Entity metadata setter when the field
    # maps to a known root slot. Only a genuinely root-level gap reaches here — the
    # root `./` node folds the Investigation, so `entity_type` is None or
    # "Investigation"; otherwise we cannot commit it deterministically (it was
    # still surfaced to the user as "asked").
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


def _drafted_value(
    engine: AgentEngine,
    gap: Gap,
    *,
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
) -> str | None:
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
        fields = draft_entity_fields(
            entity_type,
            _draft_context(engine, gap),
            usage_sink=usage_sink,
            overrides=overrides,
        )
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
    lines.extend(
        [
            "Enter the suggested value, or type a modified value.",
            "Type 'skip' to leave this field unresolved, or 'build' to stop guidance "
            "and build the current crate.",
        ]
    )
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
    text = str(value).strip()
    if text.casefold() in {"skip", "skip this", "defer"}:
        return None
    return text


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


def _ground_entityless_gap(engine: AgentEngine, gap: Gap, context: dict[str, Any]) -> None:
    """Ground a typed-but-entity-less gap in the real in-state instance (Commit 2).

    MIT gaps are emitted crate-level with ``entity_id=None`` carrying only an
    ``entity_type`` (e.g. ``"CellLineSample"``). Without grounding, the phrase leaf
    sees a bare TYPE and no name, so the model invents the stock example ("HepG2").
    Here, for such a gap, we look up the type's instances in state and thread the
    REAL name(s) into ``context`` so the leaf NEVER receives a bare type with no
    name (D5: no fabrication):

    * exactly one instance -> set ``entity_name`` (and ``known_fields``) from it;
    * several instances -> surface their display names in a disambiguating
      ``known_fields["instances"]`` entry (the leaf must phrase generically about
      *the named instances*, never invent one).

    Mutates ``context`` in place; a no-op when there is no type or no named
    instance (the prompt-side D5 guard then forbids inventing a name).
    """
    named = _named_instances(engine, gap.entity_type)
    if not named:
        return
    if len(named) == 1:
        entity, name = named[0]
        context["entity_name"] = name
        known = _known_fields(entity)
        if known:
            context["known_fields"] = known
        return
    # Several instances: surface their names for disambiguation rather than
    # leaving the leaf with a bare nameless type.
    names = [name for _entity, name in named]
    known = dict(context.get("known_fields") or {})
    known["instances"] = "; ".join(names)
    context["known_fields"] = known


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

    When the gap is **entity-less but typed** (an MIT gap with ``entity_id=None``
    carrying only ``entity_type``, Commit 2 / #179), the type's in-state instances
    are looked up and the REAL instance name(s) threaded in
    (:func:`_ground_entityless_gap`) so the leaf is never handed a bare type with
    no name — which is what made the model invent the stock "HepG2" example.
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
    else:
        # (Commit 2, #179) An entity-less but typed gap (MIT, entity_id=None):
        # ground it in the type's real in-state instance(s).
        _ground_entityless_gap(engine, gap, context)
    title = (engine.state.metadata.title or "").strip()
    if title:
        context["crate_title"] = title
    description = (engine.state.metadata.description or "").strip()
    if description:
        context["crate_description"] = description
    return context


def _phrase_question(
    engine: AgentEngine,
    gap: Gap,
    *,
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
) -> str:
    """Phrase ``gap`` as one human question via the LLM leaf, with a safe fallback.

    Calls :func:`phrase_gap_question` (the drafter-tier leaf); on any failure or an
    empty result it falls back to the deterministic :func:`_ask_user_prompt`, so a
    flaky leaf never produces a blank question and never breaks the loop.
    """
    if phrase_gap_question is not None:
        try:
            question = phrase_gap_question(
                _gap_context(engine, gap), usage_sink=usage_sink, overrides=overrides
            )
        except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
            logger.warning("guidance: phrase leaf failed: %s", exc)
            question = ""
        if question and question.strip():
            return question.strip()
    return _ask_user_prompt(gap, engine)


def _deterministic_decision(gap: Gap, reply: str) -> dict[str, Any]:
    """The deterministic interpret fallback: a non-empty reply is a commit (#244).

    Used when the interpret leaf is unavailable or fails, **and** on the
    no-provider path: treat a non-empty reply as a commit and an empty one as a
    skip — *except* for an identifier-bearing field, where the user's prose must
    NOT become an identifier value (D5), so it is skipped (identifiers come from
    lookups). This preserves today's ask-and-set behavior without ever storing a
    musing as an identifier.

    Its docstring used to claim to be "the documented no-provider behavior" while
    both of its call sites sat *downstream* of ``_resolve_ask_user``'s no-provider
    early return, so offline runs never reached it and the D5 skip was dead code
    (#375). ``_resolve_ask_user`` now routes the offline reply through here.
    """
    if not reply or not reply.strip():
        return {"action": "skip"}
    field = _local_name(gap.property) or (gap.property or "")
    if _is_identifier_field(field):
        return {"action": "skip"}
    return {"action": "commit", "value": reply.strip()}


def _interpret_reply(
    engine: AgentEngine,
    gap: Gap,
    question: str,
    reply: str,
    *,
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
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
        return interpret_gap_reply(
            question,
            reply,
            _gap_context(engine, gap),
            usage_sink=usage_sink,
            overrides=overrides,
        )
    except Exception as exc:  # noqa: BLE001 — a flaky leaf must not break the loop
        logger.warning("guidance: interpret leaf failed (%s); deterministic fallback", exc)
        return _deterministic_decision(gap, reply)


def _resolve_ask_user(
    engine: AgentEngine,
    human: HumanInterface,
    gap: Gap,
    *,
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
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
    # The reply goes through `_deterministic_decision` rather than straight back:
    # that is where the D5 identifier skip lives, and routing around it (#375) made
    # the documented guard unreachable on this path — prose became an identifier.
    if get_provider() is None:
        reply = _ask_user(engine, human, gap)
        if reply is None:
            return None
        value = _deterministic_decision(gap, reply).get("value")
        return value.strip() if isinstance(value, str) and value.strip() else None

    question = _phrase_question(engine, gap, usage_sink=usage_sink, overrides=overrides)
    response = human.request_input(question)
    reply = _reply_text(response)
    if reply is None:
        # An explicit skip / empty reply is a skip — never interpreted.
        return None

    follow_ups = 0
    while True:
        decision = _interpret_reply(
            engine, gap, question, reply, usage_sink=usage_sink, overrides=overrides
        )
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
            return _resolve_from_file(
                engine,
                gap,
                reply,
                decision.get("filename"),
                usage_sink=usage_sink,
                overrides=overrides,
            )

        # skip, an exhausted clarify budget, or any unrecognised action -> skip.
        return None


def _candidate_file_paths(engine: AgentEngine, filename: str | None, reply: str) -> list[str]:
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
            logger.warning("guidance: unexpected from_file read error for %s: %s", candidate, exc)
            continue
        if body and body.strip():
            return body[:_MAX_FILE_EXTRACT_CHARS]
    return None


def _resolve_from_file(
    engine: AgentEngine,
    gap: Gap,
    reply: str,
    filename: str | None,
    *,
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
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
        logger.info("guidance: from_file file unreadable / outside scan roots; skipping (#257).")
        return None

    try:
        value = extract_field_from_file(
            field,
            file_text,
            _gap_context(engine, gap),
            usage_sink=usage_sink,
            overrides=overrides,
        )
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
    usage_sink: UsageSink | None,
    overrides: ModelOverrides | None = None,
) -> bool:
    """Resolve a single ``gap``; return ``True`` iff it committed a change.

    Dispatches on ``auto_fixable`` / ``fix_hint``:

    * **auto_fixable** -> the deterministic repair, no prompt.
    * **draft** -> draft a value, show it, require confirmation (D5); on reject,
      fall through to ask-user.
    * **ask-user** (or any non-auto fallback) -> prompt and apply the answer.

    Records the action in ``resolved`` (committed) or ``asked`` (surfaced to the
    user) for the run summary. ``usage_sink`` is threaded to every leaf this gap
    may reach so the tail's token spend is accounted (#384).
    """
    record = {
        "tier": gap.tier,
        "source": gap.source,
        "entity_id": gap.entity_id,
        # A typed gap (MIT) carries no entity_id, so entity_type is the only thing
        # identifying WHICH entity it was about — and after #375 it is also what
        # decides where the answer is written. Recorded so a caller can tell two
        # same-property gaps apart (e.g. Assay:description vs LabProtocol:description).
        "entity_type": gap.entity_type,
        "property": gap.property,
        "fix_hint": gap.fix_hint,
    }

    # --- auto-fixable: deterministic repair, no human prompt -------------------
    if gap.auto_fixable:
        result = engine.run_tool("fix_required_issues", profile="all", severity="required")
        fixed = bool(result.get("fixed"))
        if fixed:
            resolved.append({**record, "via": "fix_required_issues"})
        return fixed

    # --- draftable: draft -> confirm -> commit (D5) ---------------------------
    if gap.fix_hint == "draft":
        candidate = _drafted_value(engine, gap, usage_sink=usage_sink)
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
                if _apply_value(engine, gap, value, human):
                    resolved.append({**record, "via": "draft-confirmed"})
                    return True
                return False
        # No usable draft, or the user rejected it -> fall through to ask-user.

    # --- ask-user: phrase -> interpret -> apply (LLM-mediated, #244) ----------
    asked.append(record)
    value = _resolve_ask_user(engine, human, gap, usage_sink=usage_sink)
    if value is None:
        return False
    if _apply_value(engine, gap, value, human):
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
    usage_sink: UsageSink | None = None,
    overrides: ModelOverrides | None = None,
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
        usage_sink: Where each leaf call reports its token usage (#384). Defaults
            to the shared :func:`builder.agents.llm.make_usage_logger`, so the tail is
            accounted whether or not a caller asks for it: without one, every
            phrase / interpret / draft / from-file call is missing from
            ``profile.ndjson`` and the status bar re-printed before EVERY
            question shows a frozen token count while real money is spent. A
            caller that injects its own sink takes over the accounting entirely
            — including the profile write and its own totals — so the returned
            ``usage`` then reports zero.

    Returns:
        A summary dict::

            {
                "resolved": [ {tier, source, entity_id, property, fix_hint, via}, ... ],
                "asked":    [ {tier, source, entity_id, property, fix_hint}, ... ],
                "remaining_gaps": {must_open, should_open, may_open},
                "conformance":    {base, isa, tox},
                "rounds":         <int>,
                "usage":          {input_tokens, output_tokens, total_tokens},
            }
    """
    # Token accounting (#384), mirroring ``run_pipeline``'s contract: the default
    # sink both accumulates ``totals`` (the in-memory figure the summary reports)
    # and logs the ``node_end``/``node="model"`` profile event that the status bar,
    # the ``--dashboard`` table and the eval all read.
    totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    sink = usage_sink or make_usage_logger(engine, totals)
    resolved: list[dict[str, Any]] = []
    asked: list[dict[str, Any]] = []
    rounds = 0
    report: GapReport = assess_gaps(engine.state)
    # Indices into the CURRENT report's gaps that were tried this round and could
    # not be progressed (e.g. the user skipped them). They are skipped so the loop
    # advances to the next actionable gap instead of re-offering the same one; the
    # set is cleared whenever a commit invalidates the report (a fresh re-assess).
    skipped: set[int] = set()
    # (Commit 1, #179) The per-RUN skip-set, keyed by gap IDENTITY (not report
    # index). A gap the loop surfaced/answered but could NOT progress is recorded
    # here and never re-drawn, even after a different gap commits and resets the
    # per-report ``skipped`` index set above. This stops the always-highest-
    # priority root citation MUST gap (which re-emits every round when its answer
    # cannot be applied) from being re-asked 6+ times.
    tried_identities: set[GapIdentity] = set()

    for _ in range(max(0, max_rounds)):
        # A real interactive user may explicitly end guidance even while MUST
        # gaps remain; export the current crate rather than forcing more prompts.
        if _user_signals_done(human):
            break
        index = _next_actionable_index(report, skipped, tried_identities)

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
        identity = _gap_identity(gap)
        resolved_before = len(resolved)
        progressed = _resolve_gap(
            engine, human, gap, resolved=resolved, asked=asked, usage_sink=sink
        )

        if progressed:
            # State changed: re-assess from scratch and forget the per-report
            # index skip-set (the gap indices no longer refer to the same gaps).
            # The per-RUN identity skip-set persists so an already-tried gap that
            # re-emits is not re-drawn.
            report = assess_gaps(engine.state)
            skipped = set()
            # (#375) "The setter returned True" is not "the gap cleared". If the
            # same identity is still in the fresh report, the commit did not close
            # it — so do NOT count it resolved (`format_guidance_summary` prints
            # that list verbatim) and never draw it again, or it is re-asked every
            # round until `max_rounds` runs out and starves every gap behind it.
            if any(_gap_identity(g) == identity for g in report.gaps):
                del resolved[resolved_before:]
                tried_identities.add(identity)
        else:
            # This one gap is not resolvable right now (e.g. skipped or its answer
            # could not be applied). Skip it BOTH by report index (so the next
            # round draws the next actionable gap in the SAME report) AND by
            # identity (so it is not re-drawn even after a later commit clears the
            # index set and a fresh re-assess re-emits it — the #179 re-ask fix).
            # The loop only stops once EVERY gap in the report is exhausted, so a
            # single un-progressable gap never abandons the ones behind it. Still
            # bounded by ``max_rounds``.
            skipped.add(index)
            tried_identities.add(_gap_identity(gap))

    return {
        "resolved": resolved,
        "asked": asked,
        "remaining_gaps": dict(report.counts),
        "conformance": dict(report.conformance),
        "rounds": rounds,
        "usage": {
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        },
    }


def _next_actionable_index(
    report: GapReport,
    skipped: set[int],
    tried_identities: set[GapIdentity] | None = None,
) -> int | None:
    """Index of the highest-priority *actionable, not-yet-skipped* gap, or ``None``.

    The report is already sorted MUST -> SHOULD -> MAY (committable before
    ``report-only`` within a tier), so we walk it in order and return the index of
    the first gap that is actionable and not yet excluded. A gap is excluded when
    it is either:

    * in ``skipped`` — indices into THIS report's ``gaps`` the loop has tried and
      could not progress this round (reset on every commit); or
    * in ``tried_identities`` (Commit 1, #179) — the per-RUN set of gap IDENTITIES
      (``_gap_identity``) the loop surfaced/answered but could NOT progress, kept
      across re-assessments so an un-appliable / already-answered gap (e.g. the
      always-highest-priority root citation MUST gap) is not re-drawn even after a
      different gap commits and clears the per-report index ``skipped`` set.

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
        if tried_identities is not None and _gap_identity(gap) in tried_identities:
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
