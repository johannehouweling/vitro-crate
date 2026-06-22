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
    "conformsTo": "Add `conformsTo` to `{entity}` referencing the RO-Crate 1.1 spec.",
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


def build_and_validate(
    state: CrateState,
    severity: str = "required",
    profile: str = "all",
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
    # Imported lazily (not at module top) so validate()'s ImportError handling
    # stays intact. profiles.validator installs the roc-validator bootstrap shim
    # + bundled-ISA-ontology patch on import; importing validate_crate_dict here
    # runs that shim before any rocrate_validator import is triggered. assemble_crate
    # pulls in ro-crate-py only (no rocrate_validator), so its order is immaterial.
    from builder.tools.builder import assemble_crate
    from profiles.validator import validate_crate_dict

    try:
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        metadata_doc = crate.metadata.generate()
        results = validate_crate_dict(metadata_doc, severity=severity, profile=profile)
    except Exception as e:  # noqa: BLE001 — surface as a tool error, never crash the loop
        logger.error("build_and_validate failed: %s", e)
        return {"ok": False, "conformance": {}, "issues": [], "error": str(e)}

    conformance = {r.profile: r.passed_required for r in results}
    issues: list[dict[str, Any]] = []
    for result in results:
        for issue in result.issues:
            issues.append(
                {
                    "entity_id": issue.entity_id,
                    "property": issue.property,
                    "message": issue.message,
                    "fix": _synthesize_fix(issue),
                    "severity": issue.severity,
                    "profile": issue.profile,
                }
            )

    ok = not any(result.issues for result in results)
    return {"ok": ok, "conformance": conformance, "issues": issues}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate", validate, takes_state=True)
TOOL_REGISTRY.register("build_and_validate", build_and_validate, takes_state=True)
