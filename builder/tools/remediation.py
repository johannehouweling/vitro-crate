"""Group findings into the far smaller set of ACTIONS that would clear them.

A validator reports one finding per unsatisfied shape, which is the right unit
for a validator and the wrong unit for a person. One author with no ORCID opens
four of them — no ORCID as ``@id``, no identifier, no affiliation, no
affiliation entity — and a reader working down the list meets the same person
four times without being told once what to actually do.

    Recommended: ./#CitationAuthor_Zhongli_Chen A Person entity SHOULD have an ORCID …
    Recommended: ./#CitationAuthor_Zhongli_Chen The author SHOULD have an organizational affiliation
    Recommended: ./#CitationAuthor_Zhongli_Chen The author SHOULD have a Contextual Entity …
    Recommended: ./#CitationAuthor_Zhongli_Chen Person entity SHOULD have a non-empty identifier

    -> "Supply an ORCID for Zhongli Chen." (clears 4)

Findings cluster along two axes and the useful grouping is whichever covers
more: ONE ENTITY missing several things (the author above), or ONE PROPERTY
missing across several entities ("three organizations have no contact point").
Groups are chosen greedily, largest first, so each finding is counted once and
the summary is ordered by how much each action is worth.

Nothing here phrases anything: the output is structured, and how it is worded —
by a template or by a model — is the caller's business. That keeps the grouping
deterministic and testable, and keeps a model away from deciding WHICH findings
belong together.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Findings a crate cannot act on, and why. Recommending these would send a
# reader after something that is either impossible or actively harmful — the
# root identifier one breaks an ISA MUST if "fixed" (see `_crate_mapping`), and
# an optional web-presence property cannot be satisfied by a local file at all.
_NOT_ACTIONABLE: tuple[tuple[str, str], ...] = (
    (
        "PropertyValue entities for identifiers",
        "Left as-is on purpose: the ISA profile REQUIRES the root identifier to be a "
        "plain string, so satisfying this recommendation would break a stricter rule.",
    ),
    (
        "mainEntityOfPage",
        "Applies to files published behind a landing page elsewhere. These files live "
        "in the crate, so there is no such page to point at.",
    ),
    (
        "subjectOf",
        "Applies to files published behind a landing page elsewhere. These files live "
        "in the crate, so there is no such page to point at.",
    ),
)


# Keyed on the vocabulary the VALIDATOR emits, upper-cased. This used to read
# {"MUST", "SHOULD", "MAY"} — the words the SHACL messages are written in — while
# `group_findings` derives the tier from `finding["severity"]`, which
# `builder/tools/validation.py` sets to "required" / "recommended" / "optional".
# Nothing matched, so `.get(tier, 3)` returned 3 for EVERY action, every sort key
# tied, and the tier term silently dropped out: ordering collapsed to "whichever
# clears the most". A required-conformance action clearing one finding then ranked
# below any bulk advisory action and could fall past `_NEXT_STEPS_CAP` entirely —
# the report hiding the work that actually blocks the build, which is the one
# thing this section exists to surface.
# MATURITY sits between the two validator tiers on purpose: a required conformance
# failure means the crate is not a valid RO-Crate at all, which outranks a rung on a
# maturity ladder — but the rung outranks a recommendation, because it is the thing
# standing between this deposit and its next level.
_TIER_RANK = {"REQUIRED": 0, "MATURITY": 1, "RECOMMENDED": 2, "OPTIONAL": 3}

# What each tier is called in the report. The validator's own word, not the
# SHACL verb: a reader who sees "Required" beside an action can match it to the
# "Required" count in the conformance table above it.
TIER_LABEL = {
    "REQUIRED": "Required",
    "MATURITY": "Maturity",
    "RECOMMENDED": "Recommended",
    "OPTIONAL": "Optional",
}

_DEFAULT_TIER = "RECOMMENDED"


# How much a fix is worth to somebody REUSING the dataset, which is not the same
# question as how loudly a profile asks for it. Both are needed: the tier says
# what blocks conformance, this says what the crate is worth once it passes.
#
# The calibration is the user's, from reading a real report:
#
#   "Say which measurement technique was used …"  -> "this sounds way more impactful"
#   "Add a job title for Max Tio, W. Edward Visser …"
#       -> "I don't think the data will be less worth or more worth if we have a
#           job title of those gentleman, sure we can do but it is not that
#           important from a dataset perspective"
#
# Three bands, and the test for each is a question about the READER of the crate:
#
#   0 CAN THEY UNDERSTAND THE EXPERIMENT?   what was measured, how, on what, under
#     which conditions. Absent, the numbers cannot be interpreted at all.
#   1 CAN THEY FIND, TRUST AND CITE IT?     identifiers, dates, licence, who did it,
#     where to write. Absent, the data is usable but the record is weak.
#   2 COURTESY DETAIL.                      true and worth having; nobody's reuse
#     turns on it.
#
# This is a judgement, so it is written down in one place and ordered
# most-specific-first like `_WANTED` — not spread through the sort function where
# it could not be argued with. An unlisted finding lands in band 1: the middle is
# the honest default for something nobody has classified.
_IMPACT_BANDS: tuple[tuple[tuple[str, ...], int], ...] = (
    # --- 0: without this the measurement cannot be interpreted ---------------
    (("measurement technique", "measurement method"), 0),
    (("Key Event", "AOP"), 0),
    (("parameter value", "additional property"), 0),
    (("protocol",), 0),
    (("description",), 0),
    (("measured entity", "endpoint"), 0),
    # --- 2: nobody's reuse turns on it (checked BEFORE the generic identifier
    #        band below, which "job title" would otherwise fall into) ---------
    (("job title",), 2),
    # --- 1: everything else — findable, trustable, citable -------------------
    (("ORCID", "identifier"), 1),
    (("affiliation", "contactPoint", "contact point", "email"), 1),
    (("licence", "license", "dateCreated", "datePublished", "dateModified"), 1),
    (("creator", "publisher", "author"), 1),
)

_DEFAULT_IMPACT = 1


def _impact(messages: list[str]) -> int:
    """Which band the strongest finding in *messages* falls into.

    First match wins, `_WANTED`-style, so the ordering of `_IMPACT_BANDS` is the
    specification. An entity missing several things is ranked by the most
    valuable of them — the action clears all of them at once, so ranking it by
    the least valuable would bury a job worth doing.
    """
    blob = " ".join(messages)
    for needles, band in _IMPACT_BANDS:
        if any(n in blob for n in needles):
            return band
    return _DEFAULT_IMPACT


@dataclass
class Action:
    """One thing a person could do, and what it would clear.

    Attributes:
        key: Stable identifier for the group (``"entity:<id>"`` / ``"property:<p>"``).
        kind: ``"entity"`` (one thing needs several fixes) or ``"property"``
            (one fix applies to several things).
        subject: What the action is about — an entity label, or a property name.
        entity_ids: Every entity the action touches.
        findings: The finding messages this action would clear.
        tier: The strongest tier among those findings — REQUIRED > MATURITY >
            RECOMMENDED > OPTIONAL. Three are the validator's own vocabulary, so
            they line up with the conformance table; MATURITY is this module's,
            carried by the DSM blockers :func:`group_dsm_blockers` mints, and it
            outranks a recommendation while yielding to a conformance failure.
        actionable: False for a finding that is deliberately left open.
        note: Why it is not actionable, when it is not.
    """

    key: str
    kind: str
    subject: str
    entity_ids: list[str] = field(default_factory=list)
    # The subject's entity labels, unjoined. `subject` is the reader-facing
    # string; this is what it was built FROM, so a renderer can rebuild the same
    # phrase with each name marked up instead of parsing commas back out of it.
    subject_names: list[str] = field(default_factory=list)
    # Parallel to `subject_names`: the reader-facing type word for each ("Person",
    # "CellLine", …), or "" where the type says nothing worth saying. Kept apart
    # from the name so the renderer can chip the NAME and leave the type as
    # ordinary prose — the chip marks what is a thing in the crate.
    subject_types: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    tier: str = _DEFAULT_TIER
    # How much the fix is worth to a reuser (0 best). Ordered WITHIN a tier, never
    # across one: a required conformance failure still outranks the most valuable
    # recommendation, because one blocks the build and the other improves it.
    impact: int = _DEFAULT_IMPACT
    actionable: bool = True
    note: str | None = None
    # Where the representative finding came from (the record's ``profile`` key —
    # "base" / "isa" / "tox" / whatever the sweep stamps; "graph" for orphan
    # actions) and that finding's RAW validator message. The report's
    # recommendation rows show the validator's own words next to the
    # instruction, so both must survive the grouping (#607 design handoff).
    source: str = ""
    message: str = ""
    # For an action whose wording is not derived from validator findings — a DSM
    # indicator carries its own instruction and consequence in the published model's
    # own file, because the fix for "the dataset has no persistent identifier" cannot
    # be templated out of a shape message. `describe`/`why` return these verbatim, so
    # a maturity blocker and a conformance finding reach the page the same way.
    instruction: str = ""
    consequence: str = ""

    @property
    def cleared(self) -> int:
        """How many findings this action would close."""
        return len(self.findings)


def _strongest(tiers: list[str]) -> str:
    """The most severe tier in *tiers*, ranked by ``_TIER_RANK``.

    An unrecognised tier sorts last rather than raising: a new validator severity
    should make an action rank low, not crash the report. It is the DEFAULT that
    carries the risk, so it stays "RECOMMENDED" — defaulting an unknown to
    REQUIRED would push unclassified work above real conformance failures.
    """
    return sorted(tiers, key=lambda t: _TIER_RANK.get(t, 4))[0] if tiers else _DEFAULT_TIER


def _not_actionable_note(message: str) -> str | None:
    for needle, note in _NOT_ACTIONABLE:
        if needle in message:
            return note
    return None


# How each authority's IRI is said out loud when the entity it names carries no
# name of its own. The point is to say WHAT the thing is: a reader shown
# "0000-0002-7685-9462" cannot tell a person from a grant from a checksum.
_IRI_PHRASING: tuple[tuple[str, str], ...] = (
    ("https://orcid.org/", "the person with ORCID {}"),
    ("https://ror.org/", "the organization with ROR {}"),
    ("https://doi.org/", "the publication with DOI {}"),
    ("https://www.cellosaurus.org/", "the cell line {}"),
    ("https://pubchem.ncbi.nlm.nih.gov/compound/", "the compound with PubChem CID {}"),
)

# Type prefixes on a minted fragment id. `_make_entity_id` builds
# `#<Type>_<internal_id>`, and the internal id USUALLY repeats the type as its
# own first token — `#Sample_sample_…`, `#Study_study_…`, `#LabProtocol_protocol_…`
# — so stripping only the CamelCase prefix leaves "Sample sample proc … output
# sample". Both layers come off.
_ID_TYPE_PREFIXES: tuple[str, ...] = (
    "CitationAuthor_", "DefinedTerm_", "LabProcess_", "LabProtocol_", "PropertyValue_",
    "MolecularEntity_", "CellLineSample_", "Organization_", "Publication_", "Person_",
    "Sample_", "Study_", "Assay_", "Investigation_", "File_", "Dataset_",
)
_ID_ECHO_TOKENS: tuple[str, ...] = (
    "sample_", "study_", "assay_", "proc_", "protocol_", "pv_", "param_", "org_",
    "chem_", "cell_", "dt_", "inv_",
)


def _id_variants(entity_id: str) -> tuple[str, ...]:
    """The spellings of *entity_id* a lookup should try, most literal first.

    The validator reports a fragment entity as ``./#Thing`` — resolved against
    the crate base — while the graph writes the same node's ``@id`` as
    ``#Thing``. Not one node in a real crate carries the ``./#`` form, so an
    exact-match lookup missed EVERY fragment entity and every one of them fell
    through to the id-mangling fallback: "PropertyValue pv sample role" and
    "Sample sample proc … output sample" in a list that had their real names
    available the whole time.
    """
    seen: list[str] = []
    for candidate in (
        entity_id,
        entity_id.removeprefix("./"),
        entity_id if entity_id.startswith("./") else f"./{entity_id}",
    ):
        if candidate and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _lookup(mapping: dict[str, Any] | None, entity_id: str) -> Any:
    """Read *entity_id* out of a graph-keyed map, trying every id spelling.

    Shared by the name and the @type lookups so they cannot drift: both are
    keyed on the graph's `@id` and both are asked with the validator's
    base-resolved form, and fixing only one of them is exactly the bug that
    produced "for CellLine H4" in one column and a bare "for H4" in the next.
    """
    if not mapping:
        return None
    for key in _id_variants(entity_id):
        if key in mapping:
            return mapping[key]
    return None


def _entity_label(entity_id: str, labels: dict[str, str] | None) -> str:
    """A name a reader recognises, or an honest description of what the thing is.

    Order matters: the crate's own name for the entity always wins, because that
    is what the reader will search the report for. Only when the entity has no
    name — which for several of these IS the finding being reported — does this
    fall back to describing it, and it never invents one.
    """
    if name := _lookup(labels, entity_id):
        return str(name)

    if entity_id in ("./", ".", ""):
        return "the crate itself"

    for prefix, phrasing in _IRI_PHRASING:
        if entity_id.startswith(prefix):
            return phrasing.format(entity_id[len(prefix) :].strip("/"))

    tail = entity_id.rsplit("/", 1)[-1].lstrip("#").removeprefix("./")
    for prefix in _ID_TYPE_PREFIXES:
        if tail.startswith(prefix):
            tail = tail[len(prefix) :]
            # …and the type token the internal id repeats right after it.
            for echo in _ID_ECHO_TOKENS:
                if tail.startswith(echo):
                    tail = tail[len(echo) :]
                    break
            break
    return tail.replace("_", " ").strip() or entity_id


# The @type words worth saying out loud, mapped to how a person says them. A
# reader scanning "Add an identifier for H4" cannot tell a cell line from a
# person from a file; "for CellLine H4" costs three characters and removes the
# question. Types NOT listed are omitted rather than shown raw — "PropertyValue
# sample role" reads worse than "sample role", and a bare schema.org class name
# is jargon the report otherwise avoids.
_TYPE_WORDS: dict[str, str] = {
    "Person": "Person",
    "Organization": "Organization",
    "CellLineSample": "CellLine",
    "MolecularEntity": "Compound",
    "Sample": "Sample",
    "File": "File",
    "Dataset": "Dataset",
    "LabProtocol": "Protocol",
    "LabProcess": "Process",
    "ScholarlyArticle": "Publication",
    "Publication": "Publication",
}


def _type_word(entity_type: Any) -> str:
    """The reader-facing word for an entity's ``@type``, or "" if not worth saying.

    A node's ``@type`` is often a LIST (``["csvw:Column", "schema:DefinedTerm"]``)
    and often namespaced, so this takes the first recognised word from it rather
    than assuming a bare string.
    """
    candidates = entity_type if isinstance(entity_type, list) else [entity_type]
    for candidate in candidates:
        bare = str(candidate or "").rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if bare in _TYPE_WORDS:
            return _TYPE_WORDS[bare]
    return ""


def _display(name: str, type_word: str) -> str:
    """"Person Nathalie Dierichs" — the type only when it adds something.

    Skipped when the name already opens with the word, so a file called
    "File manifest.csv" does not become "File File manifest.csv".
    """
    if not type_word or name.casefold().startswith(type_word.casefold()):
        return name
    return f"{type_word} {name}"


def _property_label(prop: str) -> str:
    return (prop or "").rsplit("/", 1)[-1].rsplit("#", 1)[-1] or "property"


def group_findings(
    findings: list[dict[str, Any]],
    *,
    labels: dict[str, str] | None = None,
    types: dict[str, Any] | None = None,
) -> list[Action]:
    """Collapse *findings* into the actions that would clear them, best first.

    Each finding is a mapping with ``entity_id``, ``message`` and optionally
    ``property`` / ``severity``. Every finding lands in exactly one action, so
    the ``cleared`` counts sum to the number of findings and the summary can
    honestly claim to cover the whole list.

    Args:
        findings: The validator's findings, in any order.
        labels: Optional ``entity_id -> display name``, so an action reads
            "Zhongli Chen" rather than "#CitationAuthor_Zhongli_Chen".

    Returns:
        Actions sorted by tier, then by reuse impact (``Action.impact``), then by
        how many findings each clears, with the subject as a stable tiebreak.
        Deliberately-open findings are returned too, flagged ``actionable=False``
        so a caller can show them as answered rather than outstanding.
    """
    actionable: list[dict[str, Any]] = []
    deferred: dict[str, Action] = {}

    for finding in findings:
        message = str(finding.get("message") or "")
        note = _not_actionable_note(message)
        if note is None:
            actionable.append(finding)
            continue
        # Grouped by the reason, so "these three are deliberate" is one line.
        action = deferred.setdefault(
            note,
            Action(
                key=f"deferred:{len(deferred)}",
                kind="deferred",
                subject="Deliberately left open",
                actionable=False,
                note=note,
            ),
        )
        action.findings.append(message)
        if (eid := finding.get("entity_id")) and eid not in action.entity_ids:
            action.entity_ids.append(str(eid))

    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_property: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in actionable:
        if eid := finding.get("entity_id"):
            by_entity[str(eid)].append(finding)
        if prop := finding.get("property"):
            by_property[str(prop)].append(finding)

    # ENTITY FIRST, and not because those groups are bigger — they usually are
    # not. "Zhongli Chen needs an ORCID" is something a person can go and do;
    # "nine entities are missing an affiliation" is the same list re-sorted. An
    # entity carrying several findings almost always has ONE underlying cause,
    # and naming the entity is what surfaces it.
    #
    # A single-finding entity is left alone: on its own it says no more than the
    # finding did, and it would fragment a property group that reads far better
    # collectively ("five organizations have no website").
    remaining = {id(f): f for f in actionable}
    actions: list[Action] = []

    def _take(kind: str, subject_id: str, live: list[dict[str, Any]]) -> None:
        label = (
            _entity_label(subject_id, labels)
            if kind == "entity"
            else _property_label(subject_id)
        )
        type_word = _type_word(_lookup(types, subject_id)) if kind == "entity" else ""
        actions.append(
            Action(
                key=f"{kind}:{subject_id}",
                kind=kind,
                subject=_display(label, type_word),
                subject_names=[label],
                subject_types=[type_word],
                entity_ids=sorted({str(f.get("entity_id")) for f in live if f.get("entity_id")}),
                findings=[str(f.get("message") or "") for f in live],
                tier=(tier := _strongest(
                    [str(f.get("severity") or _DEFAULT_TIER).upper() for f in live]
                )),
                impact=_impact([str(f.get("message") or "") for f in live]),
                # The representative finding: the first of the action's own
                # (strongest) tier, so the quoted message never understates the
                # severity the badge next to it claims.
                source=str((rep := next(
                    (f for f in live
                     if str(f.get("severity") or _DEFAULT_TIER).upper() == tier),
                    live[0],
                )).get("profile") or ""),
                message=str(rep.get("message") or ""),
            )
        )
        for f in live:
            remaining.pop(id(f), None)

    for eid, _ in sorted(by_entity.items(), key=lambda kv: -len(kv[1])):
        live = [f for f in by_entity[eid] if id(f) in remaining]
        if len(live) > 1:
            _take("entity", eid, live)

    # Then whatever is left, gathered by the property they share.
    while remaining:
        candidates = [
            (len([f for f in group if id(f) in remaining]), prop)
            for prop, group in by_property.items()
        ]
        candidates = [c for c in candidates if c[0] > 1]
        if not candidates:
            break
        _, prop = max(candidates)
        _take("property", prop, [f for f in by_property[prop] if id(f) in remaining])

    # Anything still uncovered stands alone rather than being dropped.
    for finding in list(remaining.values()):
        _take("entity", str(finding.get("entity_id") or "the crate"), [finding])

    actions = _merge_identical(actions)

    actions.sort(key=lambda a: (_TIER_RANK.get(a.tier, 4), a.impact, -a.cleared, a.subject))
    return actions + list(deferred.values())


def describe(action: Action) -> str:
    """One plain sentence for *action*, without a model.

    The floor the summary always has. A model wording these reads better, but
    the report must say something useful with no provider configured, and a
    deterministic sentence that names the subject and the count is already far
    more use than the findings it replaces.
    """
    if not action.actionable:
        return action.note or "Left as-is deliberately."
    if action.instruction:
        return action.instruction
    what = _wanted(action.findings)
    subject = action.subject
    n = action.cleared
    plural = "entity is" if n == 1 else "entities are"
    if action.kind == "orphan":
        return f"Connect {subject} to the crate — {n} {plural} unreachable."
    if action.kind == "property":
        return f"Supply {subject} for the {n} {'entity' if n == 1 else 'entities'} missing it."
    return f"{what} for {subject}." if what else f"Complete the metadata for {subject}."


def describe_parts(action: Action, subject: str | None = None) -> tuple[str, str]:
    """*(instruction, subject)* — the same sentence, split where it changes topic.

    ``describe`` returns one string because callers that emit plain text want one
    string. But the HTML list runs the instruction and a long entity list
    together, and the entity list is by far the longer half — "Add an identifier
    for proc thyroid hormone receptor activation endpoint readout raw
    measurements.csv, proc … .csv, proc … .csv and 15 others." The reader is
    scanning for the verb, and it is buried.

    The two are returned separately so the renderer can give the entity list its
    own visual weight. Joining them reproduces ``describe`` exactly, and a test
    pins that, so the plain-text and HTML wordings cannot drift.

    The subject half is empty when the sentence has no entity list to separate
    (a deliberate non-action, whose text is a note about the whole finding).

    Args:
        action: The action to word.
        subject: Optional pre-rendered replacement for ``action.subject`` — the
            HTML list passes the same entities with each one marked up. Nothing
            else about the sentence changes, so the two renderings stay one
            wording.
    """
    if not action.actionable:
        return describe(action), ""
    if action.instruction:
        # No entity list to break out — the subject is the whole deposit.
        return action.instruction, ""
    what = _wanted(action.findings)
    # An already-rendered subject (the HTML list marks each entity up) is
    # substituted here rather than assembled by the caller, so this function
    # stays the ONE place the sentence is composed.
    subject = action.subject if subject is None else subject
    n = action.cleared
    plural = "entity is" if n == 1 else "entities are"
    if action.kind == "orphan":
        return "Connect", f"{subject} to the crate — {n} {plural} unreachable."
    if action.kind == "property":
        return "Supply", (
            f"{subject} for the {n} {'entity' if n == 1 else 'entities'} missing it."
        )
    return (what or "Complete the metadata"), f"for {subject}."


# What a finding is asking for, keyed on words the message actually uses. Only
# phrasings the profiles emit are listed; anything else falls back to a generic
# sentence rather than a confidently wrong one.
_WANTED: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ORCID",), "Add an ORCID"),
    (("affiliation",), "Add an institutional affiliation"),
    (("contactPoint", "contact point"), "Add a contact email"),
    (("organization SHOULD have a URL", "URL"), "Add a website"),
    (("job title",), "Add a job title"),
    (("email",), "Add an email address"),
    (("measurement technique",), "Say which measurement technique was used"),
    (("measurement method",), "Say which measurement method was used"),
    (("Key Event", "AOP"), "Link the measured endpoint to its AOP-Wiki Key Event"),
    (("creator",), "Name who created it"),
    # NOT a bare "date" needle. `_wanted` substring-matches the whole message
    # blob, so "date" fired on datePublished / dateModified / "validate" alike and
    # answered every one of them with "Add the date it was created" — a specific,
    # confident, wrong instruction. The contract for `describe()` is the MOST
    # SPECIFIC instruction that fits; a needle this broad is the opposite.
    (("dateCreated",), "Add the date it was created"),
    (("datePublished",), "Add the date it was published"),
    (("dateModified",), "Add the date it was last modified"),
    (("description",), "Add a description"),
    (("termCode",), "Add the ontology code"),
    (("parameter value", "additional property"), "Record the parameters used"),
    (("protocol",), "Link the protocol it follows"),
    (("licence", "license"), "Add a reuse licence"),
    (("identifier",), "Add an identifier"),
)


# Why the instruction is worth following — one muted clause per `_WANTED`
# instruction, keyed by the instruction itself so the clause can only ever
# describe the shape whose instruction won (`_wanted` picks it; `why` looks the
# clause up by that pick). An instruction with nothing honest to say has no
# entry and renders no clause — a generic platitude on every row would teach
# the reader to skip the column.
_WHY: dict[str, str] = {
    "Add an ORCID": "Resolves the person unambiguously for credit and search.",
    "Add an institutional affiliation": "Ties the person to an institution a registry can resolve.",
    "Add a contact email": "Gives a reuser someone to ask.",
    "Add a website": "Lets a reader confirm which organisation is meant.",
    "Add an email address": "Gives a reuser someone to ask.",
    "Say which measurement technique was used": "The values cannot be interpreted without it.",
    "Say which measurement method was used": "The values cannot be interpreted without it.",
    "Link the measured endpoint to its AOP-Wiki Key Event":
        "Places the endpoint in its adverse-outcome pathway.",
    "Name who created it": "Says who is responsible for the data.",
    "Add the date it was created": "Anchors the record in time.",
    "Add the date it was published": "Anchors the record in time.",
    "Add the date it was last modified": "Anchors the record in time.",
    "Add a description": "Nobody can tell what it is for without one.",
    "Add the ontology code": "Makes the term machine-resolvable.",
    "Record the parameters used": "The exact settings are what a re-run needs.",
    "Link the protocol it follows": "Says which procedure the step actually followed.",
    "Add a reuse licence": "Nobody may legally reuse the data without one.",
    "Add an identifier": "Lets other records cite it precisely.",
}


def why(action: Action) -> str:
    """The one-clause consequence for *action*, or ``""`` when nothing honest
    fits. Looked up by the instruction `_wanted` picked for the same findings,
    so the clause and the instruction always describe the same shape."""
    if action.consequence:
        return action.consequence
    if action.kind == "orphan":
        them = "it" if action.cleared == 1 else "them"
        return f"A consumer walking the crate from its root never reaches {them}."
    return _WHY.get(_wanted(action.findings), "")


def _wanted(messages: list[str]) -> str:
    """The most specific instruction that fits *messages*, or "" if none does.

    First match wins because `_WANTED` is ordered most-specific first, and
    specificity beats frequency here: an author missing an ORCID also trips
    generic "identifier" wording, and counting hits let the vague instruction
    outvote the useful one — "add an identifier for Zhongli Chen" when the
    answer is an ORCID.
    """
    blob = " ".join(messages)
    for needles, instruction in _WANTED:
        if any(n in blob for n in needles):
            return instruction
    return ""


def _join(
    names: list[str],
    limit: int = 6,
    wrap: Callable[[str], str] | None = None,
) -> str:
    """ "Ada, Grace and 2 others" — a subject line that stays a line.

    *wrap* decorates each NAME without touching the connective words, so the
    HTML renderer can put every entity in a ``<code>`` chip and still get this
    function's wording, counting and pluralisation. Written as a parameter
    rather than a second formatter next to it: the plain sentence and the
    rendered one are the same sentence, and two implementations of "and N
    others" would eventually disagree about N.
    """
    mark = wrap or (lambda name: name)
    shown = [mark(n) for n in names[:limit]]
    if len(names) <= limit:
        return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
    rest = len(names) - limit
    return f"{', '.join(shown)} and {rest} other{'s' if rest > 1 else ''}"


def _merge_identical(actions: list[Action]) -> list[Action]:
    """Fold entity actions that need exactly the same thing into one.

    Four authors each missing an affiliation are four identical instructions.
    Naming them once — "Meima, Cenijn, Hamers and 1 other need an affiliation" —
    is the difference between a summary and the same list with nicer headings.

    Only entity actions merge, and only on an identical finding signature: two
    entities that are missing DIFFERENT things are two different jobs, however
    similar they look.
    """
    merged: list[Action] = []
    buckets: dict[tuple[str, ...], list[Action]] = defaultdict(list)
    for action in actions:
        if action.kind != "entity":
            merged.append(action)
            continue
        buckets[tuple(sorted(action.findings))].append(action)

    for signature, group in buckets.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        # The BARE names and their types, not each action's already-composed
        # `subject`: re-prefixing a composed one would yield "Person Person Ada".
        names = [a.subject_names[0] if a.subject_names else a.subject for a in group]
        type_words = [a.subject_types[0] if a.subject_types else "" for a in group]
        merged.append(
            Action(
                key="entities:" + "|".join(sorted(a.key for a in group)),
                kind="entities",
                subject=_join([_display(n, t) for n, t in zip(names, type_words)]),
                subject_names=list(names),
                subject_types=list(type_words),
                entity_ids=sorted({e for a in group for e in a.entity_ids}),
                # Every finding, so `cleared` still totals the whole list.
                findings=[m for a in group for m in a.findings],
                impact=min(a.impact for a in group),
                tier=_strongest([a.tier for a in group]),
                # The finding signatures are identical across the group, so any
                # member's representative message speaks for the merge — losing
                # it here silently un-quoted the validator on merged rows.
                source=group[0].source,
                message=group[0].message,
            )
        )
    merged.sort(key=lambda a: (_TIER_RANK.get(a.tier, 4), a.impact, -a.cleared, a.subject))
    return merged


def dsm_indicator_actions(
    blockers: list[tuple[str, str, str]], dsm_data: dict[str, Any] | None
) -> list[Action]:
    """The DSM indicators blocking the next level, as actions the page can render.

    The report used to name a blocker with the model's own QUESTION ("Each Dataset
    purposed for sharing and re-use is assigned a unique identifier") and, where a check
    had measured something, its evidence. Neither says what to do, so the one section
    that answers "what do I do" carried conformance findings only and a reader was left
    to infer the fix for a maturity gap from the question it failed.

    Each blocker becomes an ``Action`` shaped exactly like a validator one: the
    instrument's own words in the chip, a badge, the instruction, one consequence
    clause. The instruction and the clause come from the indicator's ``remedy`` in
    ``fair/dsm_indicators.yaml``, which is repo-authored — the workbook states the
    question and never the fix.

    An indicator with no remedy is skipped rather than rendered wordless; the generator
    refuses to emit one, so that can only happen against a hand-edited model file.
    """
    remedies = {
        str(ind.get("id")): ind.get("remedy") or {}
        for ind in (dsm_data or {}).get("indicators", [])
    }
    out: list[Action] = []
    for ident, text, evidence in blockers:
        remedy = remedies.get(ident) or {}
        instruction = str(remedy.get("do") or "")
        if not instruction:
            continue
        out.append(
            Action(
                key=f"dsm:{ident}",
                kind="indicator",
                subject=ident,
                # The evidence is what this action clears; where a check measured
                # nothing there is still exactly one indicator to close.
                findings=[evidence or text],
                tier="MATURITY",
                source="dsm",
                message=text,
                instruction=instruction,
                consequence=str(remedy.get("why") or ""),
            )
        )
    return out


def group_orphans(
    orphans: list[str],
    *,
    labels: dict[str, str] | None = None,
    types: dict[str, Any] | None = None,
) -> list[Action]:
    """Collapse orphaned entities into actions, clustered by what they are.

    Thirty-six unreachable AOP nodes are one job — wire the pathway to the assay
    that measures it — not thirty-six. Clustering is by id family, which is what
    distinguishes "the whole AOP subgraph" from "one stray PropertyValue".
    """
    clusters: dict[str, list[str]] = defaultdict(list)
    for orphan in orphans:
        if "aopwiki.org" in orphan:
            clusters["the AOP-Wiki subgraph"].append(orphan)
        elif orphan.startswith(("#DefinedTerm", "./#DefinedTerm")):
            clusters["vocabulary terms"].append(orphan)
        elif orphan.startswith(("#PropertyValue", "./#PropertyValue")):
            clusters["parameter values"].append(orphan)
        else:
            clusters[_entity_label(orphan, labels)].append(orphan)

    out = [
        Action(
            key=f"orphan:{name}",
            kind="orphan",
            subject=name,
            subject_names=[name],
            subject_types=[""],
            entity_ids=sorted(ids),
            findings=[f"{i} is not reachable from the crate root" for i in sorted(ids)],
            tier=_DEFAULT_TIER,
            source="graph",
            message=f"{name} not reachable from the crate root",
        )
        for name, ids in clusters.items()
    ]
    out.sort(key=lambda a: -a.cleared)
    return out
