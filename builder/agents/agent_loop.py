"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, cast

from builder.agents.system_prompt import SYSTEM_PROMPT
from builder.agents.tools_spec import TOOL_SPECS
from builder.engine import AgentEngine

if TYPE_CHECKING:
    from typing import cast

    from pydantic import BaseModel


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangChain tool wrapper
# ---------------------------------------------------------------------------


def _build_args_schema(
    name: str, params: dict[str, Any]
) -> type[BaseModel] | None:
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
        spec_dict = cast(dict[str, Any], spec)
        name: str = cast(str, spec_dict["name"])
        description: str = cast(str, spec_dict.get("description", ""))
        params: dict[str, Any] = cast(dict[str, Any], spec_dict.get("parameters", {}))

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
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.memory import MemorySaver

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

    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown
    
    from rich.layout import Layout

    console = Console()
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="body"),
    )

    provider_name = provider or _detect_provider()

    # Use LangGraph's built-in thread tracking so the agent accumulates
    # conversation history automatically.
    thread_config = cast(RunnableConfig, {"configurable": {"thread_id": engine.state.session_id}})

    def _extract_reply(state: dict) -> str:
        """Pull the last AIMessage content from the agent state."""
        msgs = state.get("messages", [])
        # Walk backwards to find an AI message (not tool results)
        for msg in reversed(msgs):
            if hasattr(msg, "content") and msg.content:
                # Skip messages from "tool" role
                role = getattr(msg, "type", "") or ""
                if role == "ai" or (isinstance(msg, AIMessage)):
                    return str(msg.content)
                # Also accept the very last message if it has content
                if msg is msgs[-1]:
                    return str(msg.content)
        return ""

    def _print_reply(content: str) -> None:
        """Print an agent reply with Rich markdown rendering."""
        if not content:
            return
        try:
            md = Markdown(content)
            console.print(Panel(md, border_style="green"))
        except Exception:
            console.print(f"[green]{content}[/green]")

    # ── Resume summary vs fresh greeting ────────────────────────────────
    entity_count = len(engine.state.list_entities())
    file_count = len(engine.state.scanned_files)
    is_resume = entity_count > 0 or file_count > 0

    if is_resume:
        # Build a rich summary panel
        from rich.table import Table
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="bold", width=16)
        summary.add_column(style="white")

        summary.add_row("Session:", f"[cyan]{engine.state.session_id}[/cyan]")
        summary.add_row("Entities:", f"[green]{entity_count}[/green]")
        summary.add_row("Files:", f"[green]{file_count}[/green]")

        mit = getattr(engine.state, "mit_assessment", None)
        if mit and getattr(mit, "overall_score", None) is not None:
            summary.add_row("MIT score:", f"[yellow]{mit.overall_score:.0%}[/yellow]")

        val = engine.state.validation
        val_status = []
        if val.base_passed:    val_status.append("[green]base[/green]")
        else:                  val_status.append("[red]base[/red]")
        if val.isa_passed:     val_status.append("[green]ISA[/green]")
        else:                  val_status.append("[red]ISA[/red]")
        if val.tox_passed:     val_status.append("[green]ISA-Tox[/green]")
        else:                  val_status.append("[red]ISA-Tox[/red]")
        summary.add_row("Validation:", "  ".join(val_status))

        if val.required_issues:
            summary.add_row("Issues:", f"[red]{len(val.required_issues)} REQUIRED[/red]")

        # Per-type entity breakdown
        counts: dict[str, int] = {}
        for e in engine.state.list_entities():
            typ = getattr(e, "type", "Unknown")
            counts[typ] = counts.get(typ, 0) + 1
        if counts:
            parts = ", ".join(f"[cyan]{k}[/cyan]={v}" for k, v in sorted(counts.items()))
            summary.add_row("Breakdown:", parts)

        console.print(Panel(summary, title="[yellow]Resumed Session[/yellow]", border_style="yellow"))
        console.print()

        # Tell the LLM about the current state so it can give a contextual greeting
        greeting_prompt = (
            f"The user has resumed a session with {entity_count} entities and {file_count} scanned files. "
            f"Validation: base={'pass' if val.base_passed else 'fail'}, "
            f"ISA={'pass' if val.isa_passed else 'fail'}, "
            f"Tox={'pass' if val.tox_passed else 'fail'}. "
            f"Required issues: {len(val.required_issues)}. "
            f"Entity breakdown: {counts}. "
            f"Briefly welcome them back and summarise what has been done and what the next logical step is."
        )
    else:
        greeting_prompt = "Greet the user and tell them what you can help build."

    def _print_resume_fallback() -> None:
        """Print a resume fallback with next-step suggestions."""
        suggestions = (
            "Try asking to:\n"
            "  • [cyan]list entities[/cyan] — see all drafted items\n"
            "  • [cyan]run validation[/cyan] — check for missing fields\n"
            "  • [cyan]assess MIT[/cyan] — check Minimum Information coverage\n"
            "  • [cyan]draft[/cyan] <entity type> — add more entities\n"
            "  • [cyan]build crate[/cyan] — assemble the RO-Crate"
        )
        if val.required_issues:
            suggestions = (
                f"There are [red]{len(val.required_issues)} REQUIRED validation issues[/red].\n"
                "Try asking to [cyan]validate[/cyan] to see them."
            )
        console.print(
            Panel(
                f"[bold]Welcome back![/bold]\n"
                f"You have [bold cyan]{entity_count}[/bold cyan] entities drafted "
                f"across {len(counts)} types.\n"
                f"[dim]{suggestions}[/dim]",
                border_style="green",
            )
        )

    def _print_fresh_fallback() -> None:
        """Print a fresh-start fallback with next-step suggestions."""
        console.print(
            Panel(
                "[bold]Hello![/bold] I can help you build an ISA-Tox RO-Crate.\n"
                "Try asking me to:\n"
                "  • [cyan]draft an Investigation[/cyan] — start a new project\n"
                "  • [cyan]scan[/cyan] a data directory — import files\n"
                "  • [cyan]help[/cyan] — see all available tools",
                border_style="green",
            )
        )

    try:
        root_logger = logging.getLogger()
        old_root_level = root_logger.level
        root_logger.setLevel(logging.ERROR)
        with console.status(
            "[yellow]intoxicating...[/yellow]",
            spinner="dots",
        ):
            result = app.invoke(
                {"messages": [HumanMessage(content=greeting_prompt)]},
                thread_config,
            )
        root_logger.setLevel(old_root_level)
        reply = _extract_reply(result)
        if reply:
            _print_reply(reply)
        else:
            if is_resume:
                _print_resume_fallback()
            else:
                _print_fresh_fallback()
    except Exception as exc:
        logger.debug("Greeting skipped: %s", exc)
        console.print(
            Panel(
                "[yellow]Could not reach the LLM.[/yellow]\n"
                "Check your [bold]SSL_CERT_FILE[/bold] and [bold]VITRO_API_BASE[/bold] settings.\n"
                "The session is saved — you can resume later with "
                f"[cyan]--resume {engine.state.session_id}[/cyan]",
                border_style="yellow",
            )
        )
        if is_resume:
            _print_resume_fallback()
        else:
            _print_fresh_fallback()

    # ── Goodbye helper ──────────────────────────────────────────────────

    def _print_goodbye(state: Any) -> None:
        """Print a goodbye message with resume instructions."""
        session_id = getattr(state, "session_id", None) or engine.state.session_id
        console.print()

        from rich.table import Table
        t = Table.grid(padding=(0, 1))
        t.add_column(style="yellow bold", width=14)
        t.add_column(style="white")
        t.add_row("Session:", f"[cyan]{session_id}[/cyan]")

        entities = state.list_entities() if hasattr(state, "list_entities") else []
        if entities:
            counts: dict[str, int] = {}
            for e in entities:
                typ = getattr(e, "type", "Unknown")
                counts[typ] = counts.get(typ, 0) + 1
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            t.add_row("Entities:", parts)
        else:
            t.add_row("Entities:", "0")

        from pathlib import Path
        if Path("sessions").is_dir():
            t.add_row("Resume:", f"python -m main [cyan]--resume {session_id}[/cyan] [dim]--interactive[/dim]")

        console.print(Panel(t, title="[yellow]Goodbye![/yellow]", border_style="yellow"))
        console.print()

    # ── Main loop ───────────────────────────────────────────────────────
    while True:
        try:
            # Use console.input directly instead of Prompt.ask so we
            # reliably get KeyboardInterrupt on Ctrl+C and EOFError on Ctrl+D.
            entity_count = len(engine.state.list_entities())
            prompt_suffix = f"[bold cyan]You[/bold cyan] [dim]({entity_count} entities)[/dim]"
            user_input = console.input(prompt_suffix + " ").strip()
            if user_input:
                # Wrap user input in a grey panel
                console.print(Panel(f"[white]{user_input}[/white]", border_style="grey50"))
        except KeyboardInterrupt:
            # Ctrl+C: clear the line and re-prompt
            console.print()
            continue
        except EOFError:
            # Ctrl+D: exit
            console.print()
            _print_goodbye(engine.state)
            break

        if user_input.lower() in ("quit", "exit", "q"):
            _print_goodbye(engine.state)
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
            import random

            TOX_SPINNER_PHRASES = [
                "intoxicating",
                "culturing cells",
                "diluting compounds",
                "pipetting samples",
                "calibrating instruments",
                "incubating cultures",
                "centrifuging lysates",
                "quantifying endpoints",
                "analysing chromatograms",
                "normalising to control",
                "consulting the tox literature",
                "measuring cytotoxicity",
                "checking dose-response",
                "warming up the LC-MS",
                "reviewing SOPs",
                "in silico modelling",
                "consulting PubChem",
                "querying Cellosaurus",
                "parsing ISA-Tox profile",
                "brewing coffee for the researcher",
            ]

            # Temporarily mute WARNING+ logs to avoid interleaving with spinner
            root_logger = logging.getLogger()
            old_root_level = root_logger.level
            root_logger.setLevel(logging.ERROR)

            with console.status(
                f"[yellow]{random.choice(TOX_SPINNER_PHRASES)}...[/yellow]",
                spinner="dots",
            ):
                result = app.invoke(
                    {"messages": [HumanMessage(content=enriched_input)]},
                    thread_config,
                )

            root_logger.setLevel(old_root_level)
            reply = _extract_reply(result)
            if reply:
                _print_reply(reply)

            try:
                from builder.tools.session import save_session
                save_session(engine.state)
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Agent error")
            console.print(f"[red bold]Error:[/red bold] {exc}")
            console.print()


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