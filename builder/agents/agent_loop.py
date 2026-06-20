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

            return StructuredTool.from_function(
                func=_run,
                name=tool_name,
                description=tool_desc,
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

    Returns ``"openai"``, ``"anthropic"``, or ``None`` if neither is configured.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _build_chat_model(provider: str | None = None) -> Any:
    """Build a LangChain chat model for the given or detected provider.

    Args:
        provider: One of ``"openai"``, ``"anthropic"``.  If ``None``, auto-detect.

    Returns:
        A LangChain ``BaseChatModel`` instance.

    Raises:
        RuntimeError: If no provider can be detected or the provider is unknown.
    """
    provider = provider or _detect_provider()
    if provider is None:
        raise RuntimeError(
            "No LLM provider configured. Set OPENAI_API_KEY or "
            "ANTHROPIC_API_KEY environment variable, or pass "
            "--provider openai|anthropic."
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o", temperature=0)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model="claude-sonnet-4-20250514", temperature=0)

    raise RuntimeError(f"Unknown provider: {provider!r}. Use openai or anthropic.")
# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


def run_interactive_agent(
    engine: AgentEngine,
    provider: str | None = None,
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
        max_iterations: Maximum tool-calling iterations before forcing
            a final response.
    """
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    tools = _build_langchain_tools(engine)
    model = _build_chat_model(provider)

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="chat_history", optional=True),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(model, tools, prompt)
    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        max_iterations=max_iterations,
        handle_parsing_errors=True,
    )

    print()
    print("=== ISA-Tox RO-Crate Builder \u2014 Interactive Agent ===")
    print(f"Session:  {engine.state.session_id}")
    print(f"Provider:  {provider or _detect_provider()}")
    print(f"Tools:     {len(tools)} available")
    print(f"Entities:  {len(engine.state.list_entities())}")
    print("Type 'quit' or 'exit' to stop.")
    print()

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
            f"User: {user_input}"
        )

        try:
            result = executor.invoke({"input": enriched_input})
            print(f"Agent: {result['output']}")
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