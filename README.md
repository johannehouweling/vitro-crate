# ISA-Tox RO-Crate Builder (vitro-crate)

**LLM-assisted builder for profile-conformant RO-Crates of *in vitro* toxicology data.**

[![CI](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml/badge.svg)](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml)

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

# Optional: cheap drafter model for bounded extraction (model tiering).
# Unset = single model (the drafter uses VITRO_OPENAI_MODEL).
export VITRO_OPENAI_DRAFTER_MODEL="gpt-4o-mini"

# Optional: enable reasoning ("thinking") on reasoning-capable models
# (o-series, gpt-5.x). Values: none (default) | low | medium | high.
# Omit for non-reasoning models like gpt-4o. When reasoning is active the
# builder stops sending temperature=0, which gpt-5.x rejects while reasoning.
export VITRO_OPENAI_REASONING_EFFORT="medium"
```

#### Anthropic

```bash
# Required: API key
export VITRO_ANTHROPIC_API_KEY="sk-ant-..."    # or ANTHROPIC_API_KEY

# Optional: model name (default: claude-sonnet-4-20250514)
export VITRO_ANTHROPIC_MODEL="claude-sonnet-4-20250514"  # or ANTHROPIC_MODEL

# Optional: cheap drafter model for bounded extraction (model tiering).
# Unset = single model (the drafter uses VITRO_ANTHROPIC_MODEL).
export VITRO_ANTHROPIC_DRAFTER_MODEL="claude-haiku-4"
```

---

## Usage

### Interactive build mode (recommended)

`--interactive` offers **two supported build architectures over the same toolbox**.
Both are maintained — pick the one that fits how you want to work:

| Variant | Flag | What it does | When to pick it |
| --- | --- | --- | --- |
| **Deterministic pipeline + HITL guidance** (default) | `--interactive` | Code drives the known step ordering (scaffold the ISA backbone, draft and materialize entities, validate, auto-fix REQUIRED issues), then walks you through any remaining gaps it can't close on its own. | You want a **deterministic, cheaper, reproducible** build. It is the default and won the in-repo A/B gate (full ISA-Tox conformance on the shared corpus where the ReAct loop stalled). |
| **Conversational ReAct agent** | `--interactive --legacy-react` | An LLM agent decides the order of tool calls turn by turn. | You want **flexible, conversational exploration** and to let the model drive. |

Both variants are first-class and actively maintained — this is an ongoing
exploration, not a one-way migration. See the architecture docs (`AGENTS.md` §14)
for the full comparison.

```bash
# Default deterministic pipeline — with an existing research data folder:
uv run python -m main --interactive -i /path/to/experiment/

# Start fresh (no input — build the backbone, then guided enrichment):
uv run python -m main --interactive

# Specify provider / model / endpoint:
uv run python -m main --interactive --provider openai --model gpt-4o-mini
uv run python -m main --interactive --provider openai --api-base http://localhost:11434/v1

# Conversational ReAct agent (supported alternative):
uv run python -m main --interactive --legacy-react
```

**Where the crate is written.** The completed build is written to disk as a valid
RO-Crate (`ro-crate-metadata.json` plus payload), and the **absolute** output path
is printed at the end. The destination is chosen as follows:

- `--output` / `-o` always wins.
- **Default** (no `--output`, with `--input`): a **sibling of the input folder** —
  `<input_parent>/<input_name>-ro-crate/`. For example, `-i /data/experiment/`
  writes to `/data/experiment-ro-crate/`.
- No `--input` (conversation mode): the session working directory
  (`sessions/<session_id>/working_crate/`).

```bash
# Default: writes to /path/to/experiment-ro-crate/ (sibling of the input)
uv run python -m main --interactive -i /path/to/experiment/

# Override the destination explicitly:
uv run python -m main --interactive -i /path/to/experiment/ -o /path/to/my-crate/
```

### Configuration file (pre-populated)

Settings are stored in ``~/.config/vitro-crate/config.toml`` (Linux/macOS)
or ``%APPDATA%\\vitro-crate\\config.toml`` (Windows).
You can pre-populate this file so the builder doesn't prompt you on first run:

```toml
# ~/.config/vitro-crate/config.toml
[openai]
api_key = "sk-proj-..."
base_url = "https://api.openai.com/v1"
model = "gpt-4o"
# Optional: cheap drafter model (model tiering). Omit for a single model.
drafter_model = "gpt-4o-mini"
# Optional: reasoning ("thinking") for gpt-5.x / o-series: none|low|medium|high.
# reasoning_effort = "medium"

[anthropic]
api_key = "sk-ant-..."
model = "claude-sonnet-4-20250514"
# Optional: cheap drafter model (model tiering). Omit for a single model.
drafter_model = "claude-haiku-4"

[_global]
# How many times to retry LLM API calls on transient errors
# (rate limits, 5xx, network blips)
max_retries = 5

[agent]
# Maximum tool-calling iterations per request before the agent
# stops to avoid an endless loop. Increase for complex tasks,
# decrease to catch runaway loops earlier. Default: 100.
max_iterations = 100
# Approximate token budget for the message history replayed to the
# model each turn. The transcript is trimmed/pruned before every call
# so verbose tool outputs (e.g. scan listings, already in CrateState)
# aren't replayed verbatim and per-turn input stays bounded. Default: 12000.
max_history_tokens = 12000
```

Environment variables (``VITRO_*``) always win over the config file:

| Variable | Description | Default |
|----------|-------------|---------|
| ``VITRO_OPENAI_API_KEY`` | API key for OpenAI / compatible providers | — |
| ``VITRO_OPENAI_BASE_URL`` | API base URL override | ``https://api.openai.com/v1`` |
| ``VITRO_OPENAI_MODEL`` | Model name for OpenAI | ``gpt-4o`` |
| ``VITRO_OPENAI_DRAFTER_MODEL`` | Cheap drafter model for OpenAI (model tiering) | — (uses ``VITRO_OPENAI_MODEL``) |
| ``VITRO_OPENAI_REASONING_EFFORT`` | Reasoning ("thinking") effort for reasoning-capable OpenAI models (o-series, gpt-5.x): ``none``/``low``/``medium``/``high`` | — (off) |
| ``VITRO_ANTHROPIC_API_KEY`` | API key for Anthropic | — |
| ``VITRO_ANTHROPIC_MODEL`` | Model name for Anthropic | ``claude-sonnet-4-20250514`` |
| ``VITRO_ANTHROPIC_DRAFTER_MODEL`` | Cheap drafter model for Anthropic (model tiering) | — (uses ``VITRO_ANTHROPIC_MODEL``) |
| ``VITRO_MAX_RETRIES`` | LLM API retry count on transient errors | ``3`` |
| ``VITRO_MAX_ITERATIONS`` | Max tool-calling iterations per request | ``100`` |
| ``VITRO_MAX_HISTORY_TOKENS`` | Approx. token budget for replayed message history per turn | ``12000`` |

Once in the agent loop, you can type requests like:

> *"Scan my data folder and draft an investigation"*
> *"Look up Silychristin A on PubChem"*
> *"Draft a cell culture process for HepG2 cells"*
> *"Build the crate and validate it"*
> *"Assess MIT coverage"*

While iterating, the agent checks conformance with `build_and_validate`, which
assembles and validates the crate **in memory** (no files written) and returns
issues keyed to the entity and property that failed. Only when the crate is
conformant does it call `export_crate` to write the RO-Crate directory to disk.

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
  -o, --output PATH      Output path for the RO-Crate directory. Defaults to a
                         sibling of --input: <input>-ro-crate/ (or the session
                         working_crate/ when no --input is given)
  -r, --resume SESSION   Resume a previous session by ID
  -I, --interactive      Run in interactive build mode: deterministic pipeline +
                         HITL guidance tail (requires LangChain + API key)
      --legacy-react     With --interactive, use the legacy ReAct agent loop
                         instead of the default pipeline+guidance build
  -p, --provider STR     LLM provider: 'openai' or 'anthropic' (auto-detected from env)
  -m, --model STR        Model name override (e.g. gpt-4o-mini, llama3.2, claude-sonnet-4)
  -b, --api-base URL     Custom API base URL for OpenAI-compatible providers
                         (e.g. http://localhost:11434/v1 for Ollama)
  -C, --configure        Run the interactive setup wizard to configure LLM provider
      --show-config      Show current LLM configuration and exit
  -v, --verbose          Increase verbosity (-v = INFO, -vv = DEBUG)
                            -v: INFO level — normal progress messages
                            -vv: DEBUG level — tool internals, scanner
                               timing, and profiling log output
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

## Profiling

See **[docs/profiling.md](docs/profiling.md)** for details on:
- Running with `-vv` for debug logging and scanner timing
- The `profile.ndjson` event schema (`tool_call`, `node_start`, `node_end`, etc.)
- How to analyse profile logs with `jq`, `pandas`, or simple scripts
- Troubleshooting interactive vs batch mode

---

## Development

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for:
- `uv sync --dev --extra langchain --extra system-certs` — full dev setup
- `uv run pytest` — run tests
- `uv run ty` — type checking
- `uvx ruff check` — linting
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
| `VITRO_OPENAI_DRAFTER_MODEL` | No | — (uses `VITRO_OPENAI_MODEL`) | Cheap drafter model (model tiering). Unset → single model, unchanged behaviour |
| `VITRO_OPENAI_REASONING_EFFORT` | No | — (off) | Reasoning ("thinking") effort for reasoning-capable OpenAI models (o-series, gpt-5.x): `none`/`low`/`medium`/`high`. Omit for non-reasoning models. When active, the builder stops sending `temperature=0` (gpt-5.x rejects it while reasoning). |
| `VITRO_ANTHROPIC_API_KEY` | For Anthropic | — | Anthropic API key |
| `ANTHROPIC_API_KEY` | Fallback | — | Same, unprefixed fallback |
| `VITRO_ANTHROPIC_MODEL` | No | `claude-sonnet-4-20250514` | Model name for Anthropic |
| `ANTHROPIC_MODEL` | Fallback | `claude-sonnet-4-20250514` | Same, unprefixed fallback |
| `VITRO_ANTHROPIC_DRAFTER_MODEL` | No | — (uses `VITRO_ANTHROPIC_MODEL`) | Cheap drafter model (model tiering). Unset → single model, unchanged behaviour |
| `VITRO_MAX_RETRIES` | No | `3` | Max retry attempts for LLM API calls (configurable via ``[openai]`` / ``[anthropic]`` in config file) |
| `VITRO_MAX_ITERATIONS` | No | `100` | Max tool-calling iterations per request before the agent stops to avoid endless loops. Set in `[agent]` section of config file. |
| `VITRO_MAX_HISTORY_TOKENS` | No | `12000` | Approximate token budget for the message history replayed to the model each turn. The transcript is trimmed/pruned before every call so verbose tool outputs aren't replayed verbatim and per-turn input stays bounded. Set in `[agent]` section of config file. |

For OpenAI-compatible providers (Ollama, LiteLLM, vLLM, etc.):
- Set `VITRO_OPENAI_API_KEY` (any non-empty value works for Ollama)
- Set `VITRO_OPENAI_BASE_URL` to the provider's `/v1` endpoint
- Set `VITRO_OPENAI_MODEL` to the model name the provider serves

---

## PDF Extraction Tool

The builder includes a PDF extraction tool (`extract_pdf_text`) that reads scientific publications and extracts structured content:

- **Text extraction** — all readable text content (via `pdfplumber`)
- **Table detection** — tables with ruling lines are extracted as pipe-delimited markdown tables; aligned columnar text appears as `[Text]` entries
- **Image metadata** — images are reported with their dimensions (width × height in points)
- **Structured output** — content is formatted with `[Page N]`, `[Text]`, `[Table N]`, and `[Image]` section markers so the LLM agent can understand document layout
- **Security guards** — files >100 MB are skipped; password-protected PDFs are rejected; invalid/corrupt PDFs return `None`

This is useful when a research folder contains PDFs of related publications that contain experimental data (IC₅₀ values, cell viability stats, dosing information). The agent can call `extract_pdf_text` on any PDF in the approved scan root to extract information for entity drafting.

```python
from builder.tools.scanner import extract_pdf_text

result = extract_pdf_text("/path/to/publication.pdf")
# Returns: "[Page 1]\n[Text] Abstract\n[Text] ...\n[Table 1 (3 rows)]\n| Compound | IC50 | Cell Line |\n| --- | --- | --- |\n| ..."
```
