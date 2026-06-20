# PRD: LLM-Assisted ISA-Tox RO-Crate Builder Backend

## Problem Statement

Toxicology researchers generate *in vitro* data that must be packaged as FAIR, profile-conformant RO-Crates for submission to public repositories. Currently, creating an ISA-Tox RO-Crate requires manual metadata assembly across multiple tools (PubChem lookups, Cellosaurus queries, SHACL validation, MIT coverage checks). This process is time-consuming, error-prone, and requires expertise in both toxicology semantics and RO-Crate internals.

Researchers lack a guided, interactive assistant that can:
- Accept diverse input formats (raw directories, research folders with metadata files, conversational descriptions)
- Automatically enrich metadata via authoritative lookups (PubChem, Cellosaurus, AOP-Wiki, ORCID, ROR, Crossref)
- Validate against the three-tier ISA-Tox profile (MUST / SHOULD / MAY)
- Score against the Minimum Information for *in vitro* Toxicology (MIT) and FAIR maturity indicators
- Produce a fully-assembled RO-Crate with traceable provenance for every field

## Solution

An LLM-agent-assisted builder backend that uses a **toolbox architecture** rather than a rigid pipeline. The agent (LLM) dynamically decides which tools to call based on the current `CrateState` — the single source of truth that tracks every entity, field completion status, validation results, and session progress.

The system:
1. Scans input to build a raw file inventory (path, size, mime type, first rows) — no role classification, no ARC sorting, just what's in the input — and seeds an initial state
2. Drafts entities (Investigation, Study, Assay, MolecularEntity, CellLineSample, LabProcess types, People, Organizations, Publications) using LLM calls backed by verified lookups
3. Validates continuously against the three-pass SHACL profile — MUST issues block progress, SHOULD/MAY issues guide improvement
4. Scores MIT coverage (per-module completion percentages) and FAIR maturity (indicator-level pass/fail with DSM level)
5. Lets the user review, edit, or reject at checkpoints via Human-in-the-Loop (HITL)
6. Persists session state for half-finished crates to be resumed later
7. Produces a final, valid RO-Crate with full provenance tracking

## User Stories
1. As a toxicology researcher, I want to use natural language to describe my experiment, so that the builder can draft the RO-Crate entities for me without requiring technical knowledge of RO-Crate or JSON-LD.
2. As a toxicology researcher, I want to point the builder at a raw data directory, so that the builder scans the folder and I can describe the experiment conversationally.
3. As a toxicology researcher, I want to enter a DOI, so that the builder fetches publication metadata from Crossref and populates the ScholarlyArticle entity.
4. As a toxicology researcher, I want to enter a compound name, so that the builder looks up SMILES, InChIKey, formula, and CAS from PubChem and fills the MolecularEntity.
5. As a toxicology researcher, I want to provide a Cellosaurus accession (CVCL_xxxx), so that the builder resolves species, organ, tissue, and sex for the cell line sample.
6. As a toxicology researcher, I want to reference an AOP-Wiki ID, so that the builder resolves the AOP graph (key events, relationships) as DefinedTerm annotations.
7. As a toxicology researcher, I want to look up an ontology term (e.g., BAO query), so that the builder finds the correct IRI for measurement methods and techniques.
8. As a toxicology researcher, I want to enter a person's ORCID, so that the builder resolves name, affiliation, and ROR from ORCID/ROR APIs.
9. As a toxicology researcher, I want the agent to never fabricate identifiers, so that I can trust every ID in the crate is verified at its source.
10. As a toxicology researcher, I want to review each drafted entity before it's committed, so that I can approve, edit, or reject the agent's work.
11. As a toxicology researcher, I want to see a validation report after drafting, so that I know which REQUIRED fields are still missing and what SHOULD/MAY improvements are available.
12. As a toxicology researcher, I want the builder to block crate assembly if there are any REQUIRED validation failures, so that I never produce an invalid RO-Crate.
13. As a toxicology researcher, I want to see MIT coverage scores per module (Chemical Information, Biological Model Information, General Information), so that I can prioritize filling gaps.
14. As a toxicology researcher, I want to see FAIR maturity scores, so that I understand how findable, accessible, interoperable, and reusable my crate is.
15. As a toxicology researcher, I want to save my session mid-way, so that I can resume work later without losing progress.
16. As a toxicology researcher, I want the builder to auto-save after key milestones (scan, draft, validate, HITL), so that I never lose work.
17. As a toxicology researcher, I want to resume a saved session, so that the agent picks up from its last checkpoint with full context.
18. As a toxicology researcher, I want to run validation on the assembled crate before finalization, so that I catch any issues introduced during assembly.
19. As a toxicology researcher, I want the agent to detect when it's stuck and escalate to me for guidance.
20. As a toxicology researcher, I want the final output to be an ARC directory (which *is* the RO-Crate), so that the `ro-crate-metadata.json` at its root describes every file already organized in studies, assays, datasets, and protocols.
21. As a developer integrating this builder, I want to understand the toolbox interface, so that I can add new tools or input readers without changing the agent loop.
22. As a developer extending the system, I want field-level completion metadata on every entity, so that I can build custom UIs that show exactly what's missing.
## Implementation Decisions

### Architecture: Toolbox, Not Graph
The LLM agent is given a set of tools (file scanning, entity drafting, lookups, validation, assessment, session management) and decides the order of calls based on the current `CrateState`. No predefined workflow graph. Validation failures and HITL feedback can route the agent to any earlier stage. Max iterations and HITL escalation prevent infinite loops.

### CrateState: The Single Source of Truth
All state lives in a serializable `CrateState` dataclass. It tracks: session metadata, all entities by type with field-level completion (`_completion`) and provenance (`_provenance`), scanned file classifications, three-pass validation results with REQUIRED/SHOULD/MAY separation, MIT per-module scores, FAIR indicator results + DSM level, checkpoint log (reasoning trace, next actions, completed checkpoints), iteration count, and stuck detection.

### Entity Model: Three Layers
1. **Packaging layer** — RO-Crate 1.1 base (Dataset, File, Person, Organization)
2. **Structural layer** — ISA hierarchy (Investigation, Study, Assay, LabProcess, LabProtocol, Sample)
3. **Domain layer** — Toxicology extension (MolecularEntity, CellLineSample, LabProcessExposure, EndpointReadout, CellCulture, DataAnalysis)

### Anti-Hallucination
All identifiers are verified against their source. The agent never fabricates. Verification failures clear the field and trigger alternative strategies or ask the user. Leaving a field empty is acceptable (affects MIT/FAIR scores but doesn't block crate assembly).

### Three-Tier Validation
- **MUST** = blocking — crate cannot be assembled
- **SHOULD** = recommended — agent attempts to fill if data is available
- **MAY** = informational — noted for the user
- **MIT/FAIR** = scores (not pass/fail) — presented as improvement suggestions

### Fixed Initialization: scan_files
Before the agent loop starts, one step runs as a hard precondition:
1. `scan_files` — builds a raw file inventory (path, size, mime type, first rows of CSV/TSV/XLSX). No role classification, no ARC sorting — just a list of what's in the input directory.

This inventory is the only precondition. The agent uses it during entity drafting to bind files to `LabProcess` instances as annotations emerge. The ARC folder structure is not scaffolded upfront — it is produced as an output by `arc_writer.py` once entity annotations are complete.

### Input Readers Are Format-Agnostic
Each input format (unstructured folder containing research objects such as protocols, SOPs, publications, raw data, processed data, metadata, and other relevant resources to interpret the data, existing crate, directory, conversation) has a dedicated reader that converts to canonical entity state in `CrateState`. The core agent loop never touches input formats directly.

### Session Persistence
State auto-saves to `sessions/<session_id>/` at every milestone. The saved `crate_state.json` plus a partial `ro-crate-metadata.json` allow full session resume. Recovery path: if state is lost, reconstruct from the partial crate.

### HITL Checkpoints
The agent presents to the human at checkpoints: file scan review, entity draft review, identifier verification, validation gates, MIT score review, pre-finalization, and when stuck. User response options: Approve, Edit, Reject with explanation, Skip. All feedback logged in entity `_provenance`.

### Observability via Reasoning Log
Every tool call, state change, and reasoning step is recorded in `CrateState.checkpoint.reasoning_log` as a structured event: `{step, action, tool, result, timestamp}`. This enables live status for web UIs (`get_status()` returns phase, entity counts, MIT scores, iteration count, last action), session replay for debugging, progress tracking, and diagnostics. The reasoning log is persisted with the session and survives resume. A future web UI can tail or stream this log without changing the builder's internals — the data structure already supports it.

### Lookup Pattern
All lookups follow: return `{found: bool, data: dict, error: str | None}`, never throw, LRU cached, rate-limited. Multi-strategy for chemicals: try name -> CAS -> ChEBI, then ask user.

### Existing Base
The following already exists in the codebase and will be used directly:
- `profiles/context.py` — Complete JSON-LD context with 175+ mappings
- `profiles/validator.py` — Three-pass SHACL validation wrapping `rocrate_validator`
- `profiles/models/isa.py` — LabProcess, LabProtocol, ParameterValue, Sample, File entity classes
- `profiles/models/tox.py` — LabProcessExposure, EndpointReadout, CellCulture, DataAnalysis, CellLineSample
- `profiles/schemas/isa.yaml` and `profiles/schemas/tox.yaml` — ISA and ISA-Tox profile schemas
- `lookups/` — PubChem, Cellosaurus, AOP-Wiki, BAO, ORCID, ROR, Crossref, IUCLID clients
- `mit/invitro_tox.yaml` — Complete MIT YAML (~3900 lines) with `crate_slot` mappings
- `fair/indicators.yaml` and `fair/dsm_indicators.yaml` — FAIR maturity indicators
- `arc/` — ARC template specification and template files
- `input/raw/` — Example zip folders (S-VHPS21.zip, S-VHPS22.zip, S-VHPS26.zip) containing search output with various file types and metadata across one or more assays

## Testing Decisions

### Testing Philosophy
Tests focus on external behavior, not implementation details. The key seam is the **agent engine orchestrator** (`engine.py`), tested with mocked tools. Individual tools are tested with real lookups (where feasible) or mocked APIs.

### Test Seams (highest first)
1. **Engine orchestration** — The agent loop's decision-making: given a `CrateState` and tool outputs, does the engine choose the right next tool? Test with mocked tools returning deterministic results.
2. **CrateState serialization** — Round-trip: `CrateState -> JSON -> CrateState`. Field-level completion, provenance, and checkpoint log must survive serialization.
3. **Individual tools** — Each tool function tested in isolation: scan_files, draft_* tools, lookup_* clients (mock HTTP), build_crate, validate, assess_mit_coverage, assess_fair_maturity.
4. **Input readers** — Each reader (directory scanner, existing crate) given example input -> correct CrateState seed.
5. **Session persistence** — Save and resume: does the engine restore correctly from a saved session?

### Prior Art
No existing tests in the codebase — this is the first testing effort. `profiles/validator.py` has well-defined `ValidationResult` dataclass. Lookups follow a consistent `{found, data, error}` return shape making them mockable via a simple adapter.

## Out of Scope

- **Web API / Frontend**: The builder is a Python library. FastAPI or Streamlit frontend is future work.
- **Multi-user workflows**: Provenance model supports single-user only. Multi-persona collaboration is future work.
- **MCP integration**: The toolbox architecture is MCP-ready but MCP server registration is future work.
- **ARC workflow/run execution**: The builder produces an ARC directory as output (which *is* the RO-Crate), but executing the ARC `workflows/` and `runs/` (running the analysis tools themselves) is out of scope.
- **Custom profile loading**: Additional profiles beyond ISA-Tox are future work.
- **Batch processing**: Each session is isolated. Parallel sessions straightforward but not implemented.
- **Non-toxicology domains**: The MIT YAML and SHACL shapes are specific to *in vitro* toxicology.

## Further Notes

- The system must work with a **half-finished crate** — users start, get interrupted, resume days later. Session persistence and field-level completion tracking exist for this use case.
- The agent **never blocks on missing SHOULD/MAY fields** or low MIT/FAIR scores. It reports them as suggestions and moves on. Blocking only happens on REQUIRED validation failures.
- The agent **prefers asking the user over fabricating data** when lookups fail. The anti-hallucination principle is non-negotiable.
- File scanning never reads entire large files into context. It reads metadata and first rows, reporting confidence levels.
- The existing `profiles/models/isa.py` and `profiles/models/tox.py` handle JSON-LD assembly. The builder's `state.py` entities are intermediate representations that map to these crate classes.