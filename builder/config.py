"""Persistent configuration for the ISA-Tox RO-Crate Builder.

Stores LLM provider settings in a platform-appropriate config directory:

- **Linux / macOS**: ``~/.config/vitro-crate/config.toml``
    (also respects ``$XDG_CONFIG_HOME`` if set)
- **Windows**: ``%APPDATA%\\vitro-crate\\config.toml``

Precedence (highest to lowest):
    1. CLI flags (--provider, --model, --api-base)
    2. Environment variables (VITRO_OPENAI_API_KEY, VITRO_MAX_RETRIES, etc.)
    3. Config file (~/.config/vitro-crate/config.toml)

Timezone
-------
All timestamps in the application are localised to the configured timezone.
The timezone is read from ``[display] timezone`` in config.toml, falling
back to the ``VITRO_TIMEZONE`` environment variable, then to ``Europe/Amsterdam``.
Use ``get_timezone()`` to retrieve the configured ``datetime.tzinfo`` and
``now()`` to get the current localised timestamp.
"""

from __future__ import annotations

import os
import sys
import tomllib
from datetime import datetime, tzinfo
from datetime import timezone as _timezone_mod
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
    "max_history_tokens": 12000,
}

_DEFAULT_TIMEZONE = "Europe/Amsterdam"


def get_timezone() -> tzinfo:
    """Return the configured local timezone as a ``datetime.tzinfo``.

    Precedence (highest to lowest):
        1. Environment variable ``VITRO_TIMEZONE``
        2. Config file value ``[display] timezone``
        3. Built-in default ``Europe/Amsterdam``

    Uses ``zoneinfo`` (Python 3.9+) which reads the IANA timezone database
    from the OS. Falls back to UTC if the configured name is not found.
    """
    try:
        from zoneinfo import ZoneInfo

        tz_name: str | None = os.environ.get("VITRO_TIMEZONE")
        if not tz_name:
            cfg = load_config()
            tz_name = cfg.get("display", {}).get("timezone")
        if not tz_name:
            tz_name = _DEFAULT_TIMEZONE
        return ZoneInfo(tz_name)
    except (KeyError, OSError, ModuleNotFoundError):
        # Fallback to UTC if zoneinfo is not available or the zone is unknown
        return _timezone_mod.utc


def now() -> datetime:
    """Return the current datetime localised to the configured timezone.

    All timestamps in the application should use this function so they
    are consistently in the user's preferred timezone.
    """
    return datetime.now(get_timezone())


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


def get_max_history_tokens() -> int:
    """Return the per-turn message-history token budget, respecting precedence.

    The agent trims/summarizes the accumulated transcript before each model
    call so verbose tool outputs are not replayed verbatim every turn and the
    per-turn input stays bounded (Issue #61). This is the approximate token
    budget for the *history* between the stable system prompt and the trailing
    state brief.

    Precedence (highest to lowest):
        1. Environment variable VITRO_MAX_HISTORY_TOKENS
        2. Config file value [agent.max_history_tokens]
        3. Built-in default (12000)
    """
    env_val = os.environ.get("VITRO_MAX_HISTORY_TOKENS")
    if env_val is not None:
        try:
            return int(env_val)
        except (ValueError, TypeError):
            pass
    cfg = load_config()
    cfg_val = cfg.get("agent", {}).get("max_history_tokens")
    if cfg_val is not None:
        try:
            return int(cfg_val)
        except (ValueError, TypeError):
            pass
    return DEFAULTS.get("max_history_tokens", 12000)


def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist.

    The directory holds the LLM provider API key, so it is created (and, if it
    already exists, tightened) to owner-only ``0o700`` permissions so the secret
    is never group/world-readable (Issue #170). Permission tightening is a no-op
    on platforms (e.g. Windows) that ignore POSIX modes.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        # Best-effort on platforms without POSIX permissions; the secret is
        # still written below with the most restrictive mode the OS honours.
        pass
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

    The file stores the LLM provider API key in plaintext, so it is written
    with owner-only ``0o600`` permissions (Issue #170): the file is opened with
    that mode when created, and an explicit ``chmod`` afterwards tightens any
    pre-existing, loosely-permissioned file. The TOML format is unchanged, so
    existing readers keep working. Permission handling is best-effort on
    platforms (e.g. Windows) that ignore POSIX modes.
    """
    ensure_config_dir()
    existing = load_config()
    existing.update(config)
    # ``os.open`` with mode 0o600 creates a new file owner-read/write only; the
    # process umask can only further *restrict* it, never loosen it.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_format_toml(existing))
    try:
        # Covers the already-exists case: O_CREAT does not change the mode of a
        # file that was already on disk with looser permissions.
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def merge_with_env(config: dict[str, Any]) -> dict[str, Any]:
    """Merge config-file values into environment variables.

    Config file values are used as fallbacks -- the env var always wins.
    """
    mapping = {
        ("openai", "api_key"): "VITRO_OPENAI_API_KEY",
        ("openai", "base_url"): "VITRO_OPENAI_BASE_URL",
        ("openai", "model"): "VITRO_OPENAI_MODEL",
        ("openai", "drafter_model"): "VITRO_OPENAI_DRAFTER_MODEL",
        ("openai", "reasoning_effort"): "VITRO_OPENAI_REASONING_EFFORT",
        ("openai", "model_provider"): "VITRO_OPENAI_MODEL_PROVIDER",
        ("anthropic", "api_key"): "VITRO_ANTHROPIC_API_KEY",
        ("anthropic", "model"): "VITRO_ANTHROPIC_MODEL",
        ("anthropic", "drafter_model"): "VITRO_ANTHROPIC_DRAFTER_MODEL",
        ("anthropic", "model_provider"): "VITRO_ANTHROPIC_MODEL_PROVIDER",
        ("_global", "max_retries"): "VITRO_MAX_RETRIES",
    }
    for (section, key), env_var in mapping.items():
        if env_var not in os.environ:
            val = config.get(section, {}).get(key)
            if val is not None:
                os.environ[env_var] = str(val)
    return config


def get_provider() -> str | None:
    """Detect which LLM API family is configured.

    Returns ``\"openai\"``, ``\"anthropic\"``, or *None* if neither is configured.
    This distinguishes the *API protocol* (OpenAI-compatible vs Anthropic),
    not the *model vendor*. For model-level provider disambiguation (e.g.
    DeepSeek-via-Azure vs DeepSeek-native), use :func:`get_model_provider`.
    """
    if os.environ.get("VITRO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("VITRO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    cfg = load_config()
    if cfg.get("openai", {}).get("api_key"):
        return "openai"
    if cfg.get("anthropic", {}).get("api_key"):
        return "anthropic"
    return None


def get_model_provider() -> str | None:
    """Return the user-configured model vendor/provider for cost calculation.

    Reads from the ``VITRO_OPENAI_MODEL_PROVIDER`` env var (or
    ``VITRO_ANTHROPIC_MODEL_PROVIDER``) first, then falls back to the config
    file's ``openai.model_provider`` or ``anthropic.model_provider``.

    The value should be one of the LiteLLM pricing-JSON provider prefixes
    (e.g. ``\"deepseek\"``, ``\"azure\"``, ``\"openai\"``, ``\"together\"``, etc.)
    as configured during :func:`interactive_setup`.

    Returns *None* if no model provider is configured — cost display will
    be unavailable.
    """
    # Check env vars — the active API family determines which env var to read
    family = get_provider()
    if family == "openai":
        val = os.environ.get("VITRO_OPENAI_MODEL_PROVIDER")
        if val:
            return val.strip().lower()
    elif family == "anthropic":
        val = os.environ.get("VITRO_ANTHROPIC_MODEL_PROVIDER")
        if val:
            return val.strip().lower()

    # Fall back to config file
    cfg = load_config()
    if family == "openai":
        return cfg.get("openai", {}).get("model_provider")
    if family == "anthropic":
        return cfg.get("anthropic", {}).get("model_provider")
    return None


def get_drafter_model() -> str | None:
    """Return the configured *drafter* model name, or ``None`` if unset.

    Model tiering (Issue #96) lets the cheap, bounded-extraction drafter use a
    different model from the strong orchestrator. The drafter model is read for
    the active API family:

    - ``openai``    -> ``VITRO_OPENAI_DRAFTER_MODEL``
    - ``anthropic`` -> ``VITRO_ANTHROPIC_DRAFTER_MODEL``

    Precedence mirrors the primary-model knobs: the env var wins, then the
    config-file value (``[openai] drafter_model`` / ``[anthropic]
    drafter_model``).

    Returns *None* when no drafter model is configured — callers MUST treat this
    as "use the primary model", so the default is a strict no-op (single model,
    identical to today's behaviour).
    """
    family = get_provider()
    if family == "openai":
        env_var = "VITRO_OPENAI_DRAFTER_MODEL"
    elif family == "anthropic":
        env_var = "VITRO_ANTHROPIC_DRAFTER_MODEL"
    else:
        return None

    val = os.environ.get(env_var)
    if val:
        return val
    cfg = load_config()
    return cfg.get(family, {}).get("drafter_model")


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
        "VITRO_TIMEZONE": os.environ.get("VITRO_TIMEZONE") or "Europe/Amsterdam (default)",
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
        base_url = (
            input("API base URL [http://localhost:11434/v1]: ").strip()
            or "http://localhost:11434/v1"
        )
        config["openai"]["base_url"] = base_url
        model = input("Model name [llama3.2]: ").strip() or "llama3.2"
        config["openai"]["model"] = model

        # Ask for model vendor/provider for cost calculation
        _ask_model_provider(config, section="openai")
    elif provider in ("2", "anthropic"):
        config["anthropic"] = {}
        print()
        api_key = input("Anthropic API key (sk-ant-...): ").strip()
        if api_key:
            config["anthropic"]["api_key"] = api_key
        model = (
            input("Model name [claude-sonnet-4-20250514]: ").strip() or "claude-sonnet-4-20250514"
        )
        config["anthropic"]["model"] = model
        # Anthropic is always served by Anthropic
        config["anthropic"]["model_provider"] = "anthropic"
    else:
        print(f"Unknown provider: {provider!r}")
        return False

    save_config(config)
    print()
    print("\u2713 Configuration saved to ~/.config/vitro-crate/config.toml")
    print("   You can override these with VITRO_* environment variables.")
    return True


def _ask_model_provider(config: dict[str, Any], section: str) -> None:
    """Prompt the user to pick a model vendor/provider from the LiteLLM pricing
    list.  The result is stored in ``config[section][\"model_provider\"]``.

    Fetches the LiteLLM pricing JSON to extract unique provider prefixes.
    Falls back to a basic prompt if the fetch fails.
    """
    try:
        from builder.pricing import list_providers

        providers = list_providers()
        if providers:
            print()
            print("  Model vendor/provider (for cost calculation):")
            for i, p in enumerate(providers, 1):
                print(f"    {i:>2}) {p}")
            try:
                prompt = f"  Choose [1-{len(providers)}, or press Enter for 'openai']: "
                choice = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                choice = ""
            if choice:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(providers):
                        config[section]["model_provider"] = providers[idx]
                        return
                except (ValueError, IndexError):
                    # Try as a raw name
                    choice_lower = choice.lower().strip()
                    provider_set = set(providers)
                    if choice_lower in provider_set:
                        config[section]["model_provider"] = choice_lower
                        return
                    print(f"  [dim]'{choice}' not recognised — using 'openai'[/dim]")
            config[section]["model_provider"] = "openai"
            return
    except Exception:
        pass

    # Fallback: basic prompt
    print()
    prompt = "Model provider for cost tracking (e.g. openai, azure, deepseek, together) [openai]: "
    mp = input(prompt).strip().lower()
    config[section]["model_provider"] = mp or "openai"


__all__ = [
    "load_config",
    "save_config",
    "merge_with_env",
    "is_configured",
    "get_provider",
    "get_model_provider",
    "get_drafter_model",
    "describe_config",
    "interactive_setup",
    "get_max_iterations",
    "get_timezone",
    "now",
    "CONFIG_DIR",
    "CONFIG_PATH",
]
