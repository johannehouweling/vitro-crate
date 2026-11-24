# Domain Docs

## Layout

Single-context. There is one `CONTEXT.md` at the repo root and (optionally) one `docs/adr/` directory for architectural decision records.

## Consumer rules

- **`CONTEXT.md`** — read this first to understand the project's domain language, key concepts, stakeholders, and glossary. Skills such as `improve-codebase-architecture` consult this file before making design or debugging decisions.
- **`docs/adr/`** — consult for past architectural decisions. Each record is one markdown file named by its decision id (`D5-…`), stating the decision and its reasoning; `docs/adr/README.md` indexes them. Skills that propose structural changes (`improve-codebase-architecture`) cross-reference ADRs to avoid revisiting settled decisions.
- **`AGENTS.md`** — the rules for working on the code and a map of the design. The design itself (components, data model, tools, the build loop) is in `docs/design/`.

## Notes

- `AGENTS.md` and `docs/design/` are used alongside `CONTEXT.md` — the former cover build-system internals, the latter covers the domain (*in vitro* toxicology, ISA-Tox profiles, RO-Crate concepts).