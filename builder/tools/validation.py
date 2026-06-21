"""Tool that wraps the three-pass SHACL validation from profiles/validator.py.

Translates the list[ValidationResult] into a single ValidationReport dataclass
with per-pass pass/fail and categorized issues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from builder.state import CrateState, ValidationReport

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
        from profiles.validator import validate_crate, ValidationResult
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
            if issue.startswith("[REQUIRED]"):
                # Already in required_issues
                pass
            elif issue.startswith("[SHOULD]"):
                if issue not in should_issues:
                    should_issues.append(issue)
            elif issue.startswith("[MAY]"):
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
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402

TOOL_REGISTRY.register("validate", validate, takes_state=True)