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
        from builder.agents.llm import _detect_provider

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
        from builder.agents.llm import _detect_provider

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
        from builder.agents.llm import _detect_provider

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
        from builder.agents.llm import _detect_provider

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
        # user_input should be used directly. Since #263 the per-turn invoke is
        # factored into the nested _run_turn(message_content) helper and the loop
        # seeds it with the plain user_input (``message = user_input``), so accept
        # either the original literal or the new plumbing — both pass the raw
        # input through unwrapped (the #66 guard above already rules out
        # enriched_input / _format_entity_summary wrapping).
        assert (
            "HumanMessage(content=user_input)" in source
            or '"messages": [HumanMessage(content=user_input)]' in source
            or (
                "message = user_input" in source
                and "HumanMessage(content=message_content)" in source
            )
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


class TestModelTiering:
    """Tests for the drafter model tier (Issue #96).

    A distinct, cheap drafter model can be selected via
    ``VITRO_OPENAI_DRAFTER_MODEL`` while the orchestrator keeps the primary
    model. With no drafter model configured, the drafter role resolves to the
    *same* model as the orchestrator — a strict no-op by default.
    """

    def _set_openai(self) -> dict[str, str | None]:
        saved = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "VITRO_OPENAI_API_KEY": os.environ.get("VITRO_OPENAI_API_KEY"),
            "VITRO_OPENAI_MODEL": os.environ.get("VITRO_OPENAI_MODEL"),
            "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
            "VITRO_OPENAI_DRAFTER_MODEL": os.environ.get("VITRO_OPENAI_DRAFTER_MODEL"),
        }
        os.environ.pop("VITRO_OPENAI_API_KEY", None)
        os.environ.pop("VITRO_OPENAI_MODEL", None)
        os.environ.pop("OPENAI_MODEL", None)
        os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        return saved

    def _restore(self, saved: dict[str, str | None]) -> None:
        for var, val in saved.items():
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)

    def test_drafter_role_uses_drafter_model_when_set(self):
        """role='drafter' picks up VITRO_OPENAI_DRAFTER_MODEL."""
        from builder.agents.agent_loop import _build_chat_model

        saved = self._set_openai()
        os.environ["VITRO_OPENAI_MODEL"] = "gpt-4o"
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "gpt-4o-mini"
        try:
            model = _build_chat_model(provider="openai", role="drafter")
            assert model.model_name == "gpt-4o-mini"
        finally:
            self._restore(saved)

    def test_orchestrator_role_ignores_drafter_model(self):
        """role='orchestrator' keeps the primary model even if a drafter is set."""
        from builder.agents.agent_loop import _build_chat_model

        saved = self._set_openai()
        os.environ["VITRO_OPENAI_MODEL"] = "gpt-4o"
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "gpt-4o-mini"
        try:
            model = _build_chat_model(provider="openai", role="orchestrator")
            assert model.model_name == "gpt-4o"
        finally:
            self._restore(saved)

    def test_drafter_role_falls_back_to_primary_when_unset(self):
        """No-op by default: with no drafter model, the drafter == orchestrator."""
        from builder.agents.agent_loop import _build_chat_model

        saved = self._set_openai()
        os.environ["VITRO_OPENAI_MODEL"] = "gpt-4o"
        try:
            orchestrator = _build_chat_model(provider="openai", role="orchestrator")
            drafter = _build_chat_model(provider="openai", role="drafter")
            assert drafter.model_name == orchestrator.model_name == "gpt-4o"
        finally:
            self._restore(saved)

    def test_default_role_is_orchestrator(self):
        """Calling without a role keeps today's behaviour (primary model)."""
        from builder.agents.agent_loop import _build_chat_model

        saved = self._set_openai()
        os.environ["VITRO_OPENAI_MODEL"] = "gpt-4o"
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "gpt-4o-mini"
        try:
            model = _build_chat_model(provider="openai")
            assert model.model_name == "gpt-4o"
        finally:
            self._restore(saved)

    def test_explicit_model_arg_overrides_role(self):
        """An explicit model= wins over role-based resolution."""
        from builder.agents.agent_loop import _build_chat_model

        saved = self._set_openai()
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "gpt-4o-mini"
        try:
            model = _build_chat_model(provider="openai", model="llama3.2", role="drafter")
            assert model.model_name == "llama3.2"
        finally:
            self._restore(saved)


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


class TestCacheFriendlyPrompt:
    """Issue #60: the system message must be byte-stable across iterations so the
    tools+system+history prefix stays cacheable (provider-agnostic prompt caching);
    the volatile per-turn state brief is delivered as the trailing message where it
    cannot bust the prefix cache."""

    def test_system_message_is_stable_and_brief_is_trailing(self):
        """First message == SYSTEM_PROMPT verbatim (no state appended); the state
        brief is the LAST message; history is preserved in between."""
        from langchain_core.messages import HumanMessage, SystemMessage

        from builder.agents.agent_loop import _assemble_model_messages
        from builder.agents.system_prompt import SYSTEM_PROMPT

        history = [HumanMessage(content="hello")]
        msgs = _assemble_model_messages(
            history,
            session_id="sid",
            entity_count=3,
            file_count=5,
            iteration_count=42,
        )

        assert isinstance(msgs[0], SystemMessage)
        assert msgs[0].content == SYSTEM_PROMPT  # byte-stable, no state appended
        assert msgs[1] is history[0]  # history preserved, in order
        assert isinstance(msgs[-1], SystemMessage)
        assert "Iteration: 42" in msgs[-1].content
        assert "sid" in msgs[-1].content

    def test_system_prefix_identical_across_iterations(self):
        """The stable prefix is byte-identical even as state changes, while the
        trailing brief reflects the new state (only the cache tail varies)."""
        from builder.agents.agent_loop import _assemble_model_messages

        a = _assemble_model_messages(
            [], session_id="s", entity_count=1, file_count=1, iteration_count=1
        )
        b = _assemble_model_messages(
            [], session_id="s", entity_count=2, file_count=9, iteration_count=99
        )
        assert a[0].content == b[0].content  # stable cacheable prefix
        assert a[-1].content != b[-1].content  # volatile tail varies per turn


class TestTrimHistory:
    """Issue #61: bound per-turn input tokens by trimming/summarizing history.

    Verbose tool outputs (esp. scan_files listings, which already live in
    CrateState) must not be replayed verbatim every turn; trimming must NEVER
    yield orphaned tool messages (an AI tool_call without its ToolMessage, or a
    ToolMessage without its preceding AI tool_call), or the provider API rejects
    the request.
    """

    @staticmethod
    def _ai_tool_call(content, call_id, name="scan_files"):
        from langchain_core.messages import AIMessage

        return AIMessage(
            content=content,
            tool_calls=[
                {"name": name, "args": {}, "id": call_id, "type": "tool_call"}
            ],
        )

    @staticmethod
    def _no_orphans(messages):
        """Return True iff every ToolMessage is immediately preceded by an AI
        message whose tool_calls include its tool_call_id, and every AI
        tool_call id (other than a trailing AI message) is answered."""
        from langchain_core.messages import AIMessage, ToolMessage

        # 1) No ToolMessage may appear without a matching open tool_call id.
        open_ids: set[str] = set()
        for msg in messages:
            if isinstance(msg, AIMessage):
                tcs = getattr(msg, "tool_calls", None) or []
                open_ids = {tc["id"] for tc in tcs}
            elif isinstance(msg, ToolMessage):
                if msg.tool_call_id not in open_ids:
                    return False
                open_ids.discard(msg.tool_call_id)
            else:
                open_ids = set()
        return True

    def test_helper_importable(self):
        """The trim helper is importable from agent_loop."""
        from builder.agents.agent_loop import _trim_history

        assert callable(_trim_history)

    def test_history_is_bounded_over_many_turns(self):
        """Over many accumulated turns, the trimmed history token count stays
        within the budget — it does NOT grow linearly with turn count."""
        from langchain_core.messages import HumanMessage, ToolMessage

        from builder.agents.agent_loop import _trim_history

        # Build a long, growing transcript: many human/AI(tool_call)/tool triples
        # with large tool outputs (simulating verbose validation/lookup payloads).
        history: list = []
        for i in range(60):
            history.append(HumanMessage(content=f"request {i}"))
            cid = f"call_{i}"
            history.append(self._ai_tool_call(f"calling tool {i}", cid, name="validate"))
            history.append(
                ToolMessage(content="X" * 2000, tool_call_id=cid)
            )

        max_tokens = 1500
        trimmed = _trim_history(history, max_tokens=max_tokens)

        from langchain_core.messages.utils import count_tokens_approximately

        assert count_tokens_approximately(trimmed) <= max_tokens
        # And it must be strictly smaller than the full (unbounded) history.
        assert count_tokens_approximately(trimmed) < count_tokens_approximately(history)

    def test_consumed_scan_output_is_pruned(self):
        """A consumed verbose scan ToolMessage (its data already in CrateState)
        is pruned/summarized — its large body is replaced by a short stub, so it
        is not replayed verbatim, while the AI/tool pairing is preserved."""
        from langchain_core.messages import HumanMessage, ToolMessage

        from builder.agents.agent_loop import _trim_history

        cid = "scan_1"
        big_listing = "\n".join(f"file_{i}.csv\t1234\ttext/csv" for i in range(500))
        history = [
            HumanMessage(content="scan my folder"),
            self._ai_tool_call("scanning", cid, name="scan_files"),
            ToolMessage(content=big_listing, tool_call_id=cid, name="scan_files"),
            HumanMessage(content="now draft an investigation"),
        ]

        # Generous budget so trimming itself would NOT drop the scan message —
        # the pruning of the verbose scan body must happen independently.
        trimmed = _trim_history(history, max_tokens=100_000)

        scan_msgs = [
            m
            for m in trimmed
            if isinstance(m, ToolMessage) and m.tool_call_id == cid
        ]
        assert scan_msgs, "scan ToolMessage must be retained (pairing preserved)"
        pruned = scan_msgs[0]
        assert len(str(pruned.content)) < len(big_listing), (
            "verbose scan listing must be pruned, not replayed verbatim"
        )
        assert self._no_orphans(trimmed)

    def test_no_orphan_tool_message_when_history_ends_mid_pair(self):
        """A history ending mid tool-call pair (AI tool_call with no ToolMessage
        yet) must NOT produce an orphaned ToolMessage at the head, nor strand a
        ToolMessage whose AI tool_call was trimmed away."""
        from langchain_core.messages import HumanMessage, ToolMessage

        from builder.agents.agent_loop import _trim_history

        history: list = []
        for i in range(10):
            history.append(HumanMessage(content=f"req {i}"))
            cid = f"c{i}"
            history.append(self._ai_tool_call("X" * 1500, cid, name="lookup_compound"))
            history.append(ToolMessage(content="Y" * 1500, tool_call_id=cid))
        # End mid-pair: an AI tool_call with no answering ToolMessage yet.
        history.append(HumanMessage(content="final"))
        last_cid = "c_last"
        history.append(self._ai_tool_call("about to call", last_cid))

        trimmed = _trim_history(history, max_tokens=400)

        # The hard invariant: no orphaned tool messages, whatever the budget.
        assert self._no_orphans(trimmed)

    def test_assemble_model_messages_trims_history(self):
        """_assemble_model_messages keeps the stable system prefix + trailing
        brief intact while bounding the history in between (Issue #61 on top of
        the #60 cache-friendly layout)."""
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        from builder.agents.agent_loop import _assemble_model_messages
        from builder.agents.system_prompt import SYSTEM_PROMPT

        history: list = []
        for i in range(40):
            history.append(HumanMessage(content=f"req {i}"))
            cid = f"c{i}"
            history.append(
                TestTrimHistory._ai_tool_call("X" * 1500, cid, name="validate")
            )
            history.append(ToolMessage(content="Y" * 1500, tool_call_id=cid))

        msgs = _assemble_model_messages(
            history,
            session_id="sid",
            entity_count=3,
            file_count=5,
            iteration_count=42,
            max_history_tokens=2000,
        )

        # Stable prefix + trailing brief preserved (Issue #60 layout intact).
        assert isinstance(msgs[0], SystemMessage)
        assert msgs[0].content == SYSTEM_PROMPT
        assert isinstance(msgs[-1], SystemMessage)
        assert "Iteration: 42" in msgs[-1].content

        # The history in between is bounded (fewer than the 120 we fed in).
        inner = msgs[1:-1]
        assert len(inner) < len(history)
        assert TestTrimHistory._no_orphans(inner)


class TestThinkingSpinnerPause:
    """The thinking spinner must yield the terminal to a HITL prompt.

    A scan-root approval (or any ask-user) calls ``input()`` mid-``invoke`` while
    the spinner's Rich Live region is repainting; without a pause the prompt is
    clobbered and stdin is unusable. The spinner registers itself as the active
    console animation so ``suspend_console_animation`` can pause/resume it.
    """

    class _FakeStatus:
        def __init__(self) -> None:
            self.events: list[str] = []

        def start(self) -> None:
            self.events.append("start")

        def stop(self) -> None:
            self.events.append("stop")

        def update(self, *_a, **_k) -> None:
            pass

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, *_a) -> None:
            self.stop()

    class _FakeConsole:
        def __init__(self, status) -> None:
            self._status = status

        def status(self, *_a, **_k):
            return self._status

    def test_pause_stops_and_resume_starts_the_live_region(self) -> None:
        from builder.agents.agent_loop import _ThinkingSpinner

        st = self._FakeStatus()
        sp = _ThinkingSpinner(self._FakeConsole(st), "x")

        sp.pause()
        assert sp._paused.is_set() is True
        assert "stop" in st.events

        sp.resume()
        assert sp._paused.is_set() is False
        assert "start" in st.events

    def test_suspend_console_animation_pauses_then_resumes_spinner(self) -> None:
        from builder.agents.agent_loop import _ThinkingSpinner
        from builder.tools.hitl import (
            register_console_animation,
            suspend_console_animation,
            unregister_console_animation,
        )

        st = self._FakeStatus()
        sp = _ThinkingSpinner(self._FakeConsole(st), "x")
        register_console_animation(sp)
        try:
            with suspend_console_animation():
                paused_in_body = sp._paused.is_set()
        finally:
            unregister_console_animation(sp)

        assert paused_in_body is True  # paused for the duration of the prompt
        assert sp._paused.is_set() is False  # resumed after


class TestCompletenessNudge:
    """Issue #251: a deterministic present/missing/next-action nudge in the
    per-turn brief steers the weak model to the next concrete step instead of
    stalling once the obvious entities exist."""

    def _state(self, *types: str):
        """A CrateState pre-populated with one entity per requested type."""
        from builder.state import CrateState, Entity

        state = CrateState()
        for i, t in enumerate(types):
            state.add_entity(Entity(entity_id=f"e{i}", type=t))  # ty: ignore[invalid-argument-type]
        return state

    def test_nudge_lists_present_and_missing_with_next_action(self):
        """Backbone + person + compounds but no process chain / files / export
        => the nudge names what's present, what's missing, and a next action."""
        from builder.agents.agent_loop import _completeness_nudge

        state = self._state(
            "Investigation",
            "Study",
            "Assay",
            "Person",
            "MolecularEntity",
            "MolecularEntity",
        )
        nudge = _completeness_nudge(state)

        # Present items are surfaced (with the ✓ marker)
        assert "backbone ✓" in nudge
        assert "person ✓" in nudge
        # Compound count is surfaced
        assert "2 compounds ✓" in nudge
        # Missing items are named
        assert "process chain" in nudge
        assert "file" in nudge.lower()
        assert "export" in nudge.lower()
        # A concrete next-action hint with real tool names is present
        assert "next:" in nudge.lower()
        assert (
            "draft_process_chain" in nudge
            or "attach_files" in nudge
            or "build_and_validate" in nudge
            or "export_crate" in nudge
        )

    def test_nudge_is_short(self):
        """The nudge stays token-cheap — a single short line."""
        from builder.agents.agent_loop import _completeness_nudge

        state = self._state("Investigation", "Study", "Assay", "Person")
        nudge = _completeness_nudge(state)
        assert nudge.count("\n") == 0
        assert len(nudge) < 320

    def test_complete_state_suggests_validate_and_export(self):
        """When the crate looks complete (backbone, person, compounds, process
        chain, files), the nudge suggests validate + export."""
        from builder.agents.agent_loop import _completeness_nudge

        state = self._state(
            "Investigation",
            "Study",
            "Assay",
            "Person",
            "MolecularEntity",
            "LabProcess",
            "File",
        )
        nudge = _completeness_nudge(state)
        assert "export_crate" in nudge or "export" in nudge.lower()
        assert "build_and_validate" in nudge or "validate" in nudge.lower()

    def test_empty_state_nudge_does_not_crash(self):
        """An empty crate still yields a (short, non-crashing) nudge."""
        from builder.agents.agent_loop import _completeness_nudge

        nudge = _completeness_nudge(self._state())
        assert isinstance(nudge, str)
        # The very first thing to do on an empty crate is the backbone
        assert "backbone" in nudge.lower()

    def test_brief_includes_nudge_when_passed(self):
        """_build_system_prompt_with_state surfaces the nudge when given one."""
        from builder.agents.agent_loop import _build_system_prompt_with_state

        nudge = "backbone ✓; missing: export → next: export_crate"
        brief = _build_system_prompt_with_state(
            session_id="sid",
            entity_count=3,
            file_count=0,
            iteration_count=1,
            nudge=nudge,
        )
        assert nudge in brief

    def test_brief_omits_nudge_line_when_none(self):
        """No nudge => the brief is unchanged (back-compat)."""
        from builder.agents.agent_loop import _build_system_prompt_with_state

        brief = _build_system_prompt_with_state(
            session_id="sid",
            entity_count=0,
            file_count=0,
            iteration_count=1,
        )
        assert "next:" not in brief.lower()


class TestFinishBackstop:
    """Issue #251: a deterministic finish backstop guarantees a crate lands on
    disk when the session ends with un-exported entities — even when the weak
    LLM stalled before ever calling export_crate."""

    def _engine(self, *types: str):
        """A real engine with one entity per requested type."""
        from builder.engine import AgentEngine
        from builder.state import Entity

        engine = AgentEngine()
        engine.state.session_id = "test_backstop"
        for i, t in enumerate(types):
            engine.state.add_entity(Entity(entity_id=f"e{i}", type=t))  # ty: ignore[invalid-argument-type]
        return engine

    def _spy(self, engine):
        """Replace engine.run_tool with a recording spy that no-ops the heavy
        build/validate/export tools and records the call order."""
        calls: list[str] = []

        def fake_run_tool(tool_name: str, **kwargs):
            calls.append(tool_name)
            if tool_name == "export_crate":
                return {"success": True, "crate_path": "/tmp/out-ro-crate", "error": None}
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]
        return calls

    def test_backstop_builds_and_exports_when_entities_exist(self):
        """Non-empty crate not yet exported => build_and_validate THEN
        export_crate run, and the resolved path is surfaced."""
        from builder.agents.agent_loop import _finish_backstop

        engine = self._engine("Investigation", "Study")
        calls = self._spy(engine)
        surfaced: list[str] = []

        result = _finish_backstop(engine, emit=surfaced.append)

        assert "build_and_validate" in calls
        assert "export_crate" in calls
        # build_and_validate must precede export_crate
        assert calls.index("build_and_validate") < calls.index("export_crate")
        assert result is not None and result.get("success") is True
        # The resolved path is surfaced to the user
        assert any("ro-crate" in m for m in surfaced)

    def test_backstop_noop_when_crate_empty(self):
        """An empty crate => no build, no export (nothing to write)."""
        from builder.agents.agent_loop import _finish_backstop

        engine = self._engine()  # no entities
        calls = self._spy(engine)

        result = _finish_backstop(engine, emit=lambda _m: None)

        assert calls == []
        assert result is None

    def test_backstop_is_idempotent(self):
        """Calling the backstop twice exports only once (no double-export)."""
        from builder.agents.agent_loop import _finish_backstop

        engine = self._engine("Investigation")
        calls = self._spy(engine)

        _finish_backstop(engine, emit=lambda _m: None)
        first = list(calls)
        _finish_backstop(engine, emit=lambda _m: None)

        assert calls.count("export_crate") == 1, (
            f"export_crate must run at most once, got {calls}"
        )
        # The second call adds nothing
        assert calls == first

    def test_backstop_never_raises(self):
        """A failure inside the export path is caught, not propagated out of the
        exit path."""
        from builder.agents.agent_loop import _finish_backstop

        engine = self._engine("Investigation")

        def boom(tool_name: str, **kwargs):
            raise RuntimeError("export blew up")

        engine.run_tool = boom  # type: ignore[method-assign]

        # Must not raise
        result = _finish_backstop(engine, emit=lambda _m: None)
        assert result is None or result.get("success") is False

    def test_backstop_honors_metadata_output_path(self):
        """export_crate is called via run_tool with no explicit path so it honors
        state.metadata.output_path; the resolved path is surfaced."""
        from builder.agents.agent_loop import _finish_backstop

        engine = self._engine("Investigation")
        engine.state.metadata.output_path = "/tmp/custom-ro-crate"
        captured: list[str] = []

        def fake_run_tool(tool_name: str, **kwargs):
            if tool_name == "export_crate":
                # honor metadata.output_path exactly as export_crate would
                path = kwargs.get("output_path") or engine.state.metadata.output_path
                return {"success": True, "crate_path": path, "error": None}
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]

        _finish_backstop(engine, emit=captured.append)
        assert any("custom-ro-crate" in m for m in captured)

    def test_run_interactive_agent_calls_backstop_on_exit(self):
        """run_interactive_agent must call the backstop on its exit paths."""
        import inspect

        from builder.agents import agent_loop

        source = inspect.getsource(agent_loop.run_interactive_agent)
        assert "_finish_backstop" in source, (
            "run_interactive_agent must invoke _finish_backstop on session end"
        )


# ---------------------------------------------------------------------------
# Issue #263: legacy-react stall recovery (Fix A) + autonomous continuation
# (Fix B). The model / app.invoke are stubbed; no real LLM or network.
# ---------------------------------------------------------------------------


class TestRequestTimeout:
    """Fix A: _build_chat_model wires a request timeout onto the chat model so a
    silent provider stall can never hang the turn forever (#263)."""

    def test_default_request_timeout_is_set(self):
        """With no override, the model carries a finite request timeout."""
        from builder.agents.agent_loop import _build_chat_model

        old_key = os.environ.get("OPENAI_API_KEY")
        old_to = os.environ.pop("VITRO_REQUEST_TIMEOUT", None)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            model = _build_chat_model(provider="openai")
            # ChatOpenAI exposes the timeout as request_timeout (alias "timeout").
            assert model.request_timeout is not None
            assert float(model.request_timeout) > 0
        finally:
            if old_key:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if old_to is not None:
                os.environ["VITRO_REQUEST_TIMEOUT"] = old_to

    def test_explicit_timeout_passed_through(self):
        """An explicit ``timeout`` argument reaches the model."""
        from builder.agents.agent_loop import _build_chat_model

        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            model = _build_chat_model(provider="openai", timeout=42.0)
            assert float(model.request_timeout) == 42.0
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
            else:
                os.environ.pop("OPENAI_API_KEY", None)

    def test_timeout_from_env_var(self):
        """VITRO_REQUEST_TIMEOUT is honored when no argument is given."""
        from builder.agents.agent_loop import _build_chat_model

        old_key = os.environ.get("OPENAI_API_KEY")
        old_to = os.environ.get("VITRO_REQUEST_TIMEOUT")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["VITRO_REQUEST_TIMEOUT"] = "17"
        try:
            model = _build_chat_model(provider="anthropic")
            # ChatAnthropic stores it as default_request_timeout (alias "timeout").
            assert float(model.default_request_timeout) == 17.0
        finally:
            if old_key:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if old_to is not None:
                os.environ["VITRO_REQUEST_TIMEOUT"] = old_to
            else:
                os.environ.pop("VITRO_REQUEST_TIMEOUT", None)


class TestInvokeWithTimeout:
    """Fix A: a wall-clock guard around app.invoke so a hung model never blocks
    the loop indefinitely and never lets an exception escape (#263)."""

    def test_returns_ok_and_result_on_success(self):
        from builder.agents.agent_loop import _invoke_with_timeout

        sentinel = {"messages": ["done"]}

        class _App:
            def invoke(self, payload, config):
                return sentinel

        result, outcome = _invoke_with_timeout(
            _App(), {"messages": []}, {}, timeout=5.0
        )
        assert outcome == "ok"
        assert result is sentinel

    def test_returns_timeout_when_invoke_hangs(self):
        """A model call that runs past the timeout ends gracefully with a
        ``"timeout"`` outcome — no hang, no exception escapes."""
        import threading
        import time

        from builder.agents.agent_loop import _invoke_with_timeout

        release = threading.Event()

        class _HangingApp:
            def invoke(self, payload, config):
                # Block well past the (tiny) test timeout.
                release.wait(timeout=5.0)
                return {"messages": ["late"]}

        start = time.monotonic()
        result, outcome = _invoke_with_timeout(
            _HangingApp(), {"messages": []}, {}, timeout=0.1
        )
        elapsed = time.monotonic() - start
        release.set()  # let the daemon worker unwind
        assert outcome == "timeout"
        assert result is None
        # Returned promptly rather than after the full 5s hang.
        assert elapsed < 3.0

    def test_returns_error_outcome_when_invoke_raises(self):
        """An exception inside invoke is captured, not propagated."""
        from builder.agents.agent_loop import _invoke_with_timeout

        class _BoomApp:
            def invoke(self, payload, config):
                raise RuntimeError("provider exploded")

        result, outcome = _invoke_with_timeout(
            _BoomApp(), {"messages": []}, {}, timeout=5.0
        )
        assert outcome == "error"
        assert result is None


class TestReplyIsQuestion:
    """Fix B: deterministic question detection drives whether the loop prompts
    the user or auto-continues (#263)."""

    def test_trailing_question_mark_is_question(self):
        from builder.agents.agent_loop import _reply_is_question

        assert _reply_is_question("Which compound should I use?")

    def test_interrogative_opener_is_question(self):
        from builder.agents.agent_loop import _reply_is_question

        assert _reply_is_question("Could you confirm the cell line name")

    def test_plain_narration_is_not_question(self):
        from builder.agents.agent_loop import _reply_is_question

        assert not _reply_is_question(
            "I drafted the Investigation and added 3 compounds."
        )

    def test_empty_reply_is_not_question(self):
        from builder.agents.agent_loop import _reply_is_question

        assert not _reply_is_question("")
        assert not _reply_is_question(None)  # type: ignore[arg-type]

    def test_trailing_question_after_narration_is_question(self):
        from builder.agents.agent_loop import _reply_is_question

        assert _reply_is_question(
            "I added the process chain.\nDo you want me to export now?"
        )


class TestCrateIsComplete:
    """Fix B: completeness short-circuits the autonomous loop (#263)."""

    def _engine(self):
        from builder.engine import AgentEngine
        from builder.state import Entity

        engine = AgentEngine()
        engine.state.session_id = "test_complete"
        engine.state.add_entity(Entity(entity_id="e0", type="Investigation"))
        return engine

    def test_incomplete_when_validation_not_passed(self):
        from builder.agents.agent_loop import _crate_is_complete

        engine = self._engine()
        engine.state.validation.base_passed = True
        engine.state.validation.isa_passed = False
        engine.state.validation.tox_passed = False
        assert not _crate_is_complete(engine)

    def test_incomplete_with_required_issues(self):
        from builder.agents.agent_loop import _crate_is_complete

        engine = self._engine()
        engine.state.validation.base_passed = True
        engine.state.validation.isa_passed = True
        engine.state.validation.tox_passed = True
        engine.state.validation.required_issues = ["fix this"]
        assert not _crate_is_complete(engine)

    def test_complete_when_all_pass_and_no_issues(self):
        from builder.agents.agent_loop import _crate_is_complete

        engine = self._engine()
        engine.state.validation.base_passed = True
        engine.state.validation.isa_passed = True
        engine.state.validation.tox_passed = True
        engine.state.validation.required_issues = []
        assert _crate_is_complete(engine)

    def test_empty_crate_is_not_complete(self):
        from builder.agents.agent_loop import _crate_is_complete
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.state.session_id = "test_empty"
        engine.state.validation.base_passed = True
        engine.state.validation.isa_passed = True
        engine.state.validation.tox_passed = True
        # No entities → nothing to be "complete" about.
        assert not _crate_is_complete(engine)


class _LoopHarness:
    """Drives run_interactive_agent with the LLM/app/stdin/backstop stubbed.

    Records the order of model invocations and every stdin read so tests can
    assert auto-continuation vs prompting without any real network.
    """

    def __init__(self, monkeypatch, *, replies, stdin_lines, complete_after=None):
        from builder.agents import agent_loop
        from builder.engine import AgentEngine
        from builder.state import Entity

        self.agent_loop = agent_loop
        self.monkeypatch = monkeypatch
        self.replies = list(replies)
        self.stdin_lines = list(stdin_lines)
        self.complete_after = complete_after

        self.invoke_count = 0
        self.stdin_reads: list[str] = []
        self.backstop_calls = 0

        engine = AgentEngine()
        engine.state.session_id = "test_loop_263"
        engine.state.add_entity(Entity(entity_id="e0", type="Investigation"))
        self.engine = engine

    def _fake_reply(self):
        if self.replies:
            return self.replies.pop(0)
        return ""

    def install(self):
        from langchain_core.messages import AIMessage

        agent_loop = self.agent_loop
        harness = self

        # No real model / graph.
        self.monkeypatch.setattr(agent_loop, "_build_chat_model", lambda **kw: object())

        def _is_greeting(payload):
            try:
                content = str(payload["messages"][0].content)
            except (KeyError, IndexError, AttributeError):
                return False
            return content.startswith(("Greet the user", "The user has resumed"))

        class _FakeApp:
            def invoke(self, payload, config):
                # The one-shot greeting invoke is NOT a turn — serve it a fixed
                # reply and do not count it, so invoke_count tracks loop turns.
                if _is_greeting(payload):
                    return {"messages": [AIMessage(content="Welcome back!")]}
                harness.invoke_count += 1
                text = harness._fake_reply()
                # Optionally mark the crate complete after N invocations so the
                # autonomous loop can short-circuit on completion.
                if (
                    harness.complete_after is not None
                    and harness.invoke_count >= harness.complete_after
                ):
                    v = harness.engine.state.validation
                    v.base_passed = v.isa_passed = v.tox_passed = True
                    v.required_issues = []
                return {"messages": [AIMessage(content=text)]}

        self.monkeypatch.setattr(
            agent_loop, "_build_agent_graph", lambda *a, **k: _FakeApp()
        )

        # Stdin reader: record every read; raise EOFError when exhausted so the
        # loop exits its main while-True deterministically.
        def fake_boxed_input(console, label="❯"):
            if not harness.stdin_lines:
                raise EOFError
            line = harness.stdin_lines.pop(0)
            harness.stdin_reads.append(line)
            return line

        self.monkeypatch.setattr(agent_loop, "_boxed_input", fake_boxed_input)

        # Count backstop invocations; do not touch disk.
        def fake_backstop(engine, *, emit=None):
            harness.backstop_calls += 1
            return None

        self.monkeypatch.setattr(agent_loop, "_finish_backstop", fake_backstop)

        # Session save is best-effort and writes to disk — stub it out.
        import builder.tools.session as session_mod

        self.monkeypatch.setattr(
            session_mod, "save_session", lambda *a, **k: {"success": True}
        )

    def run(self):
        self.install()
        self.agent_loop.run_interactive_agent(self.engine)


class TestAutonomousContinuation:
    """Fix B: the loop auto-continues on narration and only prompts the user on
    a genuine question (#263)."""

    def test_narration_auto_continues_without_reading_stdin(self, monkeypatch):
        """A non-question reply with an incomplete crate re-invokes the model
        WITHOUT consuming stdin — the user is not asked to type "ok"."""
        # First stdin line kicks off a turn; the model narrates twice, then asks
        # a question (which should send the loop back to prompt the user).
        harness = _LoopHarness(
            monkeypatch,
            replies=[
                "I drafted the Investigation.",  # narration → auto-continue
                "Added the process chain.",      # narration → auto-continue
                "What output path would you like?",  # question → prompt user
            ],
            stdin_lines=["start building"],  # only the kickoff; then EOF
        )
        harness.run()

        # The kickoff produced narration that auto-continued (no stdin read in
        # between), and only ONE stdin line was consumed before EOF.
        assert harness.stdin_reads == ["start building"]
        # Three model invocations: kickoff + two auto-continues until the
        # question surfaced and the loop went back to prompt (hitting EOF).
        assert harness.invoke_count == 3
        # Backstop still runs on the EOF exit path.
        assert harness.backstop_calls >= 1

    def test_question_prompts_user(self, monkeypatch):
        """A reply that IS a question prompts the user (stdin read once)."""
        harness = _LoopHarness(
            monkeypatch,
            replies=["Which cell line are you using?"],  # question on first turn
            stdin_lines=["scan my files"],  # kickoff; then EOF on the re-prompt
        )
        harness.run()

        # Exactly one model invocation (the kickoff), then it prompted the user
        # (consuming the one stdin line) and hit EOF.
        assert harness.invoke_count == 1
        assert harness.stdin_reads == ["scan my files"]

    def test_max_autonomous_turns_caps_the_loop(self, monkeypatch):
        """Endless narration is bounded by the max-autonomous-turns cap."""
        from builder.agents.agent_loop import _MAX_AUTONOMOUS_TURNS

        # Always narrate; never a question, never complete → only the cap stops it.
        harness = _LoopHarness(
            monkeypatch,
            replies=["working..."] * 500,
            stdin_lines=["go"],  # single kickoff
        )
        harness.run()

        # kickoff + at most _MAX_AUTONOMOUS_TURNS auto-continues for the turn.
        assert harness.invoke_count <= 1 + _MAX_AUTONOMOUS_TURNS
        assert harness.invoke_count >= 2  # it did auto-continue at least once

    def test_completion_stops_the_autonomous_loop(self, monkeypatch):
        """When the crate becomes complete, the loop stops auto-continuing even
        though the reply is not a question."""
        harness = _LoopHarness(
            monkeypatch,
            replies=["narrating"] * 50,
            stdin_lines=["build it"],
            complete_after=2,  # crate validates on the 2nd invocation
        )
        harness.run()

        # kickoff + one auto-continue, then completion short-circuits.
        assert harness.invoke_count == 2


class TestEmptyCompletionRecovery:
    """Fix A: N consecutive empty completions end the turn gracefully (retry
    once), instead of auto-continuing forever (#263)."""

    def test_consecutive_empty_completions_stop_autocontinue(self, monkeypatch):
        from builder.agents.agent_loop import _MAX_EMPTY_COMPLETIONS

        # Every reply is empty (the stall symptom). The loop must retry once and
        # then stop auto-continuing rather than spinning to the full cap.
        harness = _LoopHarness(
            monkeypatch,
            replies=[""] * 50,
            stdin_lines=["start"],
        )
        harness.run()

        # kickoff (1 empty) + at most _MAX_EMPTY_COMPLETIONS - 1 retries.
        assert harness.invoke_count <= _MAX_EMPTY_COMPLETIONS
        assert harness.backstop_calls >= 1


class TestTimeoutEndsTurnGracefully:
    """Fix A: a model call exceeding the wall-clock guard ends the turn without
    hanging and without an exception escaping; the backstop stays reachable."""

    def test_timeout_does_not_hang_or_raise(self, monkeypatch):
        import threading

        from langchain_core.messages import AIMessage

        from builder.agents import agent_loop
        from builder.engine import AgentEngine
        from builder.state import Entity

        engine = AgentEngine()
        engine.state.session_id = "test_timeout_263"
        engine.state.add_entity(Entity(entity_id="e0", type="Investigation"))

        release = threading.Event()
        invoke_count = {"n": 0}
        backstop_calls = {"n": 0}

        class _HangingApp:
            def invoke(self, payload, config):
                invoke_count["n"] += 1
                release.wait(timeout=5.0)
                return {"messages": [AIMessage(content="late")]}

        monkeypatch.setattr(agent_loop, "_build_chat_model", lambda **kw: object())
        monkeypatch.setattr(agent_loop, "_build_agent_graph", lambda *a, **k: _HangingApp())
        # Tiny timeout so the guard fires fast.
        monkeypatch.setenv("VITRO_REQUEST_TIMEOUT", "0.1")

        stdin = ["build"]

        def fake_boxed_input(console, label="❯"):
            if not stdin:
                raise EOFError
            return stdin.pop(0)

        monkeypatch.setattr(agent_loop, "_boxed_input", fake_boxed_input)

        def fake_backstop(engine, *, emit=None):
            backstop_calls["n"] += 1
            return None

        monkeypatch.setattr(agent_loop, "_finish_backstop", fake_backstop)

        import builder.tools.session as session_mod

        monkeypatch.setattr(session_mod, "save_session", lambda *a, **k: {"success": True})

        # Must return (no hang) and not raise.
        agent_loop.run_interactive_agent(engine)
        release.set()

        # The hung greeting/turn timed out; the backstop still ran on EOF exit.
        assert backstop_calls["n"] >= 1


# ---------------------------------------------------------------------------
# Issue #287: export-on-completed-build (Fix A) + repeated-non-progress
# loop-breaker (Fix B). The engine/tools are stubbed; no SHACL / LLM / network.
# ---------------------------------------------------------------------------


class TestExportOnCompletedBuild:
    """Issue #287 Fix A: a successful in-loop build_and_validate auto-exports the
    crate to disk (no quit needed), surfaces the absolute path, stamps the
    _EXPORTED_FLAG, and leaves the finish backstop a no-op afterward."""

    def _engine_with_entities(self, *types: str):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.state.session_id = "test_export_287"
        for i, t in enumerate(types or ("Investigation", "Study")):
            engine.state.add_entity(Entity(entity_id=f"e{i}", type=t))
        return engine

    def _install_spy(self, engine, *, build_result, export_result):
        """Replace engine.run_tool with a recording spy returning canned results
        for build_and_validate / export_crate; record the call order."""
        calls: list[str] = []

        def fake_run_tool(tool_name: str, **kwargs):
            calls.append(tool_name)
            if tool_name == "build_and_validate":
                return dict(build_result)
            if tool_name in ("export_crate", "build_crate"):
                return dict(export_result)
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]
        return calls

    def test_successful_build_and_validate_auto_exports_once(self):
        from builder.agents.agent_loop import (
            _EXPORTED_FLAG,
            _build_langchain_tools,
            _finish_backstop,
        )

        engine = self._engine_with_entities("Investigation", "Study")
        calls = self._install_spy(
            engine,
            build_result={
                "ok": True,
                "conformance": {"base": True, "isa": True, "tox": True},
                "issues": [],
            },
            export_result={
                "success": True,
                "crate_path": "/tmp/S-VHPS26-ro-crate",
                "error": None,
            },
        )

        tools = {t.name: t for t in _build_langchain_tools(engine)}
        result = tools["build_and_validate"].invoke({"severity": None, "profile": None})

        # build_and_validate ran, then export_crate was triggered exactly once.
        assert "build_and_validate" in calls
        assert calls.count("export_crate") == 1, f"expected one export, got {calls}"
        # The original build result is still returned to the model.
        assert isinstance(result, dict) and result.get("ok") is True
        # The flag is stamped so the finish backstop is a no-op.
        assert getattr(engine, _EXPORTED_FLAG, False) is True

        # A subsequent finish backstop must NOT double-export.
        before = calls.count("export_crate")
        _finish_backstop(engine, emit=lambda _m: None)
        assert calls.count("export_crate") == before, (
            f"finish backstop must not double-export, got {calls}"
        )

    def test_crate_path_is_surfaced(self):
        """The absolute crate path is surfaced through the loop's output channel."""
        from builder.agents.agent_loop import _build_langchain_tools

        engine = self._engine_with_entities("Investigation")
        self._install_spy(
            engine,
            build_result={"ok": True, "conformance": {"base": True}, "issues": []},
            export_result={
                "success": True,
                "crate_path": "/tmp/out-ro-crate",
                "error": None,
            },
        )

        surfaced: list[str] = []
        # The loop installs an emit sink on the engine for in-loop exports.
        engine.on_auto_export = surfaced.append  # type: ignore[attr-defined]

        tools = {t.name: t for t in _build_langchain_tools(engine)}
        tools["build_and_validate"].invoke({})

        assert any("ro-crate" in m for m in surfaced), (
            f"the crate path must be surfaced, got {surfaced}"
        )

    def test_failed_build_does_not_export(self):
        """A build_and_validate that does not pass base conformance never exports."""
        from builder.agents.agent_loop import _EXPORTED_FLAG, _build_langchain_tools

        engine = self._engine_with_entities("Investigation")
        calls = self._install_spy(
            engine,
            build_result={
                "ok": False,
                "conformance": {"base": False},
                "issues": [{"x": 1}],
            },
            export_result={
                "success": True,
                "crate_path": "/tmp/out-ro-crate",
                "error": None,
            },
        )

        tools = {t.name: t for t in _build_langchain_tools(engine)}
        tools["build_and_validate"].invoke({})

        assert "export_crate" not in calls, f"a failed build must not export, got {calls}"
        assert getattr(engine, _EXPORTED_FLAG, False) is False

    def test_empty_crate_does_not_export(self):
        """A successful build over an EMPTY crate (no entities) never exports."""
        from builder.agents.agent_loop import _EXPORTED_FLAG, _build_langchain_tools
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.state.session_id = "test_export_287_empty"
        calls = self._install_spy(
            engine,
            build_result={"ok": True, "conformance": {"base": True}, "issues": []},
            export_result={
                "success": True,
                "crate_path": "/tmp/out-ro-crate",
                "error": None,
            },
        )

        tools = {t.name: t for t in _build_langchain_tools(engine)}
        tools["build_and_validate"].invoke({})

        assert "export_crate" not in calls, f"empty crate must not export, got {calls}"
        assert getattr(engine, _EXPORTED_FLAG, False) is False

    def test_re_export_when_state_changes_between_builds(self):
        """A second build after the crate has grown re-exports (the latest crate
        always lands); a second build with NO change does not re-export."""
        from builder.agents.agent_loop import _build_langchain_tools

        engine = self._engine_with_entities("Investigation")
        calls = self._install_spy(
            engine,
            build_result={"ok": True, "conformance": {"base": True}, "issues": []},
            export_result={
                "success": True,
                "crate_path": "/tmp/out-ro-crate",
                "error": None,
            },
        )
        tools = {t.name: t for t in _build_langchain_tools(engine)}

        tools["build_and_validate"].invoke({})
        assert calls.count("export_crate") == 1
        # No state change → a repeat build must NOT re-export.
        tools["build_and_validate"].invoke({})
        assert calls.count("export_crate") == 1, f"no change → no re-export, got {calls}"
        # The crate grows → the next build re-exports the latest crate.
        engine.state.add_entity(Entity(entity_id="new", type="Study"))
        tools["build_and_validate"].invoke({})
        assert calls.count("export_crate") == 2, f"grown crate → re-export, got {calls}"

    def test_failed_export_does_not_crash_or_stamp(self):
        """When the auto-export itself fails, the build result is still returned
        and the flag is not stamped (so the finish backstop can retry on exit)."""
        from builder.agents.agent_loop import _EXPORTED_FLAG, _build_langchain_tools

        engine = self._engine_with_entities("Investigation")
        self._install_spy(
            engine,
            build_result={"ok": True, "conformance": {"base": True}, "issues": []},
            export_result={"success": False, "crate_path": None, "error": "disk full"},
        )

        tools = {t.name: t for t in _build_langchain_tools(engine)}
        result = tools["build_and_validate"].invoke({})

        assert isinstance(result, dict) and result.get("ok") is True
        assert getattr(engine, _EXPORTED_FLAG, False) is False


class TestRepeatedNonProgressLoopBreaker:
    """Issue #287 Fix B: N consecutive IDENTICAL non-progress tool calls trigger
    a loop-breaker that injects the list_scanned_files inventory and steers the
    model to a concrete file path."""

    def _engine(self):
        from builder.engine import AgentEngine
        from builder.state import FileClassification

        engine = AgentEngine()
        engine.state.session_id = "test_loopbreaker_287"
        engine.state.scanned_files = [
            FileClassification(
                path="/data/run/Assay_OATP1C1/results.csv",
                filename="results.csv",
                size=1234,
                mime_type="text/csv",
            )
        ]
        return engine

    def _install_dir_reader(self, engine, message):
        """engine.run_tool returns the same directory message for every read."""
        calls: list[tuple[str, tuple]] = []

        def fake_run_tool(tool_name: str, **kwargs):
            calls.append((tool_name, tuple(sorted(kwargs.items()))))
            if tool_name == "read_file_sample":
                # Mirror what file_readers does for a directory: a real string.
                return message
            if tool_name == "list_scanned_files":
                from builder.tools.management import list_scanned_files

                return list_scanned_files(engine.state, **kwargs)
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]
        return calls

    def test_identical_directory_reads_trigger_intervention(self):
        from builder.agents.agent_loop import (
            _LOOP_BREAKER_THRESHOLD,
            _build_langchain_tools,
        )

        engine = self._engine()
        dir_msg = (
            "/data/run/Assay_OATP1C1 is a directory, not a file — use "
            "list_scanned_files to browse the inventory, then read a specific "
            "file by its path."
        )
        calls = self._install_dir_reader(engine, dir_msg)
        tools = {t.name: t for t in _build_langchain_tools(engine)}
        reader = tools["read_file_sample"]

        args = {"path": "/data/run/Assay_OATP1C1"}
        results = []
        for _ in range(_LOOP_BREAKER_THRESHOLD + 1):
            results.append(reader.invoke(dict(args)))

        # The first calls pass through the directory message unchanged. On/after
        # the threshold the loop-breaker injects the inventory and steers the
        # model — the final result must NOT be the bare directory message.
        last = str(results[-1])
        assert last != dir_msg, "the loop-breaker must change the repeated result"
        assert "results.csv" in last, (
            f"the loop-breaker must inject the scanned-file inventory, got: {last}"
        )
        # The underlying read tool was NOT called yet again after the breaker
        # fired (the identical non-progress call is refused/short-circuited).
        read_calls = [c for c in calls if c[0] == "read_file_sample"]
        assert len(read_calls) <= _LOOP_BREAKER_THRESHOLD, (
            f"the identical read must be refused after the threshold, got {calls}"
        )

    def test_error_dict_results_trigger_intervention(self):
        from builder.agents.agent_loop import (
            _LOOP_BREAKER_THRESHOLD,
            _build_langchain_tools,
        )

        engine = self._engine()
        calls: list[tuple[str, tuple]] = []

        def fake_run_tool(tool_name: str, **kwargs):
            calls.append((tool_name, tuple(sorted(kwargs.items()))))
            if tool_name == "read_file":
                # A missing path raises -> the wrapper turns it into an error dict.
                raise ValueError("Entity not found: /nope")
            if tool_name == "list_scanned_files":
                from builder.tools.management import list_scanned_files

                return list_scanned_files(engine.state, **kwargs)
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]
        tools = {t.name: t for t in _build_langchain_tools(engine)}
        reader = tools["read_file"]

        results = []
        for _ in range(_LOOP_BREAKER_THRESHOLD + 1):
            results.append(reader.invoke({"path": "/nope"}))

        last = str(results[-1])
        assert "results.csv" in last, (
            f"repeated error results must trigger the inventory injection, got {last}"
        )

    def test_distinct_calls_do_not_trigger(self):
        """Repeated but DISTINCT directory reads never trip the loop-breaker."""
        from builder.agents.agent_loop import (
            _LOOP_BREAKER_THRESHOLD,
            _build_langchain_tools,
        )

        engine = self._engine()

        def fake_run_tool(tool_name: str, **kwargs):
            if tool_name == "read_file_sample":
                return (
                    f"{kwargs.get('path')} is a directory, not a file — use "
                    "list_scanned_files."
                )
            return {"ok": True}

        engine.run_tool = fake_run_tool  # type: ignore[method-assign]
        tools = {t.name: t for t in _build_langchain_tools(engine)}
        reader = tools["read_file_sample"]

        # Each call has a DISTINCT path → never the same non-progress result.
        for i in range(_LOOP_BREAKER_THRESHOLD + 3):
            res = str(reader.invoke({"path": f"/data/dir_{i}"}))
            assert "results.csv" not in res, (
                f"distinct calls must not trip the loop-breaker, got {res}"
            )

    def test_single_repeat_below_threshold_does_not_trigger(self):
        """A repeat below the threshold passes the result through unchanged."""
        from builder.agents.agent_loop import (
            _LOOP_BREAKER_THRESHOLD,
            _build_langchain_tools,
        )

        engine = self._engine()
        dir_msg = "/data/d is a directory, not a file — use list_scanned_files."
        self._install_dir_reader(engine, dir_msg)
        tools = {t.name: t for t in _build_langchain_tools(engine)}
        reader = tools["read_file_sample"]

        # Repeat one fewer than the threshold → still the bare directory message.
        for _ in range(_LOOP_BREAKER_THRESHOLD - 1):
            res = str(reader.invoke({"path": "/data/d"}))
            assert "results.csv" not in res, (
                f"below threshold must not inject the inventory, got {res}"
            )


