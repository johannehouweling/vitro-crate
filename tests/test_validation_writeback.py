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
