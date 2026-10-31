# vitro-crate

vitro-crate builds ISA-Tox RO-Crates from in vitro toxicology data. It reads a data
folder, drafts the ISA metadata, resolves identifiers against public registries,
validates the result against the RO-Crate, ISA and ISA-Tox profiles, and writes the
crate together with a maturity report.

[![CI](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml/badge.svg)](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml)

## Installation

Requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/johannehouweling/vitro-crate
cd vitro-crate
uv sync --extra langchain
```

Configure a model provider (OpenAI, an OpenAI-compatible endpoint such as Ollama, or
Anthropic):

```bash
uv run python -m main --configure
```

The configuration is stored in `~/.config/vitro-crate/config.toml`.

## Usage

```bash
uv run python -m main --interactive -i path/to/data
```

The default build mode is a ReAct agent: a language model selects each next tool call
from the current state of the crate. During a build it:

1. reads the folder's documentation, protocols, metadata sheets and data files;
2. drafts the investigation, studies, assays, samples, chemicals and process steps;
3. resolves identifiers through PubChem, CompTox, Cellosaurus, ORCID, ROR, Crossref,
   AOP-Wiki and the BioAssay Ontology;
4. validates the crate and repairs the issues it can;
5. asks for input when a decision requires it;
6. exports the crate.

With `-i`, the agent starts building at once; without it, the session starts with an
empty crate and waits for an instruction. Instructions can be entered at any time
during the session. A question is answered by selecting an option or by typing an
answer. Ctrl+D ends the session.

## Output

The crate is written to `output/<input folder name>_crate/`. Later builds of the same
folder are versioned (`_v2`, `_v3`, …); `-o` selects another location. The crate
contains:

- `ro-crate-metadata.json` and the data files;
- `ro-crate-preview.html`: the RO-Crate community's HTML preview when its generator
  is installed (`npm install -g ro-crate-html@0.1.6`), ro-crate-py's own page
  otherwise;
- `ro-crate-metadata-maturity.html`: a self-contained report on profile conformance,
  FAIR maturity and MIT coverage, with an entity explorer. It needs no network access.

## Build modes

| Mode | Command | Description |
|---|---|---|
| ReAct agent (default) | `--interactive` | The model chooses each step. |
| Deterministic pipeline | `--interactive --pipeline` | Code runs a fixed sequence of steps and the model fills bounded parts. Lower cost and reproducible; it asks only for required fields the folder does not state. |

Both modes use the same tools and are maintained in parallel.

## Commands

| Task | Command |
|---|---|
| Monitor a running build | `uv run python -m main --dashboard` |
| Resume a session | `uv run python -m main --interactive --resume <session id>` |
| Explore an existing crate | `uv run python -m main --graph -i path/to/crate` |
| Override the model for one run | `--model`, `--provider`, `--api-base` |
| Show the configuration | `uv run python -m main --show-config` |

The session identifier is printed at the start and end of each run.

## Configuration

Environment variables override the configuration file, for example:

```bash
export VITRO_OPENAI_API_KEY="sk-..."
export VITRO_OPENAI_MODEL="gpt-4o"
```

To use a local model with [Ollama](https://ollama.com), set
`VITRO_OPENAI_BASE_URL=http://localhost:11434/v1`, a non-empty `VITRO_OPENAI_API_KEY`,
and `VITRO_OPENAI_MODEL` to a model with tool calling, such as `llama3.2`.

## Documentation

- [docs/reference.md](docs/reference.md): command-line options, environment variables,
  configuration keys, corporate certificates, smoke tests and the PDF extraction tool
- [AGENTS.md](AGENTS.md): system design
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and conventions
- [docs/profiling.md](docs/profiling.md): logging and profiling

## License

Released under the [MIT License](LICENSE). Copyright © 2026 Jente Houweling.
