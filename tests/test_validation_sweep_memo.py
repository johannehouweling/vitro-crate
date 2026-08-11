"""One SHACL sweep per state, whatever gate asks for it.

The gate is a floor, not a filter: an OPTIONAL sweep already evaluated the
REQUIRED and RECOMMENDED checks, so answering a later "required" question from
it is a list filter rather than another 20-40 seconds of SHACL.

A profiled run showed why this matters. `export_crate` validates at OPTIONAL via
`ensure_validated`, and twice the agent immediately asked for a RECOMMENDED sweep
of the same untouched crate — 41.1s and 47.7s of pure re-computation. The engine's
own debounce (Issue #155) could not help: it keys on the exact severity, and
`ensure_validated` calls `build_and_validate` directly rather than through engine
dispatch, so the export's sweep was never in that cache at all. This memo sits on
the function, so it covers both callers.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from builder.tools import validation as V
from builder.tools.drafters import draft_investigation
from profiles.validator import DictValidationResult, RoutableIssue


def _issue(severity: str, entity: str = "./#thing") -> RoutableIssue:
    return RoutableIssue(
        entity_id=entity,
        property="http://schema.org/name",
        property_value=None,
        message=f"a {severity} finding",
        severity=severity,
        check_id="ro-crate-1.2_1.1",
        profile="base",
    )


def _results(*severities: str) -> list[DictValidationResult]:
    issues = [_issue(s) for s in severities]
    return [
        DictValidationResult(
            profile="base",
            passed=not issues,
            passed_required=not any(i.severity == "required" for i in issues),
            issues=issues,
        )
    ]


@pytest.fixture
def sweeps(monkeypatch):
    """Count real sweeps, and let each test say what the validator returns."""
    V.clear_sweep_memo()
    calls: list[str] = []
    box = {"results": _results("required", "recommended", "optional")}

    def fake(state, *, severity, profile):
        calls.append(severity)
        return {}, box["results"]

    monkeypatch.setattr(V, "_assemble_and_validate", fake)
    yield calls, box
    V.clear_sweep_memo()


@pytest.fixture
def state():
    st = CrateState()
    draft_investigation(st, {"name": "T", "description": "D"})
    return st


class TestSweepReuse:
    def test_narrower_gate_is_served_from_a_wider_sweep(self, sweeps, state):
        """The export path's OPTIONAL sweep answers a later RECOMMENDED ask."""
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional")
        V.build_and_validate(state, severity="recommended")
        V.build_and_validate(state, severity="required")
        assert calls == ["optional"]

    def test_wider_gate_still_runs(self, sweeps, state):
        """A REQUIRED sweep has not evaluated the OPTIONAL checks."""
        calls, _ = sweeps
        V.build_and_validate(state, severity="required")
        V.build_and_validate(state, severity="optional")
        assert calls == ["required", "optional"]

    def test_a_changed_crate_is_swept_again(self, sweeps, state):
        """The memo keys on the validation fingerprint, so an edit busts it."""
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional")
        draft_investigation(state, {"name": "different", "description": "changed"})
        V.build_and_validate(state, severity="required")
        assert calls == ["optional", "required"]

    def test_a_narrower_profile_is_served_from_all(self, sweeps, state):
        """ "all" runs base + isa + tox, so it answers any single-profile ask.

        This test used to assert the opposite. The memo was keyed on the profile,
        so "all" and "base" looked like unrelated questions — and a profiled
        session paid for six consecutive profile="base" sweeps immediately after
        an profile="all" sweep of the identical state, none of which could hit.
        """
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional", profile="all")
        V.build_and_validate(state, severity="required", profile="tox")
        V.build_and_validate(state, severity="required", profile="base")
        assert calls == ["optional"]

    def test_a_wider_profile_still_runs(self, sweeps, state):
        """A base-only sweep never ran the isa or tox passes."""
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional", profile="base")
        V.build_and_validate(state, severity="optional", profile="all")
        assert calls == ["optional", "optional"]

    def test_disjoint_profiles_each_run(self, sweeps, state):
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional", profile="base")
        V.build_and_validate(state, severity="optional", profile="tox")
        assert calls == ["optional", "optional"]

    def test_an_unknown_profile_never_matches(self, sweeps, state):
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional", profile="all")
        assert V._scope_covers("all", "bse") is False
        assert calls == ["optional"]


class TestGateFiltering:
    def test_reused_sweep_reports_only_the_gated_tiers(self, sweeps, state):
        calls, _ = sweeps
        wide = V.build_and_validate(state, severity="optional")
        assert {i["severity"] for i in wide["issues"]} == {
            "required",
            "recommended",
            "optional",
        }

        narrow = V.build_and_validate(state, severity="required")
        assert calls == ["optional"]
        assert {i["severity"] for i in narrow["issues"]} == {"required"}
        assert narrow["ok"] is False

        mid = V.build_and_validate(state, severity="recommended")
        assert {i["severity"] for i in mid["issues"]} == {"required", "recommended"}

    def test_ok_is_true_when_the_gated_tiers_are_clean(self, sweeps, state):
        calls, box = sweeps
        box["results"] = _results("recommended", "optional")
        V.build_and_validate(state, severity="optional")
        narrow = V.build_and_validate(state, severity="required")
        assert calls == ["optional"]
        assert narrow["issues"] == []
        assert narrow["ok"] is True

    def test_an_unrecognised_severity_is_never_swallowed(self, sweeps, state):
        """`_routable_issue` falls back to the raw enum name for anything outside
        the three tiers. Filtering must not make those vanish — findings that
        disappear for no visible reason are the failure this project just fixed.
        """
        calls, box = sweeps
        box["results"] = _results("required", "violation")
        V.build_and_validate(state, severity="optional")
        narrow = V.build_and_validate(state, severity="required")
        assert calls == ["optional"]
        assert {i["severity"] for i in narrow["issues"]} == {"required", "violation"}


class TestMemoHygiene:
    def test_an_unknown_severity_does_not_match_a_cached_sweep(self, sweeps, state):
        """`tiers_covered` returns () for a typo, which must not read as 'covered'."""
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional")
        assert V._gate_covers("optional", "recomended") is False  # codespell:ignore
        assert calls == ["optional"]

    def test_memo_is_bounded(self, sweeps):
        calls, _ = sweeps
        for n in range(V._SWEEP_MEMO_MAX + 3):
            st = CrateState()
            draft_investigation(st, {"name": f"crate {n}", "description": "D"})
            V.build_and_validate(st, severity="optional")
        assert len(V._SWEEP_MEMO) <= V._SWEEP_MEMO_MAX
        assert len(calls) == V._SWEEP_MEMO_MAX + 3

    def test_clear_forgets_everything(self, sweeps, state):
        calls, _ = sweeps
        V.build_and_validate(state, severity="optional")
        V.clear_sweep_memo()
        V.build_and_validate(state, severity="optional")
        assert calls == ["optional", "optional"]

    def test_a_failed_sweep_is_not_remembered(self, monkeypatch, state):
        """An error result must not be served to the next caller as a verdict."""
        V.clear_sweep_memo()
        calls: list[str] = []

        def boom(state, *, severity, profile):
            calls.append(severity)
            raise RuntimeError("assembly exploded")

        monkeypatch.setattr(V, "_assemble_and_validate", boom)
        first = V.build_and_validate(state, severity="optional")
        assert "error" in first
        V.build_and_validate(state, severity="required")
        assert calls == ["optional", "required"]
        V.clear_sweep_memo()


class TestScoping:
    """A served answer must be indistinguishable from a real run at that scope."""

    def test_only_the_asked_for_passes_are_reported(self, monkeypatch, state):
        V.clear_sweep_memo()
        calls: list[str] = []

        def fake(state, *, severity, profile):
            calls.append(profile)
            issues = [_issue("required"), _issue("required")]
            issues[1].profile = "tox"
            return {}, [
                DictValidationResult(
                    profile="base", passed=False, passed_required=False, issues=[issues[0]]
                ),
                DictValidationResult(
                    profile="tox", passed=False, passed_required=False, issues=[issues[1]]
                ),
            ]

        monkeypatch.setattr(V, "_assemble_and_validate", fake)
        wide = V.build_and_validate(state, severity="optional", profile="all")
        assert {i["profile"] for i in wide["issues"]} == {"base", "tox"}
        assert set(wide["conformance"]) == {"base", "tox"}

        narrow = V.build_and_validate(state, severity="optional", profile="base")
        assert calls == ["all"]
        assert {i["profile"] for i in narrow["issues"]} == {"base"}
        assert set(narrow["conformance"]) == {"base"}, (
            "a base-scoped caller must not be handed tox conformance it never asked for"
        )
        V.clear_sweep_memo()
