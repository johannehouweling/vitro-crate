"""
Thin wrapper around rocrate_validator that runs the three-pass ISA-Tox
validation and returns structured, plain-English results.

Passes:
  1. Base RO-Crate 1.1   -> bundled ``ro-crate`` profile
  2. ISA RO-Crate        -> bundled ``isa-ro-crate`` profile (roc-validator >=0.10)
  3. ISA-Tox RO-Crate    -> our ``tox-ro-crate`` profile, loaded from SHAPES_DIR
                            via ``extra_profiles_path`` and composed on top of the
                            bundled ``isa-ro-crate`` it ``isProfileOf``.

The tox pass passes ``profiles_path=DEFAULT_PROFILES_PATH`` (the bundled dir)
*and* ``extra_profiles_path=SHAPES_DIR`` so the inheritance chain
tox-ro-crate -> isa-ro-crate -> ro-crate resolves; ``extra_profiles_path``
composition is verified working as of roc-validator 0.10.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rocrate_validator import models, services
from rocrate_validator.services import DEFAULT_PROFILES_PATH

# src/rocrate_wizard/core/validator.py -> repo root is four parents up.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SHAPES_DIR = REPO_ROOT / "profiles" / "shapes"


def _patch_bundled_isa_ontology() -> None:
    """Work around an upstream syntax bug in roc-validator 0.10.0.

    The bundled ``isa-ro-crate/ontology.ttl`` is missing the terminating ``.``
    after ``isa-ro-crate:Data … rdfs:label "Data"@en``, which makes rdflib raise
    a ``BadSyntax`` when the ISA-Tox pass loads the inherited ISA ontology graph.
    We never hit it before because we used to ship our own ISA shapes; consuming
    the bundled profile exposes it. This idempotently inserts the missing period
    so a fresh ``uv sync`` / reinstall self-heals on first validate. Reported
    upstream: https://github.com/crs4/rocrate-validator/issues
    """
    try:
        onto = Path(DEFAULT_PROFILES_PATH) / "isa-ro-crate" / "ontology.ttl"
        text = onto.read_text(encoding="utf-8")
        bad = 'rdfs:label "Data"@en'
        if bad in text and f"{bad} ." not in text:
            onto.write_text(text.replace(bad, f"{bad} .", 1), encoding="utf-8")
    except OSError:
        # Read-only install or missing file: nothing we can do here; validation
        # will surface the underlying parse error if the bug is still present.
        pass


_patch_bundled_isa_ontology()


@dataclass
class ValidationResult:
    profile: str
    passed: bool                                       # no issues at ANY severity
    issues: list[str] = field(default_factory=list)    # plain-English, all severities
    required_issues: list[str] = field(default_factory=list)  # REQUIRED-severity only
    passed_required: bool = True                       # no REQUIRED-severity issues


def validate_crate(crate_dir: Path) -> list[ValidationResult]:
    """Run all three validation passes against crate_dir.

    Returns one ValidationResult per pass in order:
      1. Base RO-Crate 1.1
      2. ISA RO-Crate Profile
      3. ISA-Tox RO-Crate Profile
    """
    results: list[ValidationResult] = []

    # --- Pass 1: base RO-Crate 1.1 ---
    settings = services.ValidationSettings(
        rocrate_uri=crate_dir,
        profile_identifier="ro-crate-1.1",
        requirement_severity=models.Severity.REQUIRED,
    )
    result = services.validate(settings)
    results.append(
        ValidationResult(
            profile="Base RO-Crate 1.1",
            passed=not result.has_issues(),
            issues=_format_issues(result),
            required_issues=_format_issues(result, models.Severity.REQUIRED),
            passed_required=not result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    # --- Pass 2: ISA profile (bundled upstream isa-ro-crate) ---
    # disable_inherited_profiles_issue_reporting: isa-ro-crate is-profile-of
    # ro-crate-1.1, and rocrate_validator re-reports the inherited base-profile
    # checks here. Those duplicates are false positives (they pass cleanly in the
    # standalone base pass above), so we suppress inherited reporting and let each
    # pass cover only its own layer. Base coverage is NOT lost — pass 1 owns it.
    isa_settings = services.ValidationSettings(
        rocrate_uri=crate_dir,
        profile_identifier="isa-ro-crate",
        requirement_severity=models.Severity.OPTIONAL,
        disable_inherited_profiles_issue_reporting=True,
    )
    isa_result = services.validate(isa_settings)
    results.append(
        ValidationResult(
            profile="ISA RO-Crate Profile",
            passed=not isa_result.has_issues(),
            issues=_format_issues(isa_result),
            required_issues=_format_issues(isa_result, models.Severity.REQUIRED),
            passed_required=not isa_result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    # --- Pass 3: ISA-Tox profile (our extension on top of bundled isa-ro-crate) ---
    # profiles_path=DEFAULT (bundled) + extra_profiles_path=SHAPES_DIR so the
    # tox-ro-crate -> isa-ro-crate -> ro-crate inheritance chain resolves.
    # Same inherited-reporting suppression as the ISA pass; this pass reports only
    # tox-specific shapes.
    tox_settings = services.ValidationSettings(
        rocrate_uri=crate_dir,
        profiles_path=DEFAULT_PROFILES_PATH,
        extra_profiles_path=SHAPES_DIR,
        profile_identifier="tox-ro-crate",
        requirement_severity=models.Severity.OPTIONAL,
        disable_inherited_profiles_issue_reporting=True,
    )
    tox_result = services.validate(tox_settings)
    results.append(
        ValidationResult(
            profile="ISA-Tox RO-Crate Profile",
            passed=not tox_result.has_issues(),
            issues=_format_issues(tox_result),
            required_issues=_format_issues(tox_result, models.Severity.REQUIRED),
            passed_required=not tox_result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    return results


def _format_issues(result, min_severity=None) -> list[str]:
    """Return human-readable issue strings (severity + message, no SHACL IDs).

    When ``min_severity`` is given, only issues at that severity or higher are
    returned (used to express a REQUIRED-only validation gate).
    """
    kwargs = {"min_severity": min_severity} if min_severity is not None else {}
    issues = []
    for issue in result.get_issues(**kwargs):
        severity = issue.severity.name.capitalize()
        message = issue.message or str(issue)
        issues.append(f"[{severity}] {message}")
    return issues
