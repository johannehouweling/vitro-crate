# Domain Docs

## Layout

Single-context. There is one `CONTEXT.md` at the repo root and (optionally) one `docs/adr/` directory for architectural decision records.

## Consumer rules

- **`CONTEXT.md`** — read this first to understand the project's domain language, key concepts, stakeholders, and glossary. Skills such as `improve-codebase-architecture` consult this file before making design or debugging decisions.
- **`docs/adr/`** — consult for past architectural decisions. Each ADR is a markdown file with a title, status, context, decision, and consequences. Skills that propose structural changes (`improve-codebase-architecture`) cross-reference ADRs to avoid revisiting settled decisions.
- **`AGENTS.md`** — system design document for the LLM-assisted RO-Crate builder architecture. Describes components, data model, tools, and pipeline design.

## Notes

- This repo does not yet have a `docs/adr/` directory. It will be consulted automatically once created.
- The `AGENTS.md` file is used alongside `CONTEXT.md` — the former covers build-system internals, the latter covers the domain (*in vitro* toxicology, ISA-Tox profiles, RO-Crate concepts).