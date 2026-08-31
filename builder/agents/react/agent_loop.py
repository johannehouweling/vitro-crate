"""LangChain agent loop for the ISA-Tox RO-Crate Builder.

Provides a provider-agnostic interactive agent that wraps the toolbox
and lets the LLM decide which tools to call based on user requests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import traceback
from collections import Counter
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
from builder.tools.document_discovery import looks_like_publication
from builder.tools.hitl import CONVERSATION_FIELD_TYPE, answers_are_synthetic, is_interactive

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
        # Also the authoritative "the model is deciding again" signal, which is
        # what the no-progress guard counts. Every tool call the model emits from
        # this generation shares one decision id, so a parallel batch of six
        # reads costs one strike rather than six.
        _begin_decision()

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any
    ) -> None:
        # LangChain dispatches chat models here, never to on_llm_start, and the
        # reset is the same — but this is declared separately rather than aliased
        # to on_llm_start. The base signature takes `messages`, not `prompts`, so
        # the alias was an LSP violation: a caller passing `messages=` by keyword
        # would have hit an unexpected-argument TypeError. Both bodies ignore
        # their payload, so it only ever mattered to a keyword caller — but it
        # made the class type-incorrect.
        self.spinner.set_preview(None)
        _begin_decision()

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
# Where the last successful export landed, so a suppressed repeat can name it.
_EXPORT_PATH_FLAG = "_crate_last_export_path"

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
    recommended = engine.run_tool("build_and_validate", severity="recommended", profile="all")
    if not isinstance(recommended, dict) or "error" in recommended:
        setattr(engine, _VALIDATION_ESCALATION_FP_FLAG, fingerprint)
        detail = recommended.get("error") if isinstance(recommended, dict) else None
        return {
            "recommended": {"status": "error", "detail": str(detail or "unknown error")},
            "optional": {"status": "not_reached"},
        }

    recommended_issues = recommended.get("issues") or []
    recommended_status = (
        f"{len(recommended_issues)} finding(s)" if recommended_issues else "no findings"
    )
    summary: dict[str, Any] = {
        "recommended": _escalation_tier_summary(recommended, engine.state.validation.should_issues),
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

# Read-only state queries. They mutate nothing, so re-running one against an
# unchanged crate cannot return anything new — the answer is a pure function of
# the state. The tool itself costs a millisecond; the MODEL TURN wrapped around
# it costs ~12k input tokens, which is what makes a read loop expensive.
#
# The old consecutive-identical breaker could not see this: a model rotating
# list_entities("LabProcess") -> ("CellLineSample") -> ("Sample") never repeats
# two calls in a row. One session issued 168 such reads back to back with no
# mutation between them — 82% of its tool calls, 1.5M input tokens, 42 minutes.
# Keying on (query, state) catches any order, exactly as the validation guard
# does, and any real mutation re-enables every query by changing the fingerprint.
_STATE_QUERY_TOOLS = frozenset(
    {"list_entities", "get_status", "list_scanned_files", "get_hint", "check_provenance"}
)
_STATE_QUERY_SEEN_FLAG = "_state_query_seen_fingerprints"

# Registry lookups. Unlike a state query, a lookup's answer does not depend on
# the crate AT ALL — it is a pure function of its arguments against an external
# registry — so this guard keys on (name, args) with NO fingerprint. Keying on
# state would be actively wrong here: one profiled loop re-issued the same
# lookup_orcid eight times while drafting people in between, and every one of
# those drafts would have reset a fingerprinted guard.
#
# Suppression starts at the FIRST repeat, not the third. For a state query a
# repeat is merely pointless; for a lookup the identical answer is already in
# the model's context verbatim, and nothing it can do will change it. The
# observed run served seven repeats from cache in 0.0s each and still paid a
# full ~6s model turn for every one — ~42s producing nothing. The guard hands
# the previous answer straight back rather than only scolding, because the
# model asked again precisely because it had lost track of it.
#
# `tests/test_lookup_repeat_guard.py` pins this set against the registered
# tools, so a new lookup_* cannot quietly fall outside it.
_LOOKUP_TOOLS = frozenset(
    {
        "lookup_compound",
        "lookup_cell_line",
        "lookup_cell_line_by_name",
        "lookup_aop",
        "lookup_bao_term",
        "lookup_ontology_term",
        "lookup_unit",
        "lookup_dtxsid",
        "lookup_orcid",
        "lookup_ror",
        "lookup_doi",
    }
)
# Distinct from `_LOOKUP_SEEN_FLAG` below, which records WHETHER a lookup counted
# as progress (a set of signatures, read by the idle ladder). This holds the
# ANSWER itself, so a repeat can be served from it. Two names, two shapes, one
# subject — keeping them apart matters: they are stored on the same engine object.
_LOOKUP_ANSWER_FLAG = "_lookup_seen_answers"

# Repeats of one query against one unchanged state before the guard stops
# answering and only logs. It does NOT end the turn — the idle ladder is what
# cancels an invocation, and it sees every suppressed call through
# `_track_progress`.
_STATE_QUERY_ABORT = 3

# --- consecutive calls that change nothing ----------------------------------
# The general form of every loop this codebase has hit. A tool call that leaves
# the crate fingerprint untouched made no progress, whatever it was: a read, a
# no-op write, a suppressed retry. Counting them as a RUN catches rotation that
# per-tool guards miss, because it does not care which tool or which arguments
# were used — only that nothing moved.
#
# Thresholds are measured, not guessed. Across 35 sessions, 66% of read runs are
# 1-3 calls (routine planning: entities, then status, then files) and 80% are
# <=5 — but runs of 6+ account for 1,642 calls, including runs of 187, 215 and
# 231. So five is free, the sixth earns a warning, and the ninth ends the turn.
# Set when a guard ends a turn, so the hand-back can say what it was doing
# rather than only that it stopped.
_STOP_REASON_FLAG = "_react_stop_reason"
_LAST_TOOL_FLAG = "_react_last_tool"

_IDLE_STREAK_FLAG = "_calls_without_progress"
_IDLE_BATCH_FLAG = "_last_counted_batch"
_ERRORED_ATTEMPT_FLAG = "_errored_mutation_attempts"
_ERRORED_ATTEMPT_ALLOWANCE = 3

# How many times one user message may resume itself after a guard stop. Two is
# enough to clear a bad patch and small enough that a genuinely stuck run still
# reaches the user quickly, having burned three budgets rather than one.
_MAX_SELF_CONTINUES = 2


def _self_continue_directive(outstanding: list[str]) -> str:
    """The message a self-continue sends — what "continue" should have meant.

    Resuming with the same words the user typed invites the same approach that
    just stalled. Naming the outstanding work and forbidding the querying that
    burned the last budget makes the retry differ from the attempt.
    """
    shown = "\n".join(f"  - {item}" for item in outstanding[:6])
    return (
        "Continue the work. The last stretch made no changes to the crate, so do "
        "not re-query status or re-read documents you already have — go straight "
        "to a mutation.\n\nStill open:\n" + shown + "\n\nPick the FIRST item and do "
        "it now with draft_*/set_fields/link/attach_files. If an item genuinely "
        "needs the user (a licence, who owns the crate), ask that one question."
    )


# --- one strike per model DECISION, not per tool call ------------------------
# The model emits tool calls in parallel batches — 3 to 6 per decision is normal,
# 16 has been observed. Counting each call separately meant a single decision
# could spend the entire idle budget before the model had ANY chance to react to
# a nudge: two batches of three ended a turn that, from the model's side, was
# two steps old. The streak is what the model is judged on, so it has to be
# measured in the unit the model actually controls.
# The boundary is the model's turn, taken straight from LangChain: the model is
# invoked, decides, and emits its tool calls, so everything between two
# generations belongs to ONE decision. Timing heuristics were the alternative
# and they get this wrong exactly when it matters — a batch whose calls are all
# suppressed returns so fast that the calls need not overlap at all.
_decision_lock = threading.Lock()
_decision_id = 0


def _begin_decision() -> None:
    """Note that the model has been invoked again (called from the callback)."""
    global _decision_id
    with _decision_lock:
        _decision_id += 1


def _current_decision() -> int:
    """The id of the decision whose tool calls are running now."""
    with _decision_lock:
        return _decision_id


# Nudge, then nudge harder, then hand back. The old shape spent SIX calls
# saying nothing before the first warning and then repeated one generic
# sentence — a model that has stopped making progress needs a different
# instruction, not the same one louder. Now the first nudge lands on the third
# idle call and each one names a concrete next action derived from the crate;
# after three the model has demonstrably not taken any of them, and the person
# is better placed to say what should happen than another round of guessing.
_IDLE_STREAK_WARN = 3
_IDLE_NUDGE_LIMIT = 3
_IDLE_STREAK_ABORT = _IDLE_STREAK_WARN + _IDLE_NUDGE_LIMIT

# Bounces off the suppression guard (same scope, same state) before it logs.
# Suppression alone does not stop the loop — the model reads the corrective and
# calls straight back, so every bounce still costs a full model turn. Steering,
# not stopping: ending the turn is the idle ladder's job.
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
    """Return True when the crate passes REQUIRED and has nothing left to do.

    Issue #263 (Fix B): completion short-circuits the autonomous loop so the
    agent stops re-invoking once there is nothing left to do. It used to mean
    only "all three profiles pass with no REQUIRED gaps" — which becomes true
    early and stays true, so the loop ended after EVERY turn for the rest of the
    session. The user then hand-cranked the remaining work one description at a
    time, typing "continue" to answer a question nobody had asked.

    Passing REQUIRED is the floor, not the finish. A crate with unwritten
    descriptions, unassigned protocols and an assay folder nobody modelled is
    unfinished however green the profiles are, and the outstanding list already
    knows that. Informational lines are excluded: a count of findings on
    build-generated nodes can never be worked off, so counting it would leave
    the agent running until the turn cap on every session.
    """
    try:
        if not engine.state.list_entities():
            return False
        val = engine.state.validation
        gates_pass = bool(
            val.base_passed and val.isa_passed and val.tox_passed and not val.required_issues
        )
        if not gates_pass:
            return False
        return not open_items(engine.state, actionable_only=True)
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
      propagated, so it can never escape the loop), except a
      ``GraphRecursionError`` → ``(None, "recursion")``: the graph ran out of
      budget rather than breaking (#331)
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
        if isinstance(outcome["error"], GraphRecursionError):
            # The graph ran out of recursion budget. That is a distinct ending —
            # valid-at-the-cutoff work, not a breakage (#331) — and a caller's
            # ``except GraphRecursionError`` can never see it, because this worker
            # catches BaseException first. Classify it here or a cap hit is
            # reported to the user, and to the A/B, as a generic error.
            logger.info("Model invoke hit the graph recursion cap: %s", outcome["error"])
            return (None, "recursion", None) if include_error else (None, "recursion")
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

    Issue #287 Fix A: the ReAct loop only wrote a crate via the finish
    backstop, which runs on the EXIT path (quit/EOF). In a live run the user kept
    the session alive, the weak model never called ``export_crate``, and a fully
    built, base-VALID crate (70+ entities) was NEVER written. The deterministic
    pipeline already exports on every completed build (#233); this brings the
    ReAct loop in line.

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


# Serialises the cycle bookkeeping. LangGraph's ToolNode runs a model's tool
# calls CONCURRENTLY (16 at once observed), so without this the read-modify-write
# of the shared history races.
_MUTATION_HISTORY_LOCK = threading.Lock()

# Mutations currently executing. Cycle detection compares a GLOBAL state
# fingerprint, so it is only meaningful when one mutation runs at a time: with a
# batch in flight each thread sees its siblings' writes, and "the state is
# somewhere it just was" becomes true for reasons that have nothing to do with
# the caller. Oscillation is inherently sequential — the model writes, reads the
# result, writes again — so a concurrent batch is never the thing we are hunting.
_MUTATIONS_IN_FLIGHT = "_mutations_in_flight"

# Consecutive no-op mutations per target, so writing nothing over and over
# escalates the same way cycling and re-validating do.
_NO_OP_STRIKE_FLAG = "_no_op_mutation_strikes"


def _reset_turn_guards(engine: AgentEngine) -> None:
    """Give a new user turn a fresh strike budget for every loop guard.

    The counters are per-engine, i.e. per SESSION, but their purpose is to stop
    one runaway turn — not to hold a grudge. Left standing at the limit, the
    turn after an abort died on its first offending call, so "continue" produced
    an immediate "I was repeating the same step" and the user could never get
    moving again.

    What is deliberately NOT cleared: the memoised state fingerprints. Those
    encode a fact about the crate ("this scope has already been validated
    against this exact state"), which a new user turn does not change — the call
    is still suppressed and still answered with a corrective, it just no longer
    counts toward ending the turn.
    """
    seen = getattr(engine, _BUILD_VALIDATE_SEEN_FLAG, None) or {}
    setattr(
        engine,
        _BUILD_VALIDATE_SEEN_FLAG,
        {sig: (fingerprint, 0) for sig, (fingerprint, _strikes) in seen.items()},
    )
    setattr(engine, _NO_OP_STRIKE_FLAG, {})
    setattr(engine, _STATE_QUERY_SEEN_FLAG, {})
    setattr(engine, _MUTATION_CYCLE_COUNT_FLAG, 0)
    setattr(engine, _MUTATION_HISTORY_FLAG, {})
    setattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, None)
    setattr(engine, _LOOP_BREAKER_COUNT_FLAG, 0)
    setattr(engine, _IDLE_STREAK_FLAG, 0)
    # Also NOT cleared: which lookups have already been answered. Re-asking a
    # question across turns still returns the answer already held, so it must
    # not read as fresh progress just because the user typed "continue".
    setattr(engine, _STOP_REASON_FLAG, None)


def _mutation_target(tool_name: str, kwargs: dict[str, Any]) -> str:
    """What a mutation is acting ON, for per-target cycle bookkeeping.

    Oscillation is always a fight over ONE thing — the same field on the same
    entity, rewritten a different way. Keying the history by target keeps
    unrelated concurrent mutations out of each other's history: a batch of 16
    ``resolve_compound`` calls landing together would otherwise see each other's
    fingerprints and report cycles none of them made.
    """
    for key in ("entity_id", "name", "from_id", "assay_id", "path"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}:{value.strip()[:80]}"
    return tool_name


def _record_mutation_cycle(
    engine: AgentEngine, tool_name: str, kwargs: dict[str, Any], *, concurrent: bool = False
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
    if concurrent:
        return None  # see _MUTATIONS_IN_FLIGHT — the comparison is meaningless here
    try:
        fingerprint = engine.state.validation_fingerprint()
    except Exception:  # noqa: BLE001 — best-effort bookkeeping
        return None

    # Per TARGET, under a lock. A model's tool calls run concurrently, so one
    # global history mixed unrelated mutations together and a batch of parallel
    # resolve_compound calls reported cycles none of them made — each thread saw
    # the fingerprints its siblings had just written.
    target = _mutation_target(tool_name, kwargs)
    with _MUTATION_HISTORY_LOCK:
        histories: dict[str, list[str]] = dict(getattr(engine, _MUTATION_HISTORY_FLAG, None) or {})
        history = list(histories.get(target, []))
        revisited = fingerprint in history
        history.append(fingerprint)
        histories[target] = history[-_MUTATION_HISTORY_WINDOW:]
        setattr(engine, _MUTATION_HISTORY_FLAG, histories)
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


def _progress_fingerprint(engine: AgentEngine) -> str | None:
    """What "the session moved forward" means — wider than the crate's contents.

    ``validation_fingerprint`` covers entities + metadata, which is exactly right
    for "did this write change the crate" and exactly wrong for "did this call
    achieve anything". Reading a document, scanning files, learning the user's
    answer and running the first validation all leave it untouched, so measuring
    progress with it declared the entire evidence-gathering phase to be a loop —
    a fresh session aborted before drafting a single entity.

    Progress therefore also counts: documents loaded into evidence, files
    scanned, answers received, and the validation verdict itself.
    """
    state = engine.state
    try:
        parts = [
            state.validation_fingerprint(),
            ",".join(sorted(getattr(state, "document_evidence", {}) or {})),
            str(len(getattr(state, "scanned_files", []) or [])),
            str(len(getattr(state, "user_answers", []) or [])),
            json.dumps(state.validation.to_dict(), sort_keys=True, default=str),
        ]
    except Exception:  # noqa: BLE001 — best-effort bookkeeping
        return None
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


_KNOWLEDGE_TOOL_PREFIXES = ("lookup_", "search_", "fetch_")
_LOOKUP_SEEN_FLAG = "_react_lookups_seen"

# Asking the user is a real tool and sometimes the only correct move — a licence
# choice or the crate's owner cannot be derived from the files. But it is also
# the cheapest thing for a model to do when it is unsure, and a session that
# asks its way through the work is worse than one that reads the workbook.
_HITL_TOOLS = frozenset({"present_to_human", "request_input"})
_HITL_LAST_PROGRESS_FLAG = "_react_hitl_last_progress"
_HITL_DEFLECTED_FLAG = "_react_hitl_deflected"


def _guard_human_question(
    engine: AgentEngine, tool_name: str, kwargs: dict[str, Any], progress_before: str | None
) -> str | None:
    """Deflect ONE question that arrives with nothing done since the last one.

    The rule is about effort, not about the question: ask freely whenever the
    crate has moved since you last asked. Ask twice in a row having changed
    nothing in between, and the first attempt comes back with the deterministic
    next action and the answers already on file — one round to reconsider with
    that in hand.

    It deflects at most once. A model that still wants the user after reading
    the corrective gets through, because the failure mode on the other side —
    an agent that will not ask and instead guesses at a licence or invents an
    owner — is worse than one that asks twice. Returns the corrective text to
    send instead of the question, or None to let it through.
    """
    if tool_name not in _HITL_TOOLS:
        return None
    last = getattr(engine, _HITL_LAST_PROGRESS_FLAG, None)
    if last is None or last != progress_before:
        # First question, or real work since the last one — no objection.
        setattr(engine, _HITL_LAST_PROGRESS_FLAG, progress_before)
        setattr(engine, _HITL_DEFLECTED_FLAG, 0)
        return None
    if int(getattr(engine, _HITL_DEFLECTED_FLAG, 0)) >= 1:
        setattr(engine, _HITL_DEFLECTED_FLAG, 0)
        setattr(engine, _HITL_LAST_PROGRESS_FLAG, progress_before)
        return None  # asked again anyway: it means it, let it through
    setattr(engine, _HITL_DEFLECTED_FLAG, 1)
    _log_suppressed(engine, tool_name, "hitl_without_progress", kwargs)
    try:
        next_action = _completeness_nudge(engine.state)
    except Exception:  # noqa: BLE001 — steering must never raise
        next_action = ""
    answered = _format_user_answers(engine)
    asked = str(kwargs.get("context") or kwargs.get("prompt") or "").strip()
    parts = [
        "Not sent yet — nothing about the crate has changed since your last question "
        "to the user, so this is a second ask for the same round of work."
    ]
    if asked:
        parts.append(f'You wanted to ask: "{asked[:200]}"')
    if next_action:
        parts.append(f"What the crate still needs: {next_action}")
    if answered:
        parts.append(answered.strip())
    parts.append(
        "Work out what you can from the documents, the lookups and the state above "
        "first. If the answer genuinely is not derivable — a licence choice, who owns "
        "the crate — ask again and it WILL go through to the user."
    )
    return "\n\n".join(parts)


def _is_new_fact(
    engine: AgentEngine,
    tool_name: str,
    # The (name, args) tuple from `_call_signature`, as everywhere else.
    signature: tuple[str, tuple[Any, ...]] | None,
    result: Any,
) -> bool:
    """True when this call ACQUIRED a fact the crate does not hold yet.

    A lookup is how the agent earns the values it is about to write: an RRID for
    a cell line, a CAS number for a compound. None of it touches the crate, so
    the fingerprint is unchanged and the no-progress guard counted a successful
    ``lookup_compound`` as idling — while the nudge it had just been given said
    "next: resolve_compound". The model did as it was told and the guard ended
    its turn for it.

    Only the FIRST resolution of a given query counts. Asking the same question
    twice returns the same answer and advances nothing, so a repeated lookup
    still falls through to the idle counter and cannot hold a turn open forever.
    """
    if not tool_name.startswith(_KNOWLEDGE_TOOL_PREFIXES):
        return False
    if isinstance(result, dict):
        if result.get("error") or result.get("found") is False:
            return False
        found = bool(result.get("found") or result.get("data") or result.get("entity_id"))
    else:
        found = bool(result)
    if not found:
        return False
    # Holds whichever key was used: the (name, args) signature when the caller
    # has one, else the bare tool name. Annotating it `set[str]` described only
    # the fallback.
    seen: set[tuple[str, tuple[Any, ...]] | str] = getattr(engine, _LOOKUP_SEEN_FLAG, None) or set()
    key: tuple[str, tuple[Any, ...]] | str = signature or tool_name
    if key in seen:
        return False
    seen.add(key)
    setattr(engine, _LOOKUP_SEEN_FLAG, seen)
    return True


def _track_progress(
    engine: AgentEngine,
    tool_name: str,
    before: str | None,
    result: Any,
    *,
    # The call sites pass `_call_signature(...)`, which is a (name, args) tuple
    # — not a string. It is only stored and compared for equality, so the tuple
    # always worked; the annotation just described the wrong thing. Same fix as
    # `_guard_state_query`'s `signature`.
    signature: tuple[str, tuple[Any, ...]] | None = None,
) -> Any:
    """Count consecutive calls that changed nothing, and intervene on a run.

    The single rule the per-tool guards are special cases of: a call that leaves
    the crate fingerprint untouched made no progress, whether it was a read, a
    write that wrote nothing, or a retry that got suppressed. Because it ignores
    the tool and its arguments, rotating between queries — the trick that walked
    past every earlier guard — cannot hide a run.

    A run is allowed to reach :data:`_IDLE_STREAK_WARN` before the result is
    annotated (short read bursts are how planning legitimately looks), and ends
    the turn at :data:`_IDLE_STREAK_ABORT`. Any call that changes the crate
    resets the count to zero.
    """
    if before is None:
        return result
    after = _progress_fingerprint(engine)
    if after is None:
        return result
    changed = after != before or _is_new_fact(engine, tool_name, signature, result)
    if changed:
        setattr(engine, _IDLE_STREAK_FLAG, 0)
        return result

    # A mutation that RAISED is a failed attempt, not idling. The model called
    # draft_process_chain — precisely the outstanding work — with an assay_id it
    # had not confirmed; the call errored, and the error counted toward the same
    # budget as an idle status poll. Two such attempts plus four queries ended
    # the turn while the model was doing the right thing badly. Forgiven a
    # bounded number of times per turn: the loop-breaker still catches an
    # identical failing call, and past the allowance these count again so a
    # model failing in novel ways every time cannot run forever.
    if tool_name in _MUTATION_TOOLS and isinstance(result, dict) and "error" in result:
        attempts = int(getattr(engine, _ERRORED_ATTEMPT_FLAG, 0)) + 1
        setattr(engine, _ERRORED_ATTEMPT_FLAG, attempts)
        if attempts <= _ERRORED_ATTEMPT_ALLOWANCE:
            logger.info(
                "Failed %s attempt %d/%d — not counted as idling",
                tool_name,
                attempts,
                _ERRORED_ATTEMPT_ALLOWANCE,
            )
            return result

    # One strike per DECISION. Three parallel reads that all come back
    # "already_in_evidence" are one unproductive step by the model, not three —
    # it had no opportunity to react in between, so charging it three times
    # spends the whole budget on a single step and ends turns that were two
    # moves old. Later calls in the same batch are answered without comment;
    # the first one carries the nudge.
    decision = _current_decision()
    if getattr(engine, _IDLE_BATCH_FLAG, None) == decision:
        return result
    setattr(engine, _IDLE_BATCH_FLAG, decision)

    streak = int(getattr(engine, _IDLE_STREAK_FLAG, 0)) + 1
    setattr(engine, _IDLE_STREAK_FLAG, streak)
    if streak < _IDLE_STREAK_WARN:
        return result

    _log_suppressed(engine, tool_name, f"no_progress_streak_{streak}", {})
    if streak >= _IDLE_STREAK_ABORT:
        logger.warning(
            "Ending turn: %d consecutive tool calls changed nothing (last: %s)",
            streak,
            tool_name,
        )
        setattr(
            engine,
            _STOP_REASON_FLAG,
            f"{streak} calls in a row changed nothing (last: {tool_name})",
        )
        raise _InvocationCancelled("no progress across consecutive tool calls")
    logger.info("No-progress run: %d calls, last %s", streak, tool_name)
    # The answer is still handed back — a late call in a run can carry genuinely
    # new information — with the run stated so the model can see what it is
    # doing. Withholding it would just prompt another read.
    body = result if isinstance(result, str) else str(result)[:1200]
    return f"{body}\n\n[{_idle_nudge(engine, streak, tool_name)}]"


def _idle_nudge(engine: AgentEngine, streak: int, tool_name: str) -> str:
    """One of three escalating steers, each naming a concrete next action.

    Repeating "that changed nothing" cannot help a model that has already heard
    it: if it knew what to do instead it would be doing it. Each nudge therefore
    carries the deterministic next-action line from :func:`_completeness_nudge`
    — computed from the crate, not guessed — and gets more specific about what
    happens if it is ignored. The third says plainly that the turn ends next.
    """
    nudge = streak - _IDLE_STREAK_WARN + 1  # 1, 2, 3
    try:
        next_action = _completeness_nudge(engine.state)
    except Exception:  # noqa: BLE001 — steering must never raise
        logger.debug("idle nudge: completeness line failed", exc_info=True)
        next_action = ""
    where = f" Crate now: {next_action}." if next_action else ""

    if nudge <= 1:
        return (
            f"{streak} calls in a row have changed nothing about the crate, and "
            f"{tool_name} is not moving it forward.{where} Do that next action "
            "instead of querying or re-reading — you already have what you need."
        )
    if nudge == 2:
        return (
            f"Still nothing changed after {streak} calls. Reading and listing cannot "
            f"advance the crate — only a mutation can.{where} Your NEXT call must be "
            "draft_*, set_fields, link, attach_files, or export_crate. If you genuinely "
            "cannot proceed, say so in a reply and ask the user the specific question "
            "you need answered."
        )
    return (
        f"Last warning: {streak} calls without a single change.{where} If the next "
        "call is not a mutation or a reply, this turn ends and the user is asked to "
        "decide. If you are stuck, ending with a question is a better outcome than "
        "another query."
    )


def _guard_state_query(
    engine: AgentEngine,
    tool_name: str,
    kwargs: dict[str, Any],
    # The call site passes `_call_signature(...)`, which is a (name, args) tuple
    # — not a string. It is only ever stored and compared for equality, so the
    # tuple worked; the annotation just described the wrong thing.
    signature: tuple[str, tuple[Any, ...]],
) -> str | None:
    """Stop a read-only query being re-asked of a state that has not changed.

    Returns a corrective to hand back instead of running the query, or ``None``
    when the query is new (or the state has moved on) and should run normally.
    Never ends the turn: past :data:`_STATE_QUERY_ABORT` repeats of one question
    against one unchanged crate it only logs. Cancelling an invocation is the idle
    ladder's job, and every corrective returned here is routed through
    :func:`_track_progress` so the ladder counts it.

    Keyed on (query, state fingerprint) rather than on consecutive repeats,
    because the observed loop rotated between three entity types and so never
    repeated itself twice in a row.
    """
    try:
        fingerprint = engine.state.validation_fingerprint()
    except Exception:  # noqa: BLE001 — a guard must never block a call
        return None

    # Keyed by the (name, args) call signature, matching what the caller passes.
    seen: dict[tuple[str, tuple[Any, ...]], tuple[str, int]] = dict(
        getattr(engine, _STATE_QUERY_SEEN_FLAG, None) or {}
    )
    previous = seen.get(signature)
    if previous is None or previous[0] != fingerprint:
        seen[signature] = (fingerprint, 0)
        setattr(engine, _STATE_QUERY_SEEN_FLAG, seen)
        return None

    strikes = previous[1] + 1
    seen[signature] = (fingerprint, strikes)
    setattr(engine, _STATE_QUERY_SEEN_FLAG, seen)
    _log_suppressed(engine, tool_name, f"unchanged_state_query_{strikes}", kwargs)
    logger.info(
        "Suppressed repeated %s against unchanged state (strike %d/%d)",
        signature,
        strikes,
        _STATE_QUERY_ABORT,
    )
    # Deliberately does NOT end the turn any more. Two guards racing to stop a
    # session meant whichever counted fastest won, and this one — three repeats
    # of one query — pre-empted the escalating nudges before the model had been
    # told anything useful. The idle ladder is now the single authority on
    # ending a turn: it sees this suppression too (every guarded return is
    # routed through `_track_progress`), it nudges three times with a concrete
    # next action first, and it counts decisions rather than calls.
    if strikes >= _STATE_QUERY_ABORT:
        logger.info(
            "%s asked %d times of an unchanged crate — steering, not stopping",
            signature,
            strikes,
        )
    return (
        f"{tool_name} has already been answered for this exact crate state, and nothing "
        "has changed since — reading it again cannot return anything new.\n\n"
        f"{_format_compact_state_summary(engine)}"
        f"{_suppressed_query_answer(engine, tool_name, kwargs)}\n\n"
        "Use the answer above instead of re-querying. Your next call must CHANGE "
        "something — draft, link, attach, set a field — or answer the user."
    )


def _suppressed_query_answer(engine: AgentEngine, tool_name: str, kwargs: dict[str, Any]) -> str:
    """The ids a suppressed ``list_entities`` was asking for, so the refusal answers it.

    Suppressing a read while withholding what it would have returned is what makes
    the model bounce. It was not being stubborn: it needed an entity id to write
    with, the ids are minted from names and only LOOK derivable, so it rebuilt one
    from the pattern, got it subtly wrong, and went to `list_entities` to find the
    real one. The corrective then pointed at the compact state summary — which
    carries per-type COUNTS and at most eight recent ids, so with sixteen
    LabProcesses the answer structurally was not in there. Four bounces per turn,
    in every session profiled.

    So the guard now hands back the list, the same way the repeated-lookup guard
    hands back the previous answer. Only for a typed `list_entities`: `get_status`
    and the rest are already fully answered by the summary above.
    """
    if tool_name != "list_entities":
        return ""
    wanted = kwargs.get("entity_type") or kwargs.get("type")
    if not wanted:
        return ""
    try:
        ids = [e.entity_id for e in engine.state.list_entities(str(wanted))]
    except Exception:  # noqa: BLE001 — a corrective must never raise
        logger.debug("could not list %s for the corrective", wanted, exc_info=True)
        return ""
    if not ids:
        return f"\n\nThere are no {wanted} entities. Draft one before referring to it."
    listed = "\n".join(f"  - {eid}" for eid in ids)
    return (
        f"\n\nThe {len(ids)} {wanted} id(s), which is what you asked for:\n{listed}\n"
        "Copy one of these verbatim — do not rebuild an id from an entity's name."
    )


def _lookup_is_retryable(result: Any) -> bool:
    """Whether a lookup result is a TRANSIENT failure and so worth asking again.

    The lookup layer marks a timeout / 429 / 5xx with ``transient=True`` for
    exactly this reason, and its own caches decline to store those. The guard
    follows the same rule: a momentary outage must never be frozen into a
    permanent "you already asked that".
    """
    return isinstance(result, dict) and bool(result.get("transient"))


def _remember_lookup(engine: AgentEngine, signature: tuple[str, tuple], result: Any) -> None:
    """Record a lookup's answer so a repeat can be served from it."""
    if _lookup_is_retryable(result):
        return
    seen = dict(getattr(engine, _LOOKUP_ANSWER_FLAG, None) or {})
    seen[signature] = result
    setattr(engine, _LOOKUP_ANSWER_FLAG, seen)


def _guard_repeated_lookup(
    engine: AgentEngine,
    tool_name: str,
    kwargs: dict[str, Any],
    signature: tuple[str, tuple],
) -> str | None:
    """Hand back the answer a lookup already gave, instead of asking again.

    Returns the corrective, or ``None`` when this lookup is new (or its last
    answer was a transient failure) and should run normally.
    """
    seen = getattr(engine, _LOOKUP_ANSWER_FLAG, None) or {}
    if signature not in seen:
        return None

    previous = seen[signature]
    _log_suppressed(engine, tool_name, "lookup_already_answered", kwargs)
    logger.info("Suppressed repeated %s — the answer has not changed", signature)

    args = ", ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items()))
    corrective = (
        f"{tool_name}({args}) has already been answered in this run. A lookup does not "
        "depend on the crate, so nothing you have done since can change it — asking "
        "again returns exactly this:\n\n"
        f"{previous!r}\n\n"
    )
    if isinstance(previous, dict) and previous.get("fix"):
        # The lookup failed definitively and already said what to do instead;
        # repeat that rather than inventing a second, weaker instruction.
        corrective += f"{previous['fix']}\n\nDo that now."
    elif isinstance(previous, dict) and previous.get("found") is False:
        corrective += (
            "This is a definitive not-found. Record what you do know without the "
            "identifier, or ask the user — do not look it up again."
        )
    else:
        corrective += (
            "Use that value in your next call. Your next call must CHANGE something "
            "— draft, link, attach, set a field — or answer the user."
        )
    return corrective


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


_REMOVED_IDS_FLAG = "_react_removed_entity_ids"


def _flag_remove_then_redraft(engine: AgentEngine, tool_name: str, result: Any) -> Any:
    """Notice an entity being re-created after it was removed, and say what that costs.

    Removing an entity and drafting it straight back is how a RENAME or a
    RE-PARENT gets expressed when the drafting tools mint the id from the name:
    one session removed six processes, re-drafted them under different assays,
    filled three of them in, and removed them again — losing the values it had
    just written and detaching thirteen processes on the way through.

    The re-draft itself is legitimate, so it is allowed and annotated rather than
    blocked; ``set_fields`` renames and re-parents in place, and the model can
    only choose that if someone tells it the option exists at the moment it
    matters.
    """
    try:
        removed: set[str] = getattr(engine, _REMOVED_IDS_FLAG, None) or set()
        if tool_name == "remove_entity" and isinstance(result, dict) and result.get("removed"):
            removed = removed | {str(result.get("entity_id"))}
            setattr(engine, _REMOVED_IDS_FLAG, removed)
            return result
        if not tool_name.startswith("draft_") and tool_name != "scaffold_isa_backbone":
            return result
        entity_id = getattr(result, "entity_id", None)
        if not entity_id or str(entity_id) not in removed:
            return result
        return (
            f"{result}\n\n[NOTE: '{entity_id}' is an entity you removed earlier this "
            "session, now re-created EMPTY — any values it held are gone. If the goal "
            "was to rename it or move it to a different parent, set_fields does both "
            "in place and keeps the content: "
            "set_fields(entity_id='<id>', fields={'name': …, 'assay_id': …}).]"
        )
    except Exception:  # noqa: BLE001 — annotation never breaks a call
        logger.debug("remove/redraft check failed", exc_info=True)
        return result


def _record_recent_mutation(engine: AgentEngine, result: Any) -> None:
    """Keep a bounded list of entities returned by successful mutations."""
    entity = result
    if isinstance(result, dict):
        entity = (
            result.get("entity") or result.get("updated_entity") or result.get("created_entity")
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
        recent_text = (
            ", ".join(
                f"{entity_type}:{entity_id}" + (f" ({name})" if name else "")
                for entity_type, entity_id, name in recent
            )
            or "none"
        )
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
                    kwargs = {k: v for k, v in kwargs.items() if k not in ("args", "kwargs")}
                # An explicit null is the model saying "not specified", but it
                # reaches the tool as a real None and overwrites the parameter's
                # default: `list_scanned_files(offset=None)` raised
                # "'>' not supported between NoneType and int". Weak models fill
                # in EVERY optional parameter this way, so drop the nulls and let
                # the defaults apply — omitted and null mean the same thing here.
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                # What is running right now, so a turn cut short by the wall-clock
                # guard can say what it was doing rather than only that it stopped.
                setattr(engine, _LAST_TOOL_FLAG, tool_name)
                _raise_if_invocation_cancelled()
                # Captured BEFORE the guards, not just before execution. Every
                # guard below answers the model without running the tool, and
                # each used to return straight out — past the no-progress
                # tracker at the end of this function. A suppressed call was
                # therefore invisible to every counter, so the one path designed
                # to notice "nothing is changing" could not see the calls most
                # likely to be going nowhere. With the evidence store now large
                # enough to hold a whole working set, that gap became a hang:
                # the guard served the same three documents on demand, forever,
                # and one session spent fifty turns and seventy-three suppressed
                # reads asking for files it was handed every single time.
                progress_before = _progress_fingerprint(engine)
                # Loop-breaker (#287 Fix B): if this is the Nth consecutive
                # IDENTICAL call that has been returning a non-progress result
                # (directory/None/error), REFUSE to repeat it — return a forceful
                # corrective tool message with the actual scanned-file inventory so
                # a weak model stops looping (it ignored #281's directory message
                # and looped ~36×). Distinct calls / a single retry never trip this.
                signature = _call_signature(tool_name, kwargs)
                deflected = _guard_human_question(engine, tool_name, kwargs, progress_before)
                if deflected is not None:
                    return _track_progress(
                        engine, tool_name, progress_before, deflected, signature=signature
                    )
                if tool_name in _STATE_QUERY_TOOLS:
                    query_answer = _guard_state_query(engine, tool_name, kwargs, signature)
                    if query_answer is not None:
                        return _track_progress(
                            engine,
                            tool_name,
                            progress_before,
                            query_answer,
                            signature=signature,
                        )
                if tool_name in _LOOKUP_TOOLS:
                    known = _guard_repeated_lookup(engine, tool_name, kwargs, signature)
                    if known is not None:
                        return _track_progress(
                            engine,
                            tool_name,
                            progress_before,
                            known,
                            signature=signature,
                        )
                if tool_name == "list_entities":
                    list_last = getattr(engine, _LIST_ENTITIES_LAST_SIG_FLAG, None)
                    list_count = getattr(engine, _LIST_ENTITIES_COUNT_FLAG, 0)
                    if list_last == signature and list_count >= _LIST_ENTITIES_BREAKER_THRESHOLD:
                        _log_suppressed(engine, tool_name, "repeated_list_query", kwargs)
                        return _track_progress(
                            engine, tool_name, progress_before, _list_entities_intervention(engine)
                        )
                last_sig = getattr(engine, _LOOP_BREAKER_LAST_SIG_FLAG, None)
                repeat_count = getattr(engine, _LOOP_BREAKER_COUNT_FLAG, 0)
                if last_sig == signature and repeat_count >= _LOOP_BREAKER_THRESHOLD:
                    # Do NOT run the tool again — the identical non-progress call
                    # is short-circuited and the model is steered elsewhere.
                    _log_suppressed(engine, tool_name, "loop_breaker", kwargs)
                    return _track_progress(
                        engine,
                        tool_name,
                        progress_before,
                        _loop_breaker_intervention(engine, tool_name),
                    )

                if tool_name in _FILE_READ_TOOLS:
                    evidence = getattr(engine.state, "document_evidence", {})
                    path = _reader_evidence_key(engine, str(kwargs.get("path", "")))
                    read_args = {k: v for k, v in kwargs.items() if k != "path"}
                    cached = next(
                        (
                            item
                            for item in evidence.values()
                            if item.get("path") == path
                            and item.get("args", {}) == read_args
                            # A TRUNCATED copy is not an answer to "read this
                            # file". Serving one told the model its request was
                            # already satisfied while withholding two-thirds of
                            # the document, and left it no way to ask for the
                            # rest — the readers take no offset. When the stored
                            # copy is partial, fall through and read for real:
                            # the full text is what was asked for, and the
                            # no-progress guard still stops a genuine runaway.
                            and not item.get("truncated")
                        ),
                        None,
                    )
                    if path and cached is not None:
                        # Serving counts as use: keep this document at the young
                        # end of the store so the next eviction takes something
                        # the model has stopped asking for.
                        engine.touch_document_evidence(path)
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
                        # Handing the same text back a third time answers the
                        # call and teaches nothing. What the model is missing is
                        # not the document — it has it — but what to do with it,
                        # so the outstanding list rides along with the content.
                        # A value that is not in the file will not appear on the
                        # next read, and saying so is the only way out of the
                        # loop that does not require the guard to end the turn.
                        outstanding = open_items(engine.state)
                        steer = ""
                        if outstanding:
                            steer = (
                                "\n\n[You have already read this document. Still "
                                "outstanding:\n"
                                + "\n".join(f"  - {i}" for i in outstanding[:6])
                                + "\nIf a value is in the text above, write it with "
                                "set_fields now. If it is NOT in the text, re-reading "
                                "will not produce it — ask the user for that specific "
                                "value instead.]"
                            )
                        served = (
                            f"[Serving the copy of {path} already loaded this session "
                            "— identical to re-reading it.]\n\n" + content + steer
                            if content
                            else (
                                "Already loaded this document into bounded session evidence. "
                                "Use the loaded evidence in the state context; request a "
                                "specific different slice only if needed."
                            )
                        )
                        # Counted like any other call that changed nothing: being
                        # able to answer instantly is not the same as getting
                        # somewhere, and a model re-asking for a document it has
                        # already been handed is the clearest no-progress signal
                        # there is.
                        return _track_progress(engine, tool_name, progress_before, served)
                if tool_name in ("export_crate", "build_crate") and kwargs.get("output_path"):
                    # ONE SESSION, ONE CRATE. A session is the agent improving a
                    # single crate over time, so a later export supersedes the
                    # earlier one rather than standing beside it.
                    #
                    # A profiled session exported 32 times to TWELVE directories
                    # (…_crate_v64 … _v75, one save labelled
                    # "svhps26_complete_validated_v68") because the model minted a
                    # fresh versioned path per export. Each is a complete copy —
                    # `output/` had reached 75 crates and 367 MB — and naming a
                    # path also stepped around the unchanged-crate guard below,
                    # which only applies when none is given.
                    #
                    # Enforced HERE and not in `export_crate`: that function's
                    # contract is that an explicit argument wins, which the CLI
                    # and library callers rely on. It is the AGENT that must not
                    # invent a destination mid-run for a crate it is editing; a
                    # human passing --output still decides where the crate lives.
                    established = (engine.state.metadata.output_path or "").strip()
                    asked = str(kwargs.get("output_path") or "").strip()
                    if established and asked and Path(asked) != Path(established):
                        logger.info(
                            "Export redirected to this session's crate: %s (asked for %s)",
                            established,
                            asked,
                        )
                        kwargs = {**kwargs, "output_path": established}
                        _log_suppressed(engine, tool_name, "export_path_redirected", {"to": asked})
                if tool_name in ("export_crate", "build_crate") and not kwargs.get("output_path"):
                    # Exporting an unchanged crate rewrites the identical bytes.
                    # One session called export_crate FIFTEEN times, twice per
                    # turn around a pair of set_fields — and each export now runs
                    # a full three-profile, all-tier validation sweep before it
                    # writes, so the ritual cost more than the work it wrapped.
                    # An explicit output_path is always honoured: writing the
                    # same crate to a NEW location is a real request.
                    try:
                        export_fp = engine.state.export_fingerprint()
                    except Exception:  # noqa: BLE001 — the guard never blocks a call
                        export_fp = None
                    last_fp = getattr(engine, _AUTO_EXPORT_FINGERPRINT_FLAG, None)
                    if export_fp is not None and export_fp == last_fp:
                        _log_suppressed(engine, tool_name, "export_unchanged", kwargs)
                        crate_path = getattr(engine, _EXPORT_PATH_FLAG, None) or "the output path"
                        return _track_progress(
                            engine,
                            tool_name,
                            progress_before,
                            f"Already exported this exact crate to {crate_path} — nothing "
                            "has changed since, so re-writing it would produce identical "
                            "bytes. Change something first (the outstanding list says "
                            "what), or answer the user. The crate on disk is current.",
                            signature=signature,
                        )

                if tool_name == "build_and_validate":
                    bv_sig = _build_validate_signature(kwargs)
                    try:
                        bv_fp = engine.state.validation_fingerprint()
                    except Exception:  # noqa: BLE001 — the guard must never block a call.
                        bv_fp = None
                    bv_seen: dict[tuple[str, str], tuple[str, int]] = (
                        getattr(engine, _BUILD_VALIDATE_SEEN_FLAG, None) or {}
                    )
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
                        # input tokens; 17 bounces in 90s were observed).
                        # Steer, don't stop — same reasoning as the state-query
                        # guard: the idle ladder ends turns, and it does so after
                        # three escalating nudges that name what to do instead.
                        if strikes >= _VALIDATE_SUPPRESS_ABORT:
                            logger.info(
                                "build_and_validate%s bounced %d times against "
                                "unchanged state — steering, not stopping",
                                bv_sig,
                                strikes,
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
                            opener + f"Conformance: base={'pass' if v.base_passed else 'fail'}, "
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
                        return _track_progress(engine, tool_name, progress_before, corrective)
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
                ran_concurrently = False
                try:
                    mutation_fingerprint = engine.state.validation_fingerprint()
                except Exception:  # noqa: BLE001 — best-effort bookkeeping
                    logger.debug("no-op guard: fingerprint failed", exc_info=True)
                # `progress_before` is captured at the top of this function (the
                # guards above need it too); nothing between there and here
                # mutates the crate, so it still describes the pre-call state.
                if tool_name in _MUTATION_TOOLS:
                    with _MUTATION_HISTORY_LOCK:
                        in_flight = int(getattr(engine, _MUTATIONS_IN_FLIGHT, 0)) + 1
                        setattr(engine, _MUTATIONS_IN_FLIGHT, in_flight)
                        ran_concurrently = in_flight > 1

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
                    if tool_name in _LOOKUP_TOOLS:
                        # Only a call that actually reached the registry is worth
                        # remembering; the except-branch above is a tool-body bug,
                        # not an answer.
                        _remember_lookup(engine, signature, result)
                finally:
                    if tool_name in _MUTATION_TOOLS:
                        with _MUTATION_HISTORY_LOCK:
                            still_running = int(getattr(engine, _MUTATIONS_IN_FLIGHT, 1))
                            setattr(engine, _MUTATIONS_IN_FLIGHT, max(0, still_running - 1))
                        # A sibling that STARTED while this call was running makes
                        # this one concurrent too — the fingerprint it is about to
                        # compare already contains that sibling's write.
                        ran_concurrently = ran_concurrently or still_running > 1
                if True:
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
                        setattr(engine, _EXPORT_PATH_FLAG, result.get("crate_path"))
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
                # MUTATION tools only. The fingerprint is now captured for every
                # call (the general no-progress run below needs it), so this must
                # say so explicitly — without the check, a read that changes
                # nothing by definition was being reported as a write that wrote
                # nothing, and three reads ended the turn.
                if (
                    tool_name in _MUTATION_TOOLS
                    and mutation_fingerprint is not None
                    and not (isinstance(result, dict) and result.get("error"))
                ):
                    try:
                        unchanged = engine.state.validation_fingerprint() == mutation_fingerprint
                    except Exception:  # noqa: BLE001 — best-effort bookkeeping
                        unchanged = False
                    if unchanged:
                        logger.info("No-op mutation suppressed: %s(%s)", tool_name, signature)
                        # Escalate like every other guard. Normalising reference
                        # values turned the A-B-A thrash into repeated NO-OPS,
                        # which the loop-breaker misses because it only counts
                        # IDENTICAL signatures — and the thrash's whole nature is
                        # that the arguments keep changing. Without a strike count
                        # here a model can bounce off "changed nothing" forever at
                        # a full model turn each time.
                        target = _mutation_target(tool_name, kwargs)
                        with _MUTATION_HISTORY_LOCK:
                            noops: dict[str, int] = dict(
                                getattr(engine, _NO_OP_STRIKE_FLAG, None) or {}
                            )
                            strikes = noops.get(target, 0) + 1
                            noops[target] = strikes
                            setattr(engine, _NO_OP_STRIKE_FLAG, noops)
                        if not ran_concurrently and strikes >= _MUTATION_CYCLE_ABORT:
                            logger.warning(
                                "Ending turn: %s wrote nothing %d times running",
                                target,
                                strikes,
                            )
                            _log_suppressed(engine, tool_name, f"no_op_strike_{strikes}", kwargs)
                            raise _InvocationCancelled("repeated no-op mutation")
                        result = _no_op_mutation_message(tool_name, kwargs, engine)
                    else:
                        with _MUTATION_HISTORY_LOCK:
                            noops = dict(getattr(engine, _NO_OP_STRIKE_FLAG, None) or {})
                            noops.pop(_mutation_target(tool_name, kwargs), None)
                            setattr(engine, _NO_OP_STRIKE_FLAG, noops)
                        cycle = _record_mutation_cycle(
                            engine, tool_name, kwargs, concurrent=ran_concurrently
                        )
                        if cycle is not None:
                            result = cycle

                # The general no-progress rule, applied to EVERY tool: if the
                # crate looks exactly as it did before the call, nothing moved.
                result = _track_progress(
                    engine, tool_name, progress_before, result, signature=signature
                )

                result = _flag_remove_then_redraft(engine, tool_name, result)

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


# What each process type needs before the tox profile will accept it. Each entry
# is a tuple of accepted spellings for one parameter — the build reads several
# aliases, and an item is satisfied by any of them.
_PROCESS_PARAMETERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "Exposure": (("duration",), ("cell_seeding_density",), ("microplate",)),
    "EndpointReadout": (
        ("detection_instrument",),
        ("instrument_manufacturer",),
        ("measured_entity",),
        ("endpoint",),
        ("technical_replicate",),
    ),
    "DataAnalysis": (
        ("computational_tool", "software"),
        ("data_calculation_and_statistics", "data_processing"),
    ),
    "CellCulture": (("culture_medium",),),
}


def open_items(state: CrateState, *, actionable_only: bool = False) -> list[str]:
    """The outstanding work, DERIVED from the crate rather than remembered.

    With ``actionable_only`` the list holds just the items someone can actually
    work off — used to decide whether the crate is finished. The informational
    lines (a count of findings on nodes the build generates) stay out of that
    view, because an item that can never be cleared would keep any "is there
    work left?" question answering yes forever.

    A checklist the agent announces in prose is a checklist it can quietly drop:
    the transcript gets trimmed, a checkpoint rotates, the user answers two of
    three groups, and the third is never mentioned again. Nothing about that is
    recoverable from the conversation.

    So the list is computed from state on every turn instead. An item exists
    because a field is empty and disappears the moment it is filled — it cannot
    drift out of sync with the crate, it survives any amount of context loss,
    and "did this get done" is answerable by looking rather than by trusting.

    Returns short, specific lines ("proc_exposure: missing duration, microplate")
    ordered blocking-first. Empty when there is nothing outstanding.
    """
    from builder.tools.composites import _is_consumed_by_process

    items: list[str] = []
    try:
        for proc in state.list_entities("LabProcess"):
            ptype = str(proc.fields.get("process_type") or proc.fields.get("additionalType") or "")
            expected = _PROCESS_PARAMETERS.get(ptype)
            if not expected:
                continue
            missing = [
                names[0]
                for names in expected
                if not any(str(proc.fields.get(n) or "").strip() for n in names)
            ]
            if missing:
                items.append(f"{proc.entity_id} ({ptype}): missing {', '.join(missing)}")

        meta = state.metadata
        attribution = [
            label
            for label, value in (
                ("publisher", getattr(meta, "publisher", None)),
                ("creator", getattr(meta, "creator", None)),
                ("contact", getattr(meta, "contact", None)),
            )
            if not value
        ]
        if attribution:
            items.append(f"crate attribution: {', '.join(attribution)} not set")
        if not getattr(meta, "license", None):
            items.append("licence not chosen (the crate will say the terms were never stated)")

        # A submission that ships four assay folders and gets modelled as one
        # assay validates perfectly and is three-quarters missing. Nothing in the
        # crate can reveal that — every check reads what IS there — so it is
        # measured against the INPUT: each directory holding its own metadata
        # workbook or protocol is an assay the depositor separated out. Compared
        # by count, not matched by name: which Assay covers which folder is not
        # derivable, and claiming otherwise would be worse than saying nothing.
        assay_dirs = {
            str(doc.get("relative_path", "")).split("/")[0]
            for doc in (getattr(state, "documents", None) or [])
            if "/" in str(doc.get("relative_path", ""))
            and str(doc.get("classification", "")).lower() in ("metadata", "protocol")
        }
        assays = state.list_entities("Assay")
        studies = state.list_entities("Study")
        if len(assay_dirs) > max(len(assays), 1):
            items.append(
                f"the input has {len(assay_dirs)} assay folders "
                f"({', '.join(sorted(assay_dirs)[:4])}) but the crate has "
                f"{len(assays)} Assay entit{'y' if len(assays) == 1 else 'ies'} — "
                "draft the missing ASSAYS under the existing Study (a folder per "
                "assay does not mean a Study per assay), or tell the user which "
                "folders are out of scope"
            )
        # One Study per Assay is the shape you get from reading a folder listing
        # rather than the science. In ISA a Study is the investigation's unit of
        # design and material — the assays run on it are its children — so four
        # sibling assay folders under one submission are four Assays of ONE
        # Study. Advisory, not a rule: a submission genuinely containing several
        # studies looks identical from here, and only the depositor knows which.
        if len(studies) > 1 and len(assays) == len(studies):
            singles = [
                st.entity_id
                for st in studies
                if sum(1 for a in assays if a.fields.get("study_id") == st.entity_id) == 1
            ]
            if len(singles) == len(studies):
                items.append(
                    f"{len(studies)} Studies, each holding exactly one Assay "
                    f"({', '.join(singles[:3])}{'…' if len(singles) > 3 else ''}) — if "
                    "these are assays OF one study, keep one Study, re-point each "
                    "Assay's study_id at it and remove the spares; if they really are "
                    "separate studies, say so and leave them"
                )

        # Processes with no Assay, and Assays with no processes. `remove_entity(
        # cascade=True)` on a container does not delete its contents — it clears
        # the parent reference to keep the graph free of dangling ids — so
        # removing four Assays and re-creating them left thirteen processes
        # pointing at nothing and four empty Assays, and the crate still reported
        # zero REQUIRED issues. Every check reads what IS attached; nothing was
        # looking at what came loose.
        assay_ids = {a.entity_id for a in assays}
        detached = [
            p.entity_id
            for p in state.list_entities("LabProcess")
            if str(p.fields.get("assay_id") or "") not in assay_ids
        ]
        if detached:
            items.append(
                f"{len(detached)} LabProcess entities belong to no Assay "
                f"({', '.join(detached[:3])}{'…' if len(detached) > 3 else ''}) — set "
                "assay_id on each to the Assay it was run for; they are invisible "
                "to the crate until then"
            )
        empty = [
            a.entity_id
            for a in assays
            if not any(
                str(p.fields.get("assay_id") or "") == a.entity_id
                for p in state.list_entities("LabProcess")
            )
        ]
        if empty and state.list_entities("LabProcess"):
            items.append(
                f"{len(empty)} Assay entities have no process chain "
                f"({', '.join(empty[:3])}{'…' if len(empty) > 3 else ''}) — an assay "
                "with no CellCulture/Exposure/EndpointReadout/DataAnalysis records "
                "no experiment"
            )

        # A deposited article, with no publication recorded, is a whole layer of
        # the crate missing: the article, its authors, and the compounds it
        # lists. Counting entities cannot see it — a crate with a backbone and
        # one Person looks populated — so it is read off the discovered
        # documents, which is where the fact actually lives.
        #
        # Asked of the filename rather than of the classification: a publication
        # classifies as `metadata`, because it describes the study rather than
        # measuring it, and that class alone cannot single one out (#591). Not of
        # the CONTENT either — the deposit record names a `DOI` field and a README
        # template shows a worked citation, and neither is an article.
        publications = state.list_entities("Publication")
        if not publications and any(
            looks_like_publication(str(d.get("filename") or d.get("relative_path") or ""))
            for d in (getattr(state, "documents", None) or [])
        ):
            items.append(
                "a publication document was found but no Publication entity exists "
                "— resolve_publication / draft_publication_with_authors records the "
                "article, its authors and the compounds it reports"
            )
        for pub in publications:
            if not pub.fields.get("author"):
                items.append(
                    f"{pub.entity_id}: no authors recorded — "
                    "draft_publication_with_authors, or link the Person entities"
                )

        # A duplicate person is a question for the human, not a guess for us.
        # An abbreviated and a spelled-out form of one name are almost certainly
        # one researcher — but only almost, and merging silently would be worse
        # than the duplicate. The pair goes to the user with the evidence.
        try:
            from builder.tools.drafters import probable_duplicate_people

            for first, second, why in probable_duplicate_people(state):
                items.append(
                    f"{first.entity_id} and {second.entity_id} may be the same person "
                    f"({why}) — ASK the user to confirm before merging; if they are one "
                    f"person, keep the ORCID-backed entity and remove_entity the other, "
                    f"repointing anything that referenced it"
                )
        except Exception:  # noqa: BLE001 — a checklist entry must never break the loop
            logger.debug("duplicate-person check failed", exc_info=True)

        orphans = _unreferenced_entities(state)
        for etype, ids in sorted(orphans.items()):
            items.append(
                f"{len(ids)} {etype} entit{'y' if len(ids) == 1 else 'ies'} "
                f"nothing references ({', '.join(ids[:3])}{'…' if len(ids) > 3 else ''})"
            )
        # Being MENTIONED is not being USED. A cell line the Study lists and a
        # placeholder Sample derives from still describes an experiment that
        # never cultured it, and the reference check above cannot see that. The
        # material a process consumed belongs in that process's inputs.
        for etype, process_type, field in (
            ("CellLineSample", "CellCulture", "cell_line"),
            ("MolecularEntity", "Exposure", "chemicals"),
        ):
            unused = [
                e.entity_id
                for e in state.list_entities(etype)
                if not _is_consumed_by_process(state, e.entity_id)
                and e.entity_id not in orphans.get(etype, [])
            ]
            if unused:
                items.append(
                    f"{len(unused)} {etype} mentioned but not consumed by any process "
                    f"({', '.join(unused[:3])}{'…' if len(unused) > 3 else ''}) — set "
                    f"{field} on the {process_type} process to the entity itself"
                )

        # RECOMMENDED findings, once the user has opted into them, are work — not
        # a footnote to read out at export. Reported as a bare count they were
        # announced once and then dropped ("168 findings… these do not block
        # export"); grouped and kept on the list, they are a queue with the
        # biggest class first. Aggregated because 168 individual lines would bury
        # everything else on this checklist.
        should = list(getattr(state.validation, "should_issues", None) or [])
        if should and "recommended" in (state.validation.assessed_tiers or set()):
            # Split by who can actually act. Roughly half of these are about
            # nodes the BUILDER emits — external ontology IRIs referenced without
            # a describing node — which no amount of set_fields will reach.
            # Telling the model to "fix the 168" sends it after work it cannot
            # do; naming the fixable subset is the difference between a queue and
            # a wall. (`fix_required_issues` clears none of them: measured.)
            owned = [line for line in should if _issue_targets_own_entity(state, line)]
            groups = Counter(_issue_class(line) for line in owned)
            top = ", ".join(f"{n}x {label}" for label, n in groups.most_common(3))
            if owned:
                items.append(
                    f"{len(owned)} of {len(should)} RECOMMENDED findings are on entities "
                    f"you own — {top}. Fix these with set_fields, one entity at a time"
                )
            build_side = len(should) - len(owned)
            if build_side and not actionable_only:
                # Say WHICH nodes, not a guess at why. This line used to blame
                # "external ontology IRIs with no describing node" — true of six
                # findings out of two hundred and twenty-four. The rest are the
                # CSVW columns, packaged files and typed domain nodes the mapper
                # emits, and a model told the wrong cause cannot tell when the
                # claim stops being true.
                items.append(
                    f"{build_side} further RECOMMENDED findings are on nodes the BUILD "
                    "generates (CSVW table columns, packaged file entries, profile "
                    "types) — no tool call can edit those; report the count and move on"
                )
    except Exception:  # noqa: BLE001 — a checklist must never break a turn
        logger.debug("open items: derivation failed", exc_info=True)
    return items


def _issue_targets_own_entity(state: CrateState, line: str) -> bool:
    """True when a validation line names an entity the agent can actually edit.

    Display lines start ``[profile] <entity id>: message``. An id that resolves
    to something in ``CrateState`` is the agent's to fix; an absolute IRI (an
    ontology term, an ORCID, a PubChem compound) is a reference the build emits
    and no tool call can add a name to.
    """
    # Split on ": " (colon-space), not ":" — an IRI id contains colons, and
    # splitting on the bare character cut "https" off the front of every one.
    body = str(line).split("] ", 1)[-1]
    entity_id = body.split(": ", 1)[0].strip()
    if not entity_id:
        return False
    # Membership in state decides it, IRI or not. An entity keyed by its ORCID or
    # its AOP-Wiki IRI is still the agent's to edit — 44 of them in one crate —
    # and treating "looks like a URL" as "the build emitted it" told the model to
    # ignore work it could actually do.
    bare = entity_id.lstrip("./").lstrip("#")
    for entity in state.list_entities():
        if entity_id == entity.entity_id or bare == entity.entity_id:
            return True
        if bare.endswith(f"_{entity.entity_id}"):
            return True
    # Compare against the id the MAPPER would mint, not just the state id. A File
    # is keyed in the graph by its destination path (`data/uptake.csv`) while
    # state calls it `file_uptake`, so sixteen findings on the agent's own files
    # were reported to it as "emitted by the build, not fixable from here" — the
    # one category it must never get wrong, because the model is told to stop
    # trying. Asking the mapper is exact and stays right if the id scheme moves.
    try:
        from builder.tools._crate_mapping import _file_dest, _mint_id

        for entity in state.list_entities():
            if _mint_id(entity).lstrip("./").lstrip("#") == bare:
                return True
            if entity.type == "File" and _file_dest(entity).lstrip("./") == bare:
                return True
    except Exception:  # noqa: BLE001 — classification never breaks a turn
        logger.debug("issue ownership: mapper id comparison failed", exc_info=True)
    return False


def _issue_class(line: str) -> str:
    """Collapse one validation issue line to the class of problem it reports.

    The display lines carry entity ids and quoted values, so counting them raw
    gives 168 unique strings and no signal. Stripping those leaves the shape of
    the finding — "Entities SHOULD have a human-readable name" — which is what
    tells the model whether it is facing one job repeated or many jobs.
    """
    message = str(line).split(": ", 1)[-1]
    return re.sub(r"'[^']*'|`[^`]*`", "…", message).strip()[:60] or "unclassified"


def _format_open_items(engine: AgentEngine, *, limit: int = 10) -> str:
    """Render :func:`open_items` for the per-turn state brief."""
    items = open_items(engine.state)
    if not items:
        return ""
    shown = items[:limit]
    more = f"\n  …and {len(items) - len(shown)} more" if len(items) > len(shown) else ""
    return (
        "\n[Still open — derived from the crate, not from what was said. Each line "
        "disappears when the field is filled; fill what you can from the documents "
        "and ask the user only for what genuinely is not in them]\n"
        + "\n".join(f"  - {item}" for item in shown)
        + more
    )


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
    # No licence means the crate ships the "licence not stated" entity the BASE
    # shape requires it to carry — honest, but no use to anyone wanting to reuse
    # the data, since unknown terms are not permission. Worth one question.
    has_license = bool(meta.license)
    orphans = _unreferenced_entities(state)

    present: list[str] = []
    if has_backbone:
        present.append("backbone ✓")
    if has_person:
        present.append("person ✓")
    if has_attribution:
        present.append("crate owner ✓")
    if has_license:
        present.append("licence ✓")
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
    if not has_license:
        missing.append("licence (the crate will record that none was stated)")
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
    elif not has_license:
        next_action = (
            "ask the user which licence to record — present_to_human with the "
            "options and their trade-offs (CC0 / CC-BY-4.0 / CC-BY-NC-4.0 / keep "
            "all rights reserved), then set_crate_metadata(license=<URL>). Never "
            "choose one for them: it is a legal decision about their data"
        )
    elif not has_attribution:
        next_action = (
            "set_crate_metadata(publisher=…/creator=…/contact=…) — take the "
            "corresponding person/affiliation from the assay metadata and CONFIRM "
            "with the user; never invent it"
        )
    else:
        # The crate looks complete — close it out. Unless the user opted into the
        # RECOMMENDED tier and it found work: pointing at export while 168
        # findings sit unaddressed is how they came to be announced once and
        # never touched. Opting in was a request to improve the crate, not a
        # request for a longer report.
        should = list(getattr(state.validation, "should_issues", None) or [])
        if should and "recommended" in (getattr(state.validation, "assessed_tiers", None) or set()):
            next_action = (
                f"work the {len(should)} RECOMMENDED findings the user asked for — "
                "take the ones naming YOUR entities (see the still-open list) and "
                "set the missing name/description with set_fields, entity by "
                "entity; export once they are done or the user says to stop"
            )
        else:
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
    brief = f"[Session: {session_id} | Files: {file_count} | Entities: {entity_count} | "
    if document_count:
        brief += f"Documents: {document_count} | "
    brief += f"Iteration: {iteration_count}]"
    if next_fix:
        brief += f"\n[Next REQUIRED fix: {next_fix}]"
    if nudge:
        brief += f"\n{nudge}"
    if state_summary:
        # NOT truncated to 1200 chars any more. That blanket cut sat downstream
        # of everything the summary carries — live counts, the outstanding-items
        # checklist, the user's own answers, and the loaded document text — and
        # the document text is last, so it was the part that never survived. The
        # publication record listing 22 test compounds was held in full in
        # session state, named in the brief, and clipped out of it every single
        # turn; the model drafted one compound because that is all it could see.
        # Each section is bounded at its own source (see `_format_open_items`,
        # `_format_user_answers`, `_EVIDENCE_BRIEF_BUDGET`), which is where the
        # decision about what is worth its space actually belongs.
        brief += f"\n{state_summary}"
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


def _format_user_answers(engine: AgentEngine, *, limit: int = 8) -> str:
    """The questions the user has already answered, for the state brief.

    A HITL answer is a tool result, so it lives only in the graph checkpoint —
    and a turn cut short by a guard or a timeout rotates that thread away. The
    answer vanishes while the crate state survives, and the agent asks again.
    Replaying the recent answers from state means a rotated thread still knows
    what it was told, whatever happened to the transcript.
    """
    answers = getattr(engine.state, "user_answers", None) or []
    if not answers:
        return ""
    lines = [
        f"- asked: {item.get('question', '')[:160]}\n  answered: {item.get('answer', '')[:160]}"
        for item in answers[-limit:]
    ]
    return "\n[Already answered by the user — do NOT ask these again; act on them]\n" + "\n".join(
        lines
    )


def _will_self_continue(engine: AgentEngine, self_continues: int) -> bool:
    """Whether a guard-stopped turn is about to resume without asking the user.

    The panel and the resume decision have to agree, and they used to be made in
    two places — the panel inside the turn, the decision after it — so a turn
    that was about to pick itself back up still printed "Paused … Your call",
    immediately followed by "Picking that back up myself". The box asked a
    question that had already been answered. One predicate, both call sites.
    """
    return self_continues < _MAX_SELF_CONTINUES and bool(open_items(engine.state))


def _handback_panel(engine: AgentEngine, *, headline: str, handing_back: bool = True) -> Any:
    """Render "here is where we got to, here is what you can do" for a stopped turn.

    A turn that ends on a guard used to print one dim sentence and return the
    prompt. That tells the user the run stopped without telling them the two
    things they need: what the crate looks like now, and what to type next.
    Nobody should have to guess whether their work survived, or invent the
    vocabulary to resume it.

    The framing matters as much as the content. A pause reads as a failure
    unless it says otherwise, and this one is not: the crate is unfinished by
    design at this point, the work so far is on disk, and the person now has the
    thing they rarely get — a look at the half-built crate while it can still be
    steered. So the panel is titled as a checkpoint, states plainly that the
    crate is NOT finished, and offers direction rather than a status report.

    Every suggestion is a phrase the loop already understands, and the list
    adapts to the crate — no "export" prompt before anything is drafted, no
    "fix the issues" prompt when there are none.
    """
    from rich.panel import Panel

    state = engine.state
    lines: list[str] = [f"[dim]{headline}[/dim]"]
    outstanding: list[str] = []

    stopped_doing = getattr(engine, _STOP_REASON_FLAG, None)
    if stopped_doing:
        lines.append(f"[dim]Stopped after: {stopped_doing}[/dim]")
        setattr(engine, _STOP_REASON_FLAG, None)
    last_tool = getattr(engine, _LAST_TOOL_FLAG, None)
    if last_tool and not stopped_doing:
        lines.append(f"[dim]Last step running: {last_tool}[/dim]")

    try:
        entities = state.list_entities()
        counts: dict[str, int] = {}
        for entity in entities:
            counts[entity.type] = counts.get(entity.type, 0) + 1
        # Biggest groups first: "KeyEvent 16, MolecularEntity 22" says more about
        # where the work got to than the alphabetical head of the list does.
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        top = ", ".join(f"{k} {n}" for k, n in ranked) or "nothing drafted yet"
        v = state.validation
        marks = " ".join(
            f"{'✓' if ok else '✗'} {name}"
            for name, ok in (("base", v.base_passed), ("ISA", v.isa_passed), ("Tox", v.tox_passed))
        )
        required = len(v.required_issues or [])
        # An unvalidated crate has no known blockers, which is NOT the same as
        # having none — reporting "no blockers" for a crate nobody has checked
        # is the exact false all-clear the maturity report was fixed not to give.
        validated = bool(
            v.assessed_tiers or v.input_fingerprint or v.base_passed or v.required_issues
        )
        if required:
            blocking = f"[red]{required} REQUIRED[/red]"
        elif validated:
            blocking = "[green]no blockers[/green]"
        else:
            blocking = "[dim]not validated yet[/dim]"
        lines.append(f"\n[bold]Where we got to[/bold]\n  {len(entities)} entities — {top}")
        lines.append(f"  Validation: {marks} · {blocking}")
        outstanding = open_items(state)
        if outstanding:
            shown = outstanding[:5]
            lines.append("\n[bold]Still open[/bold]")
            # One line each. These items carry a "— do X" instruction aimed at the
            # model, which is what makes them long; wrapped in a panel the
            # continuation lines run back to the left edge and the list stops
            # being scannable. The person needs the WHAT here — the how is the
            # agent's copy, which is untruncated.
            for item in shown:
                head = item.split(" — ")[0]
                if len(head) > 96:
                    head = head[:95].rstrip() + "…"
                lines.append(f"[dim]  - {head}[/dim]")
            if len(outstanding) > len(shown):
                lines.append(f"[dim]  …and {len(outstanding) - len(shown)} more[/dim]")
    except Exception:  # noqa: BLE001 — a hand-back must never raise
        logger.debug("hand-back: state summary failed", exc_info=True)
        entities, required = [], 0

    if not handing_back:
        # A turn that is about to resume itself is not paused and is not asking
        # anything, so it gets no question and no yellow. Same body — where we
        # got to, what is still open — because that IS worth reading; only the
        # framing changes, from "your call" to "carrying on".
        return Panel(
            "\n".join(lines),
            title="[bold]Still working — picking this back up myself[/bold]",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    options: list[tuple[str, str]] = [
        (
            "continue",
            "carry on with the outstanding work" if outstanding else "pick up from here",
        )
    ]
    if required:
        options.append(
            ("show issues", f"list the {required} blocking issue(s) and fix them one by one")
        )
    if entities:
        options.append(("export", "write the crate as it stands, unfinished"))
    options.append(("status", "full summary of the crate so far"))
    lines.append("\n[bold]Your call[/bold] — reply with one of:")
    lines.extend(f"  [bold]{word}[/bold][dim] — {why}[/dim]" for word, why in options)
    lines.append(
        "[dim]  …or redirect me: name what to work on, what to skip, or what I got wrong.[/dim]"
    )

    return Panel(
        "\n".join(lines),
        # Titled, because an untitled yellow box after a long wait reads as a
        # crash. This is a checkpoint in unfinished work, and the crate being
        # incomplete is the normal state at this point, not the bad news.
        title="[bold]Paused — the crate is not finished[/bold]",
        title_align="left",
        border_style="yellow",
        padding=(0, 1),
    )


# How much document text the per-turn brief may carry. Sized so ANY ONE of a
# typical submission's documents fits whole (the largest here is a 22.8k-char
# SOP): at 12,000 the 15.3k publication record — the file that lists the tested
# compounds — could never be shown, so once it aged out of the message history
# the model could not see the 22 compounds and drafted one. The brief is
# re-sent every turn, so this is real per-turn cost (~6k tokens worst case
# against ~3k before); it buys the difference between a 15-entity crate and an
# 80-entity one, which is the whole job.
_EVIDENCE_BRIEF_BUDGET = 24000


def _format_document_evidence(engine: AgentEngine, *, limit: int = _EVIDENCE_BRIEF_BUDGET) -> str:
    """Format bounded loaded document evidence for the trailing state brief.

    The brief prints as much document text as the budget allows and then NAMES
    the rest. It used to announce "[Additional loaded evidence omitted for
    context budget]", which to a reader means *missing* — and the rational
    response to missing source material is to go and read it. That single line
    invited the re-read loop the no-progress guard then had to stop: the model
    was being told, every turn, that documents it had already read were not
    available to it. Listing them by name says the opposite, which is also the
    truth: they are loaded, in full, and nothing was lost.
    """
    evidence = getattr(engine.state, "document_evidence", {})
    if not evidence:
        return ""
    parts: list[str] = ["[Loaded document evidence]"]
    used = len(parts[0])
    shown = 0
    # MOST RECENTLY USED FIRST. The store keeps LRU order — newest at the end —
    # so reading it front-to-back printed the document the model had least
    # recently asked for and merely NAMED the one it just requested. That closes
    # a loop: ask for the SOP, get served, the serve marks it most-recent, the
    # brief keeps showing the workbook instead, ask for the SOP again. The
    # document the model is working with is the one worth spending the budget on.
    printed: list[str] = []
    for path, item in reversed(list(evidence.items())):
        content = str(item.get("content", ""))
        line = f"\n{path} ({item.get('tool', 'reader')}):\n{content}"
        if used + len(line) > limit:
            # SKIP, don't stop. One document larger than the whole budget used to
            # end the loop on the first item and print nothing at all — the brief
            # went from carrying the workbook to carrying no text whatever, which
            # is the worst of both: the context is spent on nothing and the model
            # is told only that documents exist somewhere.
            continue
        parts.append(line)
        used += len(line)
        printed.append(path)
        shown += 1
    held = [path for path in reversed(list(evidence)) if path not in printed]
    if held:
        parts.append(
            "\n[Also read this session and held IN FULL, not reprinted here to save "
            f"context: {', '.join(held)}. Nothing was lost — re-reading one returns "
            "the same text you already have.]"
        )
    return "".join(parts)


# Display labels for the discovery roles whose casing is not sentence case.
_DOCUMENT_CLASS_LABELS: dict[str, str] = {
    "metadata": "Metadata",
    "protocol": "Protocol",
    "raw_data_file": "Raw data",
    "processed_data_file": "Processed data",
}


def _format_document_context(documents: list[dict[str, Any]] | None) -> str:
    """Format the ranked document discovery results as a bounded context string.

    Produces a numbered block, one MARKDOWN list item per candidate::

        1. **[Publication]** `S-VHPS26.json` — score 0.53
        2. **[Metadata]** `Assay-metadata-CHO-K1_OATP1C1-v1.1.xlsx` — score 0.33

    This IS the presentation format, not just an internal one, and the header
    says so. The model was re-rendering the same five documents differently on
    every run — sometimes a table, sometimes prose, sometimes bracketed roles —
    because nothing told it what the house style was.

    Markdown, not plain text, and that distinction is the whole point: replies
    are rendered as markdown, where single newlines are NOT line breaks. Handing
    over unformatted lines and asking for them verbatim collapsed the whole
    ranking into one run-on paragraph with no styling. As list items the
    structure survives reproduction, and the backticked filenames come back
    coloured — which is what makes the list scannable at a glance.

    Each candidate's ``reasons`` stay out of the block deliberately: the score
    already summarises them, and anything in here gets shown to the user.
    """
    if not documents:
        return ""
    lines: list[str] = []
    # Every ranked candidate: the cap is `discover_documents`' to set, and a
    # second one here silently halved it (#675). A file the agent is not told
    # exists is a file it cannot read.
    for number, doc in enumerate(documents, 1):
        raw = str(doc.get("classification", "document")).strip() or "document"
        label = _DOCUMENT_CLASS_LABELS.get(raw.lower())
        if label is None:
            spaced = raw.replace("_", " ")
            label = spaced[:1].upper() + spaced[1:]
        # The RELATIVE PATH, not the bare filename. A nested submission has five
        # different README.txt files, and a list of five identical names is both
        # unreadable and unusable: the reader gate refuses a basename that
        # matches more than one file ("Not resolving bare filename 'README.txt'
        # — 3 files share that name"), so the only spelling shown was the one
        # spelling that could never be opened. For a file at the root the
        # relative path IS the filename, so nothing is lost by always using it.
        name = doc.get("relative_path") or doc.get("filename") or "?"
        score = doc.get("score", 0.0)
        lines.append(f"{number}. **[{label}]** `{name}` — score {score:.2f}")
    return "\n".join(lines)


def _call_ids(message: Any) -> list[str]:
    """Every tool-call id on *message*, whatever shape the call objects take."""
    ids: list[str] = []
    for call in getattr(message, "tool_calls", None) or []:
        cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        if cid:
            ids.append(str(cid))
    return ids


def _drop_unanswered_tool_calls(messages: list) -> list:
    """Remove tool calls that never got a result, and the half-answers around them.

    ``start_on="human"`` stops the trimmed window BEGINNING with an orphan, which
    is the failure mode trimming can create. It cannot help with the other one: a
    turn interrupted between the model's ``tool_calls`` and the tool node's
    replies ends the history with a call nobody answered. The provider rejects
    the entire request for it — ``No tool output found for function call …`` — so
    a single Ctrl+C otherwise poisons every later turn of the session, and the
    saved history carries the poison into the next run too.

    Dropping the call is right rather than synthesising a result: the tool never
    ran, so there is no outcome, and inventing one would tell the model something
    false about the crate. When only SOME of a message's calls were answered, the
    surviving ``ToolMessage``s go with it — a result whose call is gone is just
    the same violation from the other side.
    """
    answered = {str(tid) for m in messages if (tid := getattr(m, "tool_call_id", None)) is not None}
    doomed: set[str] = set()
    for message in messages:
        ids = _call_ids(message)
        if ids and not all(cid in answered for cid in ids):
            doomed.update(ids)
    if not doomed:
        return messages

    kept = [
        m
        for m in messages
        if not (set(_call_ids(m)) & doomed)
        and str(getattr(m, "tool_call_id", "") or "") not in doomed
    ]
    logger.warning(
        "Dropped %d message(s) for %d tool call(s) that never got a result — "
        "usually an interrupted turn",
        len(messages) - len(kept),
        len(doomed),
    )
    return kept


def _trim_history(messages: list, *, max_tokens: int) -> list:
    """Bound the per-turn message history so verbose tool outputs aren't replayed.

    Two layers, in order (Issue #61):

    1. **Prune consumed verbose tool outputs** — scan/read listings already live
       in ``CrateState``, so their bodies are replaced by a short stub
       (``_prune_state_backed_outputs``). The messages themselves are kept so
       AI(tool_call) → ToolMessage pairing is preserved.
    2. **Token-budget trim** — keep the most recent messages within
       ``max_tokens`` using ``langchain_core.messages.trim_messages`` with
       ``strategy="last"``. The window may begin on a HUMAN **or an AI**
       message; either way it never *begins* with a dangling ``ToolMessage``,
       so it never produces the orphans the provider API rejects. Orphans that
       arrive already in the history — an interrupted turn leaves a tool_call
       nobody answered — are removed by ``_drop_unanswered_tool_calls`` before
       either step, which is what makes an AI-anchored window safe.

       ``start_on="human"`` ALONE is what this used to say, and it is why 36%
       of a profiled session's model turns ran with no history whatsoever. A
       ``HumanMessage`` enters the graph only at invocation start; every tool
       result and every guard corrective is a ToolMessage. So once the AI/Tool
       tail outgrew the budget, the retained window could no longer reach back
       to that single anchor, and ``trim_messages`` returned **the empty list**
       rather than a short window. Not a taper — a cliff: 31 messages in, 31
       kept; 37 in, 0 kept. From there every turn sent the system prompt and
       the state brief and nothing else, so the model re-issued the same reads
       and rebuilt entity ids by guessing at a naming convention, which is what
       produced 77 suppressions and 21 "Entity not found: person_<slug>"
       failures in one session.

       The tests missed it because each of them injects a HumanMessage per
       TURN; the real graph injects one per INVOCATION.

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

    pruned = _drop_unanswered_tool_calls(_prune_state_backed_outputs(messages))

    try:
        trimmed = trim_messages(
            pruned,
            max_tokens=max_tokens,
            token_counter="approximate",
            strategy="last",
            start_on=("human", "ai"),
            include_system=False,
            allow_partial=False,
        )
    except (ValueError, KeyError) as exc:
        # Defensive: never let a trimming edge case abort the turn. Falling back
        # to the pruned (but untrimmed) history keeps the agent running; the
        # pruning alone already removes the heaviest verbose payloads.
        logger.warning("History trim failed (%s); using pruned history", exc)
        return pruned

    # A trim that keeps NOTHING out of a non-empty history is not a trim, it is
    # amnesia — and it arrives silently, as a model turn that simply performs
    # worse. Whatever anchor rule a future edit lands on, this is the invariant
    # that must survive it: the untrimmed history is always a better answer than
    # no history, and the budget is a target, not a reason to send nothing.
    if not trimmed:
        logger.warning(
            "History trim kept nothing of %d messages; using pruned history", len(pruned)
        )
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
                content=(
                    "[Ranked input documents — when you show these to the user, "
                    "reproduce the markdown list below VERBATIM (it is already "
                    "formatted: keep the numbering, the **[Role]** and the "
                    "`backticks`, and keep each item on its OWN line), under the "
                    "heading 'Ranked input documents:' followed by a blank line. "
                    "Do not reformat into a table, reflow into a paragraph, "
                    "reorder, or restyle: the user sees this list most sessions "
                    "and it should look the same every time.]\n"
                    f"{document_context}"
                )
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
        "assess_air_readiness",
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
                + _format_open_items(engine)
                + _format_user_answers(engine)
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
        _call_started = perf_counter()
        response = model.invoke(model_messages)
        _call_seconds = perf_counter() - _call_started
        # Accumulate this call's token usage onto the crate's generator record so
        # the exported crate carries what the run cost. Done HERE rather than in
        # the profiler wrapper because that wrapper is skipped entirely when
        # profiling is off — cost accounting must not depend on instrumentation.
        try:
            call_in, call_out = _extract_token_usage(response)
            engine.state.record_llm_usage(
                {"input_tokens": call_in or 0, "output_tokens": call_out or 0},
                # Time spent waiting on the model, which is the run's real
                # machine cost — the wall clock beside it also counts the user
                # thinking, and is a different (much larger) number.
                seconds=_call_seconds,
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
    interactive: bool = True,
) -> dict[str, Any]:
    """Run the ReAct agent loop over *engine* until the session ends.

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
        verbose: Show bounded, sanitized diagnostics when a ReAct model turn
            raises. The default preserves the generic error message.
        interactive: Whether a person is at the keyboard. It is the CALLER's
            fact, never inferred: ``AgentEngine`` defaults to a
            ``SimulatedHumanInterface``, so a batch engine and a test engine are
            indistinguishable from the loop's side. ``False`` skips the banner,
            the greeting model call and the stdin read (#609).

    Returns:
        ``{"stop_reason": ..., "error": ...}``. ``stop_reason`` is
        ``"completed"`` when the session ended on its
        own terms, ``"cap_hit"`` when a turn exhausted the graph's recursion
        budget (valid-at-the-cutoff, never a clean win — #331), or ``"error"``
        when the last turn timed out or raised. ``error`` carries that last
        failure's reason chain (``None`` otherwise) — the loop absorbs model
        failures so the session survives them, so without it an automated driver
        sees a bare ``"error"`` and cannot tell a dropped connection from a bug.
        The A/B harness reports both; the CLI ignores them.

    With ``interactive=False`` the banner, the greeting model call and the stdin
    read are all skipped: *initial_prompt* and its autonomous continuation are the
    whole session, which then ends as Ctrl+D does — backstop included. That is
    what lets the A/B eval measure this arm with the budget it actually ships
    with, instead of a single bare graph invocation (#609).
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
        checkpoint_thread_id = f"{engine.state.session_id}:recovered-{checkpoint_generation}"
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
    # Only for a session someone is watching. The greeting costs a model call and
    # produces one thing: a welcome message. Headless (the A/B eval, batch), that
    # is a token bill for politeness nobody reads, charged to this arm alone —
    # so a run with no human present goes straight to work (#609).
    #
    # `resumed` is the caller's fact (--resume), never a guess from how populated
    # the state looks: initialize(--input) scans files before the loop starts, so
    # the old `entity_count > 0 or file_count > 0` inference called every fresh
    # run a resume and told the model to summarise work that did not exist — the
    # model duly recapped instead of building (#410).
    if interactive:
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
            for doc in documents:
                label = doc.get("classification", "document")
                name = doc.get("filename", doc.get("relative_path", "?"))
                score = doc.get("score", 0.0)
                document_lines.append(f"- [{label}] {name} (score: {score:.2f})")
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
                    f"  • [cyan]what is still missing?[/cyan] — "
                    f"{blocking} block conformance"
                )
                lines.append("  • [cyan]fix the required issues[/cyan] — I'll work through them")
            elif not (val.base_passed and val.isa_passed and val.tox_passed):
                nxt = "base" if not val.base_passed else "ISA" if not val.isa_passed else "ISA-Tox"
                lines.append(f"  • [cyan]validate[/cyan] — {nxt} does not pass yet")
            else:
                lines.append("  • [cyan]export the crate[/cyan] — all three profiles pass")
            if not any(e.type == "File" for e in engine.state.list_entities()) and file_count:
                lines.append(
                    f"  • [cyan]attach the data files[/cyan] — "
                    f"{file_count} scanned, none placed yet"
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
            if outcome == "error" and greeting_diagnostic:
                diagnostic_record: dict[str, Any] = {
                    "event": "model_error",
                    "exception_type": greeting_diagnostic["exception_type"],
                    "message": greeting_diagnostic["message"],
                    "exception_chain": greeting_diagnostic["exception_chain"],
                    "traceback_tail": greeting_diagnostic["traceback_tail"],
                    "stage": "react_greeting",
                }
                # Recorded whether or not -v was passed: the diagnostic is already
                # redacted and length-capped, and it goes to the profile, not the
                # screen. Gating the WRITE on the flag meant the one artifact that
                # could explain a failure existed only for a run that had already
                # been told to expect one — so diagnosing a crash required
                # reproducing it. -v still controls what is printed.
                if engine.profiler is not None:
                    engine.profiler.log_event(**diagnostic_record)
            if verbose and outcome == "error" and greeting_diagnostic:
                console.print(
                    "[yellow]ReAct greeting error[/yellow]: "
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
                    "Check your [bold]SSL_CERT_FILE[/bold] and "
                    "[bold]VITRO_API_BASE[/bold] settings.\n"
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

    # Seeded before `_run_turn` closes over it: the stopped-branch panel reads it
    # to decide whether it is handing back or carrying on.
    self_continues = 0

    # The most recent turn failure's reason chain. The loop absorbs model
    # failures so the session survives them, so this is the only way a caller can
    # tell a dropped connection from a bug (#609).
    last_error: list[str] = []

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
        except BaseException:
            # Ctrl+C is the one that matters here. Interrupting mid-turn kills the
            # tool node between an AIMessage's tool_calls and their ToolMessages,
            # so the checkpoint keeps a function call that was never answered.
            # Every later turn replays it and the provider refuses the request
            # ("No tool output found for function call …") — the exact failure
            # `_rotate_checkpoint` exists to prevent, which never ran for an
            # interrupt because KeyboardInterrupt is not an Exception and
            # propagated straight past the call below.
            outcome = "interrupt"
            raise
        finally:
            root_logger.setLevel(old_root_level)
            # In `finally` so EVERY exit rotates — including the interrupt above,
            # which leaves this function without passing the old call site.
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
            last_error.append(f"Model turn timed out after {request_timeout:.0f}s")
            # Not a failure: a built-in limit was reached and the turn is being
            # handed back. Saying "error" here sent people hunting for a bug
            # that did not exist.
            console.print(
                _handback_panel(
                    engine,
                    headline=(
                        f"One step ran past its {request_timeout:.0f}s limit, so I "
                        "stopped it there — that is a time guard, not a failure, and "
                        "everything up to it is saved. The crate is part-built: "
                        "below is what exists so far and what is still missing, "
                        "while it is all still easy to change."
                    ),
                )
            )
            console.print()
        elif outcome == "stopped":
            resuming = _will_self_continue(engine, self_continues)
            console.print(
                _handback_panel(
                    engine,
                    headline=(
                        "I was repeating the same step without getting anywhere, so I "
                        "stopped rather than keep spending on it."
                        + (" Carrying on from here." if resuming else " The session is saved.")
                    ),
                    handing_back=not resuming,
                )
            )
            console.print()
        elif outcome == "recursion":
            console.print(
                _handback_panel(
                    engine,
                    headline=(
                        f"This request hit its step limit ({max_iterations} tool "
                        "iterations), so I stopped. A smaller or more specific request "
                        "usually gets further."
                    ),
                )
            )
            console.print()
        elif outcome == "error":
            if diagnostic:
                # Keep the reason reachable by the CALLER. The loop absorbs every
                # model failure so the session survives it, which means an
                # automated driver (the A/B harness) otherwise sees a bare
                # "error" with no message — and its transient-failure retry,
                # which matches on the reason phrase, can never fire (#609).
                last_error.append(diagnostic["exception_chain"])
                # Always recorded — see the greeting path above. The profile is
                # where a failed run explains itself after the fact.
                diagnostic_record: dict[str, Any] = {
                    "event": "model_error",
                    "exception_type": diagnostic["exception_type"],
                    "message": diagnostic["message"],
                    "exception_chain": diagnostic["exception_chain"],
                    "traceback_tail": diagnostic["traceback_tail"],
                    "stage": "react_model_turn",
                }
                if engine.profiler is not None:
                    engine.profiler.log_event(**diagnostic_record)
                logger.error(
                    "Model turn failed: %s",
                    diagnostic["exception_chain"],
                )
            if verbose and diagnostic:
                console.print(
                    "[yellow]ReAct model error[/yellow]: "
                    f"{diagnostic['exception_type']}: {diagnostic['message']}"
                )
                if diagnostic["traceback_tail"]:
                    console.print(f"[dim]Traceback tail: {diagnostic['traceback_tail']}[/dim]")
                console.print("Your work so far is saved.")
            else:
                # Reserved for a GENUINE failure now that timeouts, loop guards
                # and the step limit each report themselves. The cause is named
                # here rather than withheld: telling someone to reproduce a
                # failure that cost them a long session, just to learn what it
                # was, spends their time to recover something already captured.
                cause = (
                    f" [dim]({diagnostic['exception_type']}: {diagnostic['message']})[/dim]"
                    if diagnostic
                    else ""
                )
                console.print(
                    "[yellow]Something actually went wrong on that step[/yellow] and I "
                    f"stopped.{cause} The session is saved; the full traceback is in the "
                    "session profile (`model_error`). Re-run with [bold]-v[/bold] to see "
                    "it on screen."
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
    # Every turn's outcome, so the session can report HOW it ended rather than
    # only that it did (#331 cap_hit vs a clean stop).
    turn_outcomes: list[str] = []

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
                elif answers_are_synthetic(engine.human_interface):
                    # Nobody is at the keyboard (--smoke-test). This ONE read is
                    # the reason the mode used to refuse this arm: every other
                    # prompt already goes through the HumanInterface, but the
                    # conversation was read straight off stdin, so a synthetic
                    # interface had nothing to answer and the run sat on an empty
                    # terminal. Ask the interface instead — and let a SKIP end the
                    # session exactly as Ctrl+D does (below), which is what keeps
                    # an unattended run from driving turns forever.
                    reply = engine.human_interface.request_input(
                        "What would you like to do next?",
                        CONVERSATION_FIELD_TYPE,
                    )
                    if reply.get("skipped") or not str(reply.get("value") or "").strip():
                        raise EOFError
                    user_input = str(reply["value"]).strip()
                    console.print(f"[bold cyan]❯[/bold cyan] {user_input}")
                elif not interactive:
                    # The caller told us nobody is at the keyboard (the A/B eval,
                    # batch). Reading stdin here would block a harness on a
                    # terminal it does not own, so the seeded turn and its
                    # autonomous continuation ARE the session: end it exactly as
                    # Ctrl+D does, which still runs the finish backstop so the
                    # crate lands (#609).
                    raise EOFError
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
                self_continues = 0
                # A new user turn gets a fresh strike budget. The counters live on
                # the engine, so once a guard had ended a turn they were already at
                # the limit — and the next "continue" was killed by its FIRST
                # offending call, before the agent could try anything. The memoised
                # fingerprints stay (a redundant call is still suppressed and still
                # answered with a corrective); only the escalation resets.
                _reset_turn_guards(engine)
                for _autonomous_turn in range(_MAX_AUTONOMOUS_TURNS + 1):
                    reply, outcome = _run_turn(message)
                    turn_outcomes.append(outcome)
                    # Land the turn's work in the footer immediately rather than
                    # waiting up to a tick — the reply and the counts it produced
                    # should appear together.
                    footer.refresh()

                    # A guard-stopped turn is the one non-ok outcome we can act
                    # on ourselves. Typing "continue" does exactly ONE thing —
                    # `_reset_turn_guards` — so a session that reliably resumes
                    # on "continue" was never stuck; it ran out of strike budget
                    # while there was still work on the list. Asking a human to
                    # refill a counter is not a decision, it is a keystroke, so
                    # do it here: same reset, same directive, bounded so a
                    # genuinely stuck model still reaches the user.
                    if outcome == "stopped" and _will_self_continue(engine, self_continues):
                        outstanding = open_items(engine.state)
                        if outstanding:
                            self_continues += 1
                            logger.info(
                                "Self-continue %d/%d — %d item(s) still open",
                                self_continues,
                                _MAX_SELF_CONTINUES,
                                len(outstanding),
                            )
                            console.print(
                                f"[dim]· Picking that back up myself "
                                f"({self_continues}/{_MAX_SELF_CONTINUES}) — "
                                f"{len(outstanding)} item(s) still open[/dim]"
                            )
                            _reset_turn_guards(engine)
                            message = _self_continue_directive(outstanding)
                            continue

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

    # A recursion cap ANYWHERE in the session is the headline: it means a turn
    # ran out of graph budget rather than finishing, so the run is only
    # valid-at-the-cutoff (#331). Otherwise the last turn's outcome decides.
    if "recursion" in turn_outcomes:
        stop_reason = "cap_hit"
    elif turn_outcomes and turn_outcomes[-1] in ("error", "timeout"):
        stop_reason = "error"
    else:
        stop_reason = "completed"
    return {
        "stop_reason": stop_reason,
        # Only meaningful beside ``"error"``; a cap hit is not a failure and
        # deliberately carries no message.
        "error": last_error[-1] if last_error and stop_reason == "error" else None,
    }


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
