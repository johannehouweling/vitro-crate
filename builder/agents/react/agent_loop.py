"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import logging
import threading
from time import perf_counter
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

from builder.agents import ui
from builder.agents.llm import (
    _build_chat_model,
    _extract_model_name,
    _extract_token_usage,
    _get_request_timeout,
    _recursion_limit,
)
from builder.agents.progress_spinner import ProgressSpinner
from builder.agents.react.system_prompt import SYSTEM_PROMPT
from builder.agents.react.tools_spec import TOOL_SPECS, assert_tool_spec_parity
from builder.engine import AgentEngine

if TYPE_CHECKING:
    from typing import cast

    from pydantic import BaseModel

    from builder.state import CrateState


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
# Tool-activity callback — bridges LangChain tool events to the shared spinner
# ---------------------------------------------------------------------------


class _ToolSpinnerCallback(BaseCallbackHandler):
    """Forward LangChain tool-start/end events to the shared progress spinner.

    The spinner is :class:`builder.agents.progress_spinner.ProgressSpinner`, the
    SAME live spinner the deterministic pipeline drives (#344) — here it is fed the
    per-tool signal the ReAct loop gets from LangChain callbacks (the pipeline
    feeds the same spinner from ``engine.on_tool_event``). Clearing on tool-end
    returns the line to the rotating thinking phrase.
    """

    def __init__(self, spinner: ProgressSpinner) -> None:
        self.spinner = spinner
        super().__init__()

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        self.spinner.set_current(serialized.get("name", "tool"))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.spinner.set_current(None)


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


# File-reading tools that hand back a bare ``None`` for files that are missing,
# too large, or binary/corrupt. A bare ``None`` gives a weak model nothing to act
# on, so it re-calls the tool forever and hits the iteration cap (#101, #148).
_FILE_READ_TOOLS = frozenset({"read_file_sample", "read_file", "read_excel", "read_docx"})

# Attribute stamped on the engine once the crate has been exported this session,
# so the deterministic finish backstop (#251) is idempotent across the two exit
# paths (quit/exit and EOF) and never double-exports — whether the export came
# from the agent itself or from the backstop.
_EXPORTED_FLAG = "_crate_exported_this_session"

# Attribute holding the CONTENT fingerprint of the last in-loop auto-export
# (#287 Fix A; #380). A completed build auto-exports the crate to disk so the
# user always gets a crate (the deterministic pipeline already exports on every
# completed build, #233); this fingerprint makes the auto-export idempotent —
# it re-exports only when the crate has changed since the last auto-export
# (so the *latest* crate always lands), never twice for the same build.
# It is `CrateState.export_fingerprint()`, NOT an entity count: a count is
# invariant under every field-level tool this arm is told to use for the rest of
# the session (`set_fields`, `set_crate_metadata`, `fix_required_issues`,
# `link`), so counting silently kept all of that work off disk.
_AUTO_EXPORT_FINGERPRINT_FLAG = "_crate_auto_export_fingerprint"

# Attribute holding the optional single-arg console sink the loop installs so an
# in-loop auto-export can surface the absolute crate path to the user (#287 Fix
# A). Default ``None`` is a strict no-op (mirrors ``on_tool_event``, #266), so a
# headless/test engine without a console behaves identically.
_AUTO_EXPORT_EMIT_FLAG = "on_auto_export"

# ---------------------------------------------------------------------------
# Issue #287 Fix B: loop-breaker for repeated non-progress tool calls
# ---------------------------------------------------------------------------

# How many CONSECUTIVE identical non-progress tool calls (same tool name + same
# args returning the same directory/None/error result) the loop tolerates before
# it intervenes. A weak model looped ~36× on a directory/non-existent read,
# burning millions of tokens; a small threshold breaks that fast without tripping
# on legitimately-repeated DISTINCT calls or a single normal retry.
_LOOP_BREAKER_THRESHOLD = 3

# Attributes holding the loop-breaker's per-engine detection state: the signature
# (tool name + sorted args) of the last call and the consecutive-repeat count of
# the same non-progress result. Kept on the engine so the state survives across
# the per-call tool closures and resets cleanly when a distinct/progress call
# arrives.
_LOOP_BREAKER_LAST_SIG_FLAG = "_loop_breaker_last_signature"
_LOOP_BREAKER_COUNT_FLAG = "_loop_breaker_repeat_count"

# ---------------------------------------------------------------------------
# Issue #263: stall recovery (Fix A) + autonomous continuation (Fix B)
# ---------------------------------------------------------------------------

# Maximum autonomous (non-prompted) re-invocations the loop will chain off a
# single user message before checking back in with the user. Bounds the
# auto-continue so narration can never spin forever (Fix B).
_MAX_AUTONOMOUS_TURNS = 15

# How many consecutive empty completions (no tool calls and ~empty text) end the
# turn gracefully. The first empty is one strike; we retry ONCE, so the second
# empty stops the auto-continue and lets the #254 finish-backstop run (Fix A).
_MAX_EMPTY_COMPLETIONS = 2

# Internal directive injected on an autonomous re-invocation (Fix B). It is NOT
# read from stdin — the loop continues the agent toward a complete, validated,
# exported crate and tells it to only ask when it genuinely needs input.
_AUTO_CONTINUE_DIRECTIVE = (
    "Continue working autonomously toward a complete, validated, and exported "
    "ISA-Tox RO-Crate. Take the next concrete step (draft the missing entities, "
    "wire the process chain, attach files, then build_and_validate and "
    "export_crate). Do not ask me to confirm routine steps — only ask a question "
    "if you genuinely need information that only I can provide."
)

# Lower-cased openers that mark an interrogative reply even without a trailing
# '?' (a weak model often drops the mark). Kept deliberately small and specific
# so plain narration ("Let me draft...") is never mistaken for a question.
_INTERROGATIVE_OPENERS = (
    "could you",
    "can you",
    "would you",
    "will you",
    "do you",
    "did you",
    "should i",
    "shall i",
    "which ",
    "what ",
    "where ",
    "when ",
    "who ",
    "how ",
    "are you",
    "is it",
    "please confirm",
    "please provide",
    "please specify",
    "let me know",
)


def _reply_is_question(reply: str | None) -> bool:
    """Return True when the agent's final reply is a genuine question to the user.

    Issue #263 (Fix B): after a turn ends the loop must decide whether to prompt
    the user or auto-continue. This is the deterministic heuristic for "the agent
    is actually asking me something":

    1. The (stripped) reply ends with ``?`` — the strongest signal; a trailing
       question mark anywhere on the last non-empty line counts so a question
       after a line of narration is still caught.
    2. OR the reply opens with a known interrogative phrase (``could you``,
       ``which``, ``please confirm`` …) — a fallback for when a weak model drops
       the question mark.

    Empty/whitespace-only replies are never questions (they are the stall
    symptom, handled by the empty-completion recovery). The heuristic is
    intentionally conservative: it errs toward auto-continue (narration) rather
    than re-prompting, because the bug being fixed is *over*-prompting.
    """
    if not reply:
        return False
    text = reply.strip()
    if not text:
        return False
    # A trailing '?' on the last non-empty line is the clearest question signal.
    last_line = text.splitlines()[-1].strip()
    if last_line.endswith("?"):
        return True
    lowered = text.lower()
    return any(lowered.startswith(opener) for opener in _INTERROGATIVE_OPENERS)


def _crate_is_complete(engine: AgentEngine) -> bool:
    """Return True when the crate has entities AND validation fully passes.

    Issue #263 (Fix B): completion short-circuits the autonomous loop so the
    agent stops re-invoking once there is nothing left to do. "Complete" means
    the in-memory crate is non-empty and the last write-back of
    ``state.validation`` (populated by ``build_and_validate`` via the #153
    write-back) shows all three profiles passing with no REQUIRED gaps. This is a
    pure read over engine state and never raises.
    """
    try:
        if not engine.state.list_entities():
            return False
        val = engine.state.validation
        return bool(
            val.base_passed and val.isa_passed and val.tox_passed and not val.required_issues
        )
    except Exception:  # noqa: BLE001 — a completeness probe must never raise.
        logger.debug("completeness probe failed", exc_info=True)
        return False


def _reply_is_empty_completion(reply: str | None) -> bool:
    """Return True when a turn produced no meaningful text (the stall symptom).

    A bare/whitespace reply with no tool activity is the empty completion the
    weak model emits when it stalls (#263 Fix A). We treat very short non-word
    replies as empty too (e.g. a lone ``.``).
    """
    if not reply:
        return True
    return not reply.strip()


def _invoke_with_timeout(
    app: Any,
    payload: dict[str, Any],
    config: Any,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, str]:
    """Run ``app.invoke(payload, config)`` under a wall-clock guard (#263 Fix A).

    The provider-level request timeout on the chat model is the first line of
    defence, but a hung graph (or a provider that ignores its own timeout) could
    still block the turn forever — which is exactly what happened in the reported
    run (349s+ with no response). This runs the invoke on a daemon worker thread
    and waits at most ``timeout`` seconds for it:

    - completes in time → ``(result, "ok")``
    - raises inside invoke → ``(None, "error")`` (the exception is logged, never
      propagated, so it can never escape the loop)
    - exceeds ``timeout`` → ``(None, "timeout")`` — the worker is abandoned as a
      daemon thread (it cannot block process exit) and the turn ends gracefully
      so the existing #254 finish-backstop can still run.

    This function NEVER raises and NEVER hangs longer than ``timeout``.
    """
    outcome: dict[str, Any] = {"result": None, "error": None}

    def _worker() -> None:
        try:
            outcome["result"] = app.invoke(payload, config)
        except BaseException as exc:  # noqa: BLE001 — captured, surfaced as "error".
            # Capture *everything* (including provider SDK errors) so nothing
            # escapes the worker thread and crashes the loop. Genuinely fatal
            # signals on the main thread (KeyboardInterrupt) are unaffected.
            outcome["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True, name="vitro-model-invoke")
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        logger.warning(
            "Model invoke exceeded %.1fs wall-clock timeout; ending turn gracefully",
            timeout,
        )
        return None, "timeout"
    if outcome["error"] is not None:
        logger.warning("Model invoke raised: %s", outcome["error"])
        return None, "error"
    return outcome["result"], "ok"


def _unreadable_file_message(path: str, tool_name: str = "read_file_sample") -> str:
    """Actionable message for the LLM when a file reader can't return text.

    The file-reading tools (read_file_sample / read_file / read_excel /
    read_docx) return a bare ``None`` for files that are missing, too large
    (>100MB), or binary/corrupt (e.g. .xls/.xlsx Office containers, GraphPad
    .prism/.pzf). A bare ``None`` gives the model nothing to act on, so a weak
    model re-calls the tool forever and hits the iteration cap (#101, #148).
    This turns it into a clear "stop, do something else" signal.
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1] or path or "the file"
    return (
        f"{tool_name} could not return text for '{name}'. It is missing, too "
        f"large (>100MB), or binary/corrupt — e.g. .xls/.xlsx are Office/zip "
        f"containers and .prism/.pzf are GraphPad Prism binaries. Do NOT retry "
        f"{tool_name} on it. Use list_scanned_files to see the inventory, try "
        f"read_excel/read_file for spreadsheets or Office docs, or skip this file "
        f"and continue drafting entities."
    )


# ---------------------------------------------------------------------------
# Issue #287 Fix A: auto-export the crate on every completed in-loop build
# ---------------------------------------------------------------------------


def _emit_auto_export(engine: AgentEngine, message: str) -> None:
    """Surface an in-loop auto-export status line via the engine's emit sink.

    The loop installs a single-arg sink at ``engine.<_AUTO_EXPORT_EMIT_FLAG>``
    (``console.print``); a missing/``None`` sink is a strict no-op (mirrors the
    #266 ``on_tool_event`` pattern), so a headless/test engine without a console
    behaves identically. A raising sink is swallowed — surfacing is UI chrome and
    must never break a tool call.
    """
    sink = getattr(engine, _AUTO_EXPORT_EMIT_FLAG, None)
    if sink is None:
        return
    try:
        sink(message)
    except Exception:  # noqa: BLE001 — a UI sink must never break a tool call.
        logger.debug("auto-export emit sink raised", exc_info=True)


def _auto_export_after_build(engine: AgentEngine, build_result: Any) -> None:
    """Export the crate to disk after a successful in-loop ``build_and_validate``.

    Issue #287 Fix A: the legacy ReAct loop only wrote a crate via the finish
    backstop, which runs on the EXIT path (quit/EOF). In a live run the user kept
    the session alive, the weak model never called ``export_crate``, and a fully
    built, base-VALID crate (70+ entities) was NEVER written. The deterministic
    pipeline already exports on every completed build (#233); this brings the
    legacy loop in line.

    Fires when, and only when:
      * the crate has entities (an empty crate has nothing to write), AND
      * the build passed BASE conformance (``build_result["conformance"]["base"]``
        — the same gate the pipeline uses; ISA/Tox may still have gaps but a
        base-valid crate is worth landing), AND
      * the crate has changed since the last auto-export (a CONTENT
        fingerprint over entities + metadata + the scanned-file inventory) — so
        the *latest* crate always lands and an unchanged repeat build never
        re-exports (idempotency).

    On a successful export the ``_EXPORTED_FLAG`` is stamped (so ``_finish_backstop``
    stays a no-op and never double-exports) and the resolved ABSOLUTE crate path is
    surfaced via the engine's emit sink. ``export_crate`` is called with no explicit
    path so it honors ``state.metadata.output_path`` (CLI ``--output`` / default
    ``<input>-ro-crate/``) then the session fallback. NEVER raises: an export
    failure is logged and surfaced but the build result still flows back to the
    model unchanged (the finish backstop can retry on exit).
    """
    try:
        if not isinstance(build_result, dict):
            return
        # A build error or a base-conformance miss must not export — only a
        # base-valid, non-empty crate is worth landing.
        if build_result.get("error"):
            return
        conformance = build_result.get("conformance") or {}
        if not conformance.get("base"):
            return
        if not engine.state.list_entities():
            return

        # Idempotency: re-export only when the crate changed since the last
        # auto-export, so the latest crate always lands and a repeat build of an
        # unchanged crate is a no-op (no double-export for the same build).
        fingerprint = engine.state.export_fingerprint()
        last = getattr(engine, _AUTO_EXPORT_FINGERPRINT_FLAG, None)
        if last == fingerprint and getattr(engine, _EXPORTED_FLAG, False):
            return

        # No explicit path → export_crate honors state.metadata.output_path
        # (CLI --output / default <input>-ro-crate/) then the session fallback.
        result = engine.run_tool("export_crate")
    except Exception as exc:  # noqa: BLE001 — auto-export must never break the turn.
        logger.warning("In-loop auto-export failed: %s", exc)
        return

    if isinstance(result, dict) and result.get("success"):
        # Stamp BEFORE surfacing so any later backstop call is a strict no-op,
        # and record the fingerprint so an unchanged repeat build won't re-export.
        setattr(engine, _EXPORTED_FLAG, True)
        setattr(engine, _AUTO_EXPORT_FINGERPRINT_FLAG, fingerprint)
        crate_path = result.get("crate_path")
        try:
            from pathlib import Path

            resolved = str(Path(crate_path).resolve()) if crate_path else crate_path
        except (OSError, TypeError, ValueError):
            resolved = crate_path
        logger.info("In-loop auto-export wrote crate to %s", resolved)
        _emit_auto_export(engine, f"Crate written to: {resolved}")
        return

    # export_crate returned a failure dict (it never raises by contract). Do NOT
    # stamp the flag — the finish backstop should still try on exit.
    error = (result or {}).get("error") if isinstance(result, dict) else result
    logger.warning("In-loop auto-export: export_crate failed: %s", error)
    _emit_auto_export(engine, f"Could not write the crate yet: {error}")


# ---------------------------------------------------------------------------
# Issue #287 Fix B: loop-breaker for repeated non-progress tool calls
# ---------------------------------------------------------------------------


def _is_non_progress_result(result: Any) -> bool:
    """Return True when a tool result represents NO forward progress (#287 Fix B).

    A weak model loops when a tool keeps handing back the same dead-end. Three
    shapes count as non-progress:

    1. An ``error`` dict — the wrapper turns a recoverable tool-body exception
       into ``{"error": ..., "tool": ...}`` (e.g. a non-existent path).
    2. A directory-guidance string — a reader handed a directory returns
       ``"<path> is a directory, not a file …"`` (#240/#281).
    3. An unreadable/None string — a reader that could not return text returns
       ``"<tool> could not return text …"`` (#101/#148).

    Anything else (real file content, a successful build dict, a list, …) is
    progress and resets the loop-breaker. The check is purely structural so it
    never raises.
    """
    if isinstance(result, dict):
        return "error" in result
    if isinstance(result, str):
        return "is a directory, not a file" in result or "could not return text" in result
    return False


def _call_signature(tool_name: str, kwargs: dict[str, Any]) -> tuple[str, tuple]:
    """A hashable signature for a tool call (name + sorted, stringified args).

    Args are stringified before sorting so unhashable values (lists/dicts a weak
    model may pass) can't break signature comparison — the loop-breaker only
    needs to know whether two calls are byte-identical, not to round-trip them.
    """
    try:
        items = tuple(sorted((str(k), str(v)) for k, v in kwargs.items()))
    except Exception:  # noqa: BLE001 — signature comparison must never raise.
        items = (("__repr__", repr(kwargs)),)
    return (tool_name, items)


def _loop_breaker_intervention(engine: AgentEngine, tool_name: str) -> str:
    """Build the corrective tool message that breaks a repeated non-progress loop.

    Injects the actual ``list_scanned_files`` inventory (the concrete file paths
    the model should read instead) plus a forceful directive to stop repeating the
    identical call and pick a real file path. The inventory is fetched via
    ``engine.run_tool('list_scanned_files')`` so it reflects the live scan; a
    failure degrades to the directive alone (never raises).
    """
    inventory_block = ""
    try:
        inv = engine.run_tool("list_scanned_files")
        if isinstance(inv, dict):
            files = inv.get("files") or []
            paths = [f.get("path") for f in files if isinstance(f, dict) and f.get("path")]
            if paths:
                shown = paths[:25]
                listed = "\n".join(f"  - {p}" for p in shown)
                more = (
                    f"\n  …and {len(paths) - len(shown)} more "
                    "(call list_scanned_files to page through them)."
                    if len(paths) > len(shown)
                    else ""
                )
                inventory_block = (
                    f"\nScanned files you can read by their EXACT path:\n{listed}{more}"
                )
    except Exception:  # noqa: BLE001 — the inventory is best-effort.
        logger.debug("loop-breaker: list_scanned_files failed", exc_info=True)

    return (
        f"STOP — you have called {tool_name} with the same arguments "
        f"{_LOOP_BREAKER_THRESHOLD} times in a row and it is not making progress "
        f"(the path is a directory, missing, or unreadable). Do NOT call "
        f"{tool_name} again with those arguments. Pick a CONCRETE file path from "
        f"the inventory below, or move on to drafting/validating entities."
        f"{inventory_block}"
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

    # Fail fast if the advertised specs have drifted from the tools the shared
    # engine can actually run (#327): the A/B must compare the *same* toolbox, and
    # a silently-missing schema means the LLM can never call an available tool.
    assert_tool_spec_parity()

    langchain_tools: list[BaseTool] = []

    for spec in TOOL_SPECS:
        spec_dict = cast(dict[str, Any], spec)
        name: str = cast(str, spec_dict["name"])
        description: str = cast(str, spec_dict.get("description", ""))
        params: dict[str, Any] = cast(dict[str, Any], spec_dict.get("parameters", {}))

        def _make_tool(tool_name: str, tool_desc: str, tool_params: dict) -> BaseTool:
            def _run(**kwargs: Any) -> Any:
                # Loop-breaker (#287 Fix B): if this is the Nth consecutive
                # IDENTICAL call that has been returning a non-progress result
                # (directory/None/error), REFUSE to repeat it — return a forceful
                # corrective tool message with the actual scanned-file inventory so
                # a weak model stops looping (it ignored #281's directory message
                # and looped ~36×). Distinct calls / a single retry never trip this.
                signature = _call_signature(tool_name, kwargs)
                last_sig = getattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, None)
                repeat_count = getattr(engine, _LOOP_BREAKER_COUNT_FLAG, 0)
                if last_sig == signature and repeat_count >= _LOOP_BREAKER_THRESHOLD:
                    # Do NOT run the tool again — the identical non-progress call
                    # is short-circuited and the model is steered elsewhere.
                    return _loop_breaker_intervention(engine, tool_name)

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
                    result = {"error": str(exc), "tool": tool_name}
                else:
                    # scan_files returns the full list[FileClassification] (already
                    # stored in state); hand the LLM a compact summary instead of
                    # the raw blob so it gets a clear success signal and does not
                    # re-scan in a loop.
                    if tool_name == "scan_files" and isinstance(result, list):
                        from builder.tools.scanner import summarize_scan_result

                        result = summarize_scan_result(result)
                    # The file-reading tools return None for missing/oversized/
                    # binary files; hand the LLM an actionable message so it stops
                    # re-calling them (#101, #148).
                    elif tool_name in _FILE_READ_TOOLS and result is None:
                        result = _unreadable_file_message(kwargs.get("path", ""), tool_name)
                    # When the agent itself successfully exports, stamp the engine
                    # so the deterministic finish backstop (#251) does not
                    # double-export on session exit. Also record the entity-count
                    # fingerprint so an unchanged follow-up build_and_validate does
                    # not redundantly re-export (#287 Fix A idempotency).
                    elif (
                        tool_name in ("export_crate", "build_crate")
                        and isinstance(result, dict)
                        and result.get("success")
                    ):
                        setattr(engine, _EXPORTED_FLAG, True)
                        try:
                            setattr(
                                engine,
                                _AUTO_EXPORT_FINGERPRINT_FLAG,
                                engine.state.export_fingerprint(),
                            )
                        except Exception:  # noqa: BLE001 — fingerprint is best-effort.
                            logger.debug("fingerprint stamp failed", exc_info=True)
                    # Auto-export on every completed in-loop build (#287 Fix A):
                    # the user kept the session alive and the weak model never
                    # called export_crate, so a base-valid 70+-entity crate never
                    # landed. Mirror the deterministic pipeline (#233) — write the
                    # crate whenever build_and_validate passes base conformance.
                    elif tool_name == "build_and_validate":
                        _auto_export_after_build(engine, result)

                # Update the loop-breaker detection state AFTER post-processing so
                # it sees the same message the model sees. A repeated identical
                # non-progress call increments the streak; any distinct call or a
                # progress result resets it.
                if last_sig == signature and _is_non_progress_result(result):
                    setattr(engine, _LOOP_BREAKER_COUNT_FLAG, repeat_count + 1)
                elif _is_non_progress_result(result):
                    setattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, signature)
                    setattr(engine, _LOOP_BREAKER_COUNT_FLAG, 1)
                else:
                    setattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, None)
                    setattr(engine, _LOOP_BREAKER_COUNT_FLAG, 0)

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
            input_tokens, output_tokens = _extract_token_usage(last_msg)
            model_name = _extract_model_name(last_msg)
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


def _completeness_nudge(state: CrateState) -> str:
    """Compute a short deterministic present/missing/next-action steering line.

    Issue #251: once the obvious entities exist, a weak ReAct model tends to
    stall (empty completions) instead of advancing to the process chain, file
    attachments, validation, and export — so a crate never lands. This collapses
    the current ``CrateState`` into ONE token-cheap line naming what is present
    (with a ✓), what is still missing, and the single concrete *next tool* to
    call, e.g.::

        backbone ✓, person ✓, 22 compounds ✓; missing: process chain, file
        attachments → next: draft_process_chain

    When the crate looks complete (backbone + person + compounds + a process
    chain + file attachments) the next action becomes validate + export. The
    line is deterministic (no LLM), idempotent, and never raises — it is a pure
    read over the entity store.
    """
    counts: dict[str, int] = {}
    for ent in state.list_entities():
        typ = getattr(ent, "type", "Unknown")
        counts[typ] = counts.get(typ, 0) + 1

    has_backbone = any(counts.get(t) for t in ("Investigation", "Study", "Assay"))
    has_person = bool(counts.get("Person"))
    n_compounds = counts.get("MolecularEntity", 0)
    n_cells = counts.get("CellLineSample", 0)
    has_process = any(counts.get(t) for t in ("LabProcess", "LabProtocol"))
    has_files = bool(counts.get("File"))

    present: list[str] = []
    if has_backbone:
        present.append("backbone ✓")
    if has_person:
        present.append("person ✓")
    if n_compounds:
        present.append(f"{n_compounds} compounds ✓")
    if n_cells:
        present.append(f"{n_cells} cell line(s) ✓")

    missing: list[str] = []
    if not has_backbone:
        missing.append("backbone")
    if not has_person:
        missing.append("person/org")
    if not n_compounds and not n_cells:
        missing.append("compounds/cell lines")
    if not has_process:
        missing.append("process chain")
    if not has_files:
        missing.append("file attachments")
    # Validation + export are always the closing steps until the crate lands.
    missing.append("validation")
    missing.append("export")

    # Pick the single highest-priority next action (drives the weak model to ONE
    # concrete step instead of re-deriving the whole plan).
    if not has_backbone:
        next_action = "scaffold_isa_backbone"
    elif not n_compounds and not n_cells:
        next_action = "resolve_compound / draft_cell_line_sample"
    elif not has_person:
        next_action = "draft_person"
    elif not has_process:
        next_action = "draft_process_chain"
    elif not has_files:
        next_action = "attach_files"
    else:
        # The crate looks complete — close it out.
        next_action = "build_and_validate then export_crate"

    present_str = ", ".join(present) if present else "nothing yet"
    return f"[Completeness: {present_str}; missing: {', '.join(missing)} → next: {next_action}]"


def _build_system_prompt_with_state(
    session_id: str,
    entity_count: int,
    file_count: int,
    iteration_count: int,
    next_fix: str | None = None,
    nudge: str | None = None,
) -> str:
    """Build a lightweight state brief appended to the system prompt.

    This is called on every model invocation (not persisted in history),
    giving the LLM awareness of current session state without accumulating
    duplicate metadata in MemorySaver.

    Returns a single short line like:
    ``[Session: sid | Files: 5 | Entities: 3 | Iteration: 42]``

    When ``next_fix`` is given (the top REQUIRED validation issue, surfaced from
    ``state.validation`` after the #153 write-back), a second line names it so a
    weak model has a durable next-step pointer and stops re-deriving the
    BASE->ISA->TOX plan from the system prompt every turn.

    When ``nudge`` is given (the deterministic present/missing/next-action line
    from :func:`_completeness_nudge`, #251), it is appended as a third line so a
    weak model is steered to the next concrete step instead of stalling once the
    obvious entities exist.
    """
    brief = (
        f"[Session: {session_id} | "
        f"Files: {file_count} | "
        f"Entities: {entity_count} | "
        f"Iteration: {iteration_count}]"
    )
    if next_fix:
        brief += f"\n[Next REQUIRED fix: {next_fix}]"
    if nudge:
        brief += f"\n{nudge}"
    return brief


# Tool names whose verbose output already lives in CrateState, so replaying it
# verbatim in the transcript is pure waste once the model has consumed it. The
# scan inventory is the canonical example — the full listing is stored in
# CrateState.scanned_files, queryable via list_scanned_files (Issue #61, #172).
_STATE_BACKED_TOOLS = frozenset({"scan_files", "read_file_sample", "read_multiple_files"})

# Above this length (chars), a consumed state-backed tool output is pruned to a
# stub. Kept modest so small/already-summarized outputs pass through untouched.
_PRUNE_CONTENT_THRESHOLD = 500


def _prune_state_backed_outputs(messages: list) -> list:
    """Replace verbose, already-consumed state-backed tool outputs with a stub.

    A ``ToolMessage`` whose ``name`` is in ``_STATE_BACKED_TOOLS`` (scan/read
    listings) carries data that is already persisted in ``CrateState``; once the
    model has seen it there is no value in replaying the raw blob every turn. We
    keep the message (so the AI tool_call → ToolMessage pairing is never broken)
    but shrink its content to a short stub. Pairing-preservation is why we
    *rewrite* rather than *drop* — dropping a ToolMessage would orphan its
    preceding AI tool_call.

    The list is returned with the same length and ordering; non-matching
    messages are passed through unchanged. Small outputs (below
    ``_PRUNE_CONTENT_THRESHOLD``) are left intact — they cost little and may
    already be summaries (e.g. ``summarize_scan_result``).
    """
    from langchain_core.messages import ToolMessage

    pruned: list = []
    for msg in messages:
        if (
            isinstance(msg, ToolMessage)
            and getattr(msg, "name", None) in _STATE_BACKED_TOOLS
            and len(str(msg.content)) > _PRUNE_CONTENT_THRESHOLD
        ):
            scan_hint = (
                " Call list_scanned_files to retrieve the full file inventory "
                "(paginated/filterable)."
                if msg.name == "scan_files"
                else ""
            )
            stub = (
                f"[{msg.name} output pruned from history to save tokens — the full "
                f"result is stored in the session state (CrateState).{scan_hint} "
                f"Do not re-run {msg.name}.]"
            )
            pruned.append(
                ToolMessage(
                    content=stub,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
            )
        else:
            pruned.append(msg)
    return pruned


def _trim_history(messages: list, *, max_tokens: int) -> list:
    """Bound the per-turn message history so verbose tool outputs aren't replayed.

    Two layers, in order (Issue #61):

    1. **Prune consumed verbose tool outputs** — scan/read listings already live
       in ``CrateState``, so their bodies are replaced by a short stub
       (``_prune_state_backed_outputs``). The messages themselves are kept so
       AI(tool_call) → ToolMessage pairing is preserved.
    2. **Token-budget trim** — keep the most recent messages within
       ``max_tokens`` using ``langchain_core.messages.trim_messages`` with
       ``strategy="last"`` and ``start_on="human"``. ``start_on="human"``
       guarantees the trimmed window never *begins* with a dangling
       ``ToolMessage`` (or an ``AIMessage`` whose tool_call lost its answer),
       i.e. it never produces orphaned tool messages — the provider API rejects
       those.

    The leading system prompt and trailing state brief are added by
    ``_assemble_model_messages`` *around* the result, so they are intentionally
    not part of ``messages`` here and the cache-friendly #60 layout is preserved.

    Args:
        messages: The accumulated conversation history (no system messages).
        max_tokens: Approximate token budget for the retained history.

    Returns:
        A bounded, orphan-free subsequence of the (pruned) history.
    """
    from langchain_core.messages import trim_messages

    if not messages:
        return []

    pruned = _prune_state_backed_outputs(messages)

    try:
        trimmed = trim_messages(
            pruned,
            max_tokens=max_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
    except (ValueError, KeyError) as exc:
        # Defensive: never let a trimming edge case abort the turn. Falling back
        # to the pruned (but untrimmed) history keeps the agent running; the
        # pruning alone already removes the heaviest verbose payloads.
        logger.warning("History trim failed (%s); using pruned history", exc)
        return pruned

    return list(trimmed)


def _assemble_model_messages(
    messages: list,
    *,
    session_id: str,
    entity_count: int,
    file_count: int,
    iteration_count: int,
    next_fix: str | None = None,
    nudge: str | None = None,
    max_history_tokens: int | None = None,
) -> list:
    """Assemble the message list for a model invocation with a cache-friendly
    layout (Issue #60) and a bounded history (Issue #61).

    Layout: ``[SystemMessage(SYSTEM_PROMPT), *trimmed_history, SystemMessage(state_brief)]``.

    The leading system message is kept **byte-stable** (``SYSTEM_PROMPT`` only, no
    volatile state appended), so every provider can cache the stable
    ``tools + system + history`` prefix across turns (history is append-only). The
    per-turn state brief (session id, counts, iteration) — which changes every
    iteration — is placed as the **last** message, where it cannot bust the prefix
    cache. Previously the brief was appended to the system message, so the cache
    broke right after the prompt and the (growing, expensive) history was never
    cached.

    The history between the prefix and the brief is **trimmed** to
    ``max_history_tokens`` (``_trim_history``) so per-turn input tokens stay
    bounded over a long session and consumed verbose tool outputs (scan
    listings) are pruned rather than replayed verbatim (Issue #61). Trimming
    keeps the *most recent* turns, so the cacheable prefix only shifts when the
    history actually rolls over the budget — far less often than it grew before.

    Neither the system message nor the brief is persisted into MemorySaver
    history — both are rebuilt fresh on every invocation, so they never accumulate
    (Issue #66). Only the model's response is returned to the reducer.
    """
    from langchain_core.messages import SystemMessage

    if max_history_tokens is None:
        from builder.config import get_max_history_tokens

        max_history_tokens = get_max_history_tokens()

    trimmed_history = _trim_history(list(messages), max_tokens=max_history_tokens)

    state_brief = _build_system_prompt_with_state(
        session_id=session_id,
        entity_count=entity_count,
        file_count=file_count,
        iteration_count=iteration_count,
        next_fix=next_fix,
        nudge=nudge,
    )
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        *trimmed_history,
        SystemMessage(content=state_brief),
    ]


# Progressive tool disclosure (#156). Tools pruned from the per-turn advertised
# set when the state they act on does not exist yet — a weak model picks more
# reliably from a smaller, state-relevant menu. Only provably-inapplicable tools
# are pruned; uncategorised tools are always advertised, and the ToolNode keeps
# the full set so execution is never blocked (advertise narrow, execute wide).
_FILE_READING_TOOLS = frozenset(
    {
        "read_file_sample",
        "read_multiple_files",
        "read_file",
        "read_excel",
        "read_docx",
        "extract_pdf_text",
        "preview_archive",
        "unzip_file",
    }
)
_ENTITY_DEPENDENT_TOOLS = frozenset(
    {
        "set_fields",
        "remove_entity",
        "link",
        "attach_files",
        "check_provenance",
        "verify_all_identifiers",
        "assess_mit_coverage",
        "assess_fair_maturity",
        "validate",
        "validate_table",
        "export_crate",
        "build_crate",
        "list_entities",
    }
)


def _tools_for_state(tools: list[Any], *, has_files: bool, has_entities: bool) -> list[Any]:
    """Return the subset of *tools* worth advertising for the current state.

    Prunes only tools that provably cannot act yet — file readers when nothing
    has been scanned, entity-dependent tools when no entity exists — so the menu
    never hides something the model could legitimately use. Scanning, every
    drafter, lookups, the build/validate loop, session + HITL, and any
    uncategorised tool stay advertised. Binding is per-turn; the ToolNode keeps
    the full set, so a tool_call from an earlier turn's wider binding still runs.
    """
    out: list[Any] = []
    for t in tools:
        name = getattr(t, "name", "")
        if not has_files and name in _FILE_READING_TOOLS:
            continue
        if not has_entities and name in _ENTITY_DEPENDENT_TOOLS:
            continue
        out.append(t)
    return out


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
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import START, StateGraph
    from langgraph.prebuilt import ToolNode

    profiler = engine.profiler if engine is not None else None
    iteration_getter = (lambda: engine.state.iteration_count) if engine is not None else None

    # Tools are bound to the model *inside* call_model (not here) so the
    # advertised set can be narrowed per-turn to the current state (#156).
    # Binding at all is essential: without it the model is never told the tools
    # exist, should_continue always routes to END, and the agent degrades to a
    # text-only chatbot that narrates "let me scan..." but never executes a tool
    # (the #71 regression). The ToolNode below keeps the full set, so a narrowed
    # advertisement never blocks execution.

    def call_model(state: dict[str, Any]) -> dict[str, Any]:
        """Model node: build a cache-friendly message list and invoke the LLM."""
        assert engine is not None, "AgentEngine must be set before call_model is invoked"
        messages = state.get("messages", [])
        # Stable SYSTEM_PROMPT prefix + history, with the volatile per-turn state
        # brief at the tail so the cacheable prefix isn't busted (Issue #60). The
        # brief is rebuilt each call and never persisted to history (Issue #66).
        # The top REQUIRED validation issue (populated by the #153 write-back) is
        # surfaced in the brief as a durable next-step pointer for a weak model.
        required_issues = engine.state.validation.required_issues
        next_fix = required_issues[0] if required_issues else None
        # Deterministic completeness nudge (#251): present/missing/next-action,
        # so a weak model is steered to the next concrete step instead of
        # stalling once the obvious entities exist.
        nudge = _completeness_nudge(engine.state)
        model_messages = _assemble_model_messages(
            messages,
            session_id=engine.state.session_id,
            entity_count=len(engine.state.list_entities()),
            file_count=len(engine.state.scanned_files),
            iteration_count=engine.state.iteration_count,
            next_fix=next_fix,
            nudge=nudge,
        )
        # Progressive tool disclosure (#156): advertise only the state-relevant
        # subset so a weak model chooses from a smaller menu. Bind per-turn; the
        # ToolNode keeps the full set (advertise narrow, execute wide).
        active_tools = _tools_for_state(
            tools,
            has_files=bool(engine.state.scanned_files),
            has_entities=bool(engine.state.list_entities()),
        )
        model = llm.bind_tools(active_tools) if active_tools else llm
        response = model.invoke(model_messages)
        # Return only the new response; the add_messages reducer appends it
        return {"messages": [response]}

    tool_node = ToolNode(tools)

    # Use a typed state with add_messages reducer so ToolNode and model
    # both append to the message list rather than replacing it.
    graph: Any = StateGraph(AgentState)  # ty: ignore[invalid-argument-type]
    graph.add_node("model", _wrap_model_node(call_model, profiler, iteration_getter))
    graph.add_node("tools", _wrap_tools_node(tool_node, profiler, iteration_getter))

    # should_continue returns "tools" or END (the string "__end__").
    # Without a path_map, the return value is used as the destination node name.
    graph.add_conditional_edges("model", should_continue)
    graph.add_edge("tools", "model")
    graph.add_edge(START, "model")

    return graph.compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Deterministic finish backstop (#251)
# ---------------------------------------------------------------------------


def _finish_backstop(
    engine: AgentEngine,
    *,
    emit: Any = None,
) -> dict[str, Any] | None:
    """Deterministically build + export the crate on session end (#251).

    The weak ReAct model frequently stalls (empty completions) *before* ever
    choosing ``export_crate``, so a rich in-memory crate exits without anything
    landing on disk. This backstop guarantees a crate is always written when the
    session ends with un-exported entities:

    1. If the crate is **empty** (``state.list_entities()`` is falsy) there is
       nothing to write — return ``None`` (no build, no export).
    2. If the crate was **already exported this session** (the agent chose to, or
       the backstop already ran) — return ``None`` (idempotent, no double-export).
    3. Otherwise run ``build_and_validate`` then ``export_crate`` via
       ``engine.run_tool`` (so each is profiled and validation is cached).
       ``export_crate`` is called with no explicit path so it honors
       ``state.metadata.output_path`` (default ``<input>-ro-crate/``). The
       resolved ABSOLUTE crate path is surfaced via *emit*.

    This runs on the **exit path**, so it must NEVER raise: every failure is
    caught, logged, surfaced via *emit*, and reported as a ``{"success": False}``
    result (or ``None``). The export-success flag is stamped on the engine so a
    second call is a no-op.

    Args:
        engine: The engine whose ``state`` is built and written.
        emit: Optional single-arg sink for human-readable status lines (e.g.
            ``console.print`` or ``print``). Defaults to a no-op.

    Returns:
        The ``export_crate`` result dict on a fresh export, or ``None`` when
        there was nothing to export / it was already exported / it failed before
        producing a result.
    """
    say = emit or (lambda _msg: None)

    try:
        if not engine.state.list_entities():
            # Nothing to write — a clean no-op (e.g. user quit immediately).
            return None
        # (#380) Gate on the CONTENT fingerprint, not on "something exported this
        # session". This is the last chance to catch a crate that changed after
        # its auto-export; gating on the boolean meant that once anything had
        # landed, `quit` neither re-exported NOR re-validated, and every
        # field-level fix made after that point was silently lost.
        fingerprint = engine.state.export_fingerprint()
        if getattr(engine, _AUTO_EXPORT_FINGERPRINT_FLAG, None) == fingerprint:
            # The crate on disk is already this exact content — a strict no-op.
            return None

        say("Finalizing: building and exporting the crate before exit…")
        # Build + validate in memory first so the written crate is the validated
        # one (mirrors the deterministic pipeline's finish, §14.5/§14.6.1).
        try:
            engine.run_tool("build_and_validate")
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            # A validation hiccup must not block the export — log and continue.
            logger.warning("Finish backstop: build_and_validate failed: %s", exc)

        # No explicit path → export_crate resolves state.metadata.output_path
        # (CLI --output / default <input>-ro-crate/) then the session fallback.
        result = engine.run_tool("export_crate")

        if isinstance(result, dict) and result.get("success"):
            # Stamp BEFORE surfacing so any later call is a strict no-op. The
            # fingerprint must be stamped too (#380): the two exit paths (quit
            # and EOF) both reach here, and without it the second would see a
            # "change" and double-export — the #251 idempotency `_EXPORTED_FLAG`
            # was introduced to protect. Reusing the pre-build value is safe:
            # `_writeback_validation` touches only `state.validation`, which
            # `validation_fingerprint()` deliberately excludes, and `export_crate`
            # does not mutate state.
            setattr(engine, _EXPORTED_FLAG, True)
            setattr(engine, _AUTO_EXPORT_FINGERPRINT_FLAG, fingerprint)
            crate_path = result.get("crate_path")
            try:
                from pathlib import Path

                resolved = str(Path(crate_path).resolve()) if crate_path else crate_path
            except (OSError, TypeError, ValueError):
                resolved = crate_path
            say(f"Crate written to: {resolved}")
            logger.info("Finish backstop exported crate to %s", resolved)
            return result

        # export_crate returned a failure dict (it never raises by contract).
        error = (result or {}).get("error") if isinstance(result, dict) else result
        logger.error("Finish backstop: export_crate failed: %s", error)
        say(f"Could not write the crate on exit: {error}")
        return result if isinstance(result, dict) else None

    except Exception as exc:  # noqa: BLE001 — the exit path must never raise.
        # A crate failing to land must never crash the goodbye/quit flow.
        logger.exception("Finish backstop failed unexpectedly")
        say(f"Could not write the crate on exit: {exc}")
        return None


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


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
    # The wall-clock guard (#263 Fix A) uses the same finite timeout that is
    # wired onto the chat model, so the loop-level guard and the provider-level
    # request timeout agree.
    request_timeout = _get_request_timeout()
    llm = _build_chat_model(
        provider=provider, model=model, base_url=base_url, timeout=request_timeout
    )

    # Build the explicit StateGraph instead of using create_agent()
    # The system prompt is prepended by the model node on every invocation.
    # Passing the engine enables node-level timing → profile.ndjson.
    app = _build_agent_graph(llm, tools, engine=engine)

    from rich.panel import Panel

    console = ui.get_console()

    # In-loop auto-export surfacing (#287 Fix A): a completed in-loop build writes
    # the crate to disk and reports its absolute path via this sink. The export
    # fires deep inside ``app.invoke`` (under the Rich Live spinner), so we BUFFER
    # the line here and flush it from ``_run_turn`` once the spinner has torn down,
    # keeping the transcript clean. Installed on the engine like ``on_tool_event``
    # (#266); a headless engine leaves it ``None`` (a strict no-op).
    auto_export_lines: list[str] = []
    setattr(engine, _AUTO_EXPORT_EMIT_FLAG, auto_export_lines.append)

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
        """Pull the last AIMessage content from the agent state, as plain text.

        Flattens structured message content through the shared formatter so
        content-block lists never leak their repr to the terminal (#341).
        """
        msgs = state.get("messages", [])
        # Walk backwards to find an AI message (not tool results)
        for msg in reversed(msgs):
            if hasattr(msg, "content") and msg.content:
                # Skip messages from "tool" role
                role = getattr(msg, "type", "") or ""
                if role == "ai" or (isinstance(msg, AIMessage)):
                    return ui.flatten_message_content(msg.content)
                # Also accept the very last message if it has content
                if msg is msgs[-1]:
                    return ui.flatten_message_content(msg.content)
        return ""

    def _print_reply(content: str) -> None:
        """Print an agent reply through the shared renderer (empty → skip)."""
        if not content:
            return
        console.print(ui.render_reply(content))

    # ── Resume summary vs fresh greeting ────────────────────────────────
    entity_count = len(engine.state.list_entities())
    file_count = len(engine.state.scanned_files)
    is_resume = entity_count > 0 or file_count > 0

    if is_resume:
        val = engine.state.validation
        # Per-type entity breakdown (also feeds the greeting prompt + fallback).
        counts: dict[str, int] = {}
        for e in engine.state.list_entities():
            typ = getattr(e, "type", "Unknown")
            counts[typ] = counts.get(typ, 0) + 1

        ui.print_resume_summary(engine)

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
        spinner = ProgressSpinner(console, "intoxicating")
        greeting_config = {
            **thread_config,
            "callbacks": [_ToolSpinnerCallback(spinner)],
        }
        with spinner:
            # Wall-clock guard (#263 Fix A): a hung greeting must never block the
            # session from starting. On timeout/error we fall through to the
            # static fallback panel below.
            result, outcome = _invoke_with_timeout(
                app,
                {"messages": [HumanMessage(content=greeting_prompt)]},
                greeting_config,
                timeout=request_timeout,
            )
        root_logger.setLevel(old_root_level)
        reply = _extract_reply(result) if (outcome == "ok" and result) else ""
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

    def _finalize_on_exit() -> None:
        """Deterministically build + export the crate before the goodbye (#251).

        The weak ReAct model often stalls before it ever calls ``export_crate``,
        so a rich in-memory crate would exit unwritten. This guarantees a crate
        ALWAYS lands when the session ends with un-exported entities. It is
        idempotent (never double-exports) and never raises (the goodbye must
        always print).
        """
        _finish_backstop(engine, emit=console.print)

    def _run_turn(message_content: str) -> tuple[str, str]:
        """Run ONE model invocation and return ``(reply, outcome)`` (#263).

        Wraps the spinner, the wall-clock timeout guard (:func:`_invoke_with_timeout`),
        reply printing, and the best-effort session save. ``outcome`` is one of
        ``"ok"`` / ``"timeout"`` / ``"error"`` / ``"recursion"``. This NEVER
        raises and NEVER hangs longer than ``request_timeout`` — so the caller
        (the main loop / autonomous continuation) can always fall through to the
        #254 finish-backstop on the exit path.
        """
        # Temporarily mute WARNING+ logs to avoid interleaving with the spinner.
        root_logger = logging.getLogger()
        old_root_level = root_logger.level
        root_logger.setLevel(logging.ERROR)

        spinner = ProgressSpinner(console)
        main_config = {
            **thread_config,
            "callbacks": [_ToolSpinnerCallback(spinner)],
        }
        outcome = "ok"
        reply = ""
        try:
            with spinner:
                result, outcome = _invoke_with_timeout(
                    app,
                    {"messages": [HumanMessage(content=message_content)]},
                    main_config,
                    timeout=request_timeout,
                )
        except GraphRecursionError:
            # The turn hit the recursion_limit safety net — treat as a graceful
            # end so the loop stops auto-continuing and the backstop can run.
            outcome = "recursion"
        finally:
            root_logger.setLevel(old_root_level)

        # Flush any in-loop auto-export status lines buffered during the invoke
        # (#287 Fix A) now the spinner's Live region is gone, so "Crate written
        # to: <abs path>" lands cleanly in the transcript.
        while auto_export_lines:
            console.print(auto_export_lines.pop(0))

        if outcome == "ok" and result:
            reply = _extract_reply(result)
            if reply:
                _print_reply(reply)
        elif outcome == "timeout":
            console.print(
                "[yellow]The model stopped responding[/yellow] and I ended this "
                "step to avoid hanging. Your work so far is saved."
            )
            console.print()
        elif outcome == "recursion":
            console.print(
                "[yellow]I reached the step limit for this request[/yellow] and "
                "stopped to avoid an endless loop. Your session is saved — try a "
                "smaller or more specific request, or ask me to continue."
            )
            console.print()
        elif outcome == "error":
            console.print(
                "[yellow]I hit an error on that step[/yellow] and stopped. Your "
                "work so far is saved."
            )
            console.print()

        # Best-effort session autosave (always attempted, even on a bad outcome).
        try:
            from builder.tools.session import save_session

            save_result = save_session(engine.state, always_write=(outcome != "ok"))
            if not save_result.get("success", True):
                logger.warning("Session save failed: %s", save_result.get("error", "Unknown error"))
        except Exception:
            logger.exception("Unexpected error during session save")

        return reply, outcome

    # ── Main loop ───────────────────────────────────────────────────────
    while True:
        try:
            # Compact status header above each prompt (counts live here now,
            # so the prompt line stays clean).
            ui.print_status_bar(engine)
            console.print()
            # Rounded input box (Claude Code style); falls back to a plain
            # prompt when not a TTY. Raises KeyboardInterrupt / EOFError.
            user_input = ui.boxed_input(console)
        except KeyboardInterrupt:
            # Ctrl+C: clear the line and re-prompt
            console.print()
            continue
        except EOFError:
            # Ctrl+D: exit
            console.print()
            _finalize_on_exit()
            ui.print_goodbye(engine)
            break

        if user_input.lower() in ("quit", "exit", "q"):
            _finalize_on_exit()
            ui.print_goodbye(engine)
            break

        if not user_input:
            continue

        # ── One user message → possibly several model turns ─────────────────
        # Fix B (#263): after the first (user-driven) turn, decide deterministically
        # whether to PROMPT the user (a genuine question) or AUTO-CONTINUE the
        # agent without reading stdin (it just narrated/worked). The autonomous
        # run is bounded by _MAX_AUTONOMOUS_TURNS and stops as soon as the crate
        # is complete. Fix A (#263): consecutive empty completions (the stall
        # symptom) end the run after one retry so the #254 backstop can land the
        # crate. _run_turn never raises and never hangs past request_timeout, so
        # an exception can never escape this loop body.
        try:
            message = user_input
            empty_streak = 0
            for _autonomous_turn in range(_MAX_AUTONOMOUS_TURNS + 1):
                reply, outcome = _run_turn(message)

                # A non-ok outcome (timeout / error / recursion) ends the turn
                # gracefully; fall back to prompting the user.
                if outcome != "ok":
                    break

                # Empty-completion recovery (Fix A): retry ONCE, then stop.
                if _reply_is_empty_completion(reply):
                    empty_streak += 1
                    if empty_streak >= _MAX_EMPTY_COMPLETIONS:
                        logger.info(
                            "Ending turn after %d consecutive empty completions",
                            empty_streak,
                        )
                        break
                    message = _AUTO_CONTINUE_DIRECTIVE
                    continue
                empty_streak = 0

                # A genuine question → stop and prompt the user (current
                # behavior). A user-typed message still overrides next loop.
                if _reply_is_question(reply):
                    break

                # The crate is finished → stop auto-continuing and check in.
                if _crate_is_complete(engine):
                    logger.info("Crate complete — ending autonomous run")
                    break

                # Otherwise the agent just narrated/worked → AUTO-CONTINUE with
                # an internal directive, WITHOUT reading stdin. The cap on the
                # enclosing range bounds this so it can never spin forever.
                message = _AUTO_CONTINUE_DIRECTIVE
            else:
                # The for-loop exhausted the cap without breaking → check in.
                logger.info(
                    "Reached max autonomous turns (%d) — checking in with the user",
                    _MAX_AUTONOMOUS_TURNS,
                )
                console.print(
                    "[dim]I've worked autonomously for a while — let me know how "
                    "you'd like to proceed.[/dim]"
                )
                console.print()
        except KeyboardInterrupt:
            # Ctrl+C during a turn / the autonomous run: stop working and return
            # to the prompt so the user can interject (preserve interruptibility).
            console.print()
            console.print("[dim]Stopped — back to you.[/dim]")
            console.print()
        except Exception as exc:  # noqa: BLE001 — the loop must never crash.
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
]
