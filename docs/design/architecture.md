# Architecture

> How the builder is put together: the one build, its initialization, the entity model, the validation layering and the RO-Crate packages it stands on.

## Architecture Overview

The ISA-Tox RO-Crate Builder is a **toolbox-based agent system** that assists researchers in creating profile-conformant RO-Crates for *in vitro* toxicology data.

> **One build: the ReAct agent loop.** `main.py --interactive` runs the ReAct loop
> over the toolbox (D15): the LLM orchestrates the tool calls and reads the deposit
> on demand. Its entrypoint, `run_build` (`builder/agents/build.py`, [the build entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy)), first
> reads the deposit's studies and assays from its folder tree once
> (`builder/agents/structure.py`, [deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)) and then hands the engine to the loop.

The builder gives an LLM agent a set of tools and lets it decide the order of operations based on the current state, rather than following a rigid pipeline with predefined steps.

### High-Level Flow

```
                         Agent Loop
   LLM Agent ◄─────────────────────────────────────
      │          Tools: draft_* (one per entity type), set_fields,
      │          remove_entity, lookup_*, verify_identifier,
      │          build_and_validate, export_crate, validate,
      │          assess_mit_coverage, assess_fair_maturity,
      │          present_to_human, request_input, save_session, get_status
      ▼
   ┌──────────────────────┐
   │    CrateState         │── persists between sessions
   │  (serializable)       │
   └──────────────────────┘

   Initialization (before agent loop):
   ┌──────────────────────────────────┐
   │  scan_files                      │
   │   → classify every scanned file  │ ──► agent loop
   │   → rank documents               │
   │   → read declared metadata       │
   └──────────────────────────────────┘

   Feedback loops:
   - Validation (MUST)  → agent re-drafts the failing entity
   - Validation (SHOULD)→ agent reports, re-drafts if data avail
   - HITL review        → agent incorporates feedback, continues
   - MIT/FAIR scores    → presented as optional improvements
```

### Toolbox, Not Graph

Within the agent loop, the agent is **not** guided by a predefined workflow graph. Instead it:

1. Examines the current `CrateState` (what's been done, what's missing, validation results)
2. Decides which tool to call next
3. Processes the result
4. Repeats until the crate is complete or escalates to the human

The orchestration is **emergent** — the agent dynamically routes itself based on context, feedback, and user input. A validation failure sends it back to drafting; an incomplete MIT score triggers more lookups.

Fixed initialization runs before the agent loop, in order:
1. `scan_files` — builds a raw file inventory (path, size, mime type, first rows). The scanner itself does **no role classification** — just a list of what's in the input directory.
2. Document discovery — stamps **every** scanned file with one classification (`metadata` / `protocol` / `raw_data_file` / `processed_data_file`), decided from content, and ranks the readable scientific documentation into `state.documents`. Classification reads content, so it stays a step beside the scanner and is never folded back into `scan_files`.
3. The declared-metadata read — the licence, DOI, title, accession and funding the deposit itself states, and the ORCID and ROR id it states for its people and organisations, are recorded on `metadata` (the licence with `license_from_deposit` set) before anything drafts one; the shallowest declaring document wins and a value already set is kept. The title is what DSM-1-R0 asks for (#722).

Steps 2 and 3 are best-effort: a failure leaves their state empty rather than aborting initialization. What the loop is handed is therefore the inventory, a classification per file, the ranked document context, and any declared licence, DOI, title, accession, funding, ORCID and ROR id — it never has to bootstrap those itself. Before the loop starts, `run_build` also reads the deposit's structure from its folder tree into `state.deposit_structure` ([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy), #852); a resumed session keeps the one it saved. The agent uses the inventory during entity drafting to bind files to `LabProcess` instances as annotations emerge. The crate's output layout is not scaffolded upfront — it is produced by `export_crate` once entity annotations are complete.

### Guard Rails: Approved Scan Roots
The agent's `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set that records user-confirmed paths. The guard **fails closed** (#197): with no approved roots, *nothing* is scannable. When the agent calls `scan_files(path)`:
1. The path is resolved to an absolute canonical form
2. If `approved_scan_roots` is empty (or the scanner receives `None`/empty `approved_roots`), the scan is **refused without walking** — the agent's own scan call never auto-approves a new root
3. It is checked against the approved set — if the target is not equal to, nor a subdirectory of, an approved root, scanning is denied
4. A hard denylist (`_is_forbidden_root`) refuses the filesystem root `/`, the user's home directory itself, and OS/system trees (`/System`, `/Library`, `/private`, `/var`, `/etc`, `/usr`, bare `/Users`, `/Volumes`) **even if such a path appears in `approved_scan_roots`**. Legitimate *subdirectories* (e.g. `~/Desktop/project`) are still allowed — only the bare roots are blocked
5. New roots are added **only** from a user-provided input path (`AgentEngine.initialize()`) or an explicit real approval — never from the agent's own scan call. The non-interactive `SimulatedHumanInterface` **denies** any `present(..., purpose="scan_root")` escalation, so it can never widen filesystem access on its own

The **same boundary now covers the file *read* and *write* tools** (#167), not just `scan_files`:
- **Reads.** `AgentEngine.run_tool` gates every file-reading tool — `read_file`, `read_excel`, `read_docx`, `read_file_sample`, `read_multiple_files`, `extract_pdf_text`, `preview_archive`, `unzip_file` — through the shared `scanner._contain(path, approved_roots)` helper. A path outside an approved root is refused before the file is opened; with no approved roots **every** read is refused (fail-closed). `read_multiple_files` filters out-of-root paths into its `skipped` list so an in-tree batch still works. This closes the prompt-injection vector where an injected metadata file made the agent read `~/.ssh/id_rsa`, `/etc/passwd`, or a `.env` of secrets.
- **Writes (export).** `_crate_mapping._file_dest` contains a File's `dest_path` to the crate output dir — an absolute path or a `..` that climbs out is refused and replaced with the safe `data/<slug>` fallback, so no payload byte is ever written outside the crate. `_file_source` refuses any source whose **realpath** escapes `input_path` (symlink-escape containment), so injection cannot package an arbitrary local file into the shareable crate.
- **Symlink escape.** `_contain` resolves the realpath before matching, so a symlink that lives inside an approved root but points outside it is refused for both reads and export sources. The `_is_forbidden_root` denylist also matches the *unresolved* path so symlinked OS trees (`/etc`→`/private/etc`, `/var`→`/private/var`) are still caught.

This prevents the LLM agent from reaching into arbitrary locations on the user's filesystem and provides a clear audit trail of which directories the system has ever accessed.

## Core Concepts

### Entity Model

Three-layer model mirroring the RO-Crate profile hierarchy:

| Layer | Description | Key Types |
|-------|-------------|-----------|
| **Packaging** | RO-Crate 1.2 base | `Dataset`, `File`, `Person`, `Organization` |
| **Structural** | ISA hierarchy | `Investigation`, `Study`, `Assay`, `LabProcess`, `LabProtocol`, `Sample` |
| **Domain** | Toxicology extension | `MolecularEntity`, `CellLineSample` (cell lines and primary cells, told apart by `source_kind`; D16), `LabProcessExposure`, `LabProcessEndpointReadout`, `LabProcessTestSystemPreparation`, `LabProcessDataAnalysis` |

### Entity Provenance

> **Status: the entity-level `status` ladder below is declared but not implemented.**
> The `EntityStatus` enum carries exactly these four values and is exported from
> `builder`, but nothing assigns or reads it — an `Entity` carries only the
> per-field completion and the provenance record.

Every entity tracks:
- `status`: `draft` | `enriched` | `reviewed` | `verified`
- `_provenance.created_by`: `scanner` | `llm` | `user` | `lookup`
- `_provenance.reviewed_by`: `user` | `null` — declared, but no code writes it
  today ([human-in-the-loop](sessions-and-hitl.md#human-in-the-loop-hitl))
- `_provenance.lookups_used`: the lookup services that contributed data
- `_completion`: per-field status (`missing` | `filled` | `verified`), keyed
  `"{type}:{field}"`

This enables session resume, quality tracking, and audit.

### Completion Model

Completion is tracked at the **field level** using `mit/invitro_tox.yaml` as reference. Each parameter's `crate_slot` mapping defines expected fields per entity type.

```
Entity: MolecularEntity (Compound)
├── name: "Silychristin A"              filled, verified
├── identifier: (CAS) "33889-69-9"     filled, needs review
├── inChIKey: missing                   missing
├── smiles: missing                     missing

MIT Module: Chemical Information
├── Compound name: ✓
├── CAS Registry Number: ✓
├── Structural formula: ✗
├── Purity: ✗
└── Score: completed / total
```

A module's denominator is its **scorable** parameters — the ones carrying a
`crate_slot`. A parameter with no parseable slot has nothing a matcher could
ever match, so counting it would depress every score permanently; it is excluded
from every denominator. The checklist fixes those counts, so no count is
hardcoded here.

## Agent Priority Heuristic (Work in Layers)

The agent's toolbox architecture means it is **not** guided by a predefined sequence of steps.
However, the agent should follow a **priority heuristic** that ensures users see tangible
progress quickly and that the validation hierarchy is respected:

```
Priority 1  →  A valid, minimal RO-Crate (base RO-Crate conformance)
Priority 2  →  ISA structural completeness (Investigation → Study → Assay → Process)
Priority 3  →  ISA-Tox domain extensions (MolecularEntity, CellLineSample, etc.)
Priority 4  →  MIT / FAIR scoring and enrichment
```

The key principle is **get to a buildable, validatable crate as fast as possible**.
A half-filled crate that passes base RO-Crate validation is worth more than a
perfectly detailed crate that doesn't build at all.

## Validation Gate Ordering

The three profile passes in `profiles/validator.py` are **independent**: a
`profile="all"` sweep runs the pass of every layer the crate claims (base, ISA and
ISA-Tox, or base and ISA for an ISA crate, D18), fanned out to a process
pool unless `VITRO_VALIDATE_SERIAL=1` forces the serial path. The ordering below
is a **fix priority for the agent**, not a gate in the validator:
- **Base RO-Crate 1.2** issues are structural — clearing them is what makes the
  ISA and ISA-Tox findings meaningful
- **ISA Profile** issues come next
- **ISA-Tox Profile** rests on both lower layers being sound

If the agent tries to validate and gets `base_passed: false`, there is no point
fixing ISA-Tox issues until the crate builds. The agent should:

1. Call `build_and_validate` early, even with minimal entities — it assembles
   and validates **in memory** and writes nothing, so it is safe every iteration
   (`export_crate`/`build_crate` is the only disk-writing step; see [the toolbox](toolbox.md) for how
   often the loop calls it)
2. Fix base RO-Crate issues first (structural integrity)
3. Then iterate on ISA issues
4. Then iterate on ISA-Tox issues
5. Report MIT/FAIR scores last, as optional improvements

This avoids the common pitfall of perfecting one entity type before verifying
the crate can assemble at all.

## Core Components

The components the ReAct loop drives, through [the toolbox](toolbox.md) and the
initialization that runs before it ([architecture overview](#architecture-overview)):

| Component | Code | Design |
|---|---|---|
| Scanner | `builder/tools/scanner.py` | [initialization](initialization.md#scanner-buildertoolsscannerpy) |
| File classification & document discovery | `builder/tools/document_discovery.py` | [initialization](initialization.md#file-classification--document-discovery-buildertoolsdocument_discoverypy) |
| Deposit structure | `builder/tools/deposit_structure.py`, `builder/agents/structure.py` | [initialization](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy) |
| Entity drafters | `builder/tools/drafters.py` | [building](building.md#entity-drafters-buildertoolsdrafterspy) |
| Crate builder | `builder/tools/builder.py` | [building](building.md#crate-builder-buildertoolsbuilderpy) |
| Build-path wiring contracts | `builder/tools/_crate_mapping.py` | [building](building.md#build-path-wiring-contracts-buildertools_crate_mappingpy) |
| Validator | `builder/tools/validation.py`, `profiles/validator.py` | [validation](validation.md) |
| Maturity assessors | `builder/tools/{mit,fair,air}_assessment.py` | [assessments](assessments.md) |
| Maturity report & entity explorer | `builder/writers/` | [report](report.md) |

## External RO-Crate Packages

This project builds on the existing RO-Crate Python ecosystem rather than reinventing crate assembly, validation, or entity models:

| Package | PyPI | What it provides | How we use it |
|---------|------|-----------------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) | `uv add rocrate`<br>(import `rocrate`) | Official Python SDK for creating and manipulating RO-Crates. Provides `ROCrate`, `ContextEntity`, `File`, and other base entity classes. | The entity model classes in `profiles/models/isa.py` and `profiles/models/tox.py` subclass `rocrate.model.ContextEntity` and `rocrate.model.File`. The builder uses `ROCrate` to assemble the crate and serialise `ro-crate-metadata.json`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) | `uv add roc-validator`<br>(import `rocrate_validator`) | Official SHACL-based validation library. Supports multi-profile validation (base RO-Crate → ISA → domain extensions) with severity levels. | `profiles/validator.py` wraps this in three passes (RO-Crate 1.2, ISA, ISA-Tox), suppressing inherited-profile duplicates so each pass reports only its own layer. |
| [`rocrate-wizard`](https://github.com/johannehouweling/rocrate-wizard) *(external frontend)* | TBD | Frontend/UI layer that uses this backend (vitro-crate) to provide a user-facing RO-Crate builder. | This repo is the dependency — `rocrate-wizard` imports from `vitro-crate` and adds the web UI/CLI on top. |

These packages are imported directly — we do not fork or vendor them. Version requirements are declared in `pyproject.toml`.

## Future Considerations

Extension points the current design leaves open (not yet built): registering
external **MCP** servers as additional tools (the toolbox is MCP-ready);
**multi-user** provenance (the model is single-user today); a **Web API / frontend**
over the builder library (FastAPI/Streamlit call in unchanged); runtime-loaded
**custom profiles** (schemas are YAML); and **batch processing** (state is
per-session, so parallel runs are straightforward).

---

*This document is a living design artifact. Update as architectural decisions evolve.*
