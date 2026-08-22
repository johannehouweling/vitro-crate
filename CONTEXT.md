# ISA-Tox RO-Crate Builder — Domain Context

> **Purpose:** Domain primer for AI agents working on this project. Provides the problem-domain language, key concepts, stakeholders, and terminology needed to make informed design and debugging decisions.
>
> **Status:** Initial scaffold — populate as the project evolves.

## What is this project?

An LLM-assisted tool that helps researchers create profile-conformant RO-Crates for *in vitro* toxicology data.

## Key domain concepts

- **RO-Crate** — a packaging format for research data with structured metadata (JSON-LD based).
- **ISA-Tox** — an extension of the ISA (Investigation-Study-Assay) framework for toxicology data.
- **CrateState** — the central data model that tracks entities, completion status, and validation results.
- **MIT (Minimum Information for in-vitro Toxicology)** — a checklist of required/recommended fields for reporting toxicology data; each item is a FAIR maturity indicator of the [tox-maturity-indicators](https://github.com/invitro-crate/tox-maturity-indicators) package (vendored copy in `mit/invitro_tox.yaml`; #313 tracks importing it).
- **FAIR indicators** — metrics for Findability, Accessibility, Interoperability, and Reusability. FAIR maturity here is two-part: the RDA FDMM indicators in `fair/indicators.yaml` (generated — do not edit by hand) and the FAIRplus Data Stewardship Maturity (DSM) ladder in `fair/dsm_indicators.yaml`.

## Stakeholders

- **Researchers (primary users)** — domain scientists who need to package their *in vitro* toxicology data into standardised RO-Crates.
- **LLM agents** — the automated system that guides users through crate creation.
- **Reviewers / data stewards** — validate crate completeness and compliance.

## Glossary

| Term | Definition |
|------|------------|
| RO-Crate | Research Object Crate — a lightweight packaging format for research data |
| ISA | Investigation-Study-Assay — a framework for describing experimental workflows |
| MolecularEntity | A compound or chemical substance used in an assay |
| CellLineSample | A cell line used in an *in vitro* experiment |
| LabProcess | A step in the experimental workflow (cell culture, exposure, endpoint readout, data analysis) |
| SHACL | Shapes Constraint Language — used for validating RDF graphs against profiles |
| HITL | Human-in-the-Loop — checkpoints where the agent asks the user for input |
| AOP | Adverse Outcome Pathway — a structured representation of toxicological processes |
| BAO | BioAssay Ontology — ontology for assay descriptions |
| Cellosaurus | A knowledge resource on cell lines |
| ToxTemp | Toxicological test method template — a structured description of a cell-based *in vitro* test method (Krebs et al. 2019, ALTEX 36(4):682–699, doi:10.14573/altex.1909271); one per assay, and one of the source standards MIT parameters are tagged with (`toxtemp` in `mit/invitro_tox.yaml`), alongside OECD GD 211 and GD 34 |

## Related files

- `AGENTS.md` — system design document (architecture, components, tools)
- `profiles/` — domain profiles (ISA, ISA-Tox) with schemas and SHACL shapes
- `lookups/` — external API clients (PubChem, Cellosaurus, AOP-Wiki, etc.)
- `mit/invitro_tox.yaml` — Minimum Information for in-vitro Toxicology checklist
- `fair/indicators.yaml` — RDA FDMM FAIR indicators. Generated from the vendored `fair/rda_fdmm.xlsx`: never edit it by hand, regenerate with `uv run python scripts/gen_fair_indicators.py`
- `fair/dsm_indicators.yaml` — FAIRplus Data Stewardship Maturity (DSM) ladder (hand-curated; no machine-readable upstream)
- `builder/state.py` — CrateState dataclass (the central data model)
- `builder/engine.py` — AgentEngine orchestrator (runs tools, manages state)
- `builder/agents/` — both build arms plus their shared layer (`build.py` dispatches on the selected mode; `llm.py`, `ui.py`)
- `builder/agents/pipeline/` — the deterministic, code-orchestrated build arm (the `--interactive` default): `pipeline`, `guidance`, `leaves`
- `builder/agents/react/` — the LangChain/LangGraph ReAct arm (`--react`): `agent_loop`, `SYSTEM_PROMPT` in `system_prompt.py`, `TOOL_SPECS` in `tools_spec.py` (kept in parity with the shared engine by `assert_tool_spec_parity`)
- `builder/tools/` — All tool implementations (scanner, drafters, lookups, builder, validation, assessment, session)
- `builder/config.py` — Persistent config (`~/.config/vitro-crate/config.toml`) for LLM provider settings