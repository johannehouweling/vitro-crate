"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import logging
import os
import threading
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any, Sequence, cast

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphRecursionError
from langgraph.graph import add_messages

# Import Annotated from typing_extensions so it survives
# from __future__ import annotations at module level.
try:
    from typing import Annotated as _Annotated
except ImportError:
    from typing_extensions import Annotated as _Annotated

from builder.agents.system_prompt import SYSTEM_PROMPT
from builder.agents.tools_spec import TOOL_SPECS
from builder.engine import AgentEngine

if TYPE_CHECKING:
    from typing import cast

    from pydantic import BaseModel


from typing import TypedDict

from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent graph state (TypedDict with add_messages reducer)
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    """State for the agent LangGraph — messages with automatic concatenation."""

    messages: _Annotated[Sequence[BaseMessage], add_messages]  # type: ignore[valid-type]


# ---------------------------------------------------------------------------
# Thinking spinner — ticks elapsed seconds and shows the active tool
# ---------------------------------------------------------------------------


class _ThinkingSpinner:
    """A Rich status spinner that ticks elapsed seconds while the agent works
    and surfaces the active tool — Claude Code style.

    Colour convention: green = the agent working (matches the ● reply marker),
    dim = elapsed/meta, cyan = the active tool.
    Used as a context manager around an ``app.invoke`` call; a daemon thread
    refreshes the elapsed time roughly twice a second.
    """

    def __init__(self, console: Any, phrase: str) -> None:
        self._console = console
        self._phrase = phrase
        self._tool: str | None = None
        self._start = monotonic()
        self._stop = threading.Event()
        self._status = console.status(self._render(), spinner="dots", spinner_style="green")
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def _render(self) -> str:
        elapsed = int(monotonic() - self._start)
        line = f"[green]{self._phrase}…[/green] [dim]({elapsed}s)[/dim]"
        if self._tool:
            line += f"  [dim]·[/dim] [cyan]{self._tool}[/cyan]"
        return line

    def _tick(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self._status.update(self._render())
            except Exception:
                break

    def set_tool(self, name: str | None) -> None:
        """Show (or clear, with ``None``) the tool currently running."""
        self._tool = name
        try:
            self._status.update(self._render())
        except Exception:
            pass

    def __enter__(self) -> "_ThinkingSpinner":
        self._status.__enter__()
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._status.__exit__(*exc)


class _ToolSpinnerCallback(BaseCallbackHandler):
    """LangChain callback that surfaces the active tool on a _ThinkingSpinner."""

    def __init__(self, spinner: _ThinkingSpinner) -> None:
        self.spinner = spinner
        super().__init__()

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        self.spinner.set_tool(serialized.get("name", "tool"))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.spinner.set_tool(None)


# ---------------------------------------------------------------------------
# LangChain tool wrapper
# ---------------------------------------------------------------------------


def _build_args_schema(name: str, params: dict[str, Any]) -> type[BaseModel] | None:
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


def _unreadable_file_message(path: str) -> str:
    """Actionable message for the LLM when read_file_sample can't return text.

    read_file_sample returns a bare ``None`` for files that are missing, too
    large, or binary (e.g. .xls/.xlsx Office containers, GraphPad .prism/.pzf).
    A bare ``None`` gives the model nothing to act on, so a weak model re-calls
    the tool forever and hits the iteration cap (#101). This turns it into a
    clear "stop, do something else" signal.
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1] or path or "the file"
    return (
        f"read_file_sample could not return text for '{name}'. It is missing, too "
        f"large (>100MB), or binary — e.g. .xls/.xlsx are Office/zip containers and "
        f".prism/.pzf are GraphPad Prism binaries. Do NOT retry read_file_sample on "
        f"it. Use the scan preview already in state, try read_excel/read_file for "
        f"spreadsheets or Office docs, or skip this file and continue drafting entities."
    )


def _build_langchain_tools(engine: AgentEngine) -> list[Any]:
    """Build LangChain BaseTool instances from the engine's tool registry.

    Each tool wraps ``engine.run_tool()`` so that the LLM calls it via
    LangChain's function-calling interface.
    """
    try:
        from langchain_core.tools import BaseTool, StructuredTool
    except ImportError:
        raise ImportError("langchain extra is required: pip install vitro-crate[langchain]")

    langchain_tools: list[BaseTool] = []

    for spec in TOOL_SPECS:
        spec_dict = cast(dict[str, Any], spec)
        name: str = cast(str, spec_dict["name"])
        description: str = cast(str, spec_dict.get("description", ""))
        params: dict[str, Any] = cast(dict[str, Any], spec_dict.get("parameters", {}))

        def _make_tool(tool_name: str, tool_desc: str, tool_params: dict) -> BaseTool:
            def _run(**kwargs: Any) -> Any:
                try:
                    result = engine.run_tool(tool_name, **kwargs)
                except (ValueError, KeyError, TypeError) as exc:
                    # Recoverable tool-body exceptions are converted to a dict
                    # with an 'error' key so the LLM receives them as a tool
                    # message and can self-correct (e.g. retry with the right
                    # entity_id) instead of the error propagating out of
                    # app.invoke and aborting the entire turn.
                    # Pydantic / arg-schema validation errors are already caught
                    # by LangChain's ToolNode and fed back as ToolInvocationError
                    # messages; this catches the tool-body exceptions that would
                    # otherwise escape both the ToolNode and the model loop.
                    # Genuinely fatal errors (SystemExit, KeyboardInterrupt) are
                    # intentionally NOT caught so they propagate normally.
                    return {"error": str(exc), "tool": tool_name}
                # scan_files returns the full list[FileClassification] (already
                # stored in state); hand the LLM a compact summary instead of
                # the raw blob so it gets a clear success signal and does not
                # re-scan in a loop.
                if tool_name == "scan_files" and isinstance(result, list):
                    from builder.tools.scanner import summarize_scan_result

                    return summarize_scan_result(result)
                # read_file_sample returns None for missing/oversized/binary files;
                # hand the LLM an actionable message so it stops re-calling it (#101).
                if tool_name == "read_file_sample" and result is None:
                    return _unreadable_file_message(kwargs.get("path", ""))
                return result

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
# Iteration safety net
# ---------------------------------------------------------------------------


def _recursion_limit(max_iterations: int) -> int:
    """Map the documented tool-iteration cap to LangGraph's ``recursion_limit``.

    Each tool iteration is roughly two super-steps (``model`` then ``tools``),
    so the recursion limit is ``2 * max_iterations``. Floored at 2 so the graph
    can always complete at least one model→tools→model cycle. Without this,
    LangGraph applies its silent default of 25 super-steps and a runaway loop
    raises an uncaught ``GraphRecursionError`` (#56).
    """
    return max(2, max_iterations * 2)


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
    max_retries: int | None = None,
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
    if max_retries is None:
        env_val = os.environ.get("VITRO_MAX_RETRIES")
        max_retries = int(env_val) if env_val is not None else 3

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
        api_key = os.environ.get("VITRO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        resolved_base = (
            base_url or os.environ.get("VITRO_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        )
        resolved_model = (
            model
            or os.environ.get("VITRO_OPENAI_MODEL")
            or os.environ.get("OPENAI_MODEL", "gpt-4o")
        )

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "temperature": 0,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if resolved_base:
            kwargs["base_url"] = resolved_base

        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        api_key = os.environ.get("VITRO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        resolved_model = (
            model
            or os.environ.get("VITRO_ANTHROPIC_MODEL")
            or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        )
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "temperature": 0,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["api_key"] = api_key
        return ChatAnthropic(**kwargs)

    raise RuntimeError(f"Unknown provider: {provider!r}. Use openai or anthropic.")


# ---------------------------------------------------------------------------
# Explicit StateGraph construction (replaces create_agent)
# ---------------------------------------------------------------------------


def should_continue(state: dict[str, Any]) -> str:
    """Route to ``"tools"`` if the last AIMessage has tool_calls, else ``END``.

    This is the conditional edge function for the LangGraph agent graph.
    """
    from langgraph.graph import END

    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    # LangGraph AIMessage stores tool_calls as an attribute
    tool_calls = getattr(last_message, "tool_calls", None)
    if tool_calls and len(tool_calls) > 0:
        return "tools"
    return END


def _tool_names_from_state(state: dict[str, Any]) -> list[str]:
    """Return the tool names requested by the last AI message's tool_calls."""
    messages = state.get("messages", [])
    if not messages:
        return []
    tool_calls = getattr(messages[-1], "tool_calls", None) or []
    return [tc.get("name", "") for tc in tool_calls]


def _wrap_model_node(call_model: Any, profiler: Any, iteration_getter: Any) -> Any:
    """Wrap the model node to log ``node_start``/``node_end`` timing.

    Returns *call_model* unchanged when no profiler is active, so the graph
    (and existing tests) behave identically without instrumentation. Console
    output is unaffected — all timing goes to ``profile.ndjson``.
    """
    if profiler is None:
        return call_model

    def timed_model_node(state: dict[str, Any]) -> dict[str, Any]:
        iteration = iteration_getter()
        messages_in = len(state.get("messages", []))
        profiler.log_event(event="node_start", node="model", iteration=iteration)
        start = perf_counter()
        result = call_model(state)
        duration_ms = (perf_counter() - start) * 1000.0
        out_messages = result.get("messages", []) if isinstance(result, dict) else []
        produced_tool_calls = any(getattr(m, "tool_calls", None) for m in out_messages)

        # Extract token usage and response text from the LLM response
        input_tokens: int | None = None
        output_tokens: int | None = None
        model_name: str | None = None
        response_text: str | None = None
        if out_messages:
            last_msg = out_messages[-1]
            # Prefer the standardised usage_metadata (langchain-core >=0.3)
            usage = getattr(last_msg, "usage_metadata", None)
            if usage is not None:
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
            # Fall back to response_metadata (provider-specific)
            if input_tokens is None or output_tokens is None:
                meta = getattr(last_msg, "response_metadata", None) or {}
                tu = meta.get("token_usage") or meta.get("usage") or {}
                if input_tokens is None:
                    input_tokens = tu.get("prompt_tokens") or tu.get("input_tokens")
                if output_tokens is None:
                    output_tokens = tu.get("completion_tokens") or tu.get("output_tokens")
            resp_meta: dict = getattr(last_msg, "response_metadata", None) or {}
            model_name = resp_meta.get("model_name") or resp_meta.get("model")
            # Capture the model's reply text — truncate to avoid bloating profile
            content = getattr(last_msg, "content", None)
            if content:
                text = str(content)
                if len(text) > 2000:
                    text = text[:1997] + "..."
                response_text = text

        profiler.log_event(
            event="node_end",
            node="model",
            duration_ms=duration_ms,
            iteration=iteration,
            messages_in=messages_in,
            messages_out=len(out_messages),
            produced_tool_calls=bool(produced_tool_calls),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_name=model_name,
            response_text=response_text,
        )
        return result

    return timed_model_node


def _wrap_tools_node(tool_node: Any, profiler: Any, iteration_getter: Any) -> Any:
    """Wrap the tools node to log ``node_start``/``node_end`` timing.

    Per-tool durations are already recorded by ``AgentEngine.run_tool`` (which
    every LangChain tool calls); this wrapper records the overall batch plus the
    tool names. Returns *tool_node* unchanged when no profiler is active.
    """
    if profiler is None:
        return tool_node

    def timed_tools_node(state: dict[str, Any]) -> Any:
        tools_called = _tool_names_from_state(state)
        profiler.log_event(
            event="node_start",
            node="tools",
            iteration=iteration_getter(),
            tools=tools_called,
        )
        start = perf_counter()
        result = tool_node.invoke(state)
        duration_ms = (perf_counter() - start) * 1000.0
        profiler.log_event(
            event="node_end",
            node="tools",
            duration_ms=duration_ms,
            iteration=iteration_getter(),
            tools=tools_called,
        )
        return result

    return timed_tools_node


def _build_system_prompt_with_state(
    session_id: str,
    entity_count: int,
    file_count: int,
    iteration_count: int,
) -> str:
    """Build a lightweight state brief appended to the system prompt.

    This is called on every model invocation (not persisted in history),
    giving the LLM awareness of current session state without accumulating
    duplicate metadata in MemorySaver.

    Returns a single short line like:
    ``[Session: sid | Files: 5 | Entities: 3 | Iteration: 42]``
    """
    return (
        f"[Session: {session_id} | "
        f"Files: {file_count} | "
        f"Entities: {entity_count} | "
        f"Iteration: {iteration_count}]"
    )


def _build_agent_graph(
    llm: Any,
    tools: list[Any],
    engine: AgentEngine | None = None,
) -> Any:
    """Build a compiled StateGraph replacing ``create_agent()``.

    Creates an explicit graph with ``"model"`` and ``"tools"`` nodes,
    with a conditional edge routing tool calls back to the model and
    final answers to END. The system prompt is prepended on every
    model invocation.

    Args:
        llm: A LangChain chat model. The tools are bound to it internally
            (via ``llm.bind_tools``) so the model can emit tool calls.
        tools: List of LangChain BaseTool instances — bound to the model
            for advertising and used by the ToolNode for execution.
        engine: Optional engine; when it has an active profiler, the
            ``"model"`` and ``"tools"`` nodes are wrapped with timing
            instrumentation that writes to ``profile.ndjson``.

    Returns:
        A compiled ``CompiledStateGraph`` ready for ``.invoke()``.
    """
    from langchain_core.messages import SystemMessage
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import START, StateGraph
    from langgraph.prebuilt import ToolNode

    profiler = engine.profiler if engine is not None else None
    iteration_getter = (lambda: engine.state.iteration_count) if engine is not None else None

    # Bind the tool schemas to the model so it can actually emit tool_calls.
    # Without this, the model is never told the tools exist: should_continue
    # always routes to END, the ToolNode is unreachable, and the agent
    # silently degrades to a text-only chatbot that narrates "let me scan..."
    # but never executes a tool. (create_agent() bound tools internally;
    # the explicit-graph migration dropped this and broke tool-calling.)
    model = llm.bind_tools(tools) if tools else llm

    def call_model(state: dict[str, Any]) -> dict[str, Any]:
        """Model node: prepend system prompt and invoke the tool-bound LLM."""
        assert engine is not None, "AgentEngine must be set before call_model is invoked"
        messages = state.get("messages", [])
        # Prepend system prompt with lightweight state brief on every invocation.
        # The state brief is re-built each time so it never accumulates in
        # MemorySaver history (Issue #66).
        state_brief = _build_system_prompt_with_state(
            session_id=engine.state.session_id,
            entity_count=len(engine.state.list_entities()),
            file_count=len(engine.state.scanned_files),
            iteration_count=engine.state.iteration_count,
        )
        system_msg = SystemMessage(content=f"{SYSTEM_PROMPT}\n\n{state_brief}")
        model_messages = [system_msg, *messages]
        response = model.invoke(model_messages)
        # Return only the new response; the add_messages reducer appends it
        return {"messages": [response]}

    tool_node = ToolNode(tools)

    # Use a typed state with add_messages reducer so ToolNode and model
    # both append to the message list rather than replacing it.
    graph: Any = StateGraph(AgentState)
    graph.add_node("model", _wrap_model_node(call_model, profiler, iteration_getter))
    graph.add_node("tools", _wrap_tools_node(tool_node, profiler, iteration_getter))

    # should_continue returns "tools" or END (the string "__end__").
    # Without a path_map, the return value is used as the destination node name.
    graph.add_conditional_edges("model", should_continue)
    graph.add_edge("tools", "model")
    graph.add_edge(START, "model")

    return graph.compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


def _boxed_input(console: Any, label: str = "❯") -> str:
    """Read one line of input inside a rounded box (Claude Code style).

    Renders an ephemeral rounded box via prompt_toolkit; once submitted the
    box is erased and the line is echoed into the transcript so it persists.
    Falls back to ``console.input`` when stdin is not a TTY or prompt_toolkit
    is unavailable. Raises ``KeyboardInterrupt`` (Ctrl+C) and ``EOFError``
    (Ctrl+D on an empty line), matching ``input()``.
    """
    import sys

    def _fallback() -> str:
        return console.input(f"[bold cyan]{label} [/bold cyan]").strip()

    if not sys.stdin.isatty():
        return _fallback()
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import (
            BufferControl,
            FormattedTextControl,
        )
        from prompt_toolkit.styles import Style
    except Exception:
        return _fallback()

    buf = Buffer(multiline=False)
    outcome: dict[str, Any] = {"exc": None}
    kb = KeyBindings()

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=buf.text)

    @kb.add("c-c")
    def _(event: Any) -> None:
        outcome["exc"] = KeyboardInterrupt
        event.app.exit(result="")

    @kb.add("c-d")
    def _(event: Any) -> None:
        if not buf.text:
            outcome["exc"] = EOFError
            event.app.exit(result="")

    def _hline(left: str, right: str):
        def _get() -> list[tuple[str, str]]:
            w = get_app().output.get_size().columns
            return [("class:box", left + "─" * max(0, w - 2) + right)]

        return _get

    buf_window = Window(BufferControl(buffer=buf))
    middle = VSplit(
        [
            Window(
                FormattedTextControl([("class:box", "│ "), ("class:prompt", f"{label} ")]),
                width=4,
            ),
            buf_window,
            Window(FormattedTextControl([("class:box", " │")]), width=2),
        ],
        height=1,
    )
    root = HSplit(
        [
            Window(FormattedTextControl(_hline("╭", "╮")), height=1),
            middle,
            Window(FormattedTextControl(_hline("╰", "╯")), height=1),
        ]
    )
    style = Style.from_dict({"box": "fg:#5f5f5f", "prompt": "bold ansicyan"})
    app: Any = Application(
        layout=Layout(root, focused_element=buf_window),
        key_bindings=kb,
        style=style,
        full_screen=False,
    )
    try:
        text = app.run()
    except Exception:
        return _fallback()

    if outcome["exc"] is not None:
        raise outcome["exc"]
    text = (text or "").strip()
    if text:
        # The box was ephemeral — echo the submitted line into the transcript.
        console.print(f"[bold cyan]{label}[/bold cyan] {text}")
    return text


def run_interactive_agent(
    engine: AgentEngine,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_iterations: int | None = None,
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
        max_iterations: Maximum tool-calling iterations. Falls back to
            ``VITRO_MAX_ITERATIONS`` env var, then config file
            ``[agent.max_iterations]``, then built-in default (100).
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig

    tools = _build_langchain_tools(engine)
    llm = _build_chat_model(provider=provider, model=model, base_url=base_url)

    # Build the explicit StateGraph instead of using create_agent()
    # The system prompt is prepended by the model node on every invocation.
    # Passing the engine enables node-level timing → profile.ndjson.
    app = _build_agent_graph(llm, tools, engine=engine)

    from rich.console import Console
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.panel import Panel

    console = Console()

    if max_iterations is None:
        from builder.config import get_max_iterations

        max_iterations = get_max_iterations()

    # Use LangGraph's built-in thread tracking so the agent accumulates
    # conversation history automatically. The recursion_limit bounds a single
    # turn's model/tools alternation so a runaway loop stops gracefully instead
    # of hitting LangGraph's silent default of 25 super-steps (#56).
    thread_config = cast(
        RunnableConfig,
        {
            "configurable": {"thread_id": engine.state.session_id},
            "recursion_limit": _recursion_limit(max_iterations),
        },
    )

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
        """Print an agent reply: a slim marker plus left-indented markdown.

        Lighter than a full-width bordered panel so short answers don't get
        a big box; markdown is indented two spaces under a green marker.
        """
        if not content:
            return
        from rich.table import Table

        try:
            body: Any = Markdown(content)
        except Exception:
            body = content

        # Claude-Code style: the ● marker sits on the SAME line as the first
        # line of the reply, and continuation lines align under it. A 2-wide
        # gutter column holds the marker; the body column wraps beside it.
        grid = Table.grid(padding=(0, 0))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row("[green]●[/green]", body)

        console.print()  # breathing room above the reply
        console.print()
        console.print(grid)
        console.print()  # ...and below

    def _render_header() -> None:
        """Print a compact one-line status header before each user prompt.

        Re-rendered each turn (a recurring header rule rather than a pinned
        bar, which would conflict with ``console.input``/``console.status``).
        Shows session id, entity/file counts, validation state, and token
        usage with estimated cost.
        """
        ec = len(engine.state.list_entities())
        fc = len(engine.state.scanned_files)
        val = engine.state.validation

        def _dot(ok: bool) -> str:
            return "[green]●[/green]" if ok else "[grey50]○[/grey50]"

        sep = "[grey42]·[/grey42]"
        # Token usage with estimated cost (read from profile.ndjson)
        token_str = ""
        try:
            from builder.tools.dashboard import read_profile
            from builder.tools.profiler import SESSION_DIR

            profile_path = SESSION_DIR / engine.state.session_id / "profile.ndjson"
            if profile_path.exists():
                prof_records = read_profile(profile_path)
                if prof_records:
                    # Aggregate cumulative tokens
                    model_ends = [
                        r for r in prof_records
                        if r.get("event") == "node_end" and r.get("node") == "model"
                    ]
                    total_in = sum(int(r.get("input_tokens", 0) or 0) for r in model_ends)
                    total_out = sum(int(r.get("output_tokens", 0) or 0) for r in model_ends)
                    last_model = (model_ends[-1].get("model_name") or "") if model_ends else ""
                    if total_in + total_out > 0:
                        from builder.config import get_model_provider
                        from builder.pricing import compute_cost, format_cost

                        mp = get_model_provider()
                        cost_info = compute_cost(total_in, total_out, last_model, provider=mp)
                        total_cost = cost_info.get("total_cost")
                        cost_str = f"@{format_cost(total_cost)}" if total_cost is not None else ""
                        token_str = (
                            f"  {sep}  [dim]tok {total_in}→{total_out} ({total_in + total_out})"
                            f"{cost_str}[/dim]"
                        )
        except Exception:
            pass

        status = (
            f"[dim]{engine.state.session_id}[/dim]  {sep}  "
            f"[dim]{ec} entities[/dim]  {sep}  "
            f"[dim]{fc} files[/dim]  {sep}  "
            f"{_dot(val.base_passed)} [dim]base[/dim]  "
            f"{_dot(val.isa_passed)} [dim]ISA[/dim]  "
            f"{_dot(val.tox_passed)} [dim]Tox[/dim]"
            f"{token_str}"
        )
        # A dim, indented status line with breathing room above — lighter
        # than a full-width rule, closer to the Claude Code aesthetic.
        console.print()
        console.print(Padding(status, (0, 0, 0, 1)))

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
        if val.base_passed:
            val_status.append("[green]base[/green]")
        else:
            val_status.append("[red]base[/red]")
        if val.isa_passed:
            val_status.append("[green]ISA[/green]")
        else:
            val_status.append("[red]ISA[/red]")
        if val.tox_passed:
            val_status.append("[green]ISA-Tox[/green]")
        else:
            val_status.append("[red]ISA-Tox[/red]")
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

        console.print(
            Panel(summary, title="[yellow]Resumed Session[/yellow]", border_style="yellow")
        )
        console.print()

        # Tell the LLM about the current state so it can give a contextual greeting
        greeting_prompt = (
            f"The user has resumed a session with {entity_count} entities and "
            f"{file_count} scanned files. "
            f"Validation: base={'pass' if val.base_passed else 'fail'}, "
            f"ISA={'pass' if val.isa_passed else 'fail'}, "
            f"Tox={'pass' if val.tox_passed else 'fail'}. "
            f"Required issues: {len(val.required_issues)}. "
            f"Entity breakdown: {counts}. "
            "Briefly welcome them back and summarise what has been done "
            "and what the next logical step is."
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
        spinner = _ThinkingSpinner(console, "intoxicating")
        greeting_config = {
            **thread_config,
            "callbacks": [_ToolSpinnerCallback(spinner)],
        }
        with spinner:
            result = app.invoke(
                {"messages": [HumanMessage(content=greeting_prompt)]},
                greeting_config,
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
            t.add_row(
                "Resume:",
                f"python -m main [cyan]--resume {session_id}[/cyan] [dim]--interactive[/dim]",
            )

        console.print(Panel(t, title="[yellow]Goodbye![/yellow]", border_style="yellow"))
        console.print()

    # ── Main loop ───────────────────────────────────────────────────────
    while True:
        try:
            # Compact status header above each prompt (counts live here now,
            # so the prompt line stays clean).
            _render_header()
            console.print()
            # Rounded input box (Claude Code style); falls back to a plain
            # prompt when not a TTY. Raises KeyboardInterrupt / EOFError.
            user_input = _boxed_input(console)
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

        try:
            import random

            TOX_SPINNER_PHRASES = [
                "intoxicating",
                "ro-creating",
                "culturing",
                "FAIR-washing",
                "re-FAIR-ifying",
                "blaming the student",
                "appeasing the cells",
                "fighting reviewer 2",
                "haggling the IC50",
                "bribing the curve",
                "vortexing",
                "rehydrating",
                "titrating",
                "resuspending",
                "denaturing self-doubt",
                "autoclaving",
                "thawing the -80",
                "centrifuging",
                "pipetting",
                "decoding",
                "finding a working pipette",
                "side-eyeing",
                "labelling 'compound X'",
                "miscounting colonies",
                "warming up, emotionally",
                "manifesting p<0.05",
                "praying to FAIR gods",
                "hallucinating responsibly",
                "FAIR-ifying",
                "exposing",
                "meta-dating",
                "re-using",
                "finding",
                "interoperating",
                "re-using, eventually",
                "double-gloving",
                "brewing coffee",
                "replacing, reducing, refusing",
            ]

            # Temporarily mute WARNING+ logs to avoid interleaving with spinner
            root_logger = logging.getLogger()
            old_root_level = root_logger.level
            root_logger.setLevel(logging.ERROR)

            # Create a fresh thinking spinner + callback for this iteration
            spinner = _ThinkingSpinner(console, random.choice(TOX_SPINNER_PHRASES))
            main_config = {
                **thread_config,
                "callbacks": [_ToolSpinnerCallback(spinner)],
            }
            with spinner:
                result = app.invoke(
                    {"messages": [HumanMessage(content=user_input)]},
                    main_config,
                )

            root_logger.setLevel(old_root_level)
            reply = _extract_reply(result)
            if reply:
                _print_reply(reply)

            try:
                from builder.tools.session import save_session

                result = save_session(engine.state)
                if not result.get("success", True):
                    logger.warning("Session save failed: %s", result.get("error", "Unknown error"))
                    console.print(
                        "[dim]⚠ Session autosave failed: "
                        f"{result.get('error', 'Unknown error')}[/dim]"
                    )
            except Exception:
                # Fallback: logging is best-effort
                logger.exception("Unexpected error during session save")

        except GraphRecursionError:
            # The turn hit the recursion_limit safety net — stop gracefully
            # instead of surfacing a raw error, and persist the session.
            root_logger.setLevel(old_root_level)
            console.print(
                "[yellow]I reached the step limit for this request[/yellow] and "
                "stopped to avoid an endless loop. Your session is saved — try a "
                "smaller or more specific request, or ask me to continue."
            )
            try:
                from builder.tools.session import save_session

                result = save_session(engine.state, always_write=True)
                if not result.get("success", True):
                    logger.warning(
                        "Session save on recursion error failed: %s",
                        result.get("error", "Unknown error"),
                    )
            except Exception:
                logger.exception("Unexpected error during session save on recursion")
            console.print()
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
