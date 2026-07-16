"""Shared LLM plumbing lives in ``builder.agents.llm`` (Issue #309).

Model construction and the provider/token/timeout/recursion helpers are used by
BOTH build modes — the ReAct loop (``builder.agents.agent_loop``) and the
deterministic pipeline's bounded leaves (``builder.agents.leaves``). They were
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
            _build_chat_model,
            _detect_provider,
            _extract_model_name,
            _extract_token_usage,
            _get_request_timeout,
            _is_openai_reasoning_model,
            _recursion_limit,
        )


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
        assert _get_request_timeout() == 120.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _get_request_timeout

        monkeypatch.setenv("VITRO_REQUEST_TIMEOUT", "42")
        assert _get_request_timeout() == 42.0

    def test_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from builder.agents.llm import _get_request_timeout

        monkeypatch.setenv("VITRO_REQUEST_TIMEOUT", "not-a-number")
        assert _get_request_timeout() == 120.0


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
