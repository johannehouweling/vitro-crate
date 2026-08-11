"""Shared LLM plumbing lives in ``builder.agents.llm`` (Issue #309).

Model construction and the provider/token/timeout/recursion helpers are used by
BOTH build modes — the ReAct loop (``builder.agents.react.agent_loop``) and the
deterministic pipeline's bounded leaves (``builder.agents.pipeline.leaves``). They were
extracted out of ``agent_loop.py`` so the pipeline no longer imports from an
agent-mode module to build its drafter model. These tests pin the symbols to
their new home (import parity) and smoke-test behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest


class TestImportParity:
    """Every shared helper is importable from ``builder.agents.llm``."""

    def test_all_shared_helpers_importable(self) -> None:
        from builder.agents.llm import (  # noqa: F401
            UsageSink,
            _build_chat_model,
            _detect_provider,
            _extract_model_name,
            _extract_token_usage,
            _get_request_timeout,
            _is_openai_reasoning_model,
            _recursion_limit,
            make_usage_logger,
        )

    def test_importing_llm_does_not_drag_in_the_engine(self) -> None:
        """``make_usage_logger`` duck-types its engine on purpose (#384).

        It only reaches for ``engine.profiler`` / ``engine.state``, so
        :class:`~builder.engine.AgentEngine` stays a ``TYPE_CHECKING`` import. If
        it ever became a real one this deliberately cheap shared module would pull
        the whole engine in behind every leaf, and the leaves import it eagerly.
        Measured in a fresh interpreter because the in-process ``sys.modules`` is
        already polluted by whatever the rest of the suite imported.
        """
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        probe = "import sys, builder.agents.llm; print('builder.engine' in sys.modules)"
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "False", "builder.agents.llm must stay engine-free"


class TestRecursionLimit:
    def test_doubles_iteration_cap(self) -> None:
        from builder.agents.llm import _recursion_limit

        assert _recursion_limit(50) == 100
        assert _recursion_limit(25) == 50

    def test_floored_at_two(self) -> None:
        from builder.agents.llm import _recursion_limit

        assert _recursion_limit(0) == 2
        assert _recursion_limit(1) == 2
        assert _recursion_limit(-5) == 2


class TestDetectProvider:
    def test_none_when_no_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _detect_provider

        for var in (
            "VITRO_OPENAI_API_KEY",
            "OPENAI_API_KEY",
            "VITRO_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        assert _detect_provider() is None

    def test_openai_preferred_over_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _detect_provider

        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
        assert _detect_provider() == "openai"

    def test_anthropic_when_only_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _detect_provider

        for var in ("VITRO_OPENAI_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
        assert _detect_provider() == "anthropic"


class TestGetRequestTimeout:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _get_request_timeout

        monkeypatch.delenv("VITRO_REQUEST_TIMEOUT", raising=False)
        assert _get_request_timeout() == 600.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _get_request_timeout

        monkeypatch.setenv("VITRO_REQUEST_TIMEOUT", "42")
        assert _get_request_timeout() == 42.0

    def test_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _get_request_timeout

        monkeypatch.setenv("VITRO_REQUEST_TIMEOUT", "not-a-number")
        assert _get_request_timeout() == 600.0


class TestIsOpenAIReasoningModel:
    def test_reasoning_prefixes_true(self) -> None:
        from builder.agents.llm import _is_openai_reasoning_model

        assert _is_openai_reasoning_model("gpt-5.1")
        assert _is_openai_reasoning_model("o3-mini")
        assert _is_openai_reasoning_model("O4-preview")

    def test_non_reasoning_false(self) -> None:
        from builder.agents.llm import _is_openai_reasoning_model

        assert not _is_openai_reasoning_model("gpt-4o")
        assert not _is_openai_reasoning_model(None)
        assert not _is_openai_reasoning_model("")


class _FakeMessage:
    def __init__(self, usage_metadata: Any = None, response_metadata: Any = None) -> None:
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata


class TestExtractTokenUsage:
    def test_none_message(self) -> None:
        from builder.agents.llm import _extract_token_usage

        assert _extract_token_usage(None) == (None, None)

    def test_usage_metadata(self) -> None:
        from builder.agents.llm import _extract_token_usage

        msg = _FakeMessage(usage_metadata={"input_tokens": 11, "output_tokens": 7})
        assert _extract_token_usage(msg) == (11, 7)

    def test_response_metadata_fallback(self) -> None:
        from builder.agents.llm import _extract_token_usage

        msg = _FakeMessage(
            response_metadata={"token_usage": {"prompt_tokens": 3, "completion_tokens": 5}}
        )
        assert _extract_token_usage(msg) == (3, 5)


class TestExtractModelName:
    def test_none_when_absent(self) -> None:
        from builder.agents.llm import _extract_model_name

        assert _extract_model_name(_FakeMessage()) is None

    def test_reads_model_name(self) -> None:
        from builder.agents.llm import _extract_model_name

        msg = _FakeMessage(response_metadata={"model_name": "gpt-4o"})
        assert _extract_model_name(msg) == "gpt-4o"


class _RecordingProfiler:
    """A profiler double that keeps the kwargs of every event logged to it."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _DuckEngine:
    """The only two attributes ``make_usage_logger`` is allowed to reach for.

    Deliberately NOT an ``AgentEngine``: the sink must stay duck-typed so the
    shared llm module never needs to import the engine (see
    ``TestImportParity.test_importing_llm_does_not_drag_in_the_engine``). Its
    ``state`` is a real :class:`CrateState`, so the generator-record side of the
    sink is exercised against the real accumulator rather than a fake one.
    """

    def __init__(self, profiler: Any = None) -> None:
        from builder.state import CrateState

        self.state = CrateState()
        self.state.iteration_count = 4
        self.profiler = profiler


class TestMakeUsageLogger:
    """#384 — one shared sink builder, usable by any LLM caller in either arm.

    It was the pipeline spine's private helper while the spine was its only
    caller; the guidance tail became a second one, and a second *copy* would have
    been free to log a different event shape than the one every reader parses.
    """

    def test_accumulates_totals_and_logs_the_reader_visible_event(self) -> None:
        from builder.agents.llm import make_usage_logger

        profiler = _RecordingProfiler()
        engine = _DuckEngine(profiler)
        totals = {"input_tokens": 0, "output_tokens": 0}

        sink = make_usage_logger(engine, totals)
        sink(120, 35, "gpt-4o-mini")
        sink(80, 5, "gpt-4o-mini")

        assert totals == {"input_tokens": 200, "output_tokens": 40}
        # The exact shape ``ui._read_token_totals`` / the dashboard / the eval
        # filter on: anything else is invisible to every reader.
        assert [(e["event"], e["node"]) for e in profiler.events] == [
            ("node_end", "model"),
            ("node_end", "model"),
        ]
        assert profiler.events[0]["input_tokens"] == 120
        assert profiler.events[0]["output_tokens"] == 35
        assert profiler.events[0]["model_name"] == "gpt-4o-mini"
        assert profiler.events[0]["iteration"] == 4
        # …and the crate's own generator record, which the export carries.
        assert engine.state.generator.input_tokens == 200
        assert engine.state.generator.output_tokens == 40

    def test_unknown_usage_coerces_to_zero(self) -> None:
        """``(None, None, None)`` is what ``_extract_token_usage`` reports for an
        offline/fake model — it must record a clean zero, never crash or guess.
        """
        from builder.agents.llm import make_usage_logger

        profiler = _RecordingProfiler()
        engine = _DuckEngine(profiler)
        totals = {"input_tokens": 0, "output_tokens": 0}

        make_usage_logger(engine, totals)(None, None, None)

        assert totals == {"input_tokens": 0, "output_tokens": 0}
        assert profiler.events[0]["input_tokens"] == 0
        assert profiler.events[0]["output_tokens"] == 0

    def test_accumulates_without_a_profiler(self) -> None:
        """An engine that was never initialized has no profiler; only the profile
        write is skipped, the accounting still happens.
        """
        from builder.agents.llm import make_usage_logger

        engine = _DuckEngine(profiler=None)
        totals = {"input_tokens": 0, "output_tokens": 0}

        make_usage_logger(engine, totals)(11, 7, "gpt-4o-mini")

        assert totals == {"input_tokens": 11, "output_tokens": 7}


class TestBuildChatModel:
    def test_raises_without_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _build_chat_model

        for var in (
            "VITRO_OPENAI_API_KEY",
            "OPENAI_API_KEY",
            "VITRO_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(RuntimeError):
            _build_chat_model()

    def test_builds_openai_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _build_chat_model

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        model = _build_chat_model(provider="openai")
        assert model.model_name is not None
