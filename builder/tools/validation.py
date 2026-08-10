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

    base_passed = True
    isa_passed = True
    tox_passed = True

    for result in results:
        profile_name = result.profile.lower()
        required_issues.extend(result.required_issues)

        # Classify issues by severity
        if not result.passed_required:
            if "base" in profile_name or "ro-crate" in profile_name:
                base_passed = False
            elif "isa" in profile_name and "tox" not in profile_name:
                isa_passed = False
            elif "tox" in profile_name:
                tox_passed = False

        # Non-required issues go to should/may
        for issue in result.issues:
            if issue.startswith("[Required]"):
                # Already in required_issues
                pass
            elif issue.startswith("[Recommended]"):
                if issue not in should_issues:
                    should_issues.append(issue)
            elif issue.startswith("[Optional]"):
                if issue not in may_issues:
                    may_issues.append(issue)

    return ValidationReport(
        base_passed=base_passed,
        isa_passed=isa_passed,
        tox_passed=tox_passed,
        required_issues=required_issues,
        should_issues=should_issues,
        may_issues=may_issues,
    )


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
            scopes to a single pass (the tox pass dominates wall-clock).

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

    try:
        # include_all_scanned=False: the auto-included scanned-file leaves (#175)
        # are plain File nodes that don't change the validation verdict, so we skip
        # them on this hot in-loop path to keep build_and_validate fast. export_crate
        # uses the default (True) so the written crate packages the whole dataset.
        metadata_doc, results = _assemble_and_validate(
            state, severity=severity, profile=profile
        )
    except Exception as e:  # noqa: BLE001 — surface as a tool error, never crash the loop
        logger.error("build_and_validate failed: %s", e)
        return {"ok": False, "conformance": {}, "issues": [], "error": str(e)}

    conformance = {r.profile: r.passed_required for r in results}
    # Every finding the validator reports is reported here. We used to set aside
    # findings whose subject was an IRI in a vocabulary namespace the crate only
    # CITES (OBO, BAO, EFO, AOP-Wiki) — the upstream validator exempts the same
    # category for schema.org/w3.org/Dublin Core but its list stops at the
    # vocabularies a *workflow* crate is built from. That was a hardcoded host
    # list: it needed an edit for every new ontology, and the need only ever
    # surfaced as findings appearing for no visible reason. Modelling propertyID
    # as a string rather than a node removed most of what it was compensating
    # for; the rest is reported, consistent with showing users what is still
    # missing rather than deciding for them that it does not count.
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

    ok = not issues
    # Keep the original routable tool shape stable. The engine receives the
    # requested severity/profile as call arguments and routes writeback from
    # those arguments rather than expanding this public result contract.
    return {"ok": ok, "conformance": conformance, "issues": issues}


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


def order_issues(issues: list[dict[str, Any]], severity: str) -> list[str]:
    """Return one severity tier as stable, layer-ordered display strings."""
    selected = [i for i in issues if i.get("severity") == severity]
    selected.sort(key=lambda i: _VALIDATION_LAYER_ORDER.get(i.get("profile") or "", 99))
    return [
        (
            f"[{i.get('profile') or '?'}] {i.get('entity_id') or '?'}: "
            f"{i.get('message') or ''}"
        ).rstrip()
        for i in selected
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
                setattr(report, _TIER_FIELDS[tier], [])
                report.assessed_tiers.discard(tier)
    # File EVERY tier the gate evaluated, not just the tier named. A gate is
    # inclusive (see `tiers_covered`), so a "recommended" run's result already
    # holds the REQUIRED findings; recording only the SHOULD ones left the
    # required list stamped fresh while describing an older validation.
    for tier in covered:
        setattr(report, _TIER_FIELDS[tier], order_issues(issues, tier))
        report.assessed_tiers.add(tier)
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
    sweep per export (the tox pass dominates); it buys a report that covers the
    whole crate.

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
