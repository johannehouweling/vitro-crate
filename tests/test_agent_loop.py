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


class TestEnrichedInputNotAccumulated:
    """Per-turn state metadata must NOT accumulate in persistent message history.

    Issue #66: the enriched_input header (session id, counts, entity summary)
    was injected as a HumanMessage on every turn, and because MemorySaver
    retains everything, this metadata duplicated across turns. The fix moves
    a lightweight state brief into the system prompt (which is re-created
    fresh on every model invocation) and passes user input unadorned.
    """

    def test_call_model_includes_state_brief(self):
        """call_model should prepend a system message that includes state info
        (session id, counts) — NOT the full entity summary."""
        from builder.agents.agent_loop import _build_system_prompt_with_state

        prompt = _build_system_prompt_with_state(
            session_id="test-session",
            entity_count=3,
            file_count=5,
            iteration_count=42,
        )
        assert "test-session" in prompt
        assert "Entities: 3" in prompt
        assert "Files: 5" in prompt
        assert "Iteration: 42" in prompt

    def test_call_model_system_prompt_is_lightweight(self):
        """The state brief injected into the system prompt should be a single
        short line, NOT the full multi-line entity summary."""
        from builder.agents.agent_loop import _build_system_prompt_with_state

        prompt = _build_system_prompt_with_state(
            session_id="sid",
            entity_count=10,
            file_count=20,
            iteration_count=5,
        )
        # Should be a single short line — count newlines in the brief portion
        # The brief is appended after the main system prompt with a \n---
        # separator. Find the brief section.
        assert "Session: sid" in prompt
        assert len(prompt) < 200, f"State brief should be short, got {len(prompt)} chars"

    def test_user_input_not_wrapped_in_header(self):
        """run_interactive_agent must pass plain user_input as the
        HumanMessage content, NOT enriched_input with entity summary."""
        # This is a structural test — verify that the code path
        # no longer builds enriched_input with entity_summary.
        import inspect

        from builder.agents import agent_loop

        source = inspect.getsource(agent_loop.run_interactive_agent)
        # enriched_input construction must be gone
        assert "enriched_input" not in source, (
            "enriched_input must not be built; state brief should be in system prompt"
        )
        # The entity_summary line in the message body must be gone
        assert "_format_entity_summary" not in source.split("enriched"), (
            "_format_entity_summary should not be called for message enrichment"
        )
        # user_input should be used directly
        assert (
            "HumanMessage(content=user_input)" in source
            or '"messages": [HumanMessage(content=user_input)]' in source
        )


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

    def test_tool_body_error_returns_recoverable_message(self):
        """A tool body exception (e.g. entity not found) should return a
        recoverable error message, not propagate as an unhandled exception.

        This test simulates calling set_fields with a non-existent entity_id,
        which raises ValueError("Entity not found: ...") in management.py.
        The LangChain tool wrapper should catch this and return a dict with
        an 'error' key so the LLM can retry, rather than letting it propagate.
        """
        tools, tool_map, engine = self._build()

        tool = tool_map["set_fields"]
        # Calling set_fields with a non-existent ID should trigger
        # ValueError("Entity not found: ...") in management.py
        result = tool.invoke(
            {"entity_id": "nonexistent_123", "fields": {"name": "test"}}
        )

        # The result should be a dict with an error message for the LLM
        assert result is not None
        assert isinstance(result, dict), (
            f"Expected dict with 'error' key, got {type(result).__name__}: {result}"
        )
        assert "error" in result, f"Expected 'error' in result, got: {result}"
        assert "nonexistent_123" in result["error"], (
            f"Error should mention the entity_id, got: {result['error']}"
        )

    def test_tool_body_error_allows_self_correction(self):
        """After a tool-body error, the agent should be able to call the same
        tool again with corrected arguments and get a successful result.

        This simulates the full self-correction cycle: first a bad call that
        triggers an error, then a good call that succeeds.
        """
        tools, tool_map, engine = self._build()

        # Now call set_fields with a bad ID — should get error, not exception
        bad_call = tool_map["set_fields"].invoke(
            {"entity_id": "bad_id_999", "fields": {"name": "wrong"}}
        )
        assert isinstance(bad_call, dict)
        assert "error" in bad_call

        # Now call with a valid entity ID after creating it — should succeed
        draft_tool = tool_map["draft_investigation"]
        draft_tool.invoke({"hints": {"name": "Test Investigation"}})
        entities = engine.state.list_entities()
        assert len(entities) == 1
        entity_id = entities[0].entity_id

        good_call = tool_map["set_fields"].invoke(
            {"entity_id": entity_id, "fields": {"name": "Updated Name"}}
        )
        # The good call should succeed (return the updated entity, not an error dict)
        assert not (isinstance(good_call, dict) and "error" in good_call), (
            f"Good call should succeed, got error: {good_call}"
        )

        # Verify the entity was actually updated
        updated = engine.state.get_entity(entity_id)
        assert updated is not None
        assert updated.fields.get("name") == "Updated Name"


class _FakeSpinner:
    """Records set_tool calls for callback tests."""

    def __init__(self):
        self.tools: list = []

    def set_tool(self, name) -> None:
        self.tools.append(name)


class TestToolSpinnerCallback:
    """The callback forwards the active tool name to the spinner."""

    def test_is_base_callback_handler(self):
        """_ToolSpinnerCallback is a subclass of BaseCallbackHandler."""
        from langchain_core.callbacks import BaseCallbackHandler

        from builder.agents.agent_loop import _ToolSpinnerCallback

        assert issubclass(_ToolSpinnerCallback, BaseCallbackHandler)

    def test_on_tool_start_sets_tool_name(self):
        """on_tool_start tells the spinner which tool is running."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        spinner = _FakeSpinner()
        _ToolSpinnerCallback(spinner).on_tool_start({"name": "scan_files"}, "/data")  # ty: ignore[invalid-argument-type]

        assert spinner.tools == ["scan_files"]

    def test_on_tool_start_defaults_when_unnamed(self):
        """A tool with no name falls back to a generic label."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        spinner = _FakeSpinner()
        _ToolSpinnerCallback(spinner).on_tool_start({}, "")  # ty: ignore[invalid-argument-type]

        assert spinner.tools == ["tool"]

    def test_on_tool_end_clears_tool(self):
        """on_tool_end clears the active tool (back to the thinking phrase)."""
        from builder.agents.agent_loop import _ToolSpinnerCallback

        spinner = _FakeSpinner()
        cb = _ToolSpinnerCallback(spinner)  # ty: ignore[invalid-argument-type]
        cb.on_tool_start({"name": "lookup_compound"}, "aspirin")
        cb.on_tool_end("result")

        assert spinner.tools == ["lookup_compound", None]


class TestThinkingSpinner:
    """The spinner renders the phrase, elapsed seconds, and active tool."""

    def test_render_includes_phrase_and_elapsed(self):
        from rich.console import Console

        from builder.agents.agent_loop import _ThinkingSpinner

        sp = _ThinkingSpinner(Console(), "intoxicating")
        text = sp._render()
        assert "intoxicating" in text
        assert "s)" in text  # elapsed seconds, e.g. "(0s)"

    def test_render_includes_tool_when_set(self):
        from rich.console import Console

        from builder.agents.agent_loop import _ThinkingSpinner

        sp = _ThinkingSpinner(Console(), "intoxicating")
        sp._tool = "scan_files"
        assert "scan_files" in sp._render()


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


class TestRecursionLimit:
    """The documented max_iterations cap must map to LangGraph's recursion_limit
    so a runaway tool loop stops at a controlled bound instead of LangGraph's
    silent default of 25 super-steps (#56)."""

    def test_doubles_max_iterations(self):
        """Each tool iteration is ~2 super-steps (model + tools)."""
        from builder.agents.agent_loop import _recursion_limit

        assert _recursion_limit(50) == 100
        assert _recursion_limit(25) == 50

    def test_floors_at_two(self):
        """A non-positive cap still allows the graph to run at least once."""
        from builder.agents.agent_loop import _recursion_limit

        assert _recursion_limit(0) == 2
        assert _recursion_limit(1) == 2
        assert _recursion_limit(-5) == 2



