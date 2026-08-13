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
        tier: The strongest tier among those findings (MUST > SHOULD > MAY).
        actionable: False for a finding that is deliberately left open.
        note: Why it is not actionable, when it is not.
    """

    key: str
    kind: str
    subject: str
    entity_ids: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    tier: str = "SHOULD"
    actionable: bool = True
    note: str | None = None

    @property
    def cleared(self) -> int:
        """How many findings this action would close."""
        return len(self.findings)


_TIER_RANK = {"MUST": 0, "SHOULD": 1, "MAY": 2}


def _strongest(tiers: list[str]) -> str:
    return sorted(tiers, key=lambda t: _TIER_RANK.get(t, 3))[0] if tiers else "SHOULD"


def _not_actionable_note(message: str) -> str | None:
    for needle, note in _NOT_ACTIONABLE:
        if needle in message:
            return note
    return None


def _entity_label(entity_id: str, labels: dict[str, str] | None) -> str:
    """A name a reader recognises, falling back to the id's readable tail."""
    if labels and (name := labels.get(entity_id)):
        return name
    tail = entity_id.rsplit("/", 1)[-1].lstrip("#").removeprefix("./")
    for prefix in ("CitationAuthor_", "DefinedTerm_", "LabProcess_", "LabProtocol_"):
        tail = tail.removeprefix(prefix)
    return tail.replace("_", " ").strip() or entity_id


def _property_label(prop: str) -> str:
    return (prop or "").rsplit("/", 1)[-1].rsplit("#", 1)[-1] or "property"


def group_findings(
    findings: list[dict[str, Any]],
    *,
    labels: dict[str, str] | None = None,
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
        Actions sorted by tier, then by how many findings each clears.
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
        actions.append(
            Action(
                key=f"{kind}:{subject_id}",
                kind=kind,
                subject=(
                    _entity_label(subject_id, labels)
                    if kind == "entity"
                    else _property_label(subject_id)
                ),
                entity_ids=sorted({str(f.get("entity_id")) for f in live if f.get("entity_id")}),
                findings=[str(f.get("message") or "") for f in live],
                tier=_strongest([str(f.get("severity") or "SHOULD").upper() for f in live]),
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

    actions.sort(key=lambda a: (_TIER_RANK.get(a.tier, 3), -a.cleared, a.subject))
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
    what = _wanted(action.findings)
    subject = action.subject
    if action.kind == "orphan":
        return f"Connect {subject} to the crate — {action.cleared} entities are unreachable."
    if action.kind == "property":
        return f"Supply {subject} for {action.cleared} entities that are missing it."
    return f"{what} for {subject}." if what else f"Complete the metadata for {subject}."


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
    (("dateCreated", "date"), "Add the date it was created"),
    (("description",), "Add a description"),
    (("termCode",), "Add the ontology code"),
    (("parameter value", "additional property"), "Record the parameters used"),
    (("protocol",), "Link the protocol it follows"),
    (("identifier",), "Add an identifier"),
)


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


def _join(names: list[str], limit: int = 3) -> str:
    """ "Ada, Grace and 2 others" — a subject line that stays a line."""
    if len(names) <= limit:
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    rest = len(names) - limit
    return f"{', '.join(names[:limit])} and {rest} other{'s' if rest > 1 else ''}"


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
        subjects = [a.subject for a in group]
        merged.append(
            Action(
                key="entities:" + "|".join(sorted(a.key for a in group)),
                kind="entities",
                subject=_join(subjects),
                entity_ids=sorted({e for a in group for e in a.entity_ids}),
                # Every finding, so `cleared` still totals the whole list.
                findings=[m for a in group for m in a.findings],
                tier=_strongest([a.tier for a in group]),
            )
        )
    merged.sort(key=lambda a: (_TIER_RANK.get(a.tier, 3), -a.cleared, a.subject))
    return merged


def group_orphans(orphans: list[str], *, labels: dict[str, str] | None = None) -> list[Action]:
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
            entity_ids=sorted(ids),
            findings=[f"{i} is not reachable from the crate root" for i in sorted(ids)],
            tier="SHOULD",
        )
        for name, ids in clusters.items()
    ]
    out.sort(key=lambda a: -a.cleared)
    return out
