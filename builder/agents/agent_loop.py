"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from builder.agents.system_prompt import SYSTEM_PROMPT
from builder.agents.tools_spec import TOOL_SPECS
from builder.engine import AgentEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangChain tool wrapper
# ---------------------------------------------------------------------------


def _build_args_schema(name: str, params: dict) -> type | None:
    """Dynamically create a pydantic model from a JSON schema dict.

    Converts the ``parameters`` field of a TOOL_SPECS entry into a
    pydantic ``BaseModel`` subclass that LangChain's ``StructuredTool``
    uses to validate arguments and advertise the schema to the LLM.
    Returns ``None`` (no schema) if the params dict is empty.
    """
    if not params or not isinstance(params, dict):
        return None
    properties = params.get("properties", {})
    if not properties:
        return None

    from pydantic import BaseModel, Field, create_model

    fields: dict[str, Any] = {}
    required_set = set(params.get("required", []))

    for field_name, field_schema in properties.items():
        json_type = field_schema.get("type", "string")

        # Map JSON Schema types to Python types
        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        py_type = type_map.get(json_type, str)

        description = field_schema.get("description", "")

        # If the field has enum values, use a Literal type
        if "enum" in field_schema:
            from typing import Literal
            enum_vals = field_schema["enum"]
            py_type = Literal[tuple(enum_vals)]  # type: ignore

        if field_name in required_set:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            fields[field_name] = (py_type | None, Field(None, description=description))

    return create_model(f"{name}_args", **fields, __base__=BaseModel)


def _build_langchain_tools(engine: AgentEngine) -> list[Any]:
    """Build LangChain BaseTool instances from the engine's tool registry.

    Each tool wraps ``engine.run_tool()`` so that the LLM calls it via
    LangChain's function-calling interface.
    """
    try:
        from langchain_core.tools import BaseTool, StructuredTool
    except ImportError:
        raise ImportError(
            "langchain extra is required: pip install vitro-crate[langchain]"
        )

    langchain_tools: list[BaseTool] = []

    for spec in TOOL_SPECS:
        name = spec["name"]
        description = spec.get("description", "")
        params = spec.get("parameters", {})

        def _make_tool(tool_name: str, tool_desc: str, tool_params: dict) -> BaseTool:
            def _run(**kwargs: Any) -> Any:
                return engine.run_tool(tool_name, **kwargs)

            _run.__name__ = tool_name
            _run.__doc__ = tool_desc

            # Build a StructuredTool from the JSON schema.  The args_schema
            # is auto-generated from tool_params so the LLM knows the expected
            # inputs and their types.
            return StructuredTool.from_function(
                func=_run,
                name=tool_name,
                description=tool_desc,
                args_schema=_build_args_schema(tool_name, tool_params),
                infer_schema=False,
            )

        tool_fn = _make_tool(name, description, params)
        langchain_tools.append(tool_fn)

    return langchain_tools


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _detect_provider() -> str | None:
    """Detect which LLM provider is available based on environment variables.

    Checks ``VITRO_OPENAI_API_KEY`` / ``VITRO_ANTHROPIC_API_KEY`` first,
    then falls back to the unprefixed ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``.

    Returns ``"openai"``, ``"anthropic"``, or ``None`` if neither is configured.
    """
    if os.environ.get("VITRO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("VITRO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _build_chat_model(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Any:
    """Build a LangChain chat model for the given or detected provider.

    Supports custom endpoints (OpenAI-compatible only — Ollama, LiteLLM,
    local proxies, etc.) via the ``OPENAI_BASE_URL`` environment variable
    or the ``base_url`` parameter.

    Args:
        provider: One of ``"openai"``, ``"anthropic"``.  If ``None``, auto-detect.
        model: Model name override (e.g. ``"gpt-4o-mini"``, ``"llama3.2"``).
            Falls back to provider defaults.
        base_url: Custom API base URL for OpenAI-compatible providers.
            Falls back to ``OPENAI_BASE_URL`` env var, then provider default.

    Returns:
        A LangChain ``BaseChatModel`` instance.

    Raises:
        RuntimeError: If no provider can be detected or the provider is unknown.
    """
    provider = provider or _detect_provider()
    if provider is None:
        raise RuntimeError(
            "No LLM provider configured. Set VITRO_OPENAI_API_KEY "
            "(or OPENAI_API_KEY) or VITRO_ANTHROPIC_API_KEY "
            "(or ANTHROPIC_API_KEY) environment variable, or pass "
            "--provider openai|anthropic."
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        # VITRO_ prefixed env vars take priority, fall back to unprefixed
        api_key = (
            os.environ.get("VITRO_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        resolved_base = (
            base_url
            or os.environ.get("VITRO_OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        )
        resolved_model = (
            model
            or os.environ.get("VITRO_OPENAI_MODEL")
            or os.environ.get("OPENAI_MODEL", "gpt-4o")
        )

        kwargs: dict[str, Any] = {"model": resolved_model, "temperature": 0}
        if api_key:
            kwargs["api_key"] = api_key
        if resolved_base:
            kwargs["base_url"] = resolved_base

        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = (
            os.environ.get("VITRO_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        resolved_model = (
            model
            or os.environ.get("VITRO_ANTHROPIC_MODEL")
            or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        )
        kwargs: dict[str, Any] = {"model": resolved_model, "temperature": 0}
        if api_key:
            kwargs["api_key"] = api_key
        return ChatAnthropic(**kwargs)

    raise RuntimeError(f"Unknown provider: {provider!r}. Use openai or anthropic.")
# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


def run_interactive_agent(
    engine: AgentEngine,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_iterations: int = 50,
) -> None:
    """Run an interactive LangChain agent loop reading from stdin.

    The agent prints a prompt, reads a user request from stdin, decides
    which tools to call, calls them, and prints the result.  This continues
    until the user types ``quit``, ``exit``, or the iteration limit is
    reached.

    Args:
        engine: The AgentEngine with an initialized state.
        provider: Optional provider override (``"openai"`` / ``"anthropic"``).
        model: Model name override (e.g. ``"gpt-4o-mini"``, ``"llama3.2"``).
        base_url: Custom API base URL for OpenAI-compatible providers
            (e.g. ``http://localhost:11434/v1`` for Ollama).
        max_iterations: Maximum tool-calling iterations before forcing
            a final response.
    """
    from langchain.agents import create_agent
    from langgraph.checkpoint.memory import MemorySaver
    from langchain_core.messages import AIMessage, HumanMessage

    tools = _build_langchain_tools(engine)
    llm = _build_chat_model(provider=provider, model=model, base_url=base_url)

    # Use the new LangChain 1.3+ create_agent graph API
    # The system prompt is passed as the system_prompt kwarg;
    # conversational memory is handled by the checkpointer.
    app = create_agent(
        llm,
        tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )

    print()
    print("=== ISA-Tox RO-Crate Builder \u2014 Interactive Agent ===")
    print(f"Session:  {engine.state.session_id}")
    print(f"Provider:  {provider or _detect_provider()}")
    print(f"Tools:     {len(tools)} available")
    print(f"Entities:  {len(engine.state.list_entities())}")
    print("Type 'quit' or 'exit' to stop.")
    print()

    # Use LangGraph's built-in thread tracking so the agent accumulates
    # conversation history automatically.
    thread_config = {"configurable": {"thread_id": engine.state.session_id}}

    # Send a greeting so the LLM introduces itself and explains what it can do
    try:
        result = app.invoke(
            {"messages": [HumanMessage(content="Greet the user and tell them what you can help build.")]},
            thread_config,
        )
        if result and "messages" in result:
            msgs = result["messages"]
            if msgs:
                last = msgs[-1]
                if hasattr(last, "content") and last.content:
                    print(f"Agent: {last.content}")
        print()
    except Exception as exc:
        logger.debug("Greeting skipped: %s", exc)

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        if not user_input:
            continue

        entity_summary = _format_entity_summary(engine.state.list_entities())

        enriched_input = (
            f"[Session: {engine.state.session_id} | "
            f"Files: {len(engine.state.scanned_files)} | "
            f"Entities: {len(engine.state.list_entities())} | "
            f"Iterations: {engine.state.iteration_count}]\n"
            f"{entity_summary}\n\n"
            f"{user_input}"
        )

        try:
            result = app.invoke(
                {"messages": [HumanMessage(content=enriched_input)]},
                thread_config,
            )
            if result and "messages" in result:
                msgs = result["messages"]
                if msgs:
                    last = msgs[-1]
                    if hasattr(last, "content") and last.content:
                        print(f"Agent: {last.content}")
            print()

            try:
                from builder.tools.session import save_session
                save_session(engine.state)
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Agent error")
            print(f"[Error] {exc}")
            print()


def _format_entity_summary(entities: list[Any]) -> str:
    """Format entities as a compact summary string for the agent context."""
    if not entities:
        return "No entities yet."
    counts: dict[str, int] = {}
    for e in entities:
        t = getattr(e, "type", "Unknown")
        counts[t] = counts.get(t, 0) + 1
    parts = [f"  - {t}: {n}" for t, n in sorted(counts.items())]
    return "Current entities:\n" + "\n".join(parts)


__all__ = [
    "run_interactive_agent",
    "_build_langchain_tools",
    "_detect_provider",
    "_build_chat_model",
]