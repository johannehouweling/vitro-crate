"""Tests for the LangChain agent loop module.

These tests verify the helper functions (provider detection, tool building)
without requiring actual API keys or LLM calls.
"""

from __future__ import annotations

import os

import pytest

from builder.state import Entity


class TestDetectProvider:
    """Tests for provider detection from environment variables."""

    def test_no_key_returns_none(self):
        """_detect_provider returns None when no API key is set."""
        from builder.agents.agent_loop import _detect_provider

        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            result = _detect_provider()
            assert result is None
        finally:
            if old_openai is not None:
                os.environ["OPENAI_API_KEY"] = old_openai
            if old_anthropic is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_anthropic

    def test_openai_key_detected(self):
        """_detect_provider returns 'openai' when OPENAI_API_KEY is set."""
        from builder.agents.agent_loop import _detect_provider

        old = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["OPENAI_API_KEY"] = "sk-test123"
        try:
            result = _detect_provider()
            assert result == "openai"
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old
            else:
                del os.environ["OPENAI_API_KEY"]

    def test_anthropic_key_detected(self):
        """_detect_provider returns 'anthropic' when ANTHROPIC_API_KEY is set."""
        from builder.agents.agent_loop import _detect_provider

        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test123"
        try:
            result = _detect_provider()
            assert result == "anthropic"
        finally:
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old
            else:
                del os.environ["ANTHROPIC_API_KEY"]

    def test_both_keys_prefers_openai(self):
        """_detect_provider prefers OpenAI when both are set."""
        from builder.agents.agent_loop import _detect_provider

        old_openai = os.environ.get("OPENAI_API_KEY")
        old_anthropic = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        try:
            result = _detect_provider()
            assert result == "openai"
        finally:
            if old_openai:
                os.environ["OPENAI_API_KEY"] = old_openai
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if old_anthropic:
                os.environ["ANTHROPIC_API_KEY"] = old_anthropic
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)
class TestFormatEntitySummary:
    """Tests for the entity summary formatter."""

    def test_empty_entities(self):
        """_format_entity_summary returns appropriate message when empty."""
        from builder.agents.agent_loop import _format_entity_summary

        result = _format_entity_summary([])
        assert "No entities yet." in result

    def test_single_entity_type(self):
        """_format_entity_summary counts by entity type."""
        from builder.agents.agent_loop import _format_entity_summary

        entities = [Entity(entity_id="e1", type="Investigation")]
        result = _format_entity_summary(entities)
        assert "Investigation" in result
        assert "1" in result

    def test_multiple_types(self):
        """_format_entity_summary counts multiple types."""
        from builder.agents.agent_loop import _format_entity_summary

        entities = [
            Entity(entity_id="e1", type="Investigation"),
            Entity(entity_id="e2", type="Study"),
            Entity(entity_id="e3", type="Study"),
        ]
        result = _format_entity_summary(entities)
        assert "Investigation" in result
        assert "Study: 2" in result
class TestBuildChatModel:
    """Tests for the chat model builder."""

    def test_no_provider_raises(self):
        """_build_chat_model raises RuntimeError when no provider is available."""
        from builder.agents.agent_loop import _build_chat_model

        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with pytest.raises(RuntimeError, match="No LLM provider"):
                _build_chat_model()
        finally:
            if old_openai is not None:
                os.environ["OPENAI_API_KEY"] = old_openai
            if old_anthropic is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_anthropic

    def test_unknown_provider_raises(self):
        """_build_chat_model raises RuntimeError for unknown provider."""
        from builder.agents.agent_loop import _build_chat_model

        with pytest.raises(RuntimeError, match="Unknown provider"):
            _build_chat_model(provider="invalid_provider")

    def test_openai_provider(self):
        """_build_chat_model with provider='openai' returns ChatOpenAI."""
        from builder.agents.agent_loop import _build_chat_model

        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            model = _build_chat_model(provider="openai")
            modname = type(model).__module__
            assert "openai" in modname
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_anthropic_provider(self):
        """_build_chat_model with provider='anthropic' returns ChatAnthropic."""
        from builder.agents.agent_loop import _build_chat_model

        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        try:
            model = _build_chat_model(provider="anthropic")
            modname = type(model).__module__
            assert "anthropic" in modname
        finally:
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_default_max_retries_is_three(self):
        """_build_chat_model defaults to max_retries=3 for OpenAI."""
        from builder.agents.agent_loop import _build_chat_model

        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            model = _build_chat_model(provider="openai")
            assert model.max_retries == 3
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_custom_max_retries_passed_through(self):
        """_build_chat_model passes custom max_retries to the model."""
        from builder.agents.agent_loop import _build_chat_model

        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            model = _build_chat_model(provider="openai", max_retries=5)
            assert model.max_retries == 5
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_max_retries_from_env_var(self):
        """_build_chat_model reads VITRO_MAX_RETRIES from env when arg not given."""
        from builder.agents.agent_loop import _build_chat_model

        old_key = os.environ.get("OPENAI_API_KEY")
        old_retries = os.environ.pop("VITRO_MAX_RETRIES", None)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["VITRO_MAX_RETRIES"] = "7"
        try:
            model = _build_chat_model(provider="openai")
            assert model.max_retries == 7
        finally:
            if old_key:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if old_retries is not None:
                os.environ["VITRO_MAX_RETRIES"] = old_retries
            else:
                os.environ.pop("VITRO_MAX_RETRIES", None)
class TestBuildLangchainTools:
    """Tests for building LangChain tools from the engine registry."""

    def _build(self):
        """Helper to build tools from a fresh engine.

        Returns:
            Tuple of (tools list, tool_map dict, engine).
        """
        from builder.agents.agent_loop import _build_langchain_tools
        from builder.engine import AgentEngine

        engine = AgentEngine()
        tools = _build_langchain_tools(engine)
        return tools, {t.name: t for t in tools}, engine

    def test_tools_spec_count(self):
        """_build_langchain_tools creates one tool per spec entry."""
        from builder.agents.tools_spec import TOOL_SPECS

        tools, _, _ = self._build()
        assert len(tools) == len(TOOL_SPECS)

    def test_each_tool_has_name_and_description(self):
        """Each LangChain tool carries the correct name and description."""
        from builder.agents.tools_spec import TOOL_SPECS

        _, tool_map, _ = self._build()
        for spec in TOOL_SPECS:
            name = spec["name"]
            assert name in tool_map, f"Missing tool: {name}"
            assert tool_map[name].description == spec.get("description", "")

    def test_tool_invocation_calls_engine(self):
        """Invoking a LangChain tool calls engine.run_tool and returns result."""
        tools, tool_map, engine = self._build()

        list_tool = tool_map["list_entities"]
        result = list_tool.invoke({"entity_type": None})

        assert result == []  # type: ignore[comparison-overlap]

    def test_draft_investigation_adds_entity(self):
        """Invoking draft_investigation adds an entity to the state."""
        tools, tool_map, engine = self._build()

        tool = tool_map["draft_investigation"]
        result = tool.invoke({"hints": {"name": "Test Investigation"}})

        assert result is not None
        entities = engine.state.list_entities()
        assert len(entities) == 1
        assert entities[0].fields.get("name") == "Test Investigation"
        assert entities[0].type == "Investigation"
class TestToolSpinnerCallback:
    """Tests for the _ToolSpinnerCallback that shows tool calls behind the spinner."""

    def test_is_base_callback_handler(self):
        """_ToolSpinnerCallback is a subclass of BaseCallbackHandler."""
        from builder.agents.agent_loop import _ToolSpinnerCallback
        from langchain_core.callbacks import BaseCallbackHandler

        assert issubclass(_ToolSpinnerCallback, BaseCallbackHandler)

    def test_on_tool_start_appends_to_base_text(self):
        """on_tool_start preserves base_text and appends tool info."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        class FakeStatus:
            def __init__(self):
                self.last_text = ""
                self.base_text = ""

            def update(self, text: str) -> None:
                self.last_text = text

        status = FakeStatus()
        status.base_text = "[yellow]intoxicating...[/yellow]"
        cb = _ToolSpinnerCallback(status)

        cb.on_tool_start({"name": "scan_files"}, "/data")

        assert "intoxicating" in status.last_text
        assert "scan_files" in status.last_text
        assert "/data" in status.last_text

    def test_on_tool_start_truncates_long_args(self):
        """on_tool_start truncates arguments longer than 80 characters."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        class FakeStatus:
            def __init__(self):
                self.last_text = ""
                self.base_text = ""

            def update(self, text: str) -> None:
                self.last_text = text

        status = FakeStatus()
        status.base_text = "[yellow]intoxicating...[/yellow]"
        cb = _ToolSpinnerCallback(status)

        long_args = "x" * 200
        cb.on_tool_start({"name": "lookup_compound"}, long_args)

        # The rendered text should be shorter than the full 200-char args
        # because we truncate the args portion to 77 chars + "..."
        assert len(status.last_text) < 200
        # The base text is preserved
        assert "intoxicating" in status.last_text
        # The args content itself should be truncated
        assert "xxx" in status.last_text  # leading chars present
        assert "..." in status.last_text  # ellipsis present

    def test_on_tool_start_includes_dim_formatting(self):
        """on_tool_start uses dim style for the arguments portion."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        class FakeStatus:
            def __init__(self):
                self.last_text = ""
                self.base_text = ""

            def update(self, text: str) -> None:
                self.last_text = text

        status = FakeStatus()
        status.base_text = "[yellow]intoxicating...[/yellow]"
        cb = _ToolSpinnerCallback(status)

        cb.on_tool_start({"name": "draft_investigation"}, "hints={'title': 'test'}")

        # The tool name is in yellow markup, args in dim markup
        assert "[yellow]" in status.last_text
        assert "[/yellow]" in status.last_text
        assert "[dim]" in status.last_text
        assert "[/dim]" in status.last_text

    def test_handles_empty_args(self):
        """on_tool_start handles empty input_str gracefully."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        class FakeStatus:
            def __init__(self):
                self.last_text = ""
                self.base_text = ""

            def update(self, text: str) -> None:
                self.last_text = text

        status = FakeStatus()
        status.base_text = "[yellow]intoxicating...[/yellow]"
        cb = _ToolSpinnerCallback(status)

        cb.on_tool_start({"name": "list_entities"}, "")

        assert "list_entities" in status.last_text


class TestMainInteractiveFlag:
    """Tests for the --interactive flag in main.py."""

    def test_parse_interactive_flag(self):
        """parse_args recognises --interactive (-I)."""
        from main import parse_args

        args = parse_args(["--interactive"])
        assert args.interactive is True

    def test_parse_provider_flag(self):
        """parse_args recognises --provider flag."""
        from main import parse_args

        args = parse_args(["--provider", "openai"])
        assert args.provider == "openai"

    def test_invalid_provider_rejected(self):
        """parse_args rejects invalid provider values."""
        from main import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--provider", "invalid"])

    def test_parse_short_flags(self):
        """parse_args handles -I and -p short flags."""
        from main import parse_args

        args = parse_args(["-I", "-p", "anthropic"])
        assert args.interactive is True
        assert args.provider == "anthropic"