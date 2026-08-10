"""Tests for validation write-back to state.validation (Issue #153).

`build_and_validate` / `validate` compute a verdict but historically dropped it:
nothing ever assigned ``state.validation``, so ``get_hint``, the interactive
header, and the maturity report (#150 renders *from* ``state.validation``) all
read stale defaults. These tests pin the fix: ``AgentEngine.run_tool`` folds a
validation result back into ``state.validation`` (orchestration layer, mirroring
the existing scan_files write-back), and the next REQUIRED fix is surfaced in the
per-turn state brief so the weak model stops re-deriving the plan every turn.
"""

from __future__ import annotations

from builder.engine import AgentEngine, _order_required_issues
from builder.state import ValidationReport


class TestValidationWriteBack:
    def test_build_and_validate_writes_conformance_to_state(self):
        engine = AgentEngine()
        engine.initialize()
        assert engine.state.validation.base_passed is False  # default before any validate
        engine.run_tool("build_and_validate", profile="base")
        assert engine.state.validation.base_passed is True

    def test_build_and_validate_all_populates_required_issues(self):
        """A minimal crate passes BASE but fails ISA (missing root identifier);
        the ISA REQUIRED issues must land in state.validation."""
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate")  # profile defaults to "all"
        v = engine.state.validation
        assert v.base_passed is True
        assert v.isa_passed is False
        assert v.required_issues, "ISA REQUIRED issues should be recorded in state"

    def test_error_result_does_not_clobber_required_issues(self):
        """A failed/errored validate must not wipe previously known issues."""
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate")  # populates required_issues
        before = list(engine.state.validation.required_issues)
        assert before
        engine.run_tool("build_and_validate", severity="bogus")  # error path
        assert engine.state.validation.required_issues == before

    def test_get_hint_reflects_validation_after_writeback(self):
        """get_hint reads state.validation; before #153 it was never populated,
        so it could not reflect a real validation. Drive the real write-back with
        a synthetic build_and_validate result (deterministic, no SHACL needed)
        and confirm get_hint now surfaces the recorded REQUIRED issue."""
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("draft_investigation", hints={"name": "X"})  # >=1 entity
        engine._writeback_validation(
            "build_and_validate",
            {
                "ok": False,
                "conformance": {"base": True, "isa": False, "tox": False},
                "issues": [
                    {
                        "severity": "required",
                        "profile": "isa",
                        "entity_id": "./",
                        "message": "missing identifier",
                    }
                ],
            },
        )
        from builder.tools.session import get_hint

        hint = get_hint(engine.state)
        assert "Fix REQUIRED validation issues" in hint
        assert "missing identifier" in hint


class TestOrderRequiredIssues:
    def test_orders_base_isa_tox_and_drops_non_required(self):
        issues = [
            {"severity": "required", "profile": "tox", "entity_id": "#m", "message": "tox issue"},
            {"severity": "required", "profile": "base", "entity_id": "./", "message": "base issue"},
            {"severity": "recommended", "profile": "base", "entity_id": "./", "message": "drop me"},
            {"severity": "required", "profile": "isa", "entity_id": "./", "message": "isa issue"},
        ]
        out = _order_required_issues(issues)
        assert len(out) == 3  # the recommended issue is dropped
        assert "base issue" in out[0]
        assert "isa issue" in out[1]
        assert "tox issue" in out[2]

    def test_empty_in_empty_out(self):
        assert _order_required_issues([]) == []


class TestStateBriefNextFix:
    def test_brief_includes_next_required_fix(self):
        from builder.agents.react.agent_loop import _build_system_prompt_with_state

        brief = _build_system_prompt_with_state(
            session_id="s",
            entity_count=1,
            file_count=0,
            iteration_count=3,
            next_fix="[isa] ./: add identifier",
        )
        assert "Next REQUIRED fix" in brief
        assert "add identifier" in brief

    def test_brief_omits_next_fix_when_none(self):
        from builder.agents.react.agent_loop import _build_system_prompt_with_state

        brief = _build_system_prompt_with_state(
            session_id="s",
            entity_count=1,
            file_count=0,
            iteration_count=3,
        )
        assert "Next REQUIRED fix" not in brief


class TestValidationFreshness:
    """A verdict is only meaningful next to the crate it judged (#153).

    The agent keeps editing after validating, so a verdict recorded a few tool
    calls ago can describe a crate that no longer exists. Every write-back stamps
    the state's ``validation_fingerprint`` so staleness is answerable later —
    without it the exported maturity report ships a green "Conformant" for a
    state nobody checked.
    """

    def _state(self):
        from tests.fixtures.vhps_golden_crates import vhps_fixture_state

        return vhps_fixture_state("S-VHPS21")

    def test_writeback_stamps_the_fingerprint(self) -> None:
        from builder.tools.validation import apply_validation_result

        state = self._state()
        assert state.validation.input_fingerprint == ""
        apply_validation_result(
            state,
            "build_and_validate",
            {"ok": True, "conformance": {"base": True, "isa": True, "tox": True}, "issues": []},
        )
        assert state.validation.input_fingerprint == state.validation_fingerprint()
        assert state.validation.is_stale_for(state) is False

    def test_verdict_goes_stale_when_the_crate_changes(self) -> None:
        from builder.tools.validation import apply_validation_result

        state = self._state()
        apply_validation_result(
            state,
            "build_and_validate",
            {"ok": True, "conformance": {"base": True, "isa": True, "tox": True}, "issues": []},
        )
        state.metadata.title = "Edited after validating"
        assert state.validation.is_stale_for(state) is True

    def test_unstamped_verdict_is_not_reported_stale(self) -> None:
        # A report restored from an older checkpoint predates the stamp;
        # downgrading every one of those to "stale" would be a false alarm.
        state = self._state()
        state.validation = ValidationReport(base_passed=True, isa_passed=True, tox_passed=True)
        assert state.validation.input_fingerprint == ""
        assert state.validation.is_stale_for(state) is False

    def test_fingerprint_round_trips_through_serialisation(self) -> None:
        state = self._state()
        report = ValidationReport(base_passed=True, input_fingerprint="abc123")
        assert ValidationReport.from_dict(report.to_dict()).input_fingerprint == "abc123"
        # …and a checkpoint written before the field existed still loads.
        legacy = report.to_dict()
        del legacy["input_fingerprint"]
        assert ValidationReport.from_dict(legacy).input_fingerprint == ""
        assert ValidationReport.from_dict(legacy).is_stale_for(state) is False

    def test_engine_writeback_stamps_too(self) -> None:
        # The engine delegates to the same mapping, so a verdict reached through
        # the tool loop and one reached by export_crate carry the same stamp.
        engine = AgentEngine(state=self._state())
        engine._writeback_validation(
            "build_and_validate",
            {"ok": True, "conformance": {"base": True, "isa": True, "tox": True}, "issues": []},
            severity="required",
        )
        assert engine.state.validation.input_fingerprint == (
            engine.state.validation_fingerprint()
        )


class TestEnsureValidated:
    """``ensure_validated`` re-validates only when the recorded verdict is stale."""

    def _state(self):
        from tests.fixtures.vhps_golden_crates import vhps_fixture_state

        return vhps_fixture_state("S-VHPS21")

    def _stub(self, monkeypatch, calls: list) -> None:
        """Replace the SHACL pass so the DECISION is tested, not the validator."""
        import builder.tools.validation as validation

        def _fake(state, severity="required", profile="all"):
            calls.append(severity)
            return {
                "ok": True,
                "conformance": {"base": True, "isa": True, "tox": True},
                "issues": [],
            }

        monkeypatch.setattr(validation, "build_and_validate", _fake)

    def test_runs_when_never_validated(self, monkeypatch) -> None:
        from builder.tools.validation import ensure_validated

        calls: list = []
        self._stub(monkeypatch, calls)
        info = ensure_validated(self._state())
        assert info == {"ran": True, "reason": "never-validated", "ok": True, "error": None}
        assert calls == ["required"]

    def test_skips_when_the_verdict_is_current(self, monkeypatch) -> None:
        from builder.tools.validation import ensure_validated

        state = self._state()
        calls: list = []
        self._stub(monkeypatch, calls)
        ensure_validated(state)
        info = ensure_validated(state)
        assert info["ran"] is False and info["reason"] == "fresh"
        assert calls == ["required"], "re-validated an unchanged crate"

    def test_runs_again_once_the_crate_changes(self, monkeypatch) -> None:
        from builder.tools.validation import ensure_validated

        state = self._state()
        calls: list = []
        self._stub(monkeypatch, calls)
        ensure_validated(state)
        state.metadata.title = "Edited"
        info = ensure_validated(state)
        assert info["ran"] is True and info["reason"] == "stale"
        assert calls == ["required", "required"]

    def test_validator_failure_is_reported_not_raised(self, monkeypatch) -> None:
        # export_crate must still write the crate; the report says the verdict
        # could not be established.
        import builder.tools.validation as validation
        from builder.tools.validation import ensure_validated

        def _boom(state, severity="required", profile="all"):
            raise RuntimeError("shapes graph unavailable")

        monkeypatch.setattr(validation, "build_and_validate", _boom)
        info = ensure_validated(self._state())
        assert info["ran"] is False
        assert "shapes graph unavailable" in info["error"]
