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


def _anthropic_env(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("VITRO_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("VITRO_ANTHROPIC_MODEL", model)
    monkeypatch.delenv("VITRO_TEMPERATURE", raising=False)


def test_temperature_override_applies_to_anthropic_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VITRO_TEMPERATURE reaches BOTH providers (#402).

    The Anthropic branch hard-coded ``temperature: 0`` and never read the
    variable, so a temperature experiment on Anthropic silently did nothing — and
    an A/B asked to compare two architectures at one temperature was in fact
    comparing two temperatures.
    """
    _anthropic_env(monkeypatch, "claude-sonnet-4-20250514")
    monkeypatch.setenv("VITRO_TEMPERATURE", "0.7")
    assert _build_chat_model(provider="anthropic").temperature == pytest.approx(0.7)


def test_anthropic_keeps_the_deterministic_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _anthropic_env(monkeypatch, "claude-sonnet-4-20250514")
    assert _build_chat_model(provider="anthropic").temperature == pytest.approx(0.0)


def test_blank_temperature_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Matches the convention VITRO_OPENAI_REASONING_EFFORT already uses; before
    # the fix this raised "could not convert string to float: ''".
    _openai_env(monkeypatch, "gpt-4o")
    monkeypatch.setenv("VITRO_TEMPERATURE", "   ")
    assert _build_chat_model(provider="openai").temperature == pytest.approx(0.0)


def test_non_numeric_temperature_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A silently-ignored control is the defect being fixed, so a typo must not
    # quietly resolve to 0.
    _openai_env(monkeypatch, "gpt-4o")
    monkeypatch.setenv("VITRO_TEMPERATURE", "hot")
    with pytest.raises(ValueError, match="VITRO_TEMPERATURE"):
        _build_chat_model(provider="openai")


def test_reasoning_model_never_receives_a_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with an explicit override: the Responses API 400s on any value."""
    _openai_env(monkeypatch, "gpt-5.1")
    monkeypatch.setenv("VITRO_TEMPERATURE", "0.7")
    model = _build_chat_model(provider="openai")
    assert model.use_responses_api is True
    assert model.temperature is None


def test_optional_ca_bundle_is_passed_to_openai_clients(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A configured corporate CA bundle controls both sync and async transports."""
    _openai_env(monkeypatch, "gpt-4o")
    ca_bundle = tmp_path / "corp-ca.pem"
    ca_bundle.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("VITRO_OPENAI_CA_BUNDLE", str(ca_bundle))

    import ssl

    import httpx

    # Force the provider imports BEFORE httpx.Client is swapped out. langchain's
    # `_SyncHttpxClientWrapper` subclasses `httpx.Client` at IMPORT time, so if
    # langchain_openai first loads while the fake is installed, the wrapper
    # inherits from the fake permanently — and every later construction fails
    # openai's `isinstance(http_client, httpx.Client)` against the real class,
    # in a different test, with no visible connection to this one.
    #
    # That is exactly what happened: this module passes alone and in most shard
    # layouts, and broke the moment an unrelated PR added tests and repartitioned
    # pytest-split so `test_unset_ca_bundle_keeps_default_client_path` landed
    # after this one in shard 16.
    import langchain_openai  # noqa: F401

    class FakeContext:
        check_hostname = True

    monkeypatch.setattr(ssl, "create_default_context", lambda cafile: FakeContext())

    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            clients.append(self)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    llm = _build_chat_model(provider="openai")

    assert len(clients) == 2
    assert all(client.kwargs["verify"].check_hostname for client in clients)
    assert llm.http_client is clients[0]
    assert llm.http_async_client is clients[1]


def test_unset_ca_bundle_keeps_default_client_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the opt-in setting no custom HTTP client is injected."""
    _openai_env(monkeypatch, "gpt-4o")
    monkeypatch.delenv("VITRO_OPENAI_CA_BUNDLE", raising=False)

    llm = _build_chat_model(provider="openai")

    assert llm.http_client is None
    assert llm.http_async_client is None


def test_missing_ca_bundle_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _openai_env(monkeypatch, "gpt-4o")
    monkeypatch.setenv("VITRO_OPENAI_CA_BUNDLE", str(tmp_path / "missing.pem"))

    with pytest.raises(ValueError, match="CA certificate bundle"):
        _build_chat_model(provider="openai")
