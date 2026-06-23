"""Tests for the drafter-model tier in builder/config.py (Issue #96).

Model tiering lets the cheap drafter use a distinct model from the strong
orchestrator. These tests pin two guarantees:

1. ``VITRO_OPENAI_DRAFTER_MODEL`` / ``VITRO_ANTHROPIC_DRAFTER_MODEL`` are mapped
   from the config file into the environment, mirroring the primary-model knob.
2. ``get_drafter_model`` resolves the drafter model when configured and returns
   ``None`` (strict no-op → caller falls back to the primary model) when unset.
"""

from __future__ import annotations

import os

from builder import config


class TestDrafterModelEnvMapping:
    """`merge_with_env` must surface the drafter-model config keys as env vars."""

    def test_openai_drafter_model_mapped_from_config(self) -> None:
        old = os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)
        try:
            config.merge_with_env({"openai": {"drafter_model": "gpt-4o-mini"}})
            assert os.environ.get("VITRO_OPENAI_DRAFTER_MODEL") == "gpt-4o-mini"
        finally:
            if old is not None:
                os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = old
            else:
                os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)

    def test_anthropic_drafter_model_mapped_from_config(self) -> None:
        old = os.environ.pop("VITRO_ANTHROPIC_DRAFTER_MODEL", None)
        try:
            config.merge_with_env({"anthropic": {"drafter_model": "claude-haiku-4"}})
            assert os.environ.get("VITRO_ANTHROPIC_DRAFTER_MODEL") == "claude-haiku-4"
        finally:
            if old is not None:
                os.environ["VITRO_ANTHROPIC_DRAFTER_MODEL"] = old
            else:
                os.environ.pop("VITRO_ANTHROPIC_DRAFTER_MODEL", None)

    def test_env_var_wins_over_config(self) -> None:
        old = os.environ.get("VITRO_OPENAI_DRAFTER_MODEL")
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "from-env"
        try:
            config.merge_with_env({"openai": {"drafter_model": "from-config"}})
            assert os.environ["VITRO_OPENAI_DRAFTER_MODEL"] == "from-env"
        finally:
            if old is not None:
                os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = old
            else:
                os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)


class TestGetDrafterModel:
    """`get_drafter_model` resolves the tier, defaulting to None (no-op)."""

    def test_returns_none_when_unset(self) -> None:
        old_o = os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)
        old_a = os.environ.pop("VITRO_ANTHROPIC_DRAFTER_MODEL", None)
        old_key = os.environ.get("VITRO_OPENAI_API_KEY")
        os.environ["VITRO_OPENAI_API_KEY"] = "sk-test"
        try:
            assert config.get_drafter_model() is None
        finally:
            for var, val in (
                ("VITRO_OPENAI_DRAFTER_MODEL", old_o),
                ("VITRO_ANTHROPIC_DRAFTER_MODEL", old_a),
            ):
                if val is not None:
                    os.environ[var] = val
            if old_key is not None:
                os.environ["VITRO_OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("VITRO_OPENAI_API_KEY", None)

    def test_returns_openai_drafter_model_when_set(self) -> None:
        old_o = os.environ.get("VITRO_OPENAI_DRAFTER_MODEL")
        old_key = os.environ.get("VITRO_OPENAI_API_KEY")
        os.environ["VITRO_OPENAI_API_KEY"] = "sk-test"
        os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = "gpt-4o-mini"
        try:
            assert config.get_drafter_model() == "gpt-4o-mini"
        finally:
            if old_o is not None:
                os.environ["VITRO_OPENAI_DRAFTER_MODEL"] = old_o
            else:
                os.environ.pop("VITRO_OPENAI_DRAFTER_MODEL", None)
            if old_key is not None:
                os.environ["VITRO_OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("VITRO_OPENAI_API_KEY", None)

    def test_returns_anthropic_drafter_model_when_anthropic_active(self) -> None:
        old_a = os.environ.get("VITRO_ANTHROPIC_DRAFTER_MODEL")
        old_okey = os.environ.pop("VITRO_OPENAI_API_KEY", None)
        old_ukey = os.environ.pop("OPENAI_API_KEY", None)
        old_akey = os.environ.get("VITRO_ANTHROPIC_API_KEY")
        os.environ["VITRO_ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ["VITRO_ANTHROPIC_DRAFTER_MODEL"] = "claude-haiku-4"
        try:
            assert config.get_drafter_model() == "claude-haiku-4"
        finally:
            if old_a is not None:
                os.environ["VITRO_ANTHROPIC_DRAFTER_MODEL"] = old_a
            else:
                os.environ.pop("VITRO_ANTHROPIC_DRAFTER_MODEL", None)
            if old_akey is not None:
                os.environ["VITRO_ANTHROPIC_API_KEY"] = old_akey
            else:
                os.environ.pop("VITRO_ANTHROPIC_API_KEY", None)
            if old_okey is not None:
                os.environ["VITRO_OPENAI_API_KEY"] = old_okey
            if old_ukey is not None:
                os.environ["OPENAI_API_KEY"] = old_ukey
