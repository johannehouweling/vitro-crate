"""Opt-in LLM reasoning ("thinking") knob for ``_build_chat_model`` (OpenAI path).

Reasoning-capable OpenAI models (o-series, gpt-5.x) do NOT accept a non-default
temperature while reasoning is active — gpt-5.1 only permits a custom temperature
when ``reasoning_effort`` is ``"none"``. So ``VITRO_OPENAI_REASONING_EFFORT`` must
do two things together:

* pass ``reasoning_effort`` through to ``ChatOpenAI``, and
* drop the default ``temperature=0`` when reasoning is *active* (effort != none),
  or the API rejects the request with
  ``Unsupported value: 'temperature' does not support 0``.

Unset preserves today's behaviour exactly (``temperature=0``, no
``reasoning_effort``) so the deterministic default is untouched.
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

    def test_unset_keeps_temperature_zero_and_no_reasoning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (unset): unchanged — temperature=0, no reasoning_effort."""
        monkeypatch.delenv("VITRO_OPENAI_REASONING_EFFORT", raising=False)
        kwargs = _capture_openai_kwargs(monkeypatch)
        assert kwargs["temperature"] == 0
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
