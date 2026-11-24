# Architecture

> How the builder is put together: the one build, its initialization, the entity model, the validation layering and the RO-Crate packages it stands on.

## Architecture Overview

A **toolbox-based agent system** that builds profile-conformant RO-Crates for *in vitro*
toxicology data. There is one build, the ReAct loop (D15): `run_build`
(`builder/agents/build.py`, [the build entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy))
reads the deposit's studies and assays from its folder tree once
([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy),
#852; a resumed session keeps the one it saved) and hands the engine to the loop.

```
   LLM Agent ◄── tools: draft_*, set_fields, lookup_*, verify_identifier,
      │          build_and_validate, export_crate, assess_*, present_to_human, ...
      ▼
   CrateState (serializable) ── persists between sessions

   Initialization: scan_files → classify every file → rank documents
                   → read declared metadata ──► agent loop
```

Within the loop no workflow graph guides the agent: it reads `CrateState`, picks the next
tool, and repeats until the crate is complete or it escalates to the human (D1). A
validation failure sends it back to drafting.

Fixed initialization runs first, in order:

1. `scan_files` — the raw inventory (path, size, mime type, first rows). The scanner does
   **no classification**.
2. Document discovery — stamps **every** scanned file with one classification and ranks the
   readable documentation into `state.documents`. It reads content, so it stays a step
   beside the scanner and is never folded into `scan_files`.
3. The declared-metadata read — the licence, DOI, title, accession and funding the deposit
   states, and the ORCID / ROR id it states for its people and organisations, are recorded
   on `metadata` (the licence with `license_from_deposit`) before anything drafts one; the
   shallowest declaring document wins and a value already set is kept (title: DSM-1-R0, #722).

Steps 2 and 3 are best-effort: a failure leaves their state empty rather than aborting. The
output layout is not scaffolded upfront; `export_crate` produces it.

### Guard Rails: Approved Scan Roots

Filesystem access is limited to `CrateState.approved_scan_roots` and **fails closed**
(#197); the full rule set is [D9](../adr/D9-approved-scan-roots-security-guard-rail.md).

- `scan_files(path)` resolves the path and refuses, without walking, when there are no
  approved roots or the path is not equal to or under one.
- `_is_forbidden_root` refuses `/`, the home directory itself and OS trees (`/System`,
  `/Library`, `/private`, `/var`, `/etc`, `/usr`, bare `/Users`, `/Volumes`) **even if
  approved**; subdirectories such as `~/Desktop/project` are allowed. It also matches the
  unresolved path, so symlinked OS trees (`/etc`→`/private/etc`) are caught.
- Roots are added **only** from the user's input path (`AgentEngine.initialize()`) or a real
  approval — never from the agent's own scan. `SimulatedHumanInterface` denies every
  `purpose="scan_root"` escalation.
- **Reads (#167).** `AgentEngine.run_tool` gates `read_file`, `read_excel`, `read_docx`,
  `read_file_sample`, `read_multiple_files`, `extract_pdf_text`, `preview_archive` and
  `unzip_file` through `scanner._contain(path, approved_roots)`; with no roots every read is
  refused. `read_multiple_files` moves out-of-root paths to `skipped`. This closes the
  prompt-injection read of `~/.ssh/id_rsa`, `/etc/passwd` or a `.env`.
- **Writes.** `_crate_mapping._file_dest` replaces an absolute or `..` `dest_path` with
  `data/<slug>`; `_file_source` refuses a source whose realpath escapes `input_path`.
- `_contain` resolves the realpath first, so a symlink inside a root pointing outside it is
  refused for reads and export sources alike.

## Core Concepts

### Entity Model

| Layer | Description | Key Types |
|-------|-------------|-----------|
| **Packaging** | RO-Crate 1.2 base | `Dataset`, `File`, `Person`, `Organization` |
| **Structural** | ISA hierarchy | `Investigation`, `Study`, `Assay`, `LabProcess`, `LabProtocol`, `Sample` |
| **Domain** | Toxicology extension | `MolecularEntity`, `CellLineSample` (cell lines and primary cells, told apart by `source_kind`; D16), `LabProcessExposure`, `LabProcessEndpointReadout`, `LabProcessTestSystemPreparation`, `LabProcessDataAnalysis` |

### Entity Provenance

Every entity carries `_provenance.created_by` (`scanner` | `llm` | `user` | `lookup`),
`_provenance.lookups_used`, and `_completion`: per-field status (`missing` | `filled` |
`verified`) keyed `"{type}:{field}"`. Declared but unused: the `EntityStatus` ladder
(`draft` | `enriched` | `reviewed` | `verified`) is assigned and read by nothing, and
`_provenance.reviewed_by` has no writer
([human-in-the-loop](sessions-and-hitl.md#human-in-the-loop-hitl)).

### Completion Model

Completion is tracked per field against `mit/invitro_tox.yaml`; each parameter's `crate_slot`
names the field it expects. A module's denominator is its **scorable** parameters — those with
a `crate_slot` — because a parameter with no parseable slot could never be matched and would
depress every score permanently. No count is hardcoded here.

## Agent Priority Heuristic (Work in Layers)

```
Priority 1  →  A valid, minimal RO-Crate (base RO-Crate conformance)
Priority 2  →  ISA structural completeness (Investigation → Study → Assay → Process)
Priority 3  →  ISA-Tox domain extensions (MolecularEntity, CellLineSample, etc.)
Priority 4  →  MIT / FAIR scoring and enrichment
```

Get to a buildable, validatable crate as fast as possible: a half-filled crate that passes base
validation is worth more than a detailed one that does not build.

## Validation Gate Ordering

The profile passes in `profiles/validator.py` are **independent**: `profile="all"` runs every
layer the crate claims (D18), on a process pool unless `VITRO_VALIDATE_SERIAL=1`. The order is
a **fix priority for the agent**, not a validator gate: call `build_and_validate` early (it
assembles and validates **in memory** and writes nothing; `export_crate` is the disk writer,
see [the toolbox](toolbox.md)), fix base issues first, then ISA, then ISA-Tox, and report
MIT/FAIR last. While `base_passed` is false, ISA-Tox issues are not worth fixing.

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

Imported directly, never forked or vendored; versions are in `pyproject.toml`.

| Package | Import | How we use it |
|---------|--------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) (`rocrate`) | `rocrate` | `profiles/models/{isa,tox}.py` subclass `ContextEntity` / `File`; the builder assembles and serialises with `ROCrate`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) (`roc-validator`) | `rocrate_validator` | `profiles/validator.py` wraps it in three passes (RO-Crate 1.2, ISA, ISA-Tox), suppressing inherited duplicates so each pass reports its own layer. |
| [`rocrate-wizard`](https://github.com/johannehouweling/rocrate-wizard) | — | External frontend that imports vitro-crate and adds the web UI/CLI. |

## Future Considerations

Open extension points, not built: external **MCP** servers as tools, **multi-user** provenance,
a **Web API / frontend** over the library, runtime-loaded **custom profiles**, and **batch
processing** (state is per-session).
