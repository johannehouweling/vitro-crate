"""The provenance record names only the sampling controls actually sent (#769).

``temperature`` is omitted from the request for a Responses-API reasoning model
(the API 400s on an explicit value), so a crate that claimed one would make a
paper's Methods section factually wrong. These tests pin that the recorded
settings track the request, for both providers.
"""

from __future__ import annotations

import pytest

from builder.agents.llm import effective_sampling_settings
from builder.state import CrateState


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for stray in (
        "VITRO_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "VITRO_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "VITRO_OPENAI_USE_RESPONSES_API",
        "VITRO_TEMPERATURE",
        "VITRO_OPENAI_REASONING_EFFORT",
        "VITRO_OPENAI_MODEL",
        "VITRO_OPENAI_DRAFTER_MODEL",
        "OPENAI_MODEL",
        "VITRO_ANTHROPIC_MODEL",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(stray, raising=False)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "o3", "o4-mini"])
def test_reasoning_model_records_no_temperature(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """A Responses-API reasoning model never receives a temperature, so none is recorded."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", model)
    assert "temperature" not in effective_sampling_settings()


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1"])
def test_standard_model_records_temperature(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """A standard chat model is sent the resolved temperature, so it is recorded."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", model)
    monkeypatch.setenv("VITRO_TEMPERATURE", "0.3")
    assert effective_sampling_settings()["temperature"] == "0.3"


def test_anthropic_always_records_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic branch applies the temperature unconditionally."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    assert effective_sampling_settings()["temperature"] == "0.0"


def test_reasoning_effort_recorded_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reasoning_effort`` is recorded normalized, exactly as it is sent."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", " Medium ")
    assert effective_sampling_settings()["reasoning_effort"] == "medium"


def test_reasoning_effort_none_restores_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reasoning_effort="none"`` routes back to chat/completions, so temperature IS sent."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "none")
    settings = effective_sampling_settings()
    assert settings["temperature"] == "0.0"
    assert settings["reasoning_effort"] == "none"


def test_disagreeing_tiers_record_no_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tiers that disagree on whether a temperature is sent record none at all.

    The pipeline builds every leaf on the drafter tier, so a reasoning drafter
    behind a standard orchestrator means half the run's calls omitted the
    temperature — "no single temperature was in effect" is absence, not 0.0.
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("VITRO_OPENAI_DRAFTER_MODEL", "gpt-5.6-luna")
    assert "temperature" not in effective_sampling_settings()


def test_stamp_generator_records_effective_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exported run record carries the history budget and the effective sampling controls."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "high")
    monkeypatch.setenv("VITRO_MAX_HISTORY_TOKENS", "9000")
    state = CrateState()
    state.max_iterations = 42
    settings = state.stamp_generator(architecture="react").settings
    assert settings["max_iterations"] == "42"
    assert settings["max_history_tokens"] == "9000"
    assert settings["reasoning_effort"] == "high"
    assert "temperature" not in settings
