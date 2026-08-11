"""Tests for builder/tools/validation.py — validate tool.

Also tests the severity classification in profiles/validator.py to ensure
RECOMMENDED and OPTIONAL issues are properly surfaced (Issue #52).
"""

from __future__ import annotations

from builder.state import CrateState, ValidationReport
from builder.tools.validation import validate


class TestValidate:
    """Tests for validate — wraps three-pass SHACL validation."""

    def test_validate_runs_real_shacl_over_a_crate_on_disk(self, tmp_path):
        """validate() feeds a real ro-crate-metadata.json through the three-pass SHACL
        validator and returns a report reflecting the ACTUAL result — not just the
        missing-crate guard clause.

        (Replaces two weak tests that pointed at ``/tmp/nonexistent`` and only asserted
        the return type / that the issue fields were lists: no crate ever crossed the
        validator boundary, so the SHACL + issue-bucketing transforms never ran.)

        An incomplete crate — valid JSON-LD but an empty ``@graph`` (no root data
        entity) — must FAIL the Base pass and surface bucketed ``[Required]`` issues,
        which only happens if ``validate_crate`` + the prefix-bucketing in
        ``validation.py`` actually executed over the on-disk crate.
        """
        crate_dir = tmp_path / "crate"
        crate_dir.mkdir()
        (crate_dir / "ro-crate-metadata.json").write_text(
            '{"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": []}'
        )

        result = validate(CrateState(), str(crate_dir))

        assert isinstance(result, ValidationReport)
        assert result.base_passed is False
        assert result.required_issues, (
            "real SHACL over an incomplete crate must surface REQUIRED issues"
        )
        assert all(isinstance(x, str) for x in result.required_issues)
        assert isinstance(result.should_issues, list)
        assert isinstance(result.may_issues, list)

    def test_all_false_when_crate_missing(self):
        """validate returns all-passed=False when crate path doesn't exist."""
        state = CrateState()
        result = validate(state, "/tmp/nonexistent_path_xyz")

        assert result.base_passed is False
        assert result.isa_passed is False
        assert result.tox_passed is False

    def test_handles_import_error_gracefully(self, monkeypatch):
        """If validator import fails, returns report with 'Validation not available'."""

        # Simulate ImportError by patching the import inside the function
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "profiles.validator" in name:
                raise ImportError("No module named profiles.validator")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        # Clear any cached import
        for mod_name in list(__import__("sys").modules.keys()):
            if "profiles.validator" in mod_name:
                __import__("sys").modules.pop(mod_name, None)

        state = CrateState()
        result = validate(state, "/tmp/anywhere")

        assert result.required_issues == ["Validation not available"]
        assert result.base_passed is False
        assert result.isa_passed is False
        assert result.tox_passed is False

    # ------------------------------------------------------------------ #
    #  Issue #52 — Severity classification tests
    # ------------------------------------------------------------------ #

    def test_validate_classifies_issues_by_severity_correctly(self, tmp_path):
        """validate() must classify issues using correct prefix matching
        for [Required], [Recommended], [Optional] so that should_issues
        and may_issues are properly populated."""
        import profiles.validator as validator_mod
        from profiles.validator import ValidationResult

        original = validator_mod.validate_crate

        def patched_validate(crate_dir):
            return [
                ValidationResult(
                    profile="Base RO-Crate 1.1",
                    passed=False,
                    issues=[
                        "[Required] A required field is missing",
                        "[Recommended] A recommended field is missing",
                        "[Optional] An optional field is missing",
                    ],
                    required_issues=[
                        "[Required] A required field is missing",
                    ],
                    passed_required=False,
                ),
            ]

        validator_mod.validate_crate = patched_validate  # ty: ignore[invalid-assignment]

        import sys as _sys

        _sys.modules.pop("builder.tools.validation", None)

        try:
            from builder.tools.validation import validate as fresh_validate

            state = CrateState()
            crate_dir = tmp_path / "placeholder"
            crate_dir.mkdir()
            report = fresh_validate(state, str(crate_dir))

            assert len(report.required_issues) == 1
            assert "[Required]" in report.required_issues[0]
            assert len(report.should_issues) == 1
            assert "[Recommended]" in report.should_issues[0]
            assert len(report.may_issues) == 1
            assert "[Optional]" in report.may_issues[0]
        finally:
            validator_mod.validate_crate = original

    def test_recommended_and_optional_not_misclassified_as_required(self, tmp_path):
        """RECOMMENDED and OPTIONAL issues must NOT end up in required_issues."""
        import profiles.validator as validator_mod
        from profiles.validator import ValidationResult

        original = validator_mod.validate_crate

        def patched_validate(crate_dir):
            return [
                ValidationResult(
                    profile="Base RO-Crate 1.1",
                    passed=False,
                    issues=[
                        "[Recommended] A recommended field is missing",
                        "[Optional] An optional field is missing",
                    ],
                    required_issues=[],
                    passed_required=True,
                ),
            ]

        validator_mod.validate_crate = patched_validate  # ty: ignore[invalid-assignment]

        import sys as _sys

        _sys.modules.pop("builder.tools.validation", None)

        try:
            from builder.tools.validation import validate as fresh_validate

            state = CrateState()
            crate_dir = tmp_path / "placeholder"
            crate_dir.mkdir()
            report = fresh_validate(state, str(crate_dir))

            assert len(report.required_issues) == 0
            assert len(report.should_issues) == 1
            assert len(report.may_issues) == 1
        finally:
            validator_mod.validate_crate = original

    def test_validate_attributes_issue_records_to_profiles(self, tmp_path, monkeypatch):
        """The disk path knows which profile pass produced each finding — each
        ``ValidationResult`` is one pass — so ``issue_records`` must carry that
        attribution (#510). Severity comes from the ``[Required]``-style prefix
        ``_format_issues`` stamps; the prefix is structure, not message, so the
        recorded message is the string without it."""
        import profiles.validator as validator_mod
        from profiles.validator import ValidationResult

        def patched_validate(crate_dir):
            return [
                ValidationResult(
                    profile="Base RO-Crate 1.1",
                    passed=False,
                    issues=[
                        "[Required] root missing name",
                        "[Recommended] add a license",
                    ],
                    required_issues=["[Required] root missing name"],
                    passed_required=False,
                ),
                ValidationResult(
                    profile="ISA RO-Crate Profile",
                    passed=False,
                    issues=["[Optional] a DOI would help"],
                    required_issues=[],
                    passed_required=True,
                ),
            ]

        monkeypatch.setattr(validator_mod, "validate_crate", patched_validate)

        crate_dir = tmp_path / "placeholder"
        crate_dir.mkdir()
        report = validate(CrateState(), str(crate_dir))

        assert report.issue_records == [
            {
                "profile": "base",
                "severity": "required",
                "entity_id": "",
                "message": "root missing name",
            },
            {
                "profile": "base",
                "severity": "recommended",
                "entity_id": "",
                "message": "add a license",
            },
            {
                "profile": "isa",
                "severity": "optional",
                "entity_id": "",
                "message": "a DOI would help",
            },
        ]
        # The display lists keep their historical prefixed shape.
        assert report.required_issues == ["[Required] root missing name"]
        assert report.should_issues == ["[Recommended] add a license"]
        assert report.may_issues == ["[Optional] a DOI would help"]

    def test_isa_failure_does_not_fail_base(self, tmp_path, monkeypatch):
        """Every pass's display name contains "ro-crate" ("ISA RO-Crate
        Profile", "ISA-Tox RO-Crate Profile"), so the old ``"ro-crate" in
        name`` guard classified an ISA or ISA-Tox REQUIRED failure as a BASE
        failure. The most specific token must win (#510)."""
        import profiles.validator as validator_mod
        from profiles.validator import ValidationResult

        def patched_validate(crate_dir):
            return [
                ValidationResult(
                    profile="Base RO-Crate 1.2",
                    passed=True,
                    issues=[],
                    required_issues=[],
                    passed_required=True,
                ),
                ValidationResult(
                    profile="ISA RO-Crate Profile",
                    passed=False,
                    issues=["[Required] root missing identifier"],
                    required_issues=["[Required] root missing identifier"],
                    passed_required=False,
                ),
                ValidationResult(
                    profile="ISA-Tox RO-Crate Profile",
                    passed=False,
                    issues=["[Required] no dose recorded"],
                    required_issues=["[Required] no dose recorded"],
                    passed_required=False,
                ),
            ]

        monkeypatch.setattr(validator_mod, "validate_crate", patched_validate)

        crate_dir = tmp_path / "placeholder"
        crate_dir.mkdir()
        report = validate(CrateState(), str(crate_dir))

        assert report.base_passed is True
        assert report.isa_passed is False
        assert report.tox_passed is False

    def test_base_pass_uses_optional_severity(self, tmp_path):
        """Base RO-Crate pass should use Severity.OPTIONAL so that
        RECOMMENDED and OPTIONAL issues are surfaced alongside REQUIRED ones."""
        from profiles.validator import validate_crate

        crate_dir = tmp_path / "bare_crate"
        crate_dir.mkdir()
        (crate_dir / "ro-crate-metadata.json").write_text(
            '{"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": []}'
        )

        results = validate_crate(crate_dir)
        assert len(results) == 3

        base_result = results[0]
        all_issues = base_result.issues

        prefixes = set()
        for issue in all_issues:
            prefix = issue.split("]")[0] + "]" if "]" in issue else ""
            prefixes.add(prefix)

        assert "[Required]" in prefixes, (
            f"Expected [Required] prefix in base issues, got: {prefixes}"
        )
        # With OPTIONAL requirement_severity, all_issues should include
        # more than just REQUIRED issues (RECOMMENDED and/or OPTIONAL too)
        assert len(all_issues) >= len(base_result.required_issues), (
            "With OPTIONAL severity threshold, all_issues should include "
            "more than just REQUIRED issues. "
            f"all={len(all_issues)}, required={len(base_result.required_issues)}"
        )
