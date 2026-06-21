"""Tests for the explicit ToolRegistry replacing dir()-based discovery."""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.tools.registry import ToolRegistry


class TestToolRegistry:
    """Unit tests for the ToolRegistry container."""

    def test_register_and_get_returns_callable(self):
        reg = ToolRegistry()

        def my_tool() -> str:
            return "ok"

        reg.register("my_tool", my_tool, description="does a thing")
        assert reg.get("my_tool") is my_tool

    def test_get_unknown_raises_key_error(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")

    def test_list_returns_sorted_names(self):
        reg = ToolRegistry()
        reg.register("beta", lambda: None)
        reg.register("alpha", lambda: None)
        assert reg.list() == ["alpha", "beta"]

    def test_all_returns_full_specs(self):
        reg = ToolRegistry()

        def t(state) -> None:  # noqa: ARG001
            ...

        reg.register("t", t, description="desc", takes_state=True)
        spec = reg.all()["t"]
        assert spec.fn is t
        assert spec.description == "desc"
        assert spec.takes_state is True

    def test_takes_state_defaults_to_false(self):
        reg = ToolRegistry()
        reg.register("t", lambda: None)
        assert reg.all()["t"].takes_state is False

    def test_contains(self):
        reg = ToolRegistry()
        reg.register("t", lambda: None)
        assert "t" in reg
        assert "missing" not in reg


class TestToolModulesRegister:
    """Tool modules register their public tools into the engine registry."""

    def test_registry_contains_expected_tools(self):
        reg = AgentEngine._build_registry()
        for name in (
            "draft_investigation",
            "build_crate",
            "validate",
            "assess_mit_coverage",
            "assess_fair_maturity",
            "lookup_compound",
            "save_session",
            "list_entities",
            "verify_identifier",
        ):
            assert name in reg, f"{name} should be registered"

    def test_state_passing_is_declared_not_introspected(self):
        specs = AgentEngine._build_registry().all()
        assert specs["draft_investigation"].takes_state is True
        assert specs["build_crate"].takes_state is True
        assert specs["save_session"].takes_state is True
        assert specs["lookup_compound"].takes_state is False
        assert specs["list_sessions"].takes_state is False


class TestEngineUsesRegistry:
    """AgentEngine routes tool execution through the explicit registry."""

    def test_run_tool_state_tool_executes_with_state(self):
        engine = AgentEngine()
        result = engine.run_tool("draft_investigation", hints={"name": "Inv"})
        assert result is not None

    def test_run_tool_unknown_raises_value_error(self):
        engine = AgentEngine()
        with pytest.raises(ValueError):
            engine.run_tool("definitely_not_a_tool")

    def test_get_available_tools_excludes_leaked_classes(self):
        tools = AgentEngine().get_available_tools()
        assert "draft_investigation" in tools
        assert "scan_files" in tools
        # dir()-based discovery used to leak imported classes/typing aliases:
        for junk in ("CrateState", "Entity", "FAIRReport", "Any"):
            assert junk not in tools
