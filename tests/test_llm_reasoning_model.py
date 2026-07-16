"""OpenAI reasoning-model support in ``_build_chat_model``.

Reasoning models (gpt-5.x, o-series) reject ``temperature`` and cannot use
function tools on ``/v1/chat/completions`` with ``reasoning_effort`` — the API
returns::

    Function tools with reasoning_effort are not supported for gpt-5.6-luna in
    /v1/chat/completions. To use function tools, use /v1/responses or set
    reasoning_effort to 'none'.

Both the ReAct loop and the pipeline's drafter leaves bind tools, so such a
model must be routed through the Responses API. These tests pin that behaviour
and guarantee standard (non-reasoning) models are left exactly as before.
"""

from __future__ import annotations

import pytest

from builder.agents.llm import _build_chat_model


def _openai_env(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("VITRO_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VITRO_OPENAI_MODEL", model)
    for stray in (
        "VITRO_OPENAI_USE_RESPONSES_API",
        "VITRO_TEMPERATURE",
        "VITRO_OPENAI_REASONING_EFFORT",
        "VITRO_OPENAI_BASE_URL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(stray, raising=False)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.1", "o3", "o1-mini", "o4-mini"])
def test_reasoning_model_uses_responses_api(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """A reasoning model routes through the Responses API and is not forced to temperature 0."""
    _openai_env(monkeypatch, model)
    llm = _build_chat_model(provider="openai")
    assert llm.use_responses_api is True
    assert llm.temperature != 0  # never force an unsupported temperature on a reasoning model


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1", "gpt-3.5-turbo"])
def test_standard_model_unchanged(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """Standard models keep chat/completions and the deterministic temperature=0 default."""
    _openai_env(monkeypatch, model)
    llm = _build_chat_model(provider="openai")
    assert not llm.use_responses_api
    assert llm.temperature == 0


def test_env_forces_responses_api_for_custom_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom/Azure deployment name the heuristic can't recognise can force the Responses API."""
    _openai_env(monkeypatch, "research-reasoner-prod")
    # Without the override it would be treated as a standard model:
    assert not _build_chat_model(provider="openai").use_responses_api
    monkeypatch.setenv("VITRO_OPENAI_USE_RESPONSES_API", "1")
    assert _build_chat_model(provider="openai").use_responses_api is True


def test_env_reasoning_effort_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """VITRO_OPENAI_REASONING_EFFORT is forwarded (a cost lever for reasoning-token spend)."""
    _openai_env(monkeypatch, "gpt-5.6-luna")
    monkeypatch.setenv("VITRO_OPENAI_REASONING_EFFORT", "low")
    assert _build_chat_model(provider="openai").reasoning_effort == "low"


def test_explicit_temperature_override_for_standard_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """VITRO_TEMPERATURE overrides the 0 default on a standard model."""
    _openai_env(monkeypatch, "gpt-4o")
    monkeypatch.setenv("VITRO_TEMPERATURE", "0.7")
    assert _build_chat_model(provider="openai").temperature == pytest.approx(0.7)
