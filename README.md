# ISA-Tox RO-Crate Builder (vitro-crate)

**LLM-assisted builder for profile-conformant RO-Crates of *in vitro* toxicology data.**

The builder is a toolbox-based agent system that helps researchers create ISA-Tox profile-compliant RO-Crates. It uses a LangChain-powered LLM agent that dynamically decides which tools to call — entity drafting, lookups, validation, assessment — based on the current state of the crate.

---

## Quick Start

### Prerequisites

- **Python ≥ 3.12**
- **[uv](https://docs.astral.sh/uv/)** (recommended package manager)

### Install

```bash
# Clone the repo
git clone <repo-url> && cd vitro-crate

# Install with all extras (dev tools, LangChain, system certs)
uv sync --group dev --extra langchain --extra system-certs

# Minimal install (just the builder library, no agent):
uv sync

# With LangChain agent support:
uv sync --extra langchain

# With LangChain + system CA certs (corporate proxies):
uv sync --extra langchain --extra system-certs
```

### Set up your LLM provider

The builder uses an LLM agent (powered by LangChain) to orchestrate crate creation. There are **two ways** to configure it:

#### Option A: First-run wizard (easiest)

Just run the interactive agent — if no provider is configured, you'll be
prompted to set one up:

```bash
uv run python -m main --interactive
```

This walks you through provider choice, API key, endpoint, and model,
then saves everything to `~/.config/vitro-crate/config.toml`.

You can also run the wizard explicitly:

```bash
uv run python -m main --configure
```

And view your current configuration at any time:

```bash
uv run python -m main --show-config
```

#### Option B: Environment variables

> **API key convention:** All env vars are available as `VITRO_*` (preferred, scoped) or as the standard unprefixed name (backward-compatible fallback).  
> The `VITRO_*` variant always takes precedence when both are set.
> Config file values are used as fallbacks — env vars always win.
#### OpenAI / OpenAI-compatible (recommended — works with Ollama, LiteLLM, OpenAI, etc.)

```bash
# Required: API key
export VITRO_OPENAI_API_KEY="sk-..."           # or OPENAI_API_KEY

# Optional: custom endpoint (Ollama, local proxy, etc.)
export VITRO_OPENAI_BASE_URL="http://localhost:11434/v1"  # or OPENAI_BASE_URL

# Optional: model name (default: gpt-4o)
export VITRO_OPENAI_MODEL="llama3.2"           # or OPENAI_MODEL
```

#### Anthropic

```bash
# Required: API key
export VITRO_ANTHROPIC_API_KEY="sk-ant-..."    # or ANTHROPIC_API_KEY

# Optional: model name (default: claude-sonnet-4-20250514)
export VITRO_ANTHROPIC_MODEL="claude-sonnet-4-20250514"  # or ANTHROPIC_MODEL
```

---

## Usage

### Interactive agent mode (recommended)

Start a conversational session where the LLM agent walks you through crate creation:

```bash
# With existing research data folder:
uv run python -m main --interactive -i /path/to/experiment/

# Start fresh (no input — build everything from conversation):
uv run python -m main --interactive

# Specify provider / model / endpoint:
uv run python -m main --interactive --provider openai --model gpt-4o-mini
uv run python -m main --interactive --provider openai --api-base http://localhost:11434/v1
```

Once in the agent loop, you can type requests like:

> *"Scan my data folder and draft an investigation"*
> *"Look up Silychristin A on PubChem"*
> *"Draft a cell culture process for HepG2 cells"*
> *"Build the crate and validate it"*
> *"Assess MIT coverage"*

### Batch / info mode

```bash
# Scan input and print summary (no agent):
uv run python -m main -i /path/to/experiment/

# Resume a previous session:
uv run python -m main --resume 20250620_143022
```

### Full CLI reference

```
python -m main [options]

Options:
  -i, --input PATH       Path to input directory with research data
  -o, --output PATH      Output path for the ARC directory (RO-Crate)
  -r, --resume SESSION   Resume a previous session by ID
  -I, --interactive      Run in interactive agent mode (requires LangChain + API key)
  -p, --provider STR     LLM provider: 'openai' or 'anthropic' (auto-detected from env)
  -m, --model STR        Model name override (e.g. gpt-4o-mini, llama3.2, claude-sonnet-4)
  -b, --api-base URL     Custom API base URL for OpenAI-compatible providers
                         (e.g. http://localhost:11434/v1 for Ollama)
  -C, --configure        Run the interactive setup wizard to configure LLM provider
      --show-config      Show current LLM configuration and exit
  -v, --verbose          Enable debug logging
```

---

## Examples

### Using with Ollama (local, free)

```bash
# 1. Install and start Ollama: https://ollama.com
# 2. Pull a tool-calling model:
ollama pull llama3.2

# 3. Set env vars and run:
export VITRO_OPENAI_API_KEY="ollama"                    # Ollama accepts any non-empty key
export VITRO_OPENAI_BASE_URL="http://localhost:11434/v1"
export VITRO_OPENAI_MODEL="llama3.2"

uv run python -m main --interactive
```

### Using with LiteLLM proxy

```bash
export VITRO_OPENAI_API_KEY="sk-litellm-key"
export VITRO_OPENAI_BASE_URL="http://localhost:4000/v1"
export VITRO_OPENAI_MODEL="gpt-4o-mini"

uv run python -m main --interactive
```

### Using with OpenAI

```bash
export VITRO_OPENAI_API_KEY="sk-proj-..."
uv run python -m main --interactive --model gpt-4o-mini
```

---

## Architecture

See **[AGENTS.md](AGENTS.md)** for the full system design document.

---

## Development

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for:
- `uv sync --group dev --extra langchain --extra system-certs` — full dev setup
- `uv run pytest` — run tests
- `uv run ty` — type checking
- Test-driven development workflow
## Corporate / Private CA Certificates

If your organisation uses a private CA (e.g. for a proxy or internal PyPI mirror),
Python's ``certifi`` bundle won't include it, which causes SSL errors like
``invalid peer certificate: UnknownIssuer``.

### Option A: Environment variable (simplest)

```bash
export SSL_CERT_FILE=/path/to/your/corp-ca.pem
export REQUESTS_CA_BUNDLE=/path/to/your/corp-ca.pem
```

Set these in your ``.bashrc``, ``.profile``, or pass them inline:

```bash
SSL_CERT_FILE=/path/to/corp-ca.pem uv run python -m main --interactive
```

> **WSL tip:** Your Windows CA cert is typically at ``/mnt/c/Users/<you>/corp-ca.pem``.
> Set ``SSL_CERT_FILE`` to that path, or copy/symlink it into the Linux CA store:
> ```bash
> sudo cp /mnt/c/Users/ArrasM/corp-ca.pem /usr/local/share/ca-certificates/corp-ca.crt
> sudo update-ca-certificates
> ```
> After ``update-ca-certificates``, ``certifi-system-store`` will pick it up automatically.

### Option B: ``certifi-system-store`` (manual install)

The ``certifi-system-store`` package patches ``certifi`` to use the system's
CA store (``/etc/ssl/certs``) instead of its own bundle.  Install it manually:

```bash
uv add certifi-system-store
```

This works for ``requests``, ``httpx``, ``openai``, and all other HTTP clients.

---

## All Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VITRO_OPENAI_API_KEY` | For OpenAI | — | OpenAI / OpenAI-compatible API key |
| `OPENAI_API_KEY` | Fallback | — | Same, unprefixed fallback |
| `VITRO_OPENAI_BASE_URL` | No | — | Custom endpoint URL (Ollama, LiteLLM, etc.) |
| `OPENAI_BASE_URL` | Fallback | — | Same, unprefixed fallback |
| `VITRO_OPENAI_MODEL` | No | `gpt-4o` | Model name for OpenAI-compatible providers |
| `OPENAI_MODEL` | Fallback | `gpt-4o` | Same, unprefixed fallback |
| `VITRO_ANTHROPIC_API_KEY` | For Anthropic | — | Anthropic API key |
| `ANTHROPIC_API_KEY` | Fallback | — | Same, unprefixed fallback |
| `VITRO_ANTHROPIC_MODEL` | No | `claude-sonnet-4-20250514` | Model name for Anthropic |
| `ANTHROPIC_MODEL` | Fallback | `claude-sonnet-4-20250514` | Same, unprefixed fallback |

For OpenAI-compatible providers (Ollama, LiteLLM, vLLM, etc.):
- Set `VITRO_OPENAI_API_KEY` (any non-empty value works for Ollama)
- Set `VITRO_OPENAI_BASE_URL` to the provider's `/v1` endpoint
- Set `VITRO_OPENAI_MODEL` to the model name the provider serves
