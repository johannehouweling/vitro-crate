"""Tests for builder/tools/validation.py — validate tool."""

from __future__ import annotations

from pathlib import Path

from builder.state import CrateState, ValidationReport
from builder.tools.validation import validate


class TestValidate:
    """Tests for validate — wraps three-pass SHACL validation."""

    def test_returns_validation_report(self):
        """validate returns a ValidationReport dataclass."""
        state = CrateState()
        result = validate(state, "/tmp/nonexistent")

        assert isinstance(result, ValidationReport)

    def test_all_false_when_crate_missing(self):
        """validate returns all-passed=False when crate path doesn't exist."""
        state = CrateState()
        result = validate(state, "/tmp/nonexistent_path_xyz")

        assert result.base_passed is False
        assert result.isa_passed is False
        assert result.tox_passed is False

    def test_returns_issues_list(self):
        """validate returns issue lists (possibly empty)."""
        state = CrateState()
        result = validate(state, "/tmp/missing")

        assert isinstance(result.required_issues, list)
        assert isinstance(result.should_issues, list)
        assert isinstance(result.may_issues, list)

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