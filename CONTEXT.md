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
- **MIT (Minimum Information for Toxicology)** — a checklist of required/recommended fields for reporting toxicology data. See `mit/invitro_tox.yaml`.
- **FAIR indicators** — metrics for Findability, Accessibility, Interoperability, and Reusability. See `fair/indicators.yaml`.

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

## Related files

- `AGENTS.md` — system design document (architecture, components, tools)
- `profiles/` — domain profiles (ISA, ISA-Tox) with schemas and SHACL shapes
- `lookups/` — external API clients (PubChem, Cellosaurus, AOP-Wiki, etc.)
- `mit/invitro_tox.yaml` — Minimum Information for Toxicology checklist
- `fair/indicators.yaml` — FAIR maturity indicators
- `builder/state.py` — CrateState dataclass (the central data model)
- `builder/engine.py` — AgentEngine orchestrator (runs tools, manages state)
- `builder/agents/` — LangChain agent loop with TOOL_SPECS + system prompt
- `builder/tools/` — All tool implementations (scanner, drafters, lookups, builder, validation, assessment, session)
- `builder/config.py` — Persistent config (`~/.config/vitro-crate/config.toml`) for LLM provider settings