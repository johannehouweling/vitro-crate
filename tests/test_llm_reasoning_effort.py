"""Opt-in LLM reasoning ("thinking") knob for ``_build_chat_model`` (OpenAI path).

Reasoning-capable OpenAI models (o-series, gpt-5.x) do NOT accept a non-default
temperature while reasoning is active — gpt-5.1 only permits a custom temperature
when ``reasoning_effort`` is ``"none"``. So ``VITRO_OPENAI_REASONING_EFFORT`` must
do two things together:

* pass ``reasoning_effort`` through to ``ChatOpenAI``, and
* drop the default ``temperature=0`` when reasoning is *active* (effort != none),
  or the API rejects the request with
  ``Unsupported value: 'temperature' does not support 0``.

On a reasoning model, an *unset* ``VITRO_OPENAI_REASONING_EFFORT`` still routes
through the Responses API (reasoning is on by default) and forwards no
``reasoning_effort``; ``reasoning_effort="none"`` opts back to the standard
chat/completions + ``temperature=0`` path. See ``test_llm_reasoning_model`` for
the model-name routing and the standard-model default.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from builder import config


def _capture_openai_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``ChatOpenAI`` and return the kwargs ``_build_chat_model`` passes it."""
    langchain_openai = pytest.importorskip("langchain_openai")

    captured: dict[str, Any] = {}

    def _fake_chat_openai(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _fake_chat_openai)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", "gpt-5.1")

    from builder.agents.agent_loop import _build_chat_model

    _build_chat_model(provider="openai")
    return captured


class TestReasoningEffort:
    """The OpenAI branch honours ``VITRO_OPENAI_REASONING_EFFORT``."""

    def test_unset_routes_reasoning_model_via_responses_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (unset) on a reasoning model (gpt-5.1): reasoning is on by
        default, so it routes through the Responses API and is NOT forced to
        temperature=0 (which the API rejects); no reasoning_effort is forwarded."""
        monkeypatch.delenv("VITRO_OPENAI_REASONING_EFFORT", raising=False)
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["use_responses_api"] is True
        assert "temperature" not in kwargs
        assert "reasoning_effort" not in kwargs

    def test_active_effort_sets_reasoning_and_drops_temperature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Active effort (e.g. ``medium``): pass it, and DROP temperature."""
        monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "medium")
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["reasoning_effort"] == "medium"
        # Reasoning models reject temperature=0 while reasoning is active.
        assert "temperature" not in kwargs

    def test_effort_none_passes_through_and_keeps_temperature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``none`` disables reasoning; gpt-5.1 then allows temperature=0."""
        monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "none")
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["reasoning_effort"] == "none"
        assert kwargs["temperature"] == 0

    def test_capitalized_effort_is_normalized_before_forwarding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capitalized value (``Medium``) is lowercased — the OpenAI API only
        accepts the lowercase enum, so we must not forward it verbatim."""
        monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "Medium")
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["reasoning_effort"] == "medium"
        assert "temperature" not in kwargs

    def test_whitespace_none_normalizes_and_keeps_temperature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Surrounding whitespace / casing (`` None ``) still reads as ``none``:
        it forwards the clean ``none`` and keeps the deterministic temperature=0,
        instead of forwarding an invalid `` None `` that the API would reject."""
        monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", " None ")
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["reasoning_effort"] == "none"
        assert kwargs["temperature"] == 0

    def test_whitespace_only_effort_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank/whitespace value is a no-op — same as unset: no
        ``reasoning_effort`` forwarded, and (on the reasoning model gpt-5.1) the
        Responses API route with no forced temperature."""
        monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "   ")
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["use_responses_api"] is True
        assert "temperature" not in kwargs
        assert "reasoning_effort" not in kwargs


class TestReasoningEffortConfigMapping:
    """``merge_with_env`` surfaces the config-file knob as the env var."""

    def test_reasoning_effort_mapped_from_config(self) -> None:
        old = os.environ.pop("VITRO_OPENAI_REASONING_EFFORT", None)
        try:
            config.merge_with_env({"openai": {"reasoning_effort": "high"}})
            assert os.environ.get("VITRO_OPENAI_REASONING_EFFORT") == "high"
        finally:
            if old is not None:
                os.environ["VITRO_OPENAI_REASONING_EFFORT"] = old
            else:
                os.environ.pop("VITRO_OPENAI_REASONING_EFFORT", None)

    def test_env_var_wins_over_config(self) -> None:
        old = os.environ.get("VITRO_OPENAI_REASONING_EFFORT")
        os.environ["VITRO_OPENAI_REASONING_EFFORT"] = "from-env"
        try:
            config.merge_with_env({"openai": {"reasoning_effort": "from-config"}})
            assert os.environ["VITRO_OPENAI_REASONING_EFFORT"] == "from-env"
        finally:
            if old is not None:
                os.environ["VITRO_OPENAI_REASONING_EFFORT"] = old
            else:
                os.environ.pop("VITRO_OPENAI_REASONING_EFFORT", None)
