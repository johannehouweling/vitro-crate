"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Sequence, cast, overload

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
from builder.tools.hitl import is_interactive

if TYPE_CHECKING:
    from typing import cast

    from pydantic import BaseModel

    from builder.state import CrateState


from typing import TypedDict

from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


class _InvocationCancelled(Exception):
    """Stop an abandoned graph invocation at its next cooperative boundary."""


_invocation_cancel_event: ContextVar[threading.Event | None] = ContextVar(
    "vitro_invocation_cancel_event", default=None
)


def _raise_if_invocation_cancelled() -> None:
    """Abort work belonging to a timed-out model invocation, if requested."""
    event = _invocation_cancel_event.get()
    if event is not None and event.is_set():
        raise _InvocationCancelled("model invocation was cancelled after timeout")

_DIAGNOSTIC_MAX_CHARS = 1200
_SENSITIVE_DIAGNOSTIC_RE = re.compile(
    r"(?i)(?:bearer\s+|api[_ -]?key\s*[:=]\s*|authorization\s*[:=]\s*|cookie\s*[:=]\s*)[^\s,;]+"
    r"|sk-[A-Za-z0-9_-]+"
)


def _sanitize_diagnostic(value: str, *, limit: int = _DIAGNOSTIC_MAX_CHARS) -> str:
    """Redact common credentials and bound model-error diagnostics."""
    sanitized = _SENSITIVE_DIAGNOSTIC_RE.sub("[REDACTED]", value).replace("\x00", "")
    return sanitized if len(sanitized) <= limit else sanitized[:limit] + "…"


def _exception_chain(exc: BaseException) -> str:
    """Return bounded exception class/message pairs without request details."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {_sanitize_diagnostic(str(current), limit=400)}")
        current = current.__cause__ or current.__context__
    return _sanitize_diagnostic(" <- ".join(parts))


def _error_diagnostic(exc: BaseException, traceback_text: str | None) -> dict[str, str]:
    """Build safe, bounded diagnostics for a failed model/graph invocation."""
    traceback_lines = (traceback_text or "").splitlines()
    return {
        "exception_type": type(exc).__name__,
        "message": _sanitize_diagnostic(str(exc)),
        "exception_chain": _exception_chain(exc),
        "traceback_tail": _sanitize_diagnostic("\n".join(traceback_lines[-4:])),
    }


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
        tool_name = serialized.get("name", "tool")
        # LangChain supplies the rendered tool input separately from the
        # serialized tool metadata. Show a bounded version so a long-running
        # tool call tells the user both WHAT is running and WHAT it is acting
        # on, without allowing a large payload to take over the spinner.
        tool_input = str(input_str or "").replace("\n", " ").strip()
        if len(tool_input) > 80:
            tool_input = tool_input[:77] + "..."
        current = f"{tool_name}({tool_input})" if tool_input else tool_name
        self.spinner.set_current(current)

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.spinner.set_current(None)

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        # A new generation begins: drop the previous reply's tail so the line
        # never shows text from the step before, and say the model has the floor
        # — most calls here emit only tool calls, so there may be no text at all.
        self.spinner.begin_generation()

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any
    ) -> None:
        # LangChain dispatches chat models here, never to on_llm_start, and the
        # reset is the same — but this is declared separately rather than
        # aliased to on_llm_start. The base signature takes `messages`, not
        # `prompts`, so the alias was an LSP violation: a caller passing
        # `messages=` by keyword would have hit an unexpected-argument
        # TypeError. Both bodies ignore their payload, so it only ever mattered
        # to a keyword caller — but it made the class type-incorrect.
        self.spinner.set_preview(None)

    def on_llm_new_token(self, token: Any, **kwargs: Any) -> None:
        # Only fires when the model was built with streaming. The spinner keeps
        # the running text and repaints it on its own tick, so a fast token
        # stream cannot turn into a write per token.
        #
        # `token` is NOT always a string: the Responses API (every reasoning
        # model) streams content-block lists, and concatenating one raised a
        # TypeError per token. Flattening through the shared #341 helper also
        # drops reasoning/tool-call blocks, so only real text reaches the line.
        self.spinner.append_preview(ui.flatten_message_content(token))

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        # The reply is about to be printed in full to the transcript; a stale
        # tail of it hanging around under the spinner would just be noise.
        self.spinner.set_preview(None)


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
        # A zero-parameter tool still needs an EXPLICIT empty schema. Advertised
        # with no schema at all, the model invents a placeholder payload —
        # `get_status(args=[], kwargs=None)` — which the tool function rejects
        # with a TypeError. That cost 33 of 36 get_status calls in one session:
        # each one burned a model turn and returned an error the model then
        # tried to work around.
        from pydantic import BaseModel, create_model

        return create_model(f"{name}_args", __base__=BaseModel)

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
_VALIDATION_ESCALATION_FP_FLAG = "_validation_escalation_fingerprint"
_VALIDATION_ESCALATION_PURPOSE = "validation_escalation"

# The user's standing answers live on ``state.validation_preferences``
# (``{"recommended": bool, "optional": bool}``). Whether they want the broader
# tiers is a preference about how they work, not a judgement about one crate
# state, so it is asked once and then honoured silently — and revocable through
# the set_validation_preference tool.


# How many issue strings each escalation tier contributes to the summary handed
# back to the model. Bounded so a noisy RECOMMENDED/OPTIONAL pass cannot flood
# the tool message.
_ESCALATION_MAX_ISSUES = 5

# Appended to every escalation summary: the model reported only the REQUIRED
# tier because that was the only result it ever saw. The escalation tiers run
# out-of-band (engine.run_tool, not a model tool call), so they must be handed
# back explicitly or they stay invisible in the user-facing wrap-up.
_ESCALATION_REPORT_NOTE = (
    "The user opted into these broader checks. Report the RECOMMENDED and "
    "OPTIONAL results alongside the REQUIRED result in your summary to the "
    "user — do not present the crate as validated on the REQUIRED tier alone."
)


def _escalation_tier_summary(
    result: dict[str, Any],
    state_issues: list[str] | None,
) -> dict[str, Any]:
    """Compact per-tier payload: status, issue count, and a bounded issue list.

    Prefers the ordered issue strings already written back to
    ``state.validation`` (base -> isa -> tox, human-readable); falls back to the
    raw routable issues from the tool result when the write-back has not landed.
    """
    raw = result.get("issues") or []
    if state_issues:
        shown = [str(issue) for issue in state_issues[:_ESCALATION_MAX_ISSUES]]
    else:
        shown = [
            str(issue.get("message", issue)) if isinstance(issue, dict) else str(issue)
            for issue in raw[:_ESCALATION_MAX_ISSUES]
        ]
    tier: dict[str, Any] = {
        "status": "completed",
        "issue_count": len(raw),
        "issues": shown,
    }
    if len(raw) > len(shown):
        tier["not_shown"] = len(raw) - len(shown)
    return tier


def _run_validation_escalation(
    engine: AgentEngine, required_result: dict[str, Any]
) -> dict[str, Any] | None:
    """Offer progressively broader validation once required checks pass.

    Returns a compact per-tier summary of what actually ran (``None`` when no
    escalation happened at all) so the caller can fold it into the
    ``build_and_validate`` result the model receives. The RECOMMENDED/OPTIONAL
    passes are invoked directly on the engine rather than as model tool calls,
    so without this return value the model never learns they happened and its
    closing summary reports REQUIRED findings only.
    """
    if not required_result.get("ok") or not is_interactive(engine.human_interface):
        return None
    fingerprint = engine.state.validation_fingerprint()
    if getattr(engine, _VALIDATION_ESCALATION_FP_FLAG, None) == fingerprint:
        return None

    # A tier is offered ONCE per session. The answer is a standing preference,
    # not a per-state decision: keyed on the fingerprint alone, the same question
    # came back after every mutation that re-passed REQUIRED, so a long session
    # asked "Run recommended checks?" over and over having already been told yes.
    # Held on the state (not the engine) so it survives a --resume, and so the
    # set_validation_preference tool can revoke it when the user changes their
    # mind mid-session.
    prefs = engine.state.validation_preferences

    def approved(tier: str, context: str) -> bool:
        """The user's standing answer for *tier*, asked at most once per session."""
        if tier in prefs:
            return prefs[tier]
        response = engine.human_interface.present(
            context,
            options=["yes", "no"],
            purpose=_VALIDATION_ESCALATION_PURPOSE,
        )
        prefs[tier] = response.get("action") == "approved"
        return prefs[tier]

    if not approved(
        "recommended",
        "Required validation passed. Shall we now also work on the recommended checks?",
    ):
        setattr(engine, _VALIDATION_ESCALATION_FP_FLAG, fingerprint)
        return {
            "recommended": {"status": "declined_by_user"},
            "optional": {"status": "not_offered"},
            "note": (
                "The user declined the broader checks; the crate is validated on "
                "the REQUIRED tier only. Say so explicitly in your summary."
            ),
        }

    # This call is synchronous: the optional prompt cannot be reached until the
    # recommended validator has returned and its state writeback is complete.
    recommended = engine.run_tool(
        "build_and_validate", severity="recommended", profile="all"
    )
    if not isinstance(recommended, dict) or "error" in recommended:
        setattr(engine, _VALIDATION_ESCALATION_FP_FLAG, fingerprint)
        detail = recommended.get("error") if isinstance(recommended, dict) else None
        return {
            "recommended": {"status": "error", "detail": str(detail or "unknown error")},
            "optional": {"status": "not_reached"},
        }

    recommended_issues = recommended.get("issues") or []
    recommended_status = (
        f"{len(recommended_issues)} finding(s)"
        if recommended_issues
        else "no findings"
    )
    summary: dict[str, Any] = {
        "recommended": _escalation_tier_summary(
            recommended, engine.state.validation.should_issues
        ),
        "note": _ESCALATION_REPORT_NOTE,
    }
    # The recommended result can be unsuccessful because it found SHOULD issues;
    # that is still a completed tier and should be reported before asking about
    # the optional tier. Only tool errors abort the cascade.
    # However, when recommended findings EXIST, the optional pass is blocked:
    # running optional validation while SHOULD issues are unresolved creates
    # a confusing noise floor — the model is more likely to chase MAY-level
    # findings than to fix the SHOULD-tier gaps that matter more.  Block and
    # record the fingerprint so the escalation does not repeat on the next
    # required pass over the same state.
    # (Issue #NNN: fix validation escalation and repeated validation loops.)
    has_recommended_findings = bool(recommended_issues)
    if has_recommended_findings:
        summary["optional"] = {"status": "blocked_by_recommended_findings"}
    elif approved(
        "optional",
        "Recommended checks completed ("
        + recommended_status
        + "). Shall we now also work on the optional checks?",
    ):
        # As above, this call is synchronous and completes before the escalation
        # fingerprint is recorded or control returns to the model loop.
        optional = engine.run_tool("build_and_validate", severity="optional", profile="all")
        if isinstance(optional, dict) and "error" not in optional:
            summary["optional"] = _escalation_tier_summary(
                optional, engine.state.validation.may_issues
            )
        else:
            detail = optional.get("error") if isinstance(optional, dict) else None
            summary["optional"] = {
                "status": "error",
                "detail": str(detail or "unknown error"),
            }
    else:
        summary["optional"] = {"status": "declined_by_user"}
    setattr(engine, _VALIDATION_ESCALATION_FP_FLAG, fingerprint)
    return summary

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

# Consecutive identical list reads are handled separately from failed file reads:
# list_entities is a live query, so this is a guard, never a result cache.
_LIST_ENTITIES_BREAKER_THRESHOLD = 3
_LIST_ENTITIES_LAST_SIG_FLAG = "_list_entities_last_signature"
_LIST_ENTITIES_COUNT_FLAG = "_list_entities_repeat_count"

# ---------------------------------------------------------------------------
# ReAct-level guard: repeated unchanged build_and_validate calls
# ---------------------------------------------------------------------------

# Attribute holding ``{(severity, profile): state fingerprint when it last ran}``.
# Consecutive-repeat detection alone was evadable by ALTERNATING scopes: a
# session issued 10x (required, all) and 10x (required, base) against one
# unchanged crate, and because no two consecutive calls matched, the guard never
# fired. Keying on the state a scope was last validated against catches any
# order — and needs no invalidation, since a real mutation changes the
# fingerprint and every stored entry stops matching by construction.
_BUILD_VALIDATE_SEEN_FLAG = "_build_validate_seen_fingerprints"

# Bounces off the suppression guard (same scope, same state) that end the turn.
# Suppression alone does not stop the loop — the model reads the corrective and
# calls straight back, so every bounce still costs a full model turn. Two
# warnings, then hand control to the user.
_VALIDATE_SUPPRESS_ABORT = 3


def _build_validate_signature(kwargs: dict[str, Any]) -> tuple[str, str]:
    """Normalised signature for a build_and_validate call: (severity, profile).

    Only severity and profile determine whether a re-run is redundant.  Other
    kwargs (if any ever appear) are ignored so the model cannot circumvent the
    guard by adding a spurious argument.
    """
    return (
        str(kwargs.get("severity") or "required"),
        str(kwargs.get("profile") or "all"),
    )


def _format_validation_issues_summary(
    engine: AgentEngine,
    *,
    max_issues: int = 5,
) -> str:
    """Return a compact summary of current validation issues for the corrective
    message, preferring REQUIRED issues then RECOMMENDED, limited to *max_issues*.

    Returns an empty string when there are no issues to report.
    """
    v = engine.state.validation
    issues: list[str] = []
    for issue in (v.required_issues or [])[:max_issues]:
        issues.append(f"[REQUIRED] {issue}")
    remaining_required = max(0, (len(v.required_issues or []) - max_issues))
    if remaining_required:
        issues.append(f"[… {remaining_required} more REQUIRED issue(s) not shown]")
    for issue in (v.should_issues or [])[:max_issues]:
        issues.append(f"[RECOMMENDED] {issue}")
    remaining_should = max(0, (len(v.should_issues or []) - max_issues))
    if remaining_should:
        issues.append(f"[… {remaining_should} more RECOMMENDED issue(s) not shown]")
    for issue in (v.may_issues or [])[:max_issues]:
        issues.append(f"[OPTIONAL] {issue}")
    remaining_may = max(0, (len(v.may_issues or []) - max_issues))
    if remaining_may:
        issues.append(f"[… {remaining_may} more OPTIONAL issue(s) not shown]")
    if not issues:
        return ""
    return "\n".join(issues)


def _validation_tier_counts(engine: AgentEngine) -> str:
    """Per-tier issue counts for every tier that has actually been assessed.

    REQUIRED is always reported; RECOMMENDED/OPTIONAL only once their pass has
    run (``assessed_tiers``), so an unassessed tier is never misreported as
    clean — the distinction the user needs when deciding whether the crate is
    done.
    """
    v = engine.state.validation
    parts = [f"REQUIRED issues: {len(v.required_issues)}."]
    tiers = getattr(v, "assessed_tiers", set()) or set()
    if "recommended" in tiers:
        parts.append(f"RECOMMENDED issues: {len(v.should_issues)}.")
    if "optional" in tiers:
        parts.append(f"OPTIONAL issues: {len(v.may_issues)}.")
    return " ".join(parts)
_MUTATION_TOOLS = frozenset(
    {
        "set_fields",
        "set_crate_metadata",
        "remove_entity",
        "link",
        "attach_files",
        "populate_condition_table",
        "fix_required_issues",
        "scaffold_isa_backbone",
        "draft_process_chain",
        "resolve_compound",
        "resolve_publication",
        "materialize_aop_subgraph",
    }
)

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


# Longest a reply can be and still count as running commentary rather than
# content. "Let me fix the two issues: the supplier key in the JSON-LD context,
# and the missing additionalProperty on the DataAnalysis process" is 137.
_COMMENTARY_MAX_CHARS = 240


def _reply_is_running_commentary(reply: str | None) -> bool:
    """Whether *reply* is a throwaway "here is what I'll do next" line.

    Only these are printed transiently, because only these are worthless once
    the next step starts. The bar is deliberately high: a reply with any
    structure — more than one line, a heading, a bullet list, a table — is an
    ANSWER (the issue list the user just asked for, a summary of what was
    built), and erasing it to make room for the next "Let me…" would destroy
    the thing the user is reading.
    """
    text = (reply or "").strip()
    if not text or len(text) > _COMMENTARY_MAX_CHARS:
        return False
    if "\n" in text:
        return False  # multi-line means structure, which means content
    if text.lstrip()[:1] in {"#", "-", "*", "|", ">", "`"}:
        return False
    return not _reply_is_question(text)


def _reply_is_empty_completion(reply: str | None) -> bool:
    """Return True when a turn produced no meaningful text (the stall symptom).

    A bare/whitespace reply with no tool activity is the empty completion the
    weak model emits when it stalls (#263 Fix A). We treat very short non-word
    replies as empty too (e.g. a lone ``.``).
    """
    if not reply:
        return True
    return not reply.strip()


@overload
def _invoke_with_timeout(
    app: Any,
    payload: dict[str, Any],
    config: Any,
    *,
    timeout: float,
    include_error: Literal[False] = False,
) -> tuple[dict[str, Any] | None, str]: ...


@overload
def _invoke_with_timeout(
    app: Any,
    payload: dict[str, Any],
    config: Any,
    *,
    timeout: float,
    include_error: Literal[True],
) -> tuple[dict[str, Any] | None, str, dict[str, str] | None]: ...


def _invoke_with_timeout(
    app: Any,
    payload: dict[str, Any],
    config: Any,
    *,
    timeout: float,
    include_error: bool = False,
) -> tuple[dict[str, Any] | None, str] | tuple[dict[str, Any] | None, str, dict[str, str] | None]:
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
    outcome: dict[str, Any] = {"result": None, "error": None, "traceback": None}
    cancel_event = threading.Event()

    def _worker() -> None:
        token = _invocation_cancel_event.set(cancel_event)
        try:
            _raise_if_invocation_cancelled()
            outcome["result"] = app.invoke(payload, config)
        except BaseException as exc:  # noqa: BLE001 — captured, surfaced as "error".
            # Capture *everything* (including provider SDK errors) so nothing
            # escapes the worker thread and crashes the loop. Genuinely fatal
            # signals on the main thread (KeyboardInterrupt) are unaffected.
            outcome["error"] = exc
            outcome["traceback"] = traceback.format_exc()
        finally:
            _invocation_cancel_event.reset(token)

    worker = threading.Thread(target=_worker, daemon=True, name="vitro-model-invoke")
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        cancel_event.set()
        logger.warning(
            "Model invoke exceeded %.1fs wall-clock timeout; cancelling its next "
            "cooperative boundary and ending turn gracefully",
            timeout,
        )
        return (None, "timeout", None) if include_error else (None, "timeout")
    if outcome["error"] is not None:
        # A deliberate stop (a guard ending a non-progressing turn) is NOT an
        # error: reporting it as one would tell the user something broke when
        # the loop did exactly what it should. The timeout path also raises
        # _InvocationCancelled, so the set cancel_event distinguishes them.
        if isinstance(outcome["error"], _InvocationCancelled) and not cancel_event.is_set():
            logger.info("Model invoke stopped by a loop guard: %s", outcome["error"])
            return (None, "stopped", None) if include_error else (None, "stopped")
        if _is_timeout_error(outcome["error"]):
            # The provider's own request timeout is wired to the SAME duration as
            # the wall-clock guard above, so in practice it raises first and the
            # guard never sees a live worker. Reporting that as a generic error
            # told the user something broke when the run simply ran out of time.
            logger.info("Model invoke timed out: %s", outcome["error"])
            return (None, "timeout", None) if include_error else (None, "timeout")
        logger.warning("Model invoke raised: %s", outcome["error"])
        if include_error:
            error = outcome["error"]
            return None, "error", _error_diagnostic(error, outcome["traceback"])
        return None, "error"
    return (outcome["result"], "ok", None) if include_error else (outcome["result"], "ok")


def _is_timeout_error(exc: BaseException | None) -> bool:
    """Whether *exc* is a request/connection timeout rather than a real failure.

    Every provider SDK spells this differently (``APITimeoutError``,
    ``ReadTimeout``, ``TimeoutException``, …) and importing them all here would
    couple the loop to each vendor, so the class name and its module are matched
    instead. ``TimeoutError`` is caught outright.
    """
    if exc is None:
        return False
    if isinstance(exc, TimeoutError):
        return True
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.casefold()
        if "timeout" in name or "timedout" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


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


# Marker in the corrective returned for a mutation that left the crate
# unchanged. It doubles as the signal to _is_non_progress_result, so a repeated
# no-op feeds the loop-breaker instead of resetting it.
_NO_OP_MUTATION_MARKER = "changed nothing in the crate"


def _no_op_mutation_message(tool_name: str, kwargs: dict[str, Any], engine: AgentEngine) -> str:
    """The corrective for a mutation call that left the crate byte-identical.

    A weak model can sit in a loop of a mutation that writes nothing —
    ``set_crate_metadata`` with every field ``None`` was observed 33 times in
    one session, burning ~990k input tokens — because the call looks successful:
    it returns a normal dict, so nothing downstream treats it as a dead end.
    This says plainly that nothing changed and what to do instead.
    """
    supplied = sorted(k for k, v in kwargs.items() if v not in (None, "", [], {}))
    lines = [f"{tool_name} {_NO_OP_MUTATION_MARKER} — the state is identical to before the call."]
    if not supplied:
        lines.append(
            "Every argument was empty, so there was nothing to write. Calling it "
            "again with empty arguments will do nothing again."
        )
    else:
        lines.append(
            f"The fields you passed ({', '.join(supplied)}) already hold exactly "
            "those values, so re-writing them is a no-op."
        )
    state_summary = _format_compact_state_summary(engine)
    if state_summary:
        lines.append(f"\nCurrent state:\n{state_summary}")
    lines.append(
        "\nDo NOT repeat this call. Either call it with a genuinely different "
        "value, or move on to the next step toward a complete crate (draft the "
        "missing entities, wire the process chain, then build_and_validate)."
    )
    return "\n".join(lines)


# Recent post-mutation state fingerprints, newest last. A mutation landing on
# one of these has undone itself — the crate is back somewhere it just was.
_MUTATION_HISTORY_FLAG = "_mutation_fingerprint_history"
_MUTATION_CYCLE_COUNT_FLAG = "_mutation_cycle_count"

# How far back a revisit still counts as a cycle, and how many cycles end the
# turn. A model rewriting one field in alternating encodings produces a tight
# A-B-A-B, so a short window catches it while leaving honest edit-and-revert
# sequences (which a user drives, spaced by their own turns) alone.
_MUTATION_HISTORY_WINDOW = 8
_MUTATION_CYCLE_ABORT = 3


def _record_mutation_cycle(
    engine: AgentEngine, tool_name: str, kwargs: dict[str, Any]
) -> str | None:
    """Detect a mutation that returned the crate to a recent state.

    The no-op guard catches "this call changed nothing". It cannot catch
    "this call changed something back": writing ``hasPart`` as a string, then as
    a list, then as a string again alters the stored value every time, so every
    write looks like progress while the crate oscillates between two states.

    Returns a corrective to hand the model instead of the result, or ``None``
    when the mutation is genuinely new. Raises :class:`_InvocationCancelled`
    once the cycling has continued past :data:`_MUTATION_CYCLE_ABORT`, ending
    the turn — the same escalation the validation guard uses, for the same
    reason: only handing control back reliably stops it.
    """
    try:
        fingerprint = engine.state.validation_fingerprint()
    except Exception:  # noqa: BLE001 — best-effort bookkeeping
        return None
    history: list[str] = list(getattr(engine, _MUTATION_HISTORY_FLAG, None) or [])
    revisited = fingerprint in history
    history.append(fingerprint)
    setattr(engine, _MUTATION_HISTORY_FLAG, history[-_MUTATION_HISTORY_WINDOW:])
    if not revisited:
        setattr(engine, _MUTATION_CYCLE_COUNT_FLAG, 0)
        return None

    cycles = int(getattr(engine, _MUTATION_CYCLE_COUNT_FLAG, 0)) + 1
    setattr(engine, _MUTATION_CYCLE_COUNT_FLAG, cycles)
    logger.warning(
        "Mutation cycle detected: %s returned the crate to a recent state (cycle %d/%d)",
        tool_name,
        cycles,
        _MUTATION_CYCLE_ABORT,
    )
    _log_suppressed(engine, tool_name, f"mutation_cycle_{cycles}", kwargs)
    if cycles >= _MUTATION_CYCLE_ABORT:
        raise _InvocationCancelled("mutation cycle with no progress")
    return (
        f"{tool_name} put the crate back into a state it was in a moment ago — you are "
        "cycling, not progressing. This usually means the same fact is being rewritten "
        "in different encodings (a bare id, then a list, then an {'@id': …} object). "
        "All of those are accepted and stored identically, so rewriting it a third way "
        "changes nothing.\n\n"
        "If a validation issue persists after you set a field, the field is not the "
        "problem — re-read the issue and fix what it actually names, or ask the user. "
        "Do NOT write this field again."
    )


def _log_suppressed(
    engine: AgentEngine, tool_name: str, reason: str, kwargs: dict[str, Any]
) -> None:
    """Record a tool call the loop refused to run, and why.

    A guard that returns before ``engine.run_tool`` leaves NO profiler record —
    no ``tool_start``, no ``tool_call`` — so a model bouncing off one is
    indistinguishable from idle time in the profile. One observed session spent
    35s and ~70k input tokens on six consecutive model calls whose tools node
    ran for 20ms and executed nothing, with no way to tell which guard was
    firing. Best-effort: never raises, and a headless engine without a profiler
    is a no-op.
    """
    profiler = getattr(engine, "profiler", None)
    if profiler is None:
        return
    try:
        profiler.log_event(
            event="tool_suppressed",
            tool=tool_name,
            iteration=engine.state.iteration_count,
            args=str(kwargs)[:300] or None,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — observability must never break a turn
        logger.debug("suppression logging failed", exc_info=True)


def _is_non_progress_result(result: Any) -> bool:
    """Return True when a tool result represents NO forward progress (#287 Fix B).

    A weak model loops when a tool keeps handing back the same dead-end. Four
    shapes count as non-progress:

    1. An ``error`` dict — the wrapper turns a recoverable tool-body exception
       into ``{"error": ..., "tool": ...}`` (e.g. a non-existent path).
    2. A directory-guidance string — a reader handed a directory returns
       ``"<path> is a directory, not a file …"`` (#240/#281).
    3. An unreadable/None string — a reader that could not return text returns
       ``"<tool> could not return text …"`` (#101/#148).
    4. A no-op mutation corrective — a mutation whose call left the crate state
       unchanged (:func:`_no_op_mutation_message`). Without this a tool that
       writes nothing counts as progress and RESETS every loop guard, which is
       exactly how a 33-call ``set_crate_metadata`` loop stayed alive.

    Anything else (real file content, a successful build dict, a list, …) is
    progress and resets the loop-breaker. The check is purely structural so it
    never raises.
    """
    if isinstance(result, dict):
        return "error" in result
    if isinstance(result, str):
        return (
            "is a directory, not a file" in result
            or "could not return text" in result
            or _NO_OP_MUTATION_MARKER in result
        )
    return False


def _record_recent_mutation(engine: AgentEngine, result: Any) -> None:
    """Keep a bounded list of entities returned by successful mutations."""
    entity = result
    if isinstance(result, dict):
        entity = (
            result.get("entity")
            or result.get("updated_entity")
            or result.get("created_entity")
        )
    if not hasattr(entity, "entity_id") or not hasattr(entity, "type"):
        return
    recent = list(getattr(engine, "_react_recent_mutations", []))
    item = (str(entity.type), str(entity.entity_id), str(entity.fields.get("name", ""))[:60])
    recent = [entry for entry in recent if entry[:2] != item[:2]]
    recent.append(item)
    setattr(engine, "_react_recent_mutations", recent[-8:])


def _format_compact_state_summary(engine: AgentEngine, *, limit: int = 8) -> str:
    """Return bounded live state context for tool results and interventions."""
    try:
        entities = engine.state.list_entities()
        counts: dict[str, int] = {}
        for entity in entities:
            counts[entity.type] = counts.get(entity.type, 0) + 1
        count_text = ", ".join(f"{key}: {counts[key]}" for key in sorted(counts)) or "none"
        tracked = getattr(engine, "_react_recent_mutations", [])
        recent = tracked[-limit:] or [
            (entity.type, entity.entity_id, str(entity.fields.get("name", ""))[:60])
            for entity in entities[-limit:]
        ]
        recent_text = ", ".join(
            f"{entity_type}:{entity_id}"
            + (f" ({name})" if name else "")
            for entity_type, entity_id, name in recent
        ) or "none"
        validation = engine.state.validation
        status = (
            f"base={'pass' if validation.base_passed else 'fail'}, "
            f"isa={'pass' if validation.isa_passed else 'fail'}, "
            f"tox={'pass' if validation.tox_passed else 'fail'}, "
            f"required={len(validation.required_issues)}"
        )
        return f"[Live state | counts: {count_text} | recent: {recent_text} | validation: {status}]"
    except Exception:  # noqa: BLE001 — context must never break tool execution.
        return "[Live state unavailable]"


def _reader_evidence_key(engine: AgentEngine, path: str) -> str:
    """Normalize an approved reader path the same way engine storage does.

    A bare filename is resolved inside the approved roots first, exactly as the
    engine's gate does. Without that the two normalizations disagree: the engine
    stores evidence under ``Assay_OATP1C1/SOP.docx`` while this returned the raw
    ``SOP.docx``, so the "already loaded" check never matched and the model was
    free to re-read the same document indefinitely.
    """
    try:
        resolved_bare = engine._resolve_within_roots(path)
        if resolved_bare is not None:
            path = resolved_bare
        resolved = Path(path).resolve()
        for root in getattr(engine.state, "approved_scan_roots", set()):
            try:
                return str(resolved.relative_to(Path(root).resolve()))
            except ValueError:
                continue
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return path


def _list_entities_intervention(engine: AgentEngine) -> str:
    """Explain a repeated list query without serving a stale cached result."""
    return (
        f"STOP — you have repeated the same list_entities query "
        f"{_LIST_ENTITIES_BREAKER_THRESHOLD} times without a mutation. Do not "
        "repeat it again; use the prior result and live state summary, search with "
        "different arguments, or make the next meaningful mutation.\n"
        + _format_compact_state_summary(engine)
    )


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
            declared = set((tool_params or {}).get("properties") or {})

            def _run(**kwargs: Any) -> Any:
                # Drop the generic `args`/`kwargs` placeholders some providers
                # emit for a tool whose schema declares no such parameter. They
                # reach the tool function as unexpected keywords and raise a
                # TypeError, turning a harmless status query into a failed call.
                if kwargs and not declared.intersection(kwargs):
                    kwargs = {
                        k: v for k, v in kwargs.items() if k not in ("args", "kwargs")
                    }
                # An explicit null is the model saying "not specified", but it
                # reaches the tool as a real None and overwrites the parameter's
                # default: `list_scanned_files(offset=None)` raised
                # "'>' not supported between NoneType and int". Weak models fill
                # in EVERY optional parameter this way, so drop the nulls and let
                # the defaults apply — omitted and null mean the same thing here.
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                _raise_if_invocation_cancelled()
                # Loop-breaker (#287 Fix B): if this is the Nth consecutive
                # IDENTICAL call that has been returning a non-progress result
                # (directory/None/error), REFUSE to repeat it — return a forceful
                # corrective tool message with the actual scanned-file inventory so
                # a weak model stops looping (it ignored #281's directory message
                # and looped ~36×). Distinct calls / a single retry never trip this.
                signature = _call_signature(tool_name, kwargs)
                if tool_name == "list_entities":
                    list_last = getattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, None)
                    list_count = getattr(engine, _LIST_ENTITIES_COUNT_FLAG, 0)
                    if list_last == signature and list_count >= _LIST_ENTITIES_BREAKER_THRESHOLD:
                        _log_suppressed(engine, tool_name, "repeated_list_query", kwargs)
                        return _list_entities_intervention(engine)
                last_sig = getattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, None)
                repeat_count = getattr(engine, _LOOP_BREAKER_COUNT_FLAG, 0)
                if last_sig == signature and repeat_count >= _LOOP_BREAKER_THRESHOLD:
                    # Do NOT run the tool again — the identical non-progress call
                    # is short-circuited and the model is steered elsewhere.
                    _log_suppressed(engine, tool_name, "loop_breaker", kwargs)
                    return _loop_breaker_intervention(engine, tool_name)

                if tool_name in _FILE_READ_TOOLS:
                    evidence = getattr(engine.state, "document_evidence", {})
                    path = _reader_evidence_key(engine, str(kwargs.get("path", "")))
                    read_args = {k: v for k, v in kwargs.items() if k != "path"}
                    cached = next(
                        (
                            item
                            for item in evidence.values()
                            if item.get("path") == path and item.get("args", {}) == read_args
                        ),
                        None,
                    )
                    if path and cached is not None:
                        _log_suppressed(engine, tool_name, "already_in_evidence", kwargs)
                        # HAND BACK THE CONTENT, do not just refuse. The evidence
                        # block in the state brief is capped, so a second document
                        # is silently replaced by "[omitted for context budget]" —
                        # and telling the model "it is already in your context"
                        # when it demonstrably is not sent one session round this
                        # loop 90 times (47 read_docx + 43 read_excel, ~1M input
                        # tokens) asking for a file we were holding all along.
                        # Serving the stored copy is cheaper than the re-read it
                        # replaces and is the honest answer to the question asked.
                        content = str(cached.get("content", "")).strip()
                        if content:
                            return (
                                f"[Serving the copy of {path} already loaded this session "
                                "— identical to re-reading it.]\n\n" + content
                            )
                        return (
                            "Already loaded this document into bounded session evidence. "
                            "Use the loaded evidence in the state context; request a specific "
                            "different slice only if needed."
                        )
                if tool_name == "build_and_validate":
                    bv_sig = _build_validate_signature(kwargs)
                    try:
                        bv_fp = engine.state.validation_fingerprint()
                    except Exception:  # noqa: BLE001 — the guard must never block a call.
                        bv_fp = None
                    bv_seen: dict[tuple[str, str], tuple[str, int]] = getattr(
                        engine, _BUILD_VALIDATE_SEEN_FLAG, None
                    ) or {}
                    bv_entry = bv_seen.get(bv_sig)
                    # Suppress when THIS scope has already been validated against
                    # THIS state, whatever ran in between — alternating scopes is
                    # otherwise a free pass through a consecutive-repeat check.
                    if bv_fp is not None and bv_entry is not None and bv_entry[0] == bv_fp:
                        strikes = bv_entry[1] + 1
                        bv_seen = dict(bv_seen)
                        bv_seen[bv_sig] = (bv_fp, strikes)
                        setattr(engine, _BUILD_VALIDATE_SEEN_FLAG, bv_seen)
                        logger.info(
                            "Suppressed build_and_validate%s (strike %d/%d) — already "
                            "validated against this unchanged state",
                            bv_sig,
                            strikes,
                            _VALIDATE_SUPPRESS_ABORT,
                        )
                        _log_suppressed(
                            engine, tool_name, f"unchanged_state_strike_{strikes}", kwargs
                        )
                        # Suppressing the SHACL pass saves seconds but NOT tokens:
                        # the model reads the corrective and immediately calls
                        # again, so a bounce still costs a full model turn (~12.5k
                        # input tokens; 17 bounces in 90s were observed). After a
                        # couple of strikes, end the turn instead of answering —
                        # handing control back to the user is the only thing that
                        # reliably stops it.
                        if strikes >= _VALIDATE_SUPPRESS_ABORT:
                            logger.warning(
                                "Ending turn: build_and_validate%s bounced %d times "
                                "against unchanged state",
                                bv_sig,
                                strikes,
                            )
                            raise _InvocationCancelled(
                                "repeated validation of an unchanged crate"
                            )
                        issue_text = _format_validation_issues_summary(engine)
                        v = engine.state.validation
                        opener = (
                            f"build_and_validate({bv_sig[0]}, {bv_sig[1]}) has already run "
                            "against this exact crate state — no state change since, and "
                            "re-running another scope over the same state will not help "
                            "either. The result has not changed. "
                        )
                        if strikes > 1:
                            opener = (
                                f"STOP. build_and_validate({bv_sig[0]}, {bv_sig[1]}) has now "
                                f"been called {strikes} times against an unchanged crate — "
                                "no state change, so no new result is possible. "
                            )
                        corrective = (
                            opener
                            + f"Conformance: base={'pass' if v.base_passed else 'fail'}, "
                            f"isa={'pass' if v.isa_passed else 'fail'}, "
                            f"tox={'pass' if v.tox_passed else 'fail'}. "
                            f"{_validation_tier_counts(engine)} "
                            "Use the existing validation result and address the "
                            "reported issues instead of re-validating."
                        )
                        if issue_text:
                            corrective += f"\n\nCurrent issues:\n{issue_text}"
                        corrective += (
                            "\n\nYour next call MUST be a mutation (set_fields, link, "
                            "draft_*, attach_files) or export_crate — not another "
                            "validation. Validation re-runs automatically once the "
                            "state actually changes."
                        )
                        return corrective
                    if bv_fp is not None:
                        # Record BEFORE running: validation never mutates entities
                        # or metadata (the #153 write-back only touches
                        # state.validation, which the fingerprint excludes), so
                        # the pre-call fingerprint is the state it validated.
                        bv_seen = dict(bv_seen)
                        bv_seen[bv_sig] = (bv_fp, 0)
                        setattr(engine, _BUILD_VALIDATE_SEEN_FLAG, bv_seen)

                # Snapshot the state so a mutation that writes nothing can be
                # recognised AFTER the fact. The tools reject the obvious cases
                # themselves; this catches every other way a mutation can be a
                # no-op (re-linking an existing edge, re-setting an identical
                # field) — all of which otherwise read as progress and reset the
                # loop guards.
                mutation_fingerprint: str | None = None
                if tool_name in _MUTATION_TOOLS:
                    try:
                        mutation_fingerprint = engine.state.validation_fingerprint()
                    except Exception:  # noqa: BLE001 — best-effort bookkeeping
                        logger.debug("no-op guard: fingerprint failed", exc_info=True)

                try:
                    result = engine.run_tool(tool_name, **kwargs)
                    _raise_if_invocation_cancelled()
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
                        severity = kwargs.get("severity") or "required"
                        profile = kwargs.get("profile") or "all"
                        if severity == "required" and profile == "all":
                            escalation = _run_validation_escalation(engine, result)
                            # Attach the broader tiers to the result the model
                            # receives. They ran on the engine, not as model tool
                            # calls, so this is the only channel through which the
                            # model can learn about them — without it the closing
                            # summary reports REQUIRED findings only, even though
                            # the user asked for the recommended/optional passes.
                            if escalation and isinstance(result, dict):
                                result = {**result, "escalation": escalation}

                # A mutation that left the state byte-identical did NOT make
                # progress, whatever its return value looks like. Swap in the
                # corrective before the bookkeeping below, so it neither counts
                # as a mutation (which would reset every guard) nor escapes the
                # loop-breaker (_is_non_progress_result recognises the marker).
                if mutation_fingerprint is not None and not (
                    isinstance(result, dict) and result.get("error")
                ):
                    try:
                        unchanged = engine.state.validation_fingerprint() == mutation_fingerprint
                    except Exception:  # noqa: BLE001 — best-effort bookkeeping
                        unchanged = False
                    if unchanged:
                        logger.info("No-op mutation suppressed: %s(%s)", tool_name, signature)
                        result = _no_op_mutation_message(tool_name, kwargs, engine)
                    else:
                        cycle = _record_mutation_cycle(engine, tool_name, kwargs)
                        if cycle is not None:
                            result = cycle

                # Track repeated list queries independently: this never stores or
                # reuses their result, and mutations always reset the streak.
                if tool_name in _MUTATION_TOOLS and not _is_non_progress_result(result):
                    _record_recent_mutation(engine, result)
                    setattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, None)
                    setattr(engine, _LIST_ENTITIES_COUNT_FLAG, 0)
                    # The build_and_validate memo needs no clearing here: a real
                    # mutation changes the state fingerprint, so every scope it
                    # recorded stops matching and the next validation runs fresh.
                    setattr(engine, _BUILD_VALIDATE_SEEN_FLAG, {})
                elif tool_name == "list_entities":
                    list_last = getattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, None)
                    list_count = getattr(engine, _LIST_ENTITIES_COUNT_FLAG, 0)
                    if list_last == signature:
                        setattr(engine, _LIST_ENTITIES_COUNT_FLAG, list_count + 1)
                    else:
                        setattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, signature)
                        setattr(engine, _LIST_ENTITIES_COUNT_FLAG, 1)
                else:
                    setattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, None)
                    setattr(engine, _LIST_ENTITIES_COUNT_FLAG, 0)

                # The pre-run guard above is the only build_and_validate
                # gate: it suppresses BEFORE the SHACL pass and before the
                # tokens are spent. A second post-run copy used to sit here as
                # a fallback, keyed on consecutive repeats — the very scheme
                # alternating scopes walked straight through — so it could only
                # ever fire after the work it was meant to prevent.

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
        _raise_if_invocation_cancelled()
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
            # Capture the model's reply TEXT — truncate to avoid bloating profile.
            # str(content) would write the raw content-block repr (#341): with the
            # Responses API that means every profile line carried reasoning-block
            # ids and empty summaries, so `response_text` looked non-empty on all
            # 196 calls of one session while only a handful said anything.
            content = getattr(last_msg, "content", None)
            if content:
                text = ui.flatten_message_content(content)
                if len(text) > 2000:
                    text = text[:1997] + "..."
                response_text = text or None

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
        _raise_if_invocation_cancelled()
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


# Entity types that are meaningless unless something points at them. A compound
# nobody exposed, a cell line nobody cultured, a file nobody produced: each is
# extraction that never became structure. Deliberately excludes the backbone
# (Investigation/Study/Assay) and processes, which are wired by the mapper from
# their parent links rather than by an inbound reference.
_MUST_BE_LINKED_TYPES: frozenset[str] = frozenset(
    {"MolecularEntity", "CellLineSample", "Sample", "File", "Person", "Organization"}
)


def _unreferenced_entities(state: CrateState) -> dict[str, list[str]]:
    """Entities of a linkable type that nothing in the crate points at.

    One pass: collect every id mentioned by any reference field, then report the
    linkable entities missing from that set. Extraction is the easy half — a
    session resolved 22 compounds and wired NONE of them, and the crate still
    reported itself "complete" because counting entities cannot see that.

    Returns ``{entity type: [ids]}``, empty when everything is connected.
    """
    from builder.tools._crate_mapping import _REF_FIELDS

    referenced: set[str] = set()
    for ent in state.list_entities():
        for field in _REF_FIELDS:
            value = ent.fields.get(field)
            if value is None:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                key = item.get("@id") if isinstance(item, dict) else item
                if isinstance(key, str) and key.strip():
                    referenced.add(key.strip().lstrip("./").lstrip("#"))

    orphans: dict[str, list[str]] = {}
    for ent in state.list_entities():
        etype = getattr(ent, "type", "")
        if etype not in _MUST_BE_LINKED_TYPES:
            continue
        if ent.entity_id in referenced:
            continue
        orphans.setdefault(etype, []).append(ent.entity_id)
    return orphans


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
    # Crate-level attribution: who is responsible for the DATASET. Tracked here
    # because nothing else ever prompts for it — a crate can otherwise reach
    # "complete" naming six publication authors and no owner at all.
    meta = state.metadata
    has_attribution = bool(meta.publisher or meta.creator or meta.contact)
    orphans = _unreferenced_entities(state)

    present: list[str] = []
    if has_backbone:
        present.append("backbone ✓")
    if has_person:
        present.append("person ✓")
    if has_attribution:
        present.append("crate owner ✓")
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
    if not has_attribution:
        missing.append("crate owner (publisher/creator/contact)")
    if orphans:
        detail = ", ".join(f"{len(ids)} {t}" for t, ids in sorted(orphans.items()))
        missing.append(f"links for {detail}")
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
    elif orphans:
        # Extraction outran wiring. Name the actual ids so the next step is a
        # concrete link call, and say plainly that guessing is not the fallback:
        # an unresolvable connection is a question for the user, not a fifth
        # encoding of the same failed write.
        first_type, first_ids = sorted(orphans.items())[0]
        sample = ", ".join(first_ids[:3]) + ("…" if len(first_ids) > 3 else "")
        next_action = (
            f"link the unconnected {first_type}(s) ({sample}) into the chain — "
            "attach_files / link / populate_condition_table. Try it yourself "
            "FIRST; if which entity it belongs to is genuinely ambiguous, ask the "
            "user with present_to_human instead of guessing or re-writing the "
            "same field another way"
        )
    elif not has_attribution:
        next_action = (
            "set_crate_metadata(publisher=…/creator=…/contact=…) — take the "
            "corresponding person/affiliation from the assay metadata and CONFIRM "
            "with the user; never invent it"
        )
    else:
        # The crate looks complete — close it out.
        next_action = "build_and_validate then export_crate"

    present_str = ", ".join(present) if present else "nothing yet"
    return f"[Completeness: {present_str}; missing: {', '.join(missing)} → next: {next_action}]"


def _build_system_prompt_with_state(
    session_id: str,
    entity_count: int,
    file_count: int,
    document_count: int = 0,
    iteration_count: int = 0,
    next_fix: str | None = None,
    nudge: str | None = None,
    state_summary: str | None = None,
) -> str:
    """Build a lightweight state brief appended to the system prompt.

    This is called on every model invocation (not persisted in history),
    giving the LLM awareness of current session state without accumulating
    duplicate metadata in MemorySaver.

    Returns a single short line like:
    ``[Session: sid | Files: 5 | Entities: 3 | Documents: 2 | Iteration: 42]``

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
    )
    if document_count:
        brief += f"Documents: {document_count} | "
    brief += f"Iteration: {iteration_count}]"
    if next_fix:
        brief += f"\n[Next REQUIRED fix: {next_fix}]"
    if nudge:
        brief += f"\n{nudge}"
    if state_summary:
        brief += f"\n{state_summary[:1200]}"
    return brief


# Tool names whose verbose output really IS recoverable from CrateState, so
# replaying it verbatim in the transcript is pure waste once the model has
# consumed it: the full scan listing lives in CrateState.scanned_files and is
# queryable via list_scanned_files (Issue #61, #172).
_STATE_BACKED_TOOLS = frozenset({"scan_files"})

# Reader tools whose output is ALSO worth pruning once consumed, but which store
# nothing (`read_file_sample` / `read_multiple_files` return a plain string/dict
# and persist nothing — CrateState has no body store). Their stub must therefore
# never claim the text is retrievable from state, and must not tell the model not
# to re-run: re-reading is the only recovery path (#376).
_REPLAYABLE_READER_TOOLS = frozenset({"read_file_sample", "read_multiple_files"})

_PRUNABLE_TOOLS = _STATE_BACKED_TOOLS | _REPLAYABLE_READER_TOOLS

# Above this length (chars), a consumed state-backed tool output is pruned to a
# stub. Kept modest so small/already-summarized outputs pass through untouched.
_PRUNE_CONTENT_THRESHOLD = 500


def _prune_stub(name: str) -> str:
    """The replacement text for a pruned tool output, truthful per tool class.

    ``scan_files``' inventory really is in ``CrateState`` and really is queryable
    (``list_scanned_files``), so its stub says so — AGENTS.md §5 documents that
    contract. A reader's body is stored **nowhere**, so its stub must not claim
    otherwise and must not forbid re-running: re-reading is the only way back to
    the text, and ``read_file`` returns it in full up to its 64 KiB budget (#376).
    """
    if name in _STATE_BACKED_TOOLS:
        return (
            f"[{name} output pruned from history to save tokens — the full "
            f"result is stored in the session state (CrateState). Call "
            f"list_scanned_files to retrieve the full file inventory "
            f"(paginated/filterable). Do not re-run {name}.]"
        )
    return (
        f"[{name} output pruned from history to save tokens. A bounded copy may be "
        f"available in loaded document evidence; request a specific missing section "
        f"only when needed. Do not repeat the identical read automatically.]"
    )


def _prune_state_backed_outputs(messages: list) -> list:
    """Replace verbose tool outputs the model has **already consumed** with a stub.

    Once the model has answered a tool result there is no value in replaying the
    raw blob on every later turn. We keep the message (so the AI tool_call →
    ToolMessage pairing is never broken) but shrink its content to a short stub.
    Pairing-preservation is why we *rewrite* rather than *drop* — dropping a
    ToolMessage would orphan its preceding AI tool_call.

    **"Consumed" is load-bearing and is enforced here** (#376). The graph edge is
    ``tools → model``, so the node that runs immediately after a tool result is
    ``call_model`` — which assembles the history through this function. Pruning on
    name and length alone therefore destroyed a reader's body *before any model
    had seen it*, and replaced it with a stub telling the model the text was in
    ``CrateState`` (it never is, for a reader) and not to re-run. A message is
    consumed only when a later ``AIMessage`` (the model responded) or
    ``HumanMessage`` (a new turn began, which necessarily follows a final
    ``AIMessage``) exists. Only the newest tool-result block is replayed verbatim;
    everything older is still stubbed, so #61's savings are preserved.

    The list is returned with the same length and ordering; non-matching
    messages are passed through unchanged. Small outputs (below
    ``_PRUNE_CONTENT_THRESHOLD``) are left intact — they cost little and may
    already be summaries (e.g. ``summarize_scan_result``).
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    # The last point at which the model demonstrably moved past a tool result.
    # Anything at or after it has not been consumed yet and must survive intact.
    boundary = max(
        (i for i, m in enumerate(messages) if isinstance(m, (AIMessage, HumanMessage))),
        default=-1,
    )

    pruned: list = []
    for i, msg in enumerate(messages):
        if (
            i < boundary
            and isinstance(msg, ToolMessage)
            and getattr(msg, "name", None) in _PRUNABLE_TOOLS
            and len(str(msg.content)) > _PRUNE_CONTENT_THRESHOLD
        ):
            pruned.append(
                ToolMessage(
                    content=_prune_stub(str(msg.name)),
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
            )
        else:
            pruned.append(msg)
    return pruned


def _format_document_evidence(engine: AgentEngine, *, limit: int = 12000) -> str:
    """Format bounded loaded document evidence for the trailing state brief."""
    evidence = getattr(engine.state, "document_evidence", {})
    if not evidence:
        return ""
    parts: list[str] = ["[Loaded document evidence]"]
    used = len(parts[0])
    for path, item in evidence.items():
        content = str(item.get("content", ""))
        line = f"\n{path} ({item.get('tool', 'reader')}):\n{content}"
        if used + len(line) > limit:
            parts.append("\n[Additional loaded evidence omitted for context budget]")
            break
        parts.append(line)
        used += len(line)
    return "".join(parts)


def _format_document_context(documents: list[dict[str, Any]] | None) -> str:
    """Format the ranked document discovery results as a bounded context string.

    Produces one line per candidate::

        [role] filename (score: 0.85) — directory: reason, reason

    The result is a single paragraph (no markdown, no multi-line headers) so it
    slots cleanly into the system brief without busting the cache-friendly layout.
    """
    if not documents:
        return ""
    lines: list[str] = []
    for doc in documents[:20]:  # safety cap — never exceed 20 entries
        role = doc.get("role", "document")
        name = doc.get("filename", doc.get("relative_path", "?"))
        score = doc.get("score", 0.0)
        reasons = doc.get("reasons", [])
        reason_str = "; ".join(reasons[:3]) if reasons else ""
        line = f"[{role}] {name} (score: {score:.2f})"
        if reason_str:
            line += f" — {reason_str}"
        lines.append(line)
    return "\n".join(lines)


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
    document_count: int = 0,
    document_context: str | None = None,
    iteration_count: int = 0,
    next_fix: str | None = None,
    nudge: str | None = None,
    state_summary: str | None = None,
    max_history_tokens: int | None = None,
) -> list:
    """Assemble the message list for a model invocation with a cache-friendly
    layout (Issue #60) and a bounded history (Issue #61).

    Layout: ``[SystemMessage(SYSTEM_PROMPT), *trimmed_history, SystemMessage(state_brief)]``.

    When ``document_context`` is provided (the formatted, ranked documentation from
    initialisation), an additional ``SystemMessage`` is added between the state brief
    and the trimmed history so the model has direct access to identified SOPs,
    protocols, publications, and metadata documentation descriptions without needing
    to re-read files.

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
        document_count=document_count,
        iteration_count=iteration_count,
        next_fix=next_fix,
        nudge=nudge,
        state_summary=state_summary,
    )
    parts = [
        SystemMessage(content=SYSTEM_PROMPT),
        *trimmed_history,
    ]
    if document_context:
        parts.append(
            SystemMessage(
                content=f"[Discovered document evidence]\n{document_context}"
            )
        )
    parts.append(SystemMessage(content=state_brief))
    return parts


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
        _raise_if_invocation_cancelled()
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
        # Document discovery context (#179): ranked SOPs, protocols, publications,
        # metadata files identified during scanning. Passed as a separate block
        # so the model sees which documentation is available without re-reading.
        documents = getattr(engine.state, "documents", [])
        document_count = len(documents)
        document_context = _format_document_context(documents) if documents else None
        model_messages = _assemble_model_messages(
            messages,
            session_id=engine.state.session_id,
            entity_count=len(engine.state.list_entities()),
            file_count=len(engine.state.scanned_files),
            document_count=document_count,
            document_context=document_context,
            iteration_count=engine.state.iteration_count,
            next_fix=next_fix,
            nudge=nudge,
            state_summary=(
                _format_compact_state_summary(engine)
                + "\n"
                + _format_document_evidence(engine)
            ),
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
        _raise_if_invocation_cancelled()
        response = model.invoke(model_messages)
        # Accumulate this call's token usage onto the crate's generator record so
        # the exported crate carries what the run cost. Done HERE rather than in
        # the profiler wrapper because that wrapper is skipped entirely when
        # profiling is off — cost accounting must not depend on instrumentation.
        try:
            call_in, call_out = _extract_token_usage(response)
            engine.state.record_llm_usage(
                {"input_tokens": call_in or 0, "output_tokens": call_out or 0}
            )
        except Exception:  # noqa: BLE001 — accounting never breaks the loop
            logger.debug("Could not record LLM usage for this call", exc_info=True)
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
    *,
    resumed: bool = False,
    initial_prompt: str | None = None,
    verbose: bool = False,
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
        resumed: True iff the session was loaded with ``--resume``. Selects the
            greeting and the banner title. It is passed in, never inferred from
            ``engine.state``: a fresh ``--input`` run has already scanned files by
            the time the loop starts, so the old content-based guess greeted new
            sessions as resumes and asked the model to recap work that did not
            exist — which produced a passive summary instead of a build (#410).
        initial_prompt: An opening message to drive the first turn with, instead
            of waiting for stdin. The greeting invoke is deliberately outside the
            autonomous-continuation loop (which is keyed on a user message), so
            without a kickoff the loop greets and blocks having done no work
            (#412). Blank/whitespace is treated as absent. After this seeded turn
            the autonomous loop takes over exactly as it does for a typed line.
        verbose: Show bounded, sanitized diagnostics when a legacy model turn
            raises. The default preserves the generic error message.
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig

    tools = _build_langchain_tools(engine)
    # The wall-clock guard (#263 Fix A) uses the same finite timeout that is
    # wired onto the chat model, so the loop-level guard and the provider-level
    # request timeout agree.
    request_timeout = _get_request_timeout()
    # Streaming feeds the footer's live reply tail via on_llm_new_token. invoke()
    # still returns one aggregated message, so the graph, the timeout guard and
    # tool-call handling are unchanged — the only visible difference is that the
    # user can watch a long turn being written instead of staring at a timer.
    llm = _build_chat_model(
        provider=provider,
        model=model,
        base_url=base_url,
        timeout=request_timeout,
        streaming=True,
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
    # conversation history automatically. ``CrateState.session_id`` is durable,
    # whereas this checkpoint key is deliberately disposable: an interrupted
    # graph can persist an AI function call before its matching ToolMessage. The
    # Responses API rightly rejects that partial history on the next turn. On a
    # timeout/provider error we therefore rotate this *ephemeral* key while
    # retaining the same CrateState and all drafted work (#413).
    checkpoint_generation = 0
    checkpoint_thread_id = engine.state.session_id

    def _thread_config() -> RunnableConfig:
        """Build config from the current, recoverable LangGraph checkpoint key."""
        return cast(
            RunnableConfig,
            {
                "configurable": {"thread_id": checkpoint_thread_id},
                "recursion_limit": _recursion_limit(max_iterations),
            },
        )

    def _rotate_checkpoint(outcome: str) -> None:
        """Abandon an unsafe graph transcript after a failed invocation.

        A daemon worker cannot be forcibly killed while an HTTP request is in
        flight. Reusing its MemorySaver key could replay a checkpoint containing
        an unresolved tool call, yielding ``No tool output found for function
        call ...`` forever. A new key isolates the next user turn; CrateState is
        the durable source of truth and remains untouched.
        """
        nonlocal checkpoint_generation, checkpoint_thread_id
        if outcome == "ok":
            return
        checkpoint_generation += 1
        checkpoint_thread_id = (
            f"{engine.state.session_id}:recovered-{checkpoint_generation}"
        )
        # INFO, not WARNING: rotating is the designed response to a turn that
        # did not finish cleanly, and most non-ok outcomes are themselves
        # deliberate (a loop guard stopping the turn, a time or step limit). The
        # severity belongs to the CAUSE, which is already logged at its own
        # level by whatever produced it — warning here too made routine
        # housekeeping look like something had gone wrong.
        logger.info(
            "Rotated LangGraph checkpoint after %s; continuing with fresh thread %s",
            outcome,
            checkpoint_thread_id,
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

    replies = ui.TransientReplies(console)

    def _print_reply(content: str, *, transient: bool = False) -> None:
        """Print an agent reply through the shared renderer (empty → skip).

        Running commentary from an autonomous run is printed *transient*: the
        next one overwrites it, because "Let me build and validate" is obsolete
        the moment the next step starts and scrolling twenty of them buries the
        output that matters. A question or a final answer is printed normally
        and stays.
        """
        if not content or not content.strip():
            return
        replies.print(content, transient=transient)

    # ── Session banner + greeting ───────────────────────────────────────
    # `resumed` is the caller's fact (--resume), never a guess from how populated
    # the state looks: initialize(--input) scans files before the loop starts, so
    # the old `entity_count > 0 or file_count > 0` inference called every fresh
    # run a resume and told the model to summarise work that did not exist — the
    # model duly recapped instead of building (#410).
    entity_count = len(engine.state.list_entities())
    file_count = len(engine.state.scanned_files)
    val = engine.state.validation
    # Per-type entity breakdown (feeds the greeting prompt + the fallback panels).
    counts: dict[str, int] = {}
    for e in engine.state.list_entities():
        typ = getattr(e, "type", "Unknown")
        counts[typ] = counts.get(typ, 0) + 1

    # A no-op when there is nothing to show; the title reflects real provenance.
    ui.print_resume_summary(engine, resumed=resumed)

    if resumed:
        # Tell the LLM about the current state so it can give a contextual greeting
        greeting_prompt = (
            f"The user has resumed a session with {entity_count} entities and "
            f"{file_count} scanned files. "
            f"Validation: base={'pass' if val.base_passed else 'fail'}, "
            f"ISA={'pass' if val.isa_passed else 'fail'}, "
            f"Tox={'pass' if val.tox_passed else 'fail'}. "
            f"{_validation_tier_counts(engine)} "
            f"Entity breakdown: {counts}. "
            "Briefly welcome them back and summarise what has been done "
            "and what the next logical step is."
        )
    elif file_count:
        # New session whose input folder is already scanned. Include the ranked
        # document evidence so the user can correct a bad interpretation before
        # the agent drafts anything; filenames alone are insufficient intervention
        # context when several documents have different roles.
        documents = getattr(engine.state, "documents", [])
        document_lines = []
        for doc in documents[:20]:
            role = doc.get("role", "document")
            name = doc.get("filename", doc.get("relative_path", "?"))
            score = doc.get("score", 0.0)
            document_lines.append(f"- [{role}] {name} (score: {score:.2f})")
        discovered = "\n".join(document_lines) or "- No ranked document evidence available."
        approved_roots = sorted(getattr(engine.state, "approved_scan_roots", set()))
        input_root = approved_roots[0] if approved_roots else engine.state.metadata.input_path
        greeting_prompt = (
            f"The user has just started a new session; {file_count} input files have "
            "already been scanned from the approved input path below and no entities "
            "have been drafted yet. Do NOT call scan_files or ask for scan approval; "
            "use the existing inventory and discovered documents. Briefly explain what "
            "you will build, then list the ranked documents below so the user can "
            "correct roles or ask you to inspect a different file before drafting. "
            "Do not imply prior work exists.\n\n"
            f"Approved input path: {input_root or '(already scanned)'}\n\n"
            "Ranked input documents:\n" + discovered
        )
    else:
        greeting_prompt = "Greet the user and tell them what you can help build."

    def _print_resume_panel() -> None:
        """Print the resume welcome: where the crate stands and what to do next.

        Shown on EVERY resume, not only when the model fails to greet. The
        suggestions are derived from the crate's actual state — blocking issues
        first, then the next unmet step of the BASE -> ISA -> TOX climb, then
        export — so they are correct regardless of what the model says, and they
        are still there when it says nothing at all (a reasoning-heavy model
        answering "welcome them back" with 18 tokens of thought and no text).
        """
        lines: list[str] = []
        if val.required_issues:
            blocking = f"[red]{len(val.required_issues)} REQUIRED issue(s)[/red]"
            lines.append(
                f"  • [cyan]what is still missing?[/cyan] — {blocking} block conformance"
            )
            lines.append("  • [cyan]fix the required issues[/cyan] — I'll work through them")
        elif not (val.base_passed and val.isa_passed and val.tox_passed):
            nxt = (
                "base" if not val.base_passed else "ISA" if not val.isa_passed else "ISA-Tox"
            )
            lines.append(f"  • [cyan]validate[/cyan] — {nxt} does not pass yet")
        else:
            lines.append("  • [cyan]export the crate[/cyan] — all three profiles pass")
        if not any(e.type == "File" for e in engine.state.list_entities()) and file_count:
            lines.append(
                f"  • [cyan]attach the data files[/cyan] — {file_count} scanned, none placed yet"
            )
        lines.append("  • [cyan]list entities[/cyan] — see everything drafted so far")

        console.print(
            Panel(
                f"[bold]Welcome back![/bold] "
                f"[bold cyan]{entity_count}[/bold cyan] entities across "
                f"{len(counts)} types, {file_count} scanned files.\n"
                "[dim]Where to pick up:[/dim]\n" + "\n".join(lines),
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
        greeting_diagnostic: dict[str, str] | None = None
        greeting_config = {
            **_thread_config(),
            "callbacks": [_ToolSpinnerCallback(spinner)],
        }
        with spinner:
            # Wall-clock guard (#263 Fix A): a hung greeting must never block the
            # session from starting. On timeout/error we fall through to the
            # static fallback panel below.
            result, outcome, greeting_diagnostic = _invoke_with_timeout(
                app,
                {"messages": [HumanMessage(content=greeting_prompt)]},
                greeting_config,
                timeout=request_timeout,
                include_error=True,
            )
        root_logger.setLevel(old_root_level)
        _rotate_checkpoint(outcome)
        # .strip(): a greeting of pure whitespace (a model that spent its turn on
        # reasoning blocks and emitted a bare newline) is NOT a greeting. Left
        # untrimmed it was truthy, so the user got an empty green bullet and the
        # fallback panel — the thing that actually says what to do next — was
        # skipped precisely when it was needed most.
        reply = (_extract_reply(result) or "").strip() if (outcome == "ok" and result) else ""
        if resumed:
            # ALWAYS on a resume: where the crate stands and what to do next is
            # derived from state, so it is correct whatever the model says (or
            # fails to say). Any greeting it does produce prints underneath as
            # commentary, not as the only orientation the user gets.
            _print_resume_panel()
            if reply:
                _print_reply(reply)
        elif reply:
            _print_reply(reply)
        else:
            _print_fresh_fallback()
        if verbose and outcome == "error" and greeting_diagnostic:
            diagnostic_record: dict[str, Any] = {
                "event": "model_error",
                "exception_type": greeting_diagnostic["exception_type"],
                "message": greeting_diagnostic["message"],
                "exception_chain": greeting_diagnostic["exception_chain"],
                "traceback_tail": greeting_diagnostic["traceback_tail"],
                "stage": "legacy_greeting",
            }
            if engine.profiler is not None:
                engine.profiler.log_event(**diagnostic_record)
            console.print(
                "[yellow]Legacy greeting error[/yellow]: "
                f"{greeting_diagnostic['exception_chain']}"
            )
            if greeting_diagnostic["traceback_tail"]:
                console.print(
                    f"[dim]Traceback tail:\n{greeting_diagnostic['traceback_tail']}[/dim]"
                )
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
        if resumed:
            _print_resume_panel()
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

        Runs under the same spinner the rest of the session uses: the build here
        is a full SHACL pass plus a disk write — tens of seconds on a real crate
        — and a bare "Finalizing…" line with nothing moving reads as a hang at
        the exact moment the user is waiting to leave.
        """
        delegated = footer.active
        spinner = ProgressSpinner(
            console,
            "finalizing the crate",
            tick_interval=0.12 if delegated else 0.5,
            activity_sink=footer.set_activity if delegated else None,
            width_provider=footer.line_width if delegated else None,
        )
        # The backstop drives tools through engine.run_tool (not the LangChain
        # callbacks the turn spinner listens to), so name the running step from
        # the engine's own event hook.
        prior_tool_event = engine.on_tool_event
        engine.on_tool_event = lambda tool, phase, args_str: (
            spinner.set_current(tool) if phase == "start" else spinner.set_current(None)
        )
        try:
            with spinner:
                _finish_backstop(engine, emit=console.print)
        finally:
            engine.on_tool_event = prior_tool_event

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

        # With the footer up, the spinner paints onto its activity row instead of
        # opening a Live region in the scrolling area: the working line then
        # holds still at the bottom rather than drifting up with the transcript.
        # It ticks faster there because the footer, not Rich, animates the frame.
        delegated = footer.active
        spinner = ProgressSpinner(
            console,
            tick_interval=0.12 if delegated else 0.5,
            activity_sink=footer.set_activity if delegated else None,
            # Lets the streamed reply tail fill the footer row exactly rather
            # than sitting at a fixed width and being cut from the wrong end.
            width_provider=footer.line_width if delegated else None,
        )
        main_config = {
            **_thread_config(),
            "callbacks": [_ToolSpinnerCallback(spinner)],
        }
        outcome = "ok"
        reply = ""
        diagnostic: dict[str, str] | None = None
        try:
            with spinner:
                result, outcome, diagnostic = _invoke_with_timeout(
                    app,
                    {"messages": [HumanMessage(content=message_content)]},
                    main_config,
                    timeout=request_timeout,
                    include_error=True,
                )
        except GraphRecursionError:
            # The turn hit the recursion_limit safety net — treat as a graceful
            # end so the loop stops auto-continuing and the backstop can run.
            outcome = "recursion"
        finally:
            root_logger.setLevel(old_root_level)

        _rotate_checkpoint(outcome)

        # Flush any in-loop auto-export status lines buffered during the invoke
        # (#287 Fix A) now the spinner's Live region is gone, so "Crate written
        # to: <abs path>" lands cleanly in the transcript.
        if auto_export_lines:
            replies.invalidate()  # this output must not be erased with the reply
        while auto_export_lines:
            console.print(auto_export_lines.pop(0))

        if outcome == "ok" and result:
            reply = _extract_reply(result)
            if reply:
                # Only throwaway commentary is transient. Anything with
                # structure — the issue list just asked for, a summary — is the
                # turn's result and must survive the next step.
                _print_reply(reply, transient=_reply_is_running_commentary(reply))
        elif outcome == "timeout":
            # Not a failure: a built-in limit was reached and the turn is being
            # handed back. Saying "error" here sent people hunting for a bug
            # that did not exist.
            console.print(
                f"[dim]Over to you — that step passed the {request_timeout:.0f}s time "
                "limit, so I stopped it. Nothing is lost; the session is saved. "
                "Say [/dim][bold]continue[/bold][dim] to pick it back up.[/dim]"
            )
            console.print()
        elif outcome == "stopped":
            console.print(
                "[dim]Over to you — I was repeating the same step without getting "
                "anywhere, so I stopped rather than keep spending on it. The session "
                "is saved.[/dim]"
            )
            console.print()
        elif outcome == "stopped":
            console.print(
                "[yellow]I was repeating the same step without making progress[/yellow], "
                "so I stopped. Your work so far is saved."
            )
            console.print(
                "  [dim]Tell me what to do next — e.g.[/dim] [bold]export the crate[/bold] "
                "[dim]or[/dim] [bold]what is still missing?[/bold]"
            )
            console.print()
        elif outcome == "recursion":
            console.print(
                "[dim]Over to you — this request hit its step limit "
                f"([bold]{max_iterations}[/bold] tool iterations), so I stopped. The "
                "session is saved; a smaller or more specific request usually gets "
                "further.[/dim]"
            )
            console.print()
        elif outcome == "error":
            if verbose and diagnostic:
                diagnostic_record: dict[str, Any] = {
                    "event": "model_error",
                    "exception_type": diagnostic["exception_type"],
                    "message": diagnostic["message"],
                    "exception_chain": diagnostic["exception_chain"],
                    "traceback_tail": diagnostic["traceback_tail"],
                    "stage": "legacy_model_turn",
                }
                if engine.profiler is not None:
                    engine.profiler.log_event(**diagnostic_record)
                console.print(
                    "[yellow]Legacy model error[/yellow]: "
                    f"{diagnostic['exception_type']}: {diagnostic['message']}"
                )
                if diagnostic["traceback_tail"]:
                    console.print(f"[dim]Traceback tail: {diagnostic['traceback_tail']}[/dim]")
                console.print("Your work so far is saved.")
            else:
                # Reserved for a GENUINE failure now that timeouts, loop guards
                # and the step limit each report themselves.
                console.print(
                    "[yellow]Something actually went wrong on that step[/yellow] and I "
                    "stopped. The session is saved. Re-run with [bold]-v[/bold] to see "
                    "the underlying error."
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
    # A caller-supplied kickoff drives the first turn in place of the first stdin
    # read (#412); everything after it is an ordinary typed turn. Blank is absent.
    pending_input: str | None = (initial_prompt or "").strip() or None

    # The status footer is pinned to the bottom rows for the whole session so
    # entities, validation dots, tokens and cost keep advancing while the agent
    # works — the scrolling bar only ever refreshed when the user was prompted,
    # which on a 15-turn autonomous run meant minutes of frozen numbers. It is a
    # no-op on a non-TTY, where print_status_bar falls back to the scrolling bar.
    footer = ui.make_status_footer(engine, console)
    footer.start()
    try:
        while True:
            try:
                # Back at the prompt: whatever narration is on screen is now the
                # last thing the agent said, so it stays. Anything printed from
                # here on must not be erased by the next turn's reply.
                replies.invalidate()
                # Status before each prompt: a repaint of the pinned footer when
                # it owns the bottom rows, otherwise the scrolling header (counts
                # live there, so the prompt line stays clean).
                ui.print_status_bar(engine, footer)
                console.print()
                if pending_input is not None:
                    # Echo the seeded line so the transcript shows what drove the
                    # turn, exactly as boxed_input echoes a typed one.
                    user_input, pending_input = pending_input, None
                    console.print(f"[bold cyan]❯[/bold cyan] {user_input}")
                else:
                    # Rounded input box (Claude Code style); falls back to a plain
                    # prompt when not a TTY. Raises KeyboardInterrupt / EOFError.
                    # prompt_toolkit erases to the end of the screen on every
                    # repaint, so the footer repaints on the same beat.
                    user_input = ui.boxed_input(console, on_render=footer.refresh)
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
                    # Land the turn's work in the footer immediately rather than
                    # waiting up to a tick — the reply and the counts it produced
                    # should appear together.
                    footer.refresh()

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
    finally:
        # Always hand the bottom rows (and the scrolling region) back, on every
        # exit path — quit, EOF, or an exception escaping the loop.
        footer.stop()


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
