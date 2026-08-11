"""Tests for build_and_validate — in-memory build + routable validation (Issue #87).

build_and_validate assembles the crate from CrateState in memory and validates
the generated JSON-LD document directly (no disk round-trip), returning issues
keyed to the entity/property that failed so the agent can route a fix.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.validation import build_and_validate


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(
        Entity(
            entity_id="proc_exp",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": "Exposure step"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


class TestBuildAndValidateShape:
    def test_returns_routable_shape(self):
        report = build_and_validate(CrateState())
        # `citations` joined the contract: findings about vocabulary the crate
        # CITES are reported separately from findings about the crate itself, so
        # `issues` is what somebody can act on and nothing is dropped silently.
        assert set(report.keys()) == {"ok", "conformance", "issues", "citations"}
        assert isinstance(report["citations"], list)
        assert isinstance(report["ok"], bool)
        assert isinstance(report["conformance"], dict)
        assert set(report["conformance"]) == {"base", "isa", "tox"}
        assert all(isinstance(v, bool) for v in report["conformance"].values())
        assert isinstance(report["issues"], list)

    def test_issue_entries_have_routable_keys(self):
        report = build_and_validate(CrateState())
        assert report["issues"], "minimal crate should surface at least one issue"
        for issue in report["issues"]:
            assert set(issue.keys()) == {
                "entity_id",
                "property",
                "message",
                "fix",
                "severity",
                "profile",
            }


class TestBuildAndValidateRouting:
    def test_surfaces_root_identifier_violation(self):
        """A minimal crate misses schema:identifier on the root (ISA REQUIRED)."""
        report = build_and_validate(CrateState())
        matches = [
            i
            for i in report["issues"]
            if i["entity_id"] == "./"
            and i["property"]
            and i["property"].endswith("identifier")
        ]
        assert matches, report["issues"]
        issue = matches[0]
        assert issue["profile"] == "isa"
        assert issue["severity"] == "required"
        assert issue["fix"], "a synthesized fix hint must be present"

    def test_conformance_base_passes_isa_fails_for_minimal(self):
        report = build_and_validate(CrateState())
        assert report["conformance"]["base"] is True
        assert report["conformance"]["isa"] is False
        assert report["ok"] is False


class TestBuildAndValidateNoDisk:
    def test_writes_no_files_for_minimal(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        build_and_validate(CrateState())
        assert list(tmp_path.iterdir()) == []

    def test_writes_no_files_with_exposure(self, tmp_path, monkeypatch):
        """The only disk-writing build path (Exposure condition table) stays in memory."""
        monkeypatch.chdir(tmp_path)
        build_and_validate(_exposure_state())
        assert list(tmp_path.iterdir()) == []


class TestBuildAndValidateScoping:
    def test_profile_scope_runs_single_pass(self):
        report = build_and_validate(CrateState(), profile="base")
        assert set(report["conformance"]) == {"base"}
        assert all(i["profile"] == "base" for i in report["issues"])

    def test_profile_scope_base_is_ok(self):
        """Base scope on a minimal crate is conformant -> ok True, no issues."""
        report = build_and_validate(CrateState(), profile="base")
        assert report["ok"] is True
        assert report["issues"] == []

    def test_required_severity_only_required_issues(self):
        report = build_and_validate(CrateState(), severity="required")
        assert all(i["severity"] == "required" for i in report["issues"])

    def test_invalid_severity_surfaced_as_error(self):
        """A bad severity is surfaced as a tool error, not a silent false pass."""
        report = build_and_validate(CrateState(), severity="bogus")
        assert report["ok"] is False
        assert "error" in report

    def test_none_args_fall_back_to_defaults(self):
        """Weak models pass null for optional args explicitly; treat None as default.

        DeepSeek-flash calls build_and_validate(severity=None, profile=None) rather
        than omitting them, so None must behave like the defaults (all/required) and
        NOT raise 'Unknown profile None'.
        """
        explicit_none = build_and_validate(CrateState(), severity=None, profile=None)
        defaults = build_and_validate(CrateState())
        assert "error" not in explicit_none
        assert explicit_none["conformance"] == defaults["conformance"]
        assert set(explicit_none["conformance"]) == {"base", "isa", "tox"}
