"""Tool that wraps the three-pass SHACL validation from profiles/validator.py.

Translates the list[ValidationResult] into a single ValidationReport dataclass
with per-pass pass/fail and categorized issues.

Also exposes ``build_and_validate`` (#87): an in-memory build + validate that
returns routable, per-entity feedback without any disk round-trip — the fast
path for the agent's build/fix loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from builder.state import CrateState, ValidationReport

if TYPE_CHECKING:
    from profiles.validator import RoutableIssue

logger = logging.getLogger(__name__)


def validate(state: CrateState, crate_path: str) -> ValidationReport:
    """Run three-pass SHACL validation on the crate at crate_path.

    Wraps profiles/validator.py, translating its list[ValidationResult]
    into a single ValidationReport dataclass.

    If profiles/validator validation is not available (import error),
    return a ValidationReport with required_issues=["Validation not available"].

    Args:
        state: The current CrateState (used for context, not modified).
        crate_path: Path to the crate directory to validate.

    Returns:
        A ValidationReport with per-pass pass/fail and categorized issues.
    """
    try:
        from profiles.validator import ValidationResult, validate_crate
    except ImportError as e:
        logger.warning("Validation not available: %s", e)
        return ValidationReport(
            base_passed=False,
            isa_passed=False,
            tox_passed=False,
            required_issues=["Validation not available"],
            should_issues=[],
            may_issues=[],
        )

    crate_dir = Path(crate_path)

    # If the crate directory doesn't exist, return a default failure report
    if not crate_dir.exists():
        logger.info("Crate directory does not exist: %s", crate_dir)
        return ValidationReport(
            base_passed=False,
            isa_passed=False,
            tox_passed=False,
            required_issues=[f"Crate directory not found: {crate_path}"],
            should_issues=[],
            may_issues=[],
        )

    try:
        results: list[ValidationResult] = validate_crate(crate_dir)
    except Exception as e:
        logger.error("Validation failed with exception: %s", e)
        return ValidationReport(
            base_passed=False,
            isa_passed=False,
            tox_passed=False,
            required_issues=[f"Validation error: {e}"],
            should_issues=[],
            may_issues=[],
        )

    # Translate results into the ValidationReport structure
    required_issues: list[str] = []
    should_issues: list[str] = []
    may_issues: list[str] = []
    issue_records: list[dict[str, str]] = []

    base_passed = True
    isa_passed = True
    tox_passed = True

    def _record(profile: str, severity: str, text: str) -> dict[str, str]:
        # The disk path has no per-issue entity routing; the "[Required] " style
        # prefix is structure (already carried by `severity`), not message.
        return {
            "profile": profile,
            "severity": severity,
            "entity_id": "",
            "message": text.removeprefix(f"[{severity.capitalize()}] "),
        }

    for result in results:
        profile_key = _profile_key(result.profile)
        required_issues.extend(result.required_issues)
        issue_records.extend(_record(profile_key, "required", i) for i in result.required_issues)

        if not result.passed_required:
            if profile_key == "base":
                base_passed = False
            elif profile_key == "isa":
                isa_passed = False
            elif profile_key == "tox":
                tox_passed = False

        # Non-required issues go to should/may
        for issue in result.issues:
            if issue.startswith("[Required]"):
                # Already in required_issues
                pass
            elif issue.startswith("[Recommended]"):
                if issue not in should_issues:
                    should_issues.append(issue)
                issue_records.append(_record(profile_key, "recommended", issue))
            elif issue.startswith("[Optional]"):
                if issue not in may_issues:
                    may_issues.append(issue)
                issue_records.append(_record(profile_key, "optional", issue))

    return ValidationReport(
        base_passed=base_passed,
        isa_passed=isa_passed,
        tox_passed=tox_passed,
        required_issues=required_issues,
        should_issues=should_issues,
        may_issues=may_issues,
        # Every pass here runs at `Severity.OPTIONAL` — the widest gate — so this
        # verdict answers for all three tiers. Saying so is not bookkeeping: the
        # footer and the maturity report both read `assessed_tiers` to tell "no
        # findings" from "never ran", and leaving it empty made a full disk
        # validation render as three locked tiers sitting beside its own findings.
        # Only the success path claims this; the early returns above are failures
        # that swept nothing.
        assessed_tiers=set(_TIER_ORDER),
        issue_records=issue_records,
        # This path validated a directory, so the payload checks the in-memory
        # gate cannot run did run here — the verdict covers the files too (#530).
        payload_checked=True,
    )


def _profile_key(profile_name: str) -> str:
    """Map a validator pass's display name to its canonical layer key.

    The disk path labels its passes with prose names ("Base RO-Crate 1.2",
    "ISA RO-Crate Profile", "ISA-Tox RO-Crate Profile") rather than the
    ``base``/``isa``/``tox`` keys the dict path uses. Every one of those names
    contains "ro-crate", so the most-specific token must be tested first — the
    old ``"ro-crate" in name`` guard classified an ISA or ISA-Tox failure as a
    BASE failure.
    """
    name = profile_name.lower()
    if "tox" in name:
        return "tox"
    if "isa" in name:
        return "isa"
    if "base" in name or "ro-crate" in name:
        return "base"
    return ""


# ---------------------------------------------------------------------------
# Payload verification (#530)
# ---------------------------------------------------------------------------
# The SHACL profiles ask whether every declared Data Entity is part of the
# payload, but only the on-disk path can answer it: the in-memory document the
# build/fix loop and `export_crate` validate has no payload to inspect, so the
# check emits nothing at all — and a caller reading "no issues" as "passed"
# ships a crate whose base profile fails on disk under a green "Conformant".
#
# The fix is deliberately NOT a list of check ids to skip. Ids move between
# upstream releases (see the two namespaces tracked in #525), a list needs an
# edit per release, and skipping a check that emits nothing suppresses nothing.
# Instead this states the invariant the crate itself must satisfy — every local
# data entity is backed by a source `write()` will materialise — and answers it
# from the assembled crate, which is available wherever a crate is built, for
# any deposit.


def verify_payload(crate: Any) -> list[dict[str, Any]]:
    """Report every data entity the crate declares but will not write.

    ``ROCrate.write()`` materialises a data entity from its ``source``. An entity
    whose source is ``None`` is written to the metadata and never to disk (
    ro-crate-py warns "No source for …" and continues), leaving a crate that
    describes a file it does not contain — a REQUIRED failure of the base
    profile, and invisible to every in-memory validation.

    Checked before the write rather than after, because the maturity report is
    rendered and embedded while the crate is still in memory; a verdict reached
    afterwards could not reach the report that ships inside the crate. The two
    are equivalent for this class: ``write()`` materialises exactly the sources
    it holds.

    Entities identified by an absolute URI are remote by design and are not this
    crate's to materialise, so they are not reported.

    Args:
        crate: The assembled :class:`rocrate.rocrate.ROCrate`.

    Returns:
        Routable issue dicts in the shape :func:`build_and_validate` returns, so
        the existing write-back and the maturity report render them unchanged.
    """
    issues: list[dict[str, Any]] = []
    for entity in crate.data_entities:
        entity_id = str(getattr(entity, "id", "") or "")
        if not entity_id or entity_id.startswith(("#", "http://", "https://")):
            continue
        if getattr(entity, "source", None) is not None:
            continue
        issues.append(
            {
                "entity_id": entity_id,
                "property": "contentUrl",
                "message": (
                    f"The crate describes {entity_id!r} but carries no content for it, "
                    "so the written crate would not contain the file"
                ),
                "fix": (
                    f"Attach the real file for `{entity_id}`, or drop the entity so the "
                    "crate stops claiming a file it does not have."
                ),
                "severity": "required",
                "profile": "base",
            }
        )
    return issues


# The ISA classes this crate MINTS a structural entity for. Each carries a
# rule-set in the upstream profile that only applies once something references
# the entity, so each is a class whose whole rule-set can go silent (#537).
_ISA_STRUCTURAL_TYPES = frozenset({"LabProcess", "Sample", "LabProtocol"})

# Keys that describe the node rather than point away from it. `@type`'s values
# are class names, and a class name is not a reference to another entity.
_NON_REFERENCE_KEYS = frozenset({"@id", "@type", "@context"})


def verify_isa_reachability(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Report every ISA structural entity the crate mints and then references from nowhere.

    The ISA shapes infer their target class from the very edge whose absence is
    the defect. ``FindISAProcesses`` mints ``isa-ro-crate:Process`` only for a
    ``bioschemas:LabProcess`` some Dataset already points at, and
    ``ProcessMustBeReferencedFromDataset`` targets that inferred class — so a
    process nothing references never earns the label, and the rule written to
    catch exactly this defect has no target. Every rule keyed to the class goes
    silent with it. 11 of the profile's 12 shape files are built this way, so a
    missing structural edge switches off the whole rule-set for that layer and
    the crate reports conformant precisely when its structure is most broken.

    The upstream shapes are not ours to restructure, so the invariant is
    asserted here instead, and asserted the only way that cannot be gamed by an
    absent edge: an entity nothing points at is detached, whatever the profile
    was able to evaluate.

    Reachability is DIRECTED. ``provenance_dag.build_crate_graph`` already flags
    orphans, but over an undirected walk, where a process that points at the
    files it produced counts as connected even though nothing points at it —
    which is why it reports 19 orphans for the crate in #537 and none of them
    are its three detached processes.

    Entities identified by an absolute URI are described here and live
    elsewhere — a Cellosaurus cell line is a record of an external thing, not a
    hole in this crate's backbone — so they are not reported, the same line
    :func:`verify_payload` draws for the payload.

    Args:
        metadata: The ``crate.metadata.generate()`` document, the parsed
            ``ro-crate-metadata.json``, or the ``@graph`` list itself.

    Returns:
        Routable issue dicts in the shape :func:`build_and_validate` returns, so
        the existing write-back and the maturity report render them unchanged.
    """
    from builder.writers.provenance_dag import _refs, _types

    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = [node for node in graph if isinstance(node, dict) and node.get("@id")]

    referenced: set[str] = set()
    for node in nodes:
        keys = tuple(key for key in node if key not in _NON_REFERENCE_KEYS)
        referenced.update(ref for ref in _refs(node, keys) if ref != node.get("@id"))

    issues: list[dict[str, Any]] = []
    for node in nodes:
        entity_id = str(node["@id"])
        if entity_id.startswith(("http://", "https://")):
            continue
        if not (_types(node) & _ISA_STRUCTURAL_TYPES):
            continue
        if entity_id in referenced:
            continue
        kind = ", ".join(sorted(_types(node) & _ISA_STRUCTURAL_TYPES))
        issues.append(
            {
                "entity_id": entity_id,
                "property": "about",
                "message": (
                    f"Nothing in the crate references the {kind} {entity_id!r}, so it is "
                    "detached from the ISA backbone and every profile rule for its class "
                    "is skipped rather than passed"
                ),
                "fix": (
                    f"Reference `{entity_id}` from the entity it belongs to — a process "
                    "from its Assay's `about`, a protocol or sample from the process that "
                    "uses it — or drop it so the crate stops describing a step it does "
                    "not connect."
                ),
                "severity": "required",
                "profile": "isa",
            }
        )
    return issues


def record_isa_reachability_check(state: CrateState, crate: Any) -> list[dict[str, Any]]:
    """Fold :func:`verify_isa_reachability` into ``state.validation`` (#537).

    A detached structural entity is a REQUIRED failure of the ISA profile — the
    profile simply cannot say so itself — so it is filed through the same shape
    every other finding uses, and the header verdict flips off "Conformant"
    without the report needing to know this check exists.

    Marks the verdict ``isa_reachability_checked`` either way: a connected
    backbone is a result, and the distinction that matters downstream is
    "looked and found nothing" versus "never looked".

    Returns the issues found, for the caller to log or report.
    """
    issues = verify_isa_reachability(crate.metadata.generate())
    report = state.validation
    existing = set(report.required_issues)
    for issue, text in zip(issues, order_issues(issues, "required"), strict=True):
        if text not in existing:
            report.required_issues.append(text)
            report.issue_records.extend(_issue_records([issue], "required"))
    if issues:
        report.isa_passed = False
    report.assessed_tiers.add("required")
    report.isa_reachability_checked = True
    return issues


def record_payload_check(state: CrateState, crate: Any) -> list[dict[str, Any]]:
    """Fold :func:`verify_payload` into ``state.validation`` (#530).

    Files the crate cannot write are REQUIRED failures of the base profile, so
    they are filed as REQUIRED issues through the same shape every other finding
    uses: the maturity report renders them, and the header verdict flips off
    "Conformant" without the report needing to know this check exists.

    Marks the verdict ``payload_checked`` either way — a clean payload is a
    result, and the distinction that matters downstream is "looked and found
    nothing" versus "never looked".

    Returns the issues found, for the caller to log or report.
    """
    issues = verify_payload(crate)
    report = state.validation
    existing = set(report.required_issues)
    for issue, text in zip(issues, order_issues(issues, "required"), strict=True):
        if text not in existing:
            report.required_issues.append(text)
            report.issue_records.extend(_issue_records([issue], "required"))
    if issues:
        report.base_passed = False
    report.assessed_tiers.add("required")
    report.payload_checked = True
    return issues


# ---------------------------------------------------------------------------
# In-memory build + validate with routable feedback (#87)
# ---------------------------------------------------------------------------

# Per-property fix templates keyed by the property's local name. Synthesized
# locally (roc-validator does not provide fixes); richer guidance lands with #88.
_FIX_TEMPLATES: dict[str, str] = {
    "name": "Add a `name` to `{entity}`.",
    "description": "Add a `description` to `{entity}`.",
    "identifier": "Add an `identifier` to `{entity}`.",
    "conformsTo": "Add `conformsTo` to `{entity}` referencing the RO-Crate 1.2 spec.",
    "license": "Add a `license` to `{entity}`.",
    "author": "Add an `author` to `{entity}`.",
    "datePublished": "Add a `datePublished` to `{entity}`.",
}


def _local_name(iri: str | None) -> str:
    """Return the local part of a property IRI (after the last / or #)."""
    if not iri:
        return ""
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _synthesize_fix(issue: RoutableIssue) -> str:
    """Build a short, actionable fix hint for a routable issue."""
    entity = issue.entity_id or "the affected entity"
    prop = _local_name(issue.property)
    if prop in _FIX_TEMPLATES:
        return _FIX_TEMPLATES[prop].format(entity=entity)
    if prop:
        suffix = f" (check {issue.check_id})" if issue.check_id else ""
        return f"Set `{prop}` on `{entity}`{suffix}."
    if issue.check_id:
        return f"Resolve check `{issue.check_id}` on `{entity}`."
    return f"Fix `{entity}`: {issue.message}"


def _assemble_and_validate(
    state: CrateState, *, severity: str, profile: str
) -> tuple[dict[str, Any], list[Any]]:
    """Assemble the crate in memory and validate it, returning BOTH.

    Split out so a caller that needs the assembled document itself — the gap
    engine, whose MIT matcher scores against the assembled graph — can reuse this
    one assembly instead of building the crate a second time (#377).

    Deliberately NOT surfaced by widening ``build_and_validate``'s return:
    that is a registered ReAct tool whose result is serialized into the model's
    context, and a whole ``@graph`` there would be pure token cost.

    ``include_all_scanned=False``: the auto-included scanned-file leaves (#175)
    are plain File nodes that do not change the validation verdict, so they are
    skipped on this hot path. ``export_crate`` uses the default (True) so the
    written crate packages the whole dataset.
    """
    # Imported lazily (not at module top) so validate()'s ImportError handling
    # stays intact. profiles.validator installs the roc-validator bootstrap shim
    # + bundled-ISA-ontology patch on import; importing validate_crate_dict here
    # runs that shim before any rocrate_validator import is triggered.
    from builder.tools.builder import assemble_crate
    from profiles.validator import validate_crate_dict

    crate = assemble_crate(
        state,
        output_dir=None,
        materialize_payload=False,
        include_all_scanned=False,
    )
    metadata_doc = crate.metadata.generate()
    return metadata_doc, validate_crate_dict(metadata_doc, severity=severity, profile=profile)


# Properties whose value is a term the crate POINTS AT rather than describes.
# `propertyID` says which scheme an identifier belongs to, `propertyUrl` which
# term a column means, `conformsTo` which spec a thing follows. None of them
# assert anything about the IRI itself.
#
# Kept in step with `builder.tools.builder._TERM_REFERENCE_PROPERTIES`, which is
# the same idea for the same reason on the writing side.
_CITATION_PROPERTIES: frozenset[str] = frozenset(
    {"propertyID", "propertyUrl", "inDefinedTermSet", "conformsTo"}
)


def _context_vocabulary(metadata_doc: dict[str, Any]) -> set[str]:
    """Every IRI the crate's own ``@context`` defines a term for.

    A context maps names to classes and properties — `"KeyEvent"` to
    `https://aopwiki.org/ontology/KeyEvent`, `"has_key_event"` to
    `…/hasKeyEvent`. Those IRIs reach the validator as types and predicates, never
    as reference objects, so scanning the graph for `{"@id": …}` values misses
    every one of them: on a real crate that was 47 findings asking a *property*
    for a human-readable name.

    Vocabulary by construction. A predicate is not an entity a crate can describe,
    whoever publishes it — which is why this needs no namespace list either.
    """
    context = metadata_doc.get("@context")
    entries = context if isinstance(context, list) else [context]
    defined: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue  # a remote context reference; its terms are not ours to judge
        for term, target in entry.items():
            if term.startswith("@"):
                continue  # @vocab and friends are namespaces, not terms
            iri = target.get("@id") if isinstance(target, dict) else target
            if isinstance(iri, str) and "://" in iri:
                defined.add(iri)
    return defined


def _cited_iris(metadata_doc: dict[str, Any]) -> set[str]:
    """External IRIs the crate CITES — points at without asserting anything about.

    An RO-Crate is expected to describe what it asserts. It is not expected to
    describe the vocabularies it references: the upstream shapes say so
    themselves, excluding schema.org, w3.org, purl.org, bioschemas.org and urn:
    from these very checks. That list is the one a workflow crate needs, so a
    crate citing any other vocabulary gets findings for terms it does not own —
    "no name", "no schema.org type", "not described in the graph" — none of which
    can be fixed without copying data that belongs to, and is versioned by,
    somebody else.

    The obvious fix is to add the missing namespaces, and it is wrong. It fits
    whichever crate prompted it, needs an edit for every new ontology, and cannot
    express the case that actually matters: `https://orcid.org` is a scheme this
    crate cites, while `https://orcid.org/0009-0000-5074-6239` is an author it
    describes. Same prefix, opposite answers. No host list can separate them.

    So this asks the crate instead. An IRI is a citation when all three hold:

    * it is external — a crate-local id is always ours;
    * the crate does not describe it — nothing in the graph gives it properties;
    * nothing references it through a property that asserts something. Referenced
      only via `propertyID`/`propertyUrl`/`conformsTo`, or only as a type, it is
      vocabulary. Referenced via `author`, `hasPart` or `has_key_event`, it is an
      entity the crate is genuinely missing, and it stays reported.

    That last clause is the safety property: an author we linked but never drafted
    is referenced via `author`, so it is NOT a citation and the finding survives.
    Nothing the crate asserts can be silenced by this.

    No namespace list, no configuration, and an ontology nobody has seen before
    classifies correctly on first contact. It replaces an open-ended list of hosts
    with a closed list of four properties, which is the part worth keeping.

    Proposed upstream too, since the same rule would subsume the namespace list
    their shapes carry — see docs/upstream/rocrate-validator-cited-vocabulary.md.
    """
    graph = metadata_doc.get("@graph")
    if not isinstance(graph, list):
        return set()

    described: set[str] = set()
    asserted: set[str] = set()  # referenced through a property that means something
    mentioned: set[str] = set()  # referenced at all

    for node in graph:
        if not isinstance(node, dict):
            continue
        node_id = node.get("@id")
        # A node carrying anything beyond its identity is described. `@type`
        # alone is not description — a bare {"@id", "@type"} stub is exactly what
        # these findings are about.
        if isinstance(node_id, str) and set(node) - {"@id", "@type"}:
            described.add(node_id)
        for prop, value in node.items():
            if prop in ("@id", "@type"):
                continue
            for item in value if isinstance(value, list) else [value]:
                iri = item.get("@id") if isinstance(item, dict) else item
                if not isinstance(iri, str) or "://" not in iri:
                    continue
                mentioned.add(iri)
                if prop not in _CITATION_PROPERTIES:
                    asserted.add(iri)

    # The classes and properties the crate's own context defines are vocabulary
    # too, and they never appear as reference objects — see `_context_vocabulary`.
    return (mentioned | _context_vocabulary(metadata_doc)) - asserted - described


# A property the crate cannot state about itself without chasing its own tail.
# The maturity report is rendered FROM the validation result, so its byte count
# depends on how many findings there were — and stating that size changes the
# graph, which changes the findings, which changes the size. Measured across three
# exports of one crate: 121,626 / 96,414 / 99,458 bytes as the findings moved.
# There is no fixed point to converge on, so the size is not stated and the
# finding that follows is not actionable by anyone.
#
# Deliberately ONE entry, matched on the crate's own generated filename. It is not
# a list that grows with the domain — it is the single artifact whose content is a
# function of the answer.
_UNANSWERABLE = ((REPORT_FILENAME := "ro-crate-metadata-maturity.html"), "contentSize")


def _is_unanswerable(issue: dict[str, Any]) -> bool:
    """Whether this finding asks for something that cannot exist by construction."""
    entity, prop = _UNANSWERABLE
    return str(issue.get("entity_id") or "").endswith(entity) and prop in str(
        issue.get("message") or ""
    )


def _partition_citations(
    issues: list[dict[str, Any]], cited: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into real gaps and findings about cited vocabulary."""
    gaps, citations = [], []
    for issue in issues:
        if _is_unanswerable(issue):
            continue  # see `_UNANSWERABLE` — no one can act on it, including us
        entity = issue.get("entity_id") or ""
        (citations if entity in cited else gaps).append(issue)
    return gaps, citations


def build_and_validate(
    state: CrateState,
    severity: str | None = "required",
    profile: str | None = "all",
) -> dict[str, Any]:
    """Build the crate from CrateState in memory and validate it — no disk write.

    This is the agent's fast build/fix loop: it assembles the crate with
    ro-crate-py, generates the JSON-LD metadata document, and validates that
    document directly (no crate is written to disk, nothing is re-read). Issues
    come back keyed to the entity/property that failed so the agent can route a
    fix to a specific field rather than parsing prose.

    Args:
        state: The current CrateState to build and validate.
        severity: Gate severity ("required" | "recommended" | "optional"). The
            default "required" runs only REQUIRED-severity checks (fastest);
            lower it to also surface recommendations.
        profile: "all" runs the base -> isa -> tox passes; "base"/"isa"/"tox"
            scopes to a single pass. The BASE pass dominates wall-clock — on a
            293-entity crate it is 22.9s of a 36.9s OPTIONAL sweep (62%), against
            9.2s for tox (25%) and 4.8s for isa. Scoping to "tox" therefore saves
            far less than scoping away from "base" would.

    Returns:
        ``{"ok": bool, "conformance": {layer: bool}, "issues": [issue, ...]}``
        where each issue is ``{entity_id, property, message, fix, severity,
        profile}``. ``conformance`` reports REQUIRED-level pass/fail per layer
        validated; ``ok`` is True when there are no issues at the gate severity.
    """
    # Weak models (e.g. DeepSeek-flash) emit explicit nulls for optional tool
    # args instead of omitting them, so the function defaults never apply and a
    # bare None would otherwise raise "Unknown profile None". Treat None as "use
    # the default"; genuine typos (a non-None unknown string) still fail loudly.
    severity = "required" if severity is None else severity
    profile = "all" if profile is None else profile

    # A sweep already computed for this exact state can answer any narrower
    # question, because the gate is a floor and not a filter (see
    # :func:`tiers_covered`): an OPTIONAL sweep already carries the REQUIRED and
    # RECOMMENDED findings, so serving "required" from it is a list filter rather
    # than 20-plus seconds of SHACL. Without this, a profiled run paid for two
    # 40-second RECOMMENDED sweeps immediately after an export had validated the
    # same unchanged state at OPTIONAL.
    memo_key = _sweep_memo_key(state)
    cached = _SWEEP_MEMO.get(memo_key) if memo_key else None
    if cached is not None and _sweep_covers(cached, profile, severity):
        logger.debug(
            "Reusing the (%s, %s) sweep already computed for this state",
            cached[0],
            cached[1],
        )
        return _sweep_scoped(cached[2], cached[3], profile, severity, cached[4])

    try:
        # include_all_scanned=False: the auto-included scanned-file leaves (#175)
        # are plain File nodes that don't change the validation verdict, so we skip
        # them on this hot in-loop path to keep build_and_validate fast. export_crate
        # uses the default (True) so the written crate packages the whole dataset.
        metadata_doc, results = _assemble_and_validate(state, severity=severity, profile=profile)
    except Exception as e:  # noqa: BLE001 — surface as a tool error, never crash the loop
        logger.error("build_and_validate failed: %s", e)
        return {"ok": False, "conformance": {}, "issues": [], "error": str(e)}

    conformance = {r.profile: r.passed_required for r in results}
    # Findings about vocabulary the crate CITES are separated from findings about
    # what the crate ASSERTS — see `_cited_iris` for how that line is drawn, and
    # why it is drawn from the crate's own structure rather than from a list of
    # ontology hosts. A previous attempt at this WAS such a list, deleted in
    # 00ca43b for needing an edit per ontology; the objection was right and it
    # applies to any list, including one written today.
    #
    # Separated, not dropped: they come back under `citations`, counted, so a
    # finding never disappears without a trace. What changes is that `issues`
    # holds the things somebody can actually act on.
    issues: list[dict[str, Any]] = [
        {
            "entity_id": issue.entity_id,
            "property": issue.property,
            "message": issue.message,
            "fix": _synthesize_fix(issue),
            "severity": issue.severity,
            "profile": issue.profile,
        }
        for result in results
        for issue in result.issues
    ]

    issues, citations = _partition_citations(issues, _cited_iris(metadata_doc))
    if citations:
        logger.info(
            "%d finding(s) are about vocabulary this crate cites, not about the crate",
            len(citations),
        )

    if memo_key:
        _remember_sweep(memo_key, profile, severity, conformance, issues, citations)

    # Keep the original routable tool shape stable. The engine receives the
    # requested severity/profile as call arguments and routes writeback from
    # those arguments rather than expanding this public result contract.
    return _sweep_scoped(conformance, issues, profile, severity, citations)


# ---------------------------------------------------------------------------
# One sweep per state (#profile-20260810)
# ---------------------------------------------------------------------------
# Keyed on the validation fingerprint alone, holding the widest (profile, gate)
# computed for that state. Bounded and in-process: a crate the agent has moved on
# from is never asked about again, so a handful of entries covers the loop's
# back-and-forth without holding whole issue lists for a long session.
_SWEEP_MEMO: dict[
    str, tuple[str, str, dict[str, bool], list[dict[str, Any]], list[dict[str, Any]]]
] = {}
_SWEEP_MEMO_MAX = 4

# Which passes each `profile` argument actually runs. Mirrors the dispatch in
# `profiles.validator.validate_crate_dict`; kept here as data so scoping a cached
# sweep and validating one stay the same statement.
_PROFILE_SCOPES: dict[str, tuple[str, ...]] = {
    "all": ("base", "isa", "tox"),
    "base": ("base",),
    "isa": ("isa",),
    "tox": ("tox",),
}


def _sweep_memo_key(state: CrateState) -> str | None:
    """Memo key for *state*, or None when it cannot be fingerprinted."""
    try:
        return state.validation_fingerprint()
    except Exception:  # noqa: BLE001 — an un-fingerprintable state just re-runs
        logger.debug("State could not be fingerprinted; validating without the memo")
        return None


def _gate_covers(have: str, want: str) -> bool:
    """Whether a sweep run at *have* already evaluated everything *want* asks for."""
    have_tiers = set(tiers_covered(have))
    want_tiers = set(tiers_covered(want))
    # An unknown severity covers nothing (tiers_covered returns ()), so a typo
    # re-runs rather than silently matching every cached sweep.
    return bool(want_tiers) and want_tiers <= have_tiers


def _scope_covers(have: str, want: str) -> bool:
    """Whether a sweep over *have* already ran the passes *want* asks for.

    "all" is base + isa + tox, so it answers any single-profile question by
    filtering; the single profiles are disjoint and answer only themselves. This
    is the same containment the severity gate has, in the other dimension — and
    leaving it out is why a profiled session paid for six consecutive
    ``profile="base"`` sweeps after an ``profile="all"`` sweep of the identical
    state: the memo was keyed on the profile, so "all" and "base" looked like
    unrelated questions and not one of the six could hit.
    """
    if want not in _PROFILE_SCOPES:
        return False  # a typo re-runs rather than matching every cached sweep
    return have == want or have == "all"


def _remember_sweep(
    key: str,
    profile: str,
    severity: str,
    conformance: dict[str, bool],
    issues: list[dict[str, Any]],
    citations: list[dict[str, Any]],
) -> None:
    """Record a sweep, keeping the widest (profile, gate) seen for this state."""
    existing = _SWEEP_MEMO.get(key)
    if (
        existing is not None
        and _scope_covers(existing[0], profile)
        and _gate_covers(existing[1], severity)
    ):
        return  # what we already hold answers strictly more
    if len(_SWEEP_MEMO) >= _SWEEP_MEMO_MAX and key not in _SWEEP_MEMO:
        _SWEEP_MEMO.pop(next(iter(_SWEEP_MEMO)))
    _SWEEP_MEMO[key] = (profile, severity, conformance, issues, citations)


def _sweep_covers(
    cached: tuple[str, str, dict[str, bool], list[dict[str, Any]], list[dict[str, Any]]],
    profile: str,
    severity: str,
) -> bool:
    """Whether a cached sweep answers this (profile, severity) question outright."""
    return _scope_covers(cached[0], profile) and _gate_covers(cached[1], severity)


def _sweep_scoped(
    conformance: dict[str, bool],
    issues: list[dict[str, Any]],
    profile: str,
    severity: str,
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Narrow a sweep to the passes and tiers this call actually asked for.

    Both narrowings matter for the same reason: the answer must be
    indistinguishable from a real run at that scope and gate. Reporting isa and
    tox conformance to a caller that asked for ``profile="base"`` would hand back
    verdicts it did not request and, on the next call, did not necessarily still
    hold.

    Only a finding whose severity is a KNOWN tier wider than the gate is set
    aside. ``_routable_issue`` falls back to the raw enum name for any severity
    outside the three tiers, and a filter that kept just the recognised ones
    would silently swallow those — the same "findings vanish for no visible
    reason" failure that reporting vocabulary findings was meant to end. An
    unrecognised severity is always reported.
    """
    tiers = set(tiers_covered(severity))
    passes = set(_PROFILE_SCOPES.get(profile, ()))
    scoped = [
        issue
        for issue in issues
        if issue.get("profile") in passes
        and (issue.get("severity") not in _TIER_ORDER or issue.get("severity") in tiers)
    ]
    gated_conformance = {layer: passed for layer, passed in conformance.items() if layer in passes}
    scoped_citations = [
        issue
        for issue in (citations or [])
        if issue.get("profile") in passes
        and (issue.get("severity") not in _TIER_ORDER or issue.get("severity") in tiers)
    ]
    return {
        "ok": not scoped,
        "conformance": gated_conformance,
        "issues": scoped,
        # Scoped and gated exactly like `issues`, so a narrowed result is
        # indistinguishable from a real run at that scope — citations included.
        "citations": scoped_citations,
    }


def clear_sweep_memo() -> None:
    """Forget every remembered sweep — for tests, and for a fresh session."""
    _SWEEP_MEMO.clear()


# ---------------------------------------------------------------------------
# Verdict write-back + freshness (#153, #155)
# ---------------------------------------------------------------------------
# A ValidationReport is only meaningful next to the crate it judged. The agent
# keeps editing after validating, so a verdict recorded five tool calls ago can
# describe a crate that no longer exists — and the maturity report embedded in
# the export would then ship a green "Conformant" for a state nobody checked.
# Every write-back therefore stamps the report with the validation fingerprint of
# the state it was computed from, and `is_stale_for` answers the question later.

_VALIDATION_LAYER_ORDER: dict[str, int] = {"base": 0, "isa": 1, "tox": 2}

# Severity tiers, strictest first. A gate is inclusive: validating at
# "recommended" also runs every REQUIRED check, and "optional" runs all three.
_TIER_ORDER: tuple[str, ...] = ("required", "recommended", "optional")
_TIER_FIELDS: dict[str, str] = {
    "required": "required_issues",
    "recommended": "should_issues",
    "optional": "may_issues",
}


def tiers_covered(severity: str) -> tuple[str, ...]:
    """Return every tier a gate at *severity* actually evaluates.

    The gate is a floor, not a filter: ``requirement_severity=OPTIONAL`` runs the
    REQUIRED and RECOMMENDED checks too, and the result carries issues from all
    three. An unknown severity covers nothing, so a typo files no issues rather
    than silently filing them under the wrong tier.
    """
    if severity not in _TIER_ORDER:
        return ()
    return _TIER_ORDER[: _TIER_ORDER.index(severity) + 1]


def _select_tier(issues: list[dict[str, Any]], severity: str) -> list[dict[str, Any]]:
    """One severity tier of *issues*, in stable base → isa → tox layer order."""
    selected = [i for i in issues if i.get("severity") == severity]
    selected.sort(key=lambda i: _VALIDATION_LAYER_ORDER.get(i.get("profile") or "", 99))
    return selected


def order_issues(issues: list[dict[str, Any]], severity: str) -> list[str]:
    """Return one severity tier as stable, layer-ordered display strings."""
    return [
        (
            f"[{i.get('profile') or '?'}] {i.get('entity_id') or '?'}: {i.get('message') or ''}"
        ).rstrip()
        for i in _select_tier(issues, severity)
    ]


def _issue_records(issues: list[dict[str, Any]], severity: str) -> list[dict[str, str]]:
    """One severity tier as structured records, ordered like :func:`order_issues`.

    The records carry the attribution the display strings flatten away, so the
    maturity report can group findings per profile without re-parsing the
    ``[profile] entity: message`` shape the ReAct loop depends on (#510).
    """
    return [
        {
            "profile": str(i.get("profile") or ""),
            "severity": severity,
            "entity_id": str(i.get("entity_id") or ""),
            "message": str(i.get("message") or ""),
        }
        for i in _select_tier(issues, severity)
    ]


def apply_validation_result(
    state: CrateState,
    tool_name: str,
    result: Any,
    *,
    severity: str | None = None,
) -> None:
    """Fold a validation result into ``state.validation`` and stamp its freshness.

    ``validate`` returns a fully-formed :class:`ValidationReport` (disk,
    three-pass) — adopt it wholesale. ``build_and_validate`` returns the
    in-memory routable dict (``{"ok", "conformance", "issues"}``); map its
    per-layer ``conformance`` onto the report and record the issues for the tier
    that was gated. Layers absent from ``conformance`` (a scoped ``profile=``
    call) keep their prior value, and an errored result is left untouched so a
    transient failure never wipes known issues.

    Shared by the engine's tool write-back and by ``export_crate``, so a verdict
    reached either way carries the same shape and the same freshness stamp.
    """
    if tool_name == "validate" and isinstance(result, ValidationReport):
        result.input_fingerprint = state.validation_fingerprint()
        # A whole report replaces the previous one, so anything the old verdict
        # remembered goes with it — except for a tier this one did not answer
        # for (the failure reports `validate` returns when the validator is
        # missing or the directory is not there assess nothing at all).
        result.stale_tier_counts = {
            tier: count
            for tier, count in state.validation.stale_tier_counts.items()
            if tier not in result.assessed_tiers
        }
        state.validation = result
        return
    if tool_name != "build_and_validate" or not isinstance(result, dict):
        return
    if "error" in result:
        return
    conformance = result.get("conformance") or {}
    if not conformance:
        return
    report = state.validation
    for layer, attr in (("base", "base_passed"), ("isa", "isa_passed"), ("tox", "tox_passed")):
        if layer in conformance:
            setattr(report, attr, bool(conformance[layer]))
    issues = result.get("issues") or []
    # The caller's kwarg wins; fall back to the severity the validator stamped on
    # its own result before assuming "required", so a recommended/optional result
    # can never be filed as REQUIRED issues.
    severity = str(severity or result.get("severity") or "required")
    covered = tiers_covered(severity)
    fingerprint = state.validation_fingerprint()
    # The structured records shadow the string lists tier for tier: retired
    # together, refreshed together, so neither view can describe a different
    # validation than the other. Bucketing by the severity actually present —
    # rather than by the three known tiers — keeps a record this run neither
    # evaluated nor retired: no writer emits one today, but a checkpoint can
    # carry it, and the report renders such findings rather than dropping them.
    tier_records: dict[str, list[dict[str, str]]] = {tier: [] for tier in _TIER_ORDER}
    for record in report.issue_records:
        tier_records.setdefault(str(record.get("severity") or ""), []).append(record)
    # Retire the tiers this run did NOT cover, whenever the state has moved on
    # since the last verdict. `assessed_tiers` is otherwise additive: once an
    # export assessed all three, a later REQUIRED-only run refreshed the required
    # list, re-stamped the fingerprint, and left the OPTIONAL tier still marked
    # assessed — carrying findings computed against an older crate. The next
    # export then read that as fully covered and skipped its own sweep, shipping
    # advisory findings that no longer described the crate being written.
    # Mirrors `is_stale_for`: only a positive fingerprint mismatch retires a tier,
    # so a verdict with no stamp (a hand-built or pre-stamp report) is left alone.
    if report.input_fingerprint and report.input_fingerprint != fingerprint:
        for tier in _TIER_ORDER:
            if tier not in covered:
                # Retire the FINDINGS, remember the COUNT. Retirement says the
                # findings no longer describe this crate; it does not say the
                # tier was never looked at, and the footer could not tell those
                # apart — so a run that had been showing "4 rec 11 opt" went back
                # to "rec/opt locked" the moment a REQUIRED-gated sweep followed
                # an edit. The count is what the footer needs to say "4, as of
                # the last sweep" rather than to claim ignorance or print a 0.
                if tier in report.assessed_tiers:
                    report.stale_tier_counts[tier] = len(getattr(report, _TIER_FIELDS[tier]) or [])
                setattr(report, _TIER_FIELDS[tier], [])
                report.assessed_tiers.discard(tier)
                tier_records[tier] = []
    # File EVERY tier the gate evaluated, not just the tier named. A gate is
    # inclusive (see `tiers_covered`), so a "recommended" run's result already
    # holds the REQUIRED findings; recording only the SHOULD ones left the
    # required list stamped fresh while describing an older validation.
    for tier in covered:
        setattr(report, _TIER_FIELDS[tier], order_issues(issues, tier))
        report.assessed_tiers.add(tier)
        report.stale_tier_counts.pop(tier, None)  # answered again; nothing to remember
        tier_records[tier] = _issue_records(issues, tier)
    report.issue_records = [r for tier in _TIER_ORDER for r in tier_records[tier]] + [
        r for tier, records in tier_records.items() if tier not in _TIER_ORDER for r in records
    ]
    report.input_fingerprint = fingerprint


def ensure_validated(
    state: CrateState,
    *,
    severity: str = "optional",
    profile: str = "all",
) -> dict[str, Any]:
    """Validate *state* at the full severity gate unless the verdict is current.

    Export embeds the maturity report, and that report is only worth shipping if
    its verdict describes the crate being written. This runs
    :func:`build_and_validate` when the recorded verdict is missing, stale, or
    narrower than the requested gate, and folds the outcome into
    ``state.validation``.

    The gate defaults to ``"optional"`` — the widest one — because this is the
    export path, not the agent's inner loop. The in-loop default of "required" is
    a speed choice that is right when the model is iterating and wrong exactly
    once: at the moment the crate is written. A crate exported after a REQUIRED-only
    run shipped a maturity report whose Recommended and Optional rows read "not
    assessed", so the one artifact meant to describe the crate's quality was
    silent about two of its three tiers. Assessing everything costs one extra
    sweep per export — ~37s on a 293-entity crate, of which the BASE pass is 62%
    and tox 25% — and it buys a report that covers the whole crate. When the
    in-loop sweep already ran at this gate, :func:`build_and_validate` serves it
    from the per-state memo and the extra sweep costs nothing.

    Freshness now accounts for tier coverage as well as content: a verdict whose
    fingerprint matches but that only ever assessed REQUIRED is not sufficient
    for an OPTIONAL-gated caller, and is re-run rather than adopted.

    Never raises: a validator failure is reported in the return value so the
    caller (``export_crate``) can still write the crate and say so.

    Returns:
        ``{"ran", "reason", "ok", "error", "severity", "issue_counts"}`` where
        ``reason`` is ``"fresh"`` / ``"never-validated"`` / ``"stale"`` /
        ``"tiers-incomplete"``, and ``ok`` reports REQUIRED conformance only —
        advisory findings at the wider tiers are reported in ``issue_counts``,
        never as a failed export.
    """
    report = state.validation
    has_verdict = bool(
        report.input_fingerprint
        or report.base_passed
        or report.isa_passed
        or report.tox_passed
        or report.assessed_tiers
    )
    wanted_tiers = set(tiers_covered(severity))
    missing_tiers = wanted_tiers - set(report.assessed_tiers)
    if has_verdict and not report.is_stale_for(state):
        if not missing_tiers:
            return {
                "ran": False,
                "reason": "fresh",
                "ok": None,
                "error": None,
                "severity": severity,
                "issue_counts": _recorded_tier_counts(report),
            }
        reason = "tiers-incomplete"
    else:
        reason = "stale" if has_verdict else "never-validated"

    def _failed(error: str) -> dict[str, Any]:
        return {
            "ran": False,
            "reason": reason,
            "ok": None,
            "error": error,
            "severity": severity,
            "issue_counts": _recorded_tier_counts(report),
        }

    try:
        result = build_and_validate(state, severity=severity, profile=profile)
    except Exception as exc:  # noqa: BLE001 — export must still write the crate
        logger.warning("Export-time validation failed: %s", exc)
        return _failed(str(exc))
    if "error" in result:
        return _failed(str(result["error"]))

    apply_validation_result(state, "build_and_validate", result, severity=severity)
    # `result["ok"]` means "no issues at ANY tier the gate ran", so at the export
    # gate a single MAY-level suggestion would report the export as not ok and
    # send the agent back into a fix loop over advisory findings. Conformance is
    # the REQUIRED question; the rest are counted, not gated.
    conformance = result.get("conformance") or {}
    required_ok = bool(conformance) and all(conformance.values())
    return {
        "ran": True,
        "reason": reason,
        "ok": required_ok,
        "error": None,
        "severity": severity,
        "issue_counts": _tier_counts(result.get("issues") or []),
    }


def _tier_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    """Count issues per severity tier."""
    counts = {tier: 0 for tier in _TIER_ORDER}
    for issue in issues:
        tier = str(issue.get("severity") or "")
        if tier in counts:
            counts[tier] += 1
    return counts


def _recorded_tier_counts(report: ValidationReport) -> dict[str, int]:
    """Per-tier issue counts already recorded on *report*, for tiers it assessed.

    A tier that was never assessed is absent rather than zero — an empty list
    there means "not evaluated", and reporting it as 0 would read as clean.
    """
    return {
        tier: len(getattr(report, _TIER_FIELDS[tier]) or [])
        for tier in _TIER_ORDER
        if tier in report.assessed_tiers
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate", validate, takes_state=True)
TOOL_REGISTRY.register("build_and_validate", build_and_validate, takes_state=True)
