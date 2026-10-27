# vitro-crate

Turn a folder of in vitro toxicology data into an **ISA-Tox RO-Crate**: your data
files and their metadata, validated and packaged in one folder.

[![CI](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml/badge.svg)](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml)

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

1. **Install**

   ```bash
   git clone https://github.com/johannehouweling/vitro-crate && cd vitro-crate
   uv sync --extra langchain
   ```

2. **Connect a model**: OpenAI, an OpenAI-compatible server such as Ollama, or Anthropic.

   ```bash
   uv run python -m main --configure
   ```

   Your choice is saved in `~/.config/vitro-crate/config.toml`.

3. **Build a crate**

   ```bash
   uv run python -m main --interactive --react -i path/to/your-data --prompt "build the crate"
   ```

## How a build works

With `--react`, an agent reads your folder and decides each next step itself. It:

- reads the folder: READMEs, protocols, metadata sheets and data files;
- drafts the investigation, studies, assays, samples, chemicals and process steps;
- looks identifiers up (PubChem, CompTox, Cellosaurus, ORCID, Crossref, AOP-Wiki)
  instead of inventing them;
- validates against the RO-Crate, ISA and ISA-Tox profiles and fixes what it can;
- asks you when it needs a decision;
- writes the crate.

It works on its own and checks back when it needs you. You can also type an
instruction at any time:

> *"Draft a cell culture process for HepG2 cells"*  
> *"Look up Silychristin A on PubChem"*  
> *"What is still missing?"*  
> *"Build the crate and validate it"*

When it asks something, pick an option with ↑/↓ and Enter, or choose *Something
else* to type your own answer. Ctrl+D ends the session.

Leave out `--prompt` and the agent greets you and waits for your first
instruction. Leave out `-i` to start a conversation without a data folder.

## What you get

A folder `output/<your folder name>_crate/`. A re-run writes `_v2`, `_v3` and so on,
so nothing is overwritten; `-o` picks another place. Inside:

- `ro-crate-metadata.json`, next to your data files;
- `ro-crate-metadata-maturity.html`: conformance, FAIR maturity and MIT coverage,
  plus an explorer of everything in the crate. It is one self-contained page that
  opens offline in any browser.

## Handy commands

| To | Run |
|---|---|
| Watch a running build (in a second terminal) | `uv run python -m main --dashboard` |
| Continue a session | `uv run python -m main --interactive --react --resume <session id>` |
| Explore a crate you already have | `uv run python -m main --graph -i path/to/crate` |
| Use another model for one run | add `--model gpt-4o-mini` (or `--provider`, `--api-base`) |
| Check your configuration | `uv run python -m main --show-config` |

The session id is shown at the start and the end of every run.

## The other build mode

Without `--react`, `--interactive` runs the **deterministic pipeline**: code runs the
steps in a fixed order, and the model only fills bounded pieces. It is cheaper and
reproducible. It fills metadata from the folder's own documents and asks only about
required fields. Both modes share the same tools and are maintained side by side.

```bash
uv run python -m main --interactive -i path/to/your-data
```

## Configuration

Settings live in `~/.config/vitro-crate/config.toml`. Environment variables override
them:

```bash
export VITRO_OPENAI_API_KEY="sk-..."
export VITRO_OPENAI_MODEL="gpt-4o"
```

For a local model with [Ollama](https://ollama.com): set
`VITRO_OPENAI_BASE_URL=http://localhost:11434/v1`, any non-empty
`VITRO_OPENAI_API_KEY`, and `VITRO_OPENAI_MODEL` to a tool-calling model such as
`llama3.2`.

## More

- [docs/reference.md](docs/reference.md): every command-line option, environment
  variable and config key; corporate certificates; smoke tests; the PDF tool
- [AGENTS.md](AGENTS.md): system design
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and conventions
- [docs/profiling.md](docs/profiling.md): logs and profiling

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Jente Houweling.
