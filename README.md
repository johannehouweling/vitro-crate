# vitro-crate

Builds ISA-Tox RO-Crates from in vitro toxicology data. An agent reads a data
folder, drafts the ISA metadata, resolves identifiers against public registries,
validates the crate against the RO-Crate, ISA and ISA-Tox profiles, and writes it
with a maturity report.

[![CI](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml/badge.svg)](https://github.com/johannehouweling/vitro-crate/actions/workflows/ci.yml)

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/johannehouweling/vitro-crate
cd vitro-crate
uv sync --extra langchain
uv run python -m main --configure   # OpenAI, an OpenAI-compatible endpoint, or Anthropic
```

## Build a crate

```bash
# With you at the keyboard: the agent asks when the deposit does not say.
uv run python -m main --interactive -i path/to/data

# With nobody at the keyboard: nothing is asked, what the deposit does not state
# stays empty and is reported, and the crate is written.
uv run python -m main --unattended -i path/to/data -o path/to/crate
```

An unattended run keeps the questions it could not ask. It ends by printing the
command that puts them to you:

```bash
uv run python -m main --resume <session id> --interactive
```

Identifiers come only from lookups (PubChem, the Compound Wiki, Cellosaurus, ORCID,
ROR, Crossref, AOP-Wiki, OLS), never from the model.

## What you get

The crate is written to `output/<folder>_crate/` (or `-o`):

- `ro-crate-metadata.json` and the data files;
- `ro-crate-preview.html`;
- `ro-crate-metadata-maturity.html`: profile conformance, FAIR maturity and MIT
  coverage, with an entity explorer.

`--metadata-only` writes the crate without its data files; `--profile isa` builds a
study without an exposure as an ISA crate.

A crate conforms to ISA-Tox 1.1 only when every person in it is identified by an ORCID
iD and every organisation by a ROR ID; a funder that ROR does not register, such as a
funding programme, is a recommendation rather than a failure. An EU grant number the
deposit states is looked up in [CORDIS](https://cordis.europa.eu), which makes the
European Commission its funder. The builder looks identifiers up and never invents
one, so whoever it cannot confirm is listed at the end of the build as needing an
identifier from you. Every other identifier the crate states (a CAS number, a PubChem
or Cellosaurus address, a DOI, an ontology term) must have the form the
[Bioregistry](https://bioregistry.io) gives it.

## More

| Task | Command |
|---|---|
| Watch a running build | `uv run python -m main --dashboard` |
| Explore an existing crate | `uv run python -m main --graph -i path/to/crate` |
| Show the configuration | `uv run python -m main --show-config` |

- [docs/reference.md](docs/reference.md): every option, environment variable and
  configuration key
- [AGENTS.md](AGENTS.md): rules for working on the code, and a map of the design in [docs/design/](docs/design/) and [docs/adr/](docs/adr/)
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and conventions

Released under the [MIT License](LICENSE). Copyright © 2026 Jente Houweling.
