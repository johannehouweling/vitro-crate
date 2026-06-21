"""Persistent configuration for the ISA-Tox RO-Crate Builder.

Stores LLM provider settings in a platform-appropriate config directory:

- **Linux / macOS**: ``~/.config/vitro-crate/config.toml``
    (also respects ``$XDG_CONFIG_HOME`` if set)
- **Windows**: ``%APPDATA%\\vitro-crate\\config.toml``

Precedence (highest to lowest):
    1. CLI flags (--provider, --model, --api-base)
    2. Environment variables (VITRO_OPENAI_API_KEY, VITRO_MAX_RETRIES, etc.)
    3. Config file (~/.config/vitro-crate/config.toml)
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    """Return the platform-appropriate config directory for vitro-crate.

    - On **Linux/macOS**: ``$XDG_CONFIG_HOME/vitro-crate``
      or ``~/.config/vitro-crate`` (XDG Base Directory)
    - On **Windows**: ``%APPDATA%\\vitro-crate``
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vitro-crate"


CONFIG_DIR = _config_dir()
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULTS: dict[str, Any] = {
    "max_iterations": 100,
}


def get_max_iterations() -> int:
    """Return the max tool-calling iterations, respecting precedence.

    Precedence (highest to lowest):
        1. Environment variable VITRO_MAX_ITERATIONS
        2. Config file value [agent.max_iterations]
        3. Built-in default (100)
    """
    env_val = os.environ.get("VITRO_MAX_ITERATIONS")
    if env_val is not None:
        try:
            return int(env_val)
        except (ValueError, TypeError):
            pass
    cfg = load_config()
    cfg_val = cfg.get("agent", {}).get("max_iterations")
    if cfg_val is not None:
        try:
            return int(cfg_val)
        except (ValueError, TypeError):
            pass
    return DEFAULTS.get("max_iterations", 100)


def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_config() -> dict[str, Any]:
    """Load the persistent config file.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return dict(tomllib.load(f))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _format_toml(config: dict[str, Any]) -> str:
    """Serialize a config dict as TOML."""
    lines: list[str] = ["# ISA-Tox RO-Crate Builder configuration\n"]
    for section, values in config.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            sv = str(v)
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{sv}"')
        lines.append("")
    return "\n".join(lines)


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to ``~/.config/vitro-crate/config.toml``.

    Preserves keys not present in ``config`` (only overwrites what's given).
    """
    ensure_config_dir()
    existing = load_config()
    existing.update(config)
    with open(CONFIG_PATH, "w") as f:
        f.write(_format_toml(existing))


def merge_with_env(config: dict[str, Any]) -> dict[str, Any]:
    """Merge config-file values into environment variables.

    Config file values are used as fallbacks -- the env var always wins.
    """
    mapping = {
        ("openai", "api_key"): "VITRO_OPENAI_API_KEY",
        ("openai", "base_url"): "VITRO_OPENAI_BASE_URL",
        ("openai", "model"): "VITRO_OPENAI_MODEL",
        ("anthropic", "api_key"): "VITRO_ANTHROPIC_API_KEY",
        ("anthropic", "model"): "VITRO_ANTHROPIC_MODEL",
        ("_global", "max_retries"): "VITRO_MAX_RETRIES",
    }
    for (section, key), env_var in mapping.items():
        if env_var not in os.environ:
            val = config.get(section, {}).get(key)
            if val is not None:
                os.environ[env_var] = str(val)
    return config


def is_configured() -> bool:
    """Check if any LLM provider is configured (env or config file)."""
    if os.environ.get("VITRO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return True
    if os.environ.get("VITRO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return True
    cfg = load_config()
    if cfg.get("openai", {}).get("api_key"):
        return True
    if cfg.get("anthropic", {}).get("api_key"):
        return True
    return False


def describe_config() -> str:
    """Return a human-readable summary of current config state."""
    env_cfg: dict[str, Any] = {
        "VITRO_OPENAI_API_KEY": bool(os.environ.get("VITRO_OPENAI_API_KEY")),
        "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
        "VITRO_OPENAI_BASE_URL": os.environ.get("VITRO_OPENAI_BASE_URL") or "",
        "VITRO_OPENAI_MODEL": os.environ.get("VITRO_OPENAI_MODEL") or "gpt-4o",
        "VITRO_ANTHROPIC_API_KEY": bool(os.environ.get("VITRO_ANTHROPIC_API_KEY")),
        "VITRO_ANTHROPIC_MODEL": os.environ.get("VITRO_ANTHROPIC_MODEL") or "",
        "VITRO_MAX_RETRIES": os.environ.get("VITRO_MAX_RETRIES") or "3 (default)",
    }
    file_cfg = load_config()
    lines = ["Current LLM configuration:\n"]
    lines.append("  Environment variables:")
    for k, v in env_cfg.items():
        display = "\u2713 set" if isinstance(v, bool) and v else str(v) if v else "\u2014"
        lines.append(f"    {k}: {display}")
    if file_cfg:
        lines.append("  Config file (~/.config/vitro-crate/config.toml):")
        for section, vals in file_cfg.items():
            for k, v in vals.items():
                display = "***" if "key" in k or "password" in k else str(v)
                lines.append(f"    [{section}] {k}: {display}")
    else:
        lines.append("  Config file: not found")
    return "\n".join(lines)


def interactive_setup() -> bool:
    """Run an interactive wizard to configure LLM provider settings.

    Returns True if configuration was saved, False if the user cancelled.
    """
    print()
    print("\u2554" + "\u2550" * 54 + "\u2557")
    print("\u2551  ISA-Tox RO-Crate Builder -- First-Time Setup            \u2551")
    print("\u255a" + "\u2550" * 54 + "\u255d")
    print()
    print("This tool needs an LLM to power the agent that helps")
    print("you build RO-Crates.  You can use one of:")
    print()
    print("  1) OpenAI (or any OpenAI-compatible provider)")
    print("     -> Ollama (local, free), LiteLLM, vLLM, OpenAI API, ...")
    print("  2) Anthropic (Claude)")
    print()
    print("Your settings will be saved to ~/.config/vitro-crate/config.toml")
    print()

    try:
        provider = input("Choose provider [1=openai, 2=anthropic, q=quit]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if provider in ("q", "quit", "exit", ""):
        return False

    config: dict[str, Any] = {}

    if provider in ("1", "openai"):
        config["openai"] = {}
        print()
        api_key = input("API key (press Enter to skip if using env var): ").strip()
        if api_key:
            config["openai"]["api_key"] = api_key
        base_url = input("API base URL [http://localhost:11434/v1]: ").strip() or "http://localhost:11434/v1"
        config["openai"]["base_url"] = base_url
        model = input("Model name [llama3.2]: ").strip() or "llama3.2"
        config["openai"]["model"] = model
    elif provider in ("2", "anthropic"):
        config["anthropic"] = {}
        print()
        api_key = input("Anthropic API key (sk-ant-...): ").strip()
        if api_key:
            config["anthropic"]["api_key"] = api_key
        model = input("Model name [claude-sonnet-4-20250514]: ").strip() or "claude-sonnet-4-20250514"
        config["anthropic"]["model"] = model
    else:
        print(f"Unknown provider: {provider!r}")
        return False

    save_config(config)
    print()
    print("\u2713 Configuration saved to ~/.config/vitro-crate/config.toml")
    print("   You can override these with VITRO_* environment variables.")
    return True


__all__ = [
    "load_config",
    "save_config",
    "merge_with_env",
    "is_configured",
    "describe_config",
    "interactive_setup",
    "get_max_iterations",
    "CONFIG_DIR",
    "CONFIG_PATH",
]
