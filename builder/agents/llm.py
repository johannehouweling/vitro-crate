"""Shared LLM plumbing for both build modes (Issue #309).

Model construction (:func:`_build_chat_model`) plus the provider-detection,
token-usage, request-timeout and recursion-limit helpers used by BOTH the ReAct
agent loop (:mod:`builder.agents.react.agent_loop`) and the deterministic pipeline's
bounded leaves (:mod:`builder.agents.pipeline.leaves`).

Extracted out of ``agent_loop.py`` so the pipeline no longer imports from an
agent-mode module just to build its drafter model -- a wrong-direction
dependency the build-mode harmonization removes. This module depends on neither
mode; the provider SDKs (``langchain_openai`` / ``langchain_anthropic``) are
imported lazily inside :func:`_build_chat_model` so importing this module stays
cheap.

It is also the shared home for the token-accounting seam: :data:`UsageSink` and
:func:`make_usage_logger` live here rather than in the pipeline spine (where they
were born, #221) because the spine is not the only non-ReAct caller of a bounded
leaf any more -- the HITL guidance tail is one too (#384), and any future one
(a second guidance-style tail, an MCP front-end) gets accounting by construction
instead of by remembering. The logger only reads ``engine.state`` and
``engine.profiler``, and says so structurally via :class:`UsageEngine`, so this
module never imports the engine at all -- not even under ``TYPE_CHECKING`` -- and
stays cycle-free and cheap to import.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "ModelOverrides",
    "UsageSink",
    "make_usage_logger",
    "_build_chat_model",
    "_detect_provider",
    "_extract_model_name",
    "_extract_token_usage",
    "_get_request_timeout",
    "_apply_temperature",
    "_is_openai_reasoning_model",
    "_resolve_temperature",
    "_recursion_limit",
]

# A usage sink receives one bounded-leaf call's token usage as
# ``(input_tokens, output_tokens, model_name)``; any element may be ``None`` when
# the provider (or an offline fake) reported no usage. Callers that own an
# engine pass :func:`make_usage_logger`'s sink, which logs each call to the
# engine profiler so every surface that reads ``profile.ndjson`` -- the
# interactive status bar, the dashboard's token table, the eval's metric miner --
# sees the same numbers regardless of which orchestrator made the call.
UsageSink = Callable[[int | None, int | None, str | None], None]


class UsageEngine(Protocol):
    """The slice of an engine :func:`make_usage_logger` actually reads.

    Structural rather than nominal, and deliberately so. Annotating the parameter
    ``AgentEngine`` would state a dependency this module does not have and must
    not acquire — importing the engine here would cost every caller the whole
    engine import and risk a cycle, which is why the sink duck-types in the first
    place. It would also be a claim the type checker enforces against callers who
    legitimately pass something smaller (a test double, a future MCP front-end
    that owns state but is not an ``AgentEngine``).

    ``profiler`` is not declared: it is read with a ``getattr`` default because an
    engine that was never initialized has none, and requiring it here would make
    the annotation stricter than the code.
    """

    state: Any


@dataclass(frozen=True)
class ModelOverrides:
    """Caller-supplied model selection, threaded to BOTH build modes (#399).

    The CLI's ``--provider`` / ``--model`` / ``--api-base`` used to reach the
    ReAct loop as explicit arguments while the deterministic pipeline resolved its
    model from the ENVIRONMENT instead. The flags were accepted on both paths and
    applied on only one, so an A/B asked to compare two architectures on one model
    silently compared two models — and part of any measured token or cost delta
    was a model delta rather than an architecture delta.

    Carried as one value rather than three loose arguments because it crosses
    several layers (CLI -> build -> spine -> leaf, and the eval factory in
    parallel), and a bare triple invites one of the three being dropped at a hop.

    Every field is optional: an empty instance means "resolve from the
    environment", which is exactly the pre-existing behaviour, so threading this
    through changes nothing until a caller supplies a value.
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None

    def is_empty(self) -> bool:
        """True when nothing is pinned and the environment decides."""
        return self.provider is None and self.model is None and self.base_url is None

    def as_kwargs(self) -> dict[str, Any]:
        """The subset of :func:`_build_chat_model` arguments this pins."""
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url}


def _recursion_limit(max_iterations: int) -> int:
    """Map the documented tool-iteration cap to LangGraph's ``recursion_limit``.

    Each tool iteration is roughly two super-steps (``model`` then ``tools``),
    so the recursion limit is ``2 * max_iterations``. Floored at 2 so the graph
    can always complete at least one model→tools→model cycle. Without this,
    LangGraph applies its silent default of 25 super-steps and a runaway loop
    raises an uncaught ``GraphRecursionError`` (#56).
    """
    return max(2, max_iterations * 2)


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


# Per-request wall-clock timeout default (seconds) for the chat model when no
# VITRO_REQUEST_TIMEOUT is set. A real --legacy-react run hung with a 349s+ model
# invoke and no timeout, so the turn never ended and the #254 backstop never ran.
_DEFAULT_REQUEST_TIMEOUT = 600.0


def _get_request_timeout() -> float:
    """Return the per-request wall-clock timeout (seconds) for the chat model.

    Issue #263: a real ``--legacy-react`` run hung when the final model invoke
    ran 349s+ with no response and no timeout, so the turn never ended and the
    #254 finish-backstop never exported. A finite request timeout is the first
    line of defence (the loop's wall-clock guard in :func:`_invoke_with_timeout`
    is the second). Precedence:

        1. Environment variable ``VITRO_REQUEST_TIMEOUT`` (seconds)
        2. Built-in default (600s / 10 minutes)

    A non-positive or unparseable value falls back to the default so the model
    is never built without a finite timeout.
    """
    env_val = os.environ.get("VITRO_REQUEST_TIMEOUT")
    if env_val is not None:
        try:
            parsed = float(env_val)
            if parsed > 0:
                return parsed
        except (ValueError, TypeError):
            pass
    return _DEFAULT_REQUEST_TIMEOUT


_OPENAI_REASONING_PREFIXES: tuple[str, ...] = ("gpt-5", "o1", "o3", "o4")


def _is_openai_reasoning_model(model_name: str | None) -> bool:
    """Return True if *model_name* denotes an OpenAI reasoning model.

    Reasoning models (``gpt-5.x`` and the ``o``-series) reject the ``temperature``
    parameter and require the Responses API to bind function tools
    (``/v1/chat/completions`` returns a 400 for tools + reasoning_effort). Custom
    / Azure deployment names that do not match this heuristic can force
    Responses-API routing via ``VITRO_OPENAI_USE_RESPONSES_API``.
    """
    m = (model_name or "").strip().lower()
    return m.startswith(_OPENAI_REASONING_PREFIXES)


_DEFAULT_TEMPERATURE = 0.0


def _resolve_temperature() -> float:
    """The single parse point for ``VITRO_TEMPERATURE``, shared by every provider.

    It lived inline on the OpenAI branch while the Anthropic branch hard-coded
    ``temperature: 0``, so a temperature experiment on Anthropic silently did
    nothing — and the two build arms this repo A/B-compares were not actually
    running the same configuration (#402).

    Blank/whitespace reads as unset rather than raising, matching the convention
    ``VITRO_OPENAI_REASONING_EFFORT`` already uses. A non-numeric value still
    raises, naming the variable: a control that is silently ignored is the very
    defect being fixed here, so a typo must fail loudly rather than resolve to 0.
    """
    raw = (os.environ.get("VITRO_TEMPERATURE") or "").strip()
    if not raw:
        return _DEFAULT_TEMPERATURE
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VITRO_TEMPERATURE must be a number, got {raw!r}") from exc


def _apply_temperature(kwargs: dict[str, Any], *, supported: bool = True) -> None:
    """Set the resolved temperature on *kwargs*, or omit it entirely.

    ``supported=False`` leaves the key off rather than sending a default: a
    Responses-API reasoning model accepts only the provider default and 400s on
    an explicit value, so "no opinion" must be expressed as absence.
    """
    if supported:
        kwargs["temperature"] = _resolve_temperature()

def _build_chat_model(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    max_retries: int | None = None,
    role: str = "orchestrator",
    timeout: float | None = None,
    streaming: bool = False,
) -> Any:
    """Build a LangChain chat model for the given or detected provider.

    Supports custom endpoints (OpenAI-compatible only — Ollama, LiteLLM,
    local proxies, etc.) via the ``OPENAI_BASE_URL`` environment variable
    or the ``base_url`` parameter.

    Model tiering (Issue #96): a single ``_build_chat_model`` centralises
    construction so a different model can be bound per *role* without changing
    the graph topology. The strong ``"orchestrator"`` keeps the primary model
    (``VITRO_OPENAI_MODEL`` / ``VITRO_ANTHROPIC_MODEL``); the cheap
    ``"drafter"`` uses ``VITRO_OPENAI_DRAFTER_MODEL`` /
    ``VITRO_ANTHROPIC_DRAFTER_MODEL`` *when configured*. With no drafter model
    set, the drafter resolves to the same primary model as the orchestrator —
    a strict no-op (single model, identical to today's behaviour).

    Args:
        provider: One of ``"openai"``, ``"anthropic"``.  If ``None``, auto-detect.
        model: Model name override (e.g. ``"gpt-4o-mini"``, ``"llama3.2"``).
            An explicit value wins over role-based resolution. Falls back to
            provider/role defaults.
        base_url: Custom API base URL for OpenAI-compatible providers.
            Falls back to ``OPENAI_BASE_URL`` env var, then provider default.
        role: ``"orchestrator"`` (default) or ``"drafter"``. Selects the model
            tier when ``model`` is not given explicitly.
        timeout: Per-request wall-clock timeout in seconds wired onto the model
            (Issue #263). Falls back to ``VITRO_REQUEST_TIMEOUT`` then a finite
            built-in default so the model is never built without one — a silent
            provider stall can never hang a turn forever.
        streaming: Stream the response token-by-token so a
            ``on_llm_new_token`` callback can show the reply as it is written
            (the interactive footer's live tail). ``invoke`` still returns one
            aggregated message, so callers are unaffected. Set only by the
            interactive ReAct loop; the pipeline's bounded leaves have nothing
            to display and stay non-streaming. Off by default, and
            ``VITRO_NO_STREAM=1`` forces it off everywhere — a provider that
            mishandles streamed tool calls must be recoverable without a code
            change.

    Returns:
        A LangChain ``BaseChatModel`` instance.

    Raises:
        RuntimeError: If no provider can be detected or the provider is unknown.
    """
    if max_retries is None:
        env_val = os.environ.get("VITRO_MAX_RETRIES")
        max_retries = int(env_val) if env_val is not None else 3

    if (os.environ.get("VITRO_NO_STREAM") or "").strip().lower() in ("1", "true", "yes", "on"):
        streaming = False

    if timeout is None:
        timeout = _get_request_timeout()

    provider = provider or _detect_provider()
    if provider is None:
        raise RuntimeError(
            "No LLM provider configured. Set VITRO_OPENAI_API_KEY "
            "(or OPENAI_API_KEY) or VITRO_ANTHROPIC_API_KEY "
            "(or ANTHROPIC_API_KEY) environment variable, or pass "
            "--provider openai|anthropic."
        )

    # Model tiering: when the caller asks for the drafter role and no explicit
    # model was given, prefer the configured drafter model. If none is set,
    # ``model`` stays ``None`` and the provider branch resolves the primary
    # model exactly as before (strict no-op default).
    if model is None and role == "drafter":
        if provider == "openai":
            model = os.environ.get("VITRO_OPENAI_DRAFTER_MODEL") or None
        elif provider == "anthropic":
            model = os.environ.get("VITRO_ANTHROPIC_DRAFTER_MODEL") or None

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        # VITRO_ prefixed env vars take priority, fall back to unprefixed
        api_key = os.environ.get("VITRO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        resolved_base = (
            base_url or os.environ.get("VITRO_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        )
        ca_bundle = os.environ.get("VITRO_OPENAI_CA_BUNDLE")
        resolved_model = (
            model
            or os.environ.get("VITRO_OPENAI_MODEL")
            or os.environ.get("OPENAI_MODEL", "gpt-4o")
        )
        # Reasoning models (gpt-5.x, o-series) reject `temperature` and cannot
        # bind function tools on /v1/chat/completions with reasoning_effort — the
        # API requires the Responses API instead. Both the ReAct loop and the
        # pipeline drafter leaves bind tools, so route reasoning models through
        # the Responses API. Detect by name, with VITRO_OPENAI_USE_RESPONSES_API
        # as an explicit override for custom/Azure deployment names the heuristic
        # cannot recognise. An explicit reasoning_effort="none" turns reasoning
        # OFF, so that call is treated as a standard (chat/completions,
        # temperature-0) request.
        #
        # reasoning_effort is normalized once (strip + lowercase) so a
        # capitalized/whitespaced value like "Medium" or " none " forwards as the
        # clean lowercase enum the OpenAI API expects — and a blank value
        # collapses to None (a no-op).
        reasoning_effort = (
            os.environ.get("VITRO_OPENAI_REASONING_EFFORT") or ""
        ).strip().lower() or None
        override = os.environ.get("VITRO_OPENAI_USE_RESPONSES_API")
        if override is not None:
            use_responses = override.strip().lower() in ("1", "true", "yes", "on")
        elif reasoning_effort == "none":
            use_responses = False
        else:
            use_responses = _is_openai_reasoning_model(resolved_model)

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_retries": max_retries,
            # "timeout" is the public alias of ChatOpenAI.request_timeout (#263).
            "timeout": timeout,
        }
        if streaming:
            kwargs["streaming"] = True
            # OpenAI omits usage from a streamed response unless it is asked for.
            # Without this the token counts silently become zero — and the status
            # footer's tokens/cost, the profiler's accounting and the session
            # cost report all read from that same usage metadata.
            kwargs["stream_usage"] = True
        if use_responses:
            kwargs["use_responses_api"] = True
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        # Keep the deterministic temperature=0 default for a standard-path model
        # (VITRO_TEMPERATURE overrides it), but never force a temperature on a
        # Responses-API-routed reasoning model — it only accepts the provider
        # default (the API 400s on "temperature does not support 0").
        _apply_temperature(kwargs, supported=not use_responses)
        if api_key:
            kwargs["api_key"] = api_key
        if resolved_base:
            kwargs["base_url"] = resolved_base
        if ca_bundle:
            # Opt-in custom CA trust for corporate HTTPS endpoints.  When unset,
            # ChatOpenAI keeps its normal httpx transport and verification.
            import ssl
            from pathlib import Path

            cert_path = Path(ca_bundle).expanduser()
            if not cert_path.is_file():
                raise ValueError(
                    "VITRO_OPENAI_CA_BUNDLE must point to an existing CA certificate bundle"
                )
            import httpx

            try:
                ssl_context = ssl.create_default_context(cafile=str(cert_path))
            except (OSError, ssl.SSLError) as exc:
                raise ValueError(
                    "VITRO_OPENAI_CA_BUNDLE must contain a readable CA certificate bundle"
                ) from exc
            kwargs["http_client"] = httpx.Client(verify=ssl_context)
            kwargs["http_async_client"] = httpx.AsyncClient(verify=ssl_context)

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
            "max_retries": max_retries,
            # "timeout" is the public alias of ChatAnthropic
            # .default_request_timeout (#263).
            "timeout": timeout,
        }
        # #402: this was a hard-coded `"temperature": 0` in the literal above, so
        # VITRO_TEMPERATURE was inert on this provider entirely — an A/B asked to
        # compare two architectures at one temperature silently compared two
        # temperatures. Same resolution as the OpenAI branch.
        _apply_temperature(kwargs)
        if streaming:
            # Anthropic reports usage on the streamed message_start /
            # message_delta events, so no extra opt-in is needed here.
            kwargs["streaming"] = True
        if api_key:
            kwargs["api_key"] = api_key
        return ChatAnthropic(**kwargs)

    raise RuntimeError(f"Unknown provider: {provider!r}. Use openai or anthropic.")


def _extract_token_usage(message: Any) -> tuple[int | None, int | None]:
    """Extract ``(input_tokens, output_tokens)`` from a LangChain ``AIMessage``.

    Provider-agnostic and the SINGLE source of truth for usage mining across the
    ReAct model node (:func:`_wrap_model_node`) and the deterministic pipeline's
    bounded leaves (:mod:`builder.agents.pipeline.leaves`), so both arms of the eval
    harness record token counts with identical semantics:

    1. Prefer the standardised ``usage_metadata`` (langchain-core >=0.3).
    2. Fall back to provider-specific ``response_metadata["token_usage"]`` /
       ``["usage"]`` (``prompt_tokens`` / ``completion_tokens`` aliases).

    Returns ``(None, None)`` when neither source carries usage (e.g. an offline
    fake model) so callers can record a clean zero without crashing.
    """
    if message is None:
        return None, None

    input_tokens: int | None = None
    output_tokens: int | None = None

    usage = getattr(message, "usage_metadata", None)
    if usage is not None:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")

    if input_tokens is None or output_tokens is None:
        meta = getattr(message, "response_metadata", None) or {}
        tu = meta.get("token_usage") or meta.get("usage") or {}
        if input_tokens is None:
            input_tokens = tu.get("prompt_tokens") or tu.get("input_tokens")
        if output_tokens is None:
            output_tokens = tu.get("completion_tokens") or tu.get("output_tokens")

    return input_tokens, output_tokens


def _extract_model_name(message: Any) -> str | None:
    """Extract the model name from an ``AIMessage``'s ``response_metadata``."""
    resp_meta: dict = getattr(message, "response_metadata", None) or {}
    return resp_meta.get("model_name") or resp_meta.get("model")


def _as_int(value: Any) -> int:
    """Coerce a possibly-missing/None token count to a non-negative int."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def make_usage_logger(engine: UsageEngine, totals: dict[str, int]) -> UsageSink:
    """Build a :data:`UsageSink` that records one leaf call's token usage (#221).

    For each leaf call it (1) accumulates ``input``/``output`` tokens into
    *totals* (the running per-run sum the caller surfaces in its result dict) and
    (2) logs a ``node_end``/``node="model"`` event to the engine profiler — the
    SAME profile-event shape the ReAct model node emits — so
    :func:`eval.metrics.mine_profile_metrics`, :func:`builder.agents.ui._read_token_totals`
    and the dashboard's token table all see leaf calls identically to ReAct hops
    with no changes of their own. When no profiler is active (e.g. an engine that
    was never initialized) the accumulation still happens; only the profile write
    is skipped.

    Lives here, not in the pipeline spine, because the spine is not the only
    caller: the HITL guidance tail builds one of these too (#384). One
    implementation means the two phases of an interactive run cannot drift into
    logging two different event shapes, which is precisely how the guidance tail's
    spend went missing from the status bar in the first place.
    """

    def _sink(
        input_tokens: int | None,
        output_tokens: int | None,
        model_name: str | None,
    ) -> None:
        in_t = _as_int(input_tokens)
        out_t = _as_int(output_tokens)
        totals["input_tokens"] += in_t
        totals["output_tokens"] += out_t
        # Also accumulate onto the crate's generator record so the exported crate
        # carries what the run cost. Independent of the profiler below: cost
        # accounting must not depend on instrumentation being enabled.
        try:
            engine.state.record_llm_usage({"input_tokens": in_t, "output_tokens": out_t})
        except Exception:  # noqa: BLE001 — accounting never breaks a leaf call
            logger.debug("Could not record leaf LLM usage", exc_info=True)
        profiler = getattr(engine, "profiler", None)
        if profiler is not None:
            profiler.log_event(
                event="node_end",
                node="model",
                iteration=engine.state.iteration_count,
                input_tokens=in_t,
                output_tokens=out_t,
                model_name=model_name,
            )

    return _sink
