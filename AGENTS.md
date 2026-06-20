# ISA-Tox RO-Crate Builder — System Design

> **Purpose:** This document describes the architecture, component design, and design rationale for the LLM-assisted RO-Crate builder backend. It serves as both a developer guide and an orientation document for AI coding agents working on this codebase.
>
> **Status:** Draft — design phase

## Table of Contents

- [1. Architecture Overview](#1-architecture-overview)
- [2. Core Concepts](#2-core-concepts)
- [3. CrateState — The Central Data Model](#3-cratestate--the-central-data-model)
- [4. Pipeline Components](#4-pipeline-components)
- [5. The Agent Toolbox](#5-the-agent-toolbox)
- [6. Validation Layers](#6-validation-layers)
- [7. Session Persistence & Resume](#7-session-persistence--resume)
- [8. Human-in-the-Loop (HITL)](#8-human-in-the-loop-hitl)
- [9. Input & Output Formats](#9-input--output-formats)
- [10. Lookup Services](#10-lookup-services)
- [11. Key Design Decisions](#11-key-design-decisions)
- [12. Project Structure](#12-project-structure)
- [13. Future Considerations](#13-future-considerations)

## 1. Architecture Overview

The ISA-Tox RO-Crate Builder is a **toolbox-based agent system** that assists researchers in creating profile-conformant RO-Crates for *in vitro* toxicology data. Rather than a rigid pipeline with predefined steps, the system gives an LLM agent a set of tools and lets it decide the order of operations based on the current state.

### High-Level Flow

```
                         Agent Loop
   LLM Agent ◄─────────────────────────────────────
      │          Tools: scan_files, draft_entity, update_entity,
      │          remove_entity, lookup_*, verify_identifier,
      │          build_crate, validate, assess_mit, assess_fair,
      │          present_to_human, save_session, get_status
      ▼
   ┌──────────────────────┐
   │    CrateState         │── persists between sessions
   │  (serializable)       │
   └──────────────────────┘

   Feedback loops:
   - Validation (MUST)  → agent re-drafts the failing entity
   - Validation (SHOULD)→ agent reports, re-drafts if data avail
   - HITL review        → agent incorporates feedback, continues
   - MIT/FAIR scores    → presented as optional improvements
```

### Toolbox, Not Graph

The agent is **not** guided by a predefined workflow graph. Instead it:

1. Examines the current `CrateState` (what's been done, what's missing, validation results)
2. Decides which tool to call next
3. Processes the result
4. Repeats until the crate is complete or escalates to the human

The orchestration is **emergent** — the agent dynamically routes itself based on context, feedback, and user input. A validation failure sends it back to drafting; an incomplete MIT score triggers more lookups.
## 2. Core Concepts

### Entity Model

Three-layer model mirroring the RO-Crate profile hierarchy:

| Layer | Description | Key Types |
|-------|-------------|-----------|
| **Packaging** | RO-Crate 1.1 base | `Dataset`, `File`, `Person`, `Organization` |
| **Structural** | ISA hierarchy | `Investigation`, `Study`, `Assay`, `LabProcess`, `LabProtocol`, `Sample` |
| **Domain** | Toxicology extension | `MolecularEntity`, `CellLineSample`, `LabProcessExposure`, `LabProcessEndpointReadout`, `LabProcessCellCulture`, `LabProcessDataAnalysis` |

### Entity Provenance

Every entity tracks:
- `status`: `draft` | `enriched` | `reviewed` | `verified`
- `created_by`: `scanner` | `llm` | `user` | `lookup`
- `reviewed_by`: `user` | `null`
- `field_completion`: per-field status (`missing` | `filled` | `verified`)

This enables session resume, quality tracking, and audit.

### Completion Model

Completion is tracked at the **field level** using `mit/invitro_tox.yaml` as reference. Each parameter's `crate_slot` mapping defines expected fields per entity type.

```
Entity: MolecularEntity (Compound)
├── name: "Silychristin A"              filled, verified
├── identifier: (CAS) "33889-69-9"     filled, needs review
├── inChIKey: missing                   missing
├── smiles: missing                     missing

MIT Module: Chemical Information (12 fields)
├── Compound name: ✓
├── CAS Registry Number: ✓
├── Structural formula: ✗
├── Purity: ✗
└── Score: 6/12 (50%)
```

## 3. CrateState — The Central Data Model

`CrateState` is the single source of truth. It is serializable to JSON and persists to disk for session resume.

```
CrateState {
    session_id: str, created_at: datetime, updated_at: datetime,
    metadata: {
        title: str | None, description: str | None, accession: str | None,
        input_type: "directory" | "conversation",
        input_path: str | None, output_path: str | None,
    },
    entities: {
        investigations: [Entity], studies: [Entity], assays: [Entity],
        lab_processes: [Entity], protocols: [Entity], samples: [Entity],
        molecular_entities: [Entity], people: [Entity], organizations: [Entity],
        publications: [Entity], defined_terms: [Entity],
        property_values: [Entity], files: [Entity],
    },
    scanned_files: [{ path, filename, size, mime_type,
        suggested_role, confidence, reviewed_by_user }],
    validation: {
        base_passed: bool, isa_passed: bool, tox_passed: bool,
        required_issues: [str], should_issues: [str], may_issues: [str],
    },
    mit_assessment: { module_scores: { m: { completed, total } }, overall_score },
    fair_assessment: { indicator_results, dsm_level },
    checkpoint: { next_actions: [str], completed_checkpoints: [str],
                  reasoning_log: [{"step", "action", "result"}] },
    iteration_count: int, max_iterations: int, stuck: bool,
}
```

### Field-Level Completion Metadata

```python
{
    "entity_id": "chem_001", "type": "MolecularEntity",
    "name": "Silychristin A", "identifier": "33889-69-9",
    "_completion": {
        "MolecularEntity:name": {"status": "verified", "source": "user"},
        "MolecularEntity:identifier": {"status": "filled", "source": "lookup_pubchem"},
        "MolecularEntity:smiles": {"status": "missing"},
    },
    "_provenance": {
        "created_by": "llm", "reviewed_by": "user", "lookups_used": ["pubchem"],
    }
}
```

## 4. Pipeline Components

```
Input → Scanner → Drafter(s) → Builder → Validator → Assessor → Output
                         ↑                   ↑
                     HITL Review       Human fixes issues
```

### 4.1 Scanner
Examines input directory and classifies files by role. Reads file metadata and first rows for CSV/TSV/XLSX. Reports confidence levels. Never reads entire large files into context.

**Categories:** raw_data, processed_data, condition_table, protocol, analysis_script, metadata, other.

### 4.2 Entity Drafters
Generate metadata entities from files, conversation, or existing metadata. Each drafter collects hints, calls the LLM, and ensures identifiers come from lookups (never fabricated).

**Entity types:** Investigation, Study, Assay, MolecularEntity, CellLineSample, LabProcess (CellCulture/Exposure/EndpointReadout/DataAnalysis), Person, Organization, Publication.

### 4.3 Builder
Assembles the RO-Crate using [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) (`profiles/models/isa.py`, `profiles/models/tox.py`, `profiles/context.py`). Can produce partial crates at any point.

### 4.4 Validator
Runs three-pass SHACL validation via `profiles/validator.py`, which wraps [`rocrate_validator`](https://github.com/ResearchObject/rocrate-validator) — the official RO-Crate validation library. Returns issues by severity: REQUIRED (blocking), SHOULD (recommended), MAY (informational).

### 4.5 MIT & FAIR Assessors
Score against `mit/invitro_tox.yaml` and `fair/indicators.yaml`. Both produce scores, not pass/fail.

### 4.6 External RO-Crate Packages

This project builds on the existing RO-Crate Python ecosystem rather than reinventing crate assembly, validation, or entity models:

| Package | PyPI | What it provides | How we use it |
|---------|------|-----------------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) | `uv add rocrate`<br>(import `rocrate`) | Official Python SDK for creating and manipulating RO-Crates. Provides `ROCrate`, `ContextEntity`, `File`, and other base entity classes. | The entity model classes in `profiles/models/isa.py` and `profiles/models/tox.py` subclass `rocrate.model.ContextEntity` and `rocrate.model.File`. The builder uses `ROCrate` to assemble the crate and serialise `ro-crate-metadata.json`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) | `uv add roc-validator`<br>(import `rocrate_validator`) | Official SHACL-based validation library. Supports multi-profile validation (base RO-Crate → ISA → domain extensions) with severity levels. | `profiles/validator.py` wraps this in three passes (RO-Crate 1.1, ISA, ISA-Tox), suppressing inherited-profile duplicates so each pass reports only its own layer. |
| [`rocrate-wizard`](https://github.com/ResearchObject/rocrate-wizard) *(external frontend)* | TBD | Frontend/UI layer that uses this backend (vitro-crate) to provide a user-facing RO-Crate builder. | This repo is the dependency — `rocrate-wizard` imports from `vitro-crate` and adds the web UI/CLI on top. Referenced in the ARC template's conversion workflow. |

These packages are imported directly — we do not fork or vendor them. Version requirements are declared in `pyproject.toml`.

## 5. The Agent Toolbox

### File Tools
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20) → str | None
```

### Entity Drafting Tools
```
draft_investigation(hints: dict) → Entity
draft_study(investigation_id: str, hints: dict) → Entity
draft_assay(study_id: str, hints: dict) → Entity
draft_molecular_entity(name: str, hints: dict) → Entity
draft_cell_line_sample(name: str, hints: dict) → Entity
draft_process(assay_id: str, process_type: str, hints: dict) → Entity
draft_person(name: str, hints: dict) → Entity
draft_organization(name: str, hints: dict) → Entity
draft_publication(doi: str, hints: dict) → Entity
```

### Entity Management Tools
```
update_entity(entity_id: str, patch: dict) → Entity
remove_entity(entity_id: str) → bool
list_entities(entity_type: str | None) → [Entity]
```

### Lookup Tools
```
lookup_compound(name: str) → CompoundData | None   # PubChem
lookup_cell_line(accession: str) → CellLineData | None  # Cellosaurus
lookup_aop(aop_id: str) → AOPData | None            # AOP-Wiki
lookup_bao_term(query: str) → TermData | None       # OLS/BAO
lookup_orcid(orcid_id: str) → PersonData | None     # ORCID
lookup_ror(name: str) → OrgData | None              # ROR
lookup_doi(doi: str) → PublicationData | None       # Crossref
```

### Verification Tools
```
verify_identifier(entity_id: str, field: str) → VerificationResult
verify_all_identifiers() → [VerificationResult]
```

### Crate Assembly & Validation Tools
```
build_crate(output_path: str) → CrateBuildResult
validate(crate_path: str) → ValidationReport
```

### Assessment Tools
```
assess_mit_coverage() → MITReport
assess_fair_maturity() → FAIRReport
```

### Session & HITL Tools
```
present_to_human(context: str, options: [str]) → HumanResponse
save_session(label: str) → SessionInfo
get_status() → SessionStatus
get_hint() → str
```

## 6. Validation Layers

| Layer | Severity | Meaning | Agent Action |
|-------|----------|---------|--------------|
| Base RO-Crate 1.1 | REQUIRED | Structural validity | MUST fix before proceeding |
| ISA Profile | REQUIRED | ISA conformance | MUST fix |
| ISA Profile | SHOULD | Recommended metadata | Fix if data available |
| ISA Profile | MAY | Optional metadata | Note for user |
| ISA-Tox Profile | REQUIRED | Toxicology conformance | MUST fix |
| ISA-Tox Profile | SHOULD | Recommended tox fields | Fix if data available |
| ISA-Tox Profile | MAY | Optional tox fields | Note for user |
| MIT Coverage | Score | % of recommended fields | Improvement suggestions |
| FAIR Indicators | Score | FAIR maturity | Guidance for improvement |

### Verification Layer
Checks that identifiers resolve at their source. Verification failures are REQUIRED — the identifier must be corrected or removed. Leaving a field empty is acceptable (shows up in MIT/FAIR scores but does not block).

## 7. Session Persistence & Resume

### Save Format
```
sessions/<session_id>/
├── crate_state.json       # Serialized CrateState
├── working_crate/         # Crate directory (may be partial)
│   ├── ro-crate-metadata.json
│   ├── data/
│   └── protocols/
└── session.log            # Agent reasoning trace
```

### When Saving Happens
Automatically at: after file scanning, after each entity draft, after HITL checkpoints, after validation, when approaching context limits, and on explicit user request.

### Resume Flow
1. Load `crate_state.json` → restore `CrateState`
2. Verify `working_crate/` matches state
3. Agent examines `checkpoint.next_actions` and `completed_checkpoints`
4. Run validation to establish current baseline
5. Agent picks up where it left off

**Recovery:** If crate exists but state is missing, reconstruct from `ro-crate-metadata.json` via the entity model.

## 8. Human-in-the-Loop (HITL)

### Checkpoint Types
- **File scan review**: "I found these files and classified them as..."
- **Entity draft review**: "Here's what I drafted for the cell line."
- **Identifier verification**: "I found this compound at PubChem."
- **Validation gate**: "3 REQUIRED issues need fixing."
- **MIT score review**: "65% coverage. Missing fields: ..."
- **Completion review**: "Ready to finalize?"
- **Agent stuck**: "I'm stuck trying to..."

### Interaction Model
1. Agent presents content and a question
2. User can: **Approve**, **Edit**, **Reject with explanation**, or **Skip**
3. Agent incorporates feedback and continues
4. All feedback logged in entity `_provenance`

## 9. Input & Output Formats

### Input Formats

Input comes in tiers of readiness. The agent should prefer the most structured form available:

| Format | Curation level | Description |
|--------|---------------|-------------|
| **Directory with metadata files** | Medium — partial structure | A research folder that contains some metadata files (README, `.json`, `.yaml`, `.csv`, or other records) alongside raw data. The scanner identifies these by role and the agent drafts entities from whatever structured content they hold — **regardless of the metadata file's format or schema**. Any such file is treated as a generic metadata source, not a special-cased input type. |
| **Unstructured directory** | Low — raw data only | The worst case: a folder of research data with no accompanying metadata. All entities must be drafted from scratch through conversation with the user (file scanning, lookups, and HITL checkpoints). This is the most common real-world scenario. |

**Guiding principle:** Meet the input where it is. Read every metadata file present and reuse every field it can, whatever its structure; if nothing is present, build everything from conversation and lookups. Never discard curated metadata.

### ARC Working Layout & Output

**ARC (Annotated Research Context)** is not an input format and is **not optional** — scaffolding it is a mandatory first step in every session. Early in the run (after files are scanned and assays identified), the builder creates an empty ARC folder tree from the VHP4Safety ARC template at `arc/arc-template/` and uses it as *the* organizing structure that imposes the first layer of order on otherwise unstructured input: every scanned file is sorted into the correct ARC bucket and bound to the `LabProcess` it belongs to. This early structure is what makes downstream drafting, classification, and validation tractable. The same tree is the deliverable at the end — the `arc_writer.py` component projects CrateState entities onto it and emits the populated ARC alongside `ro-crate-metadata.json`.

```
<accession_arc>/
├── isa.investigation.xlsx      generic ARC investigation table (optional)
├── studies/<study>/
│   ├── isa.study.xlsx          generic ARC study table (optional)
│   ├── protocols/              SHARED protocols: starting material/data → samples
│   └── resources/              external data the study references
├── assays/<assay>/
│   ├── ToxTemp_<assay>.md      test-method description (authoritative per assay)
│   ├── isa.assay.xlsx          generic ARC assay table (optional)
│   ├── dataset/
│   │   ├── raw_data/           raw instrument output
│   │   └── processed_data/     analysed results
│   └── protocols/              assay-specific protocols: samples → measurement
├── workflows/<wf>/             reusable analysis scripts/tools + environment
└── runs/<run>/                 parameters + inputs for one execution of a workflow
```

The `arc_writer` maps CrateState entities (`Investigation`, `Study`, `Assay`, `LabProcess`, `Sample`, `File`) onto this directory structure. Each assay gets a `ToxTemp_<assay>.md` derived from LabProcess metadata. Protocols are exported from `LabProtocol` entities. Raw and processed data files are placed under `dataset/raw_data/` and `dataset/processed_data/` based on their `File.role`.

## 10. Lookup Services

All lookups follow a consistent pattern: return `{found: bool, data: dict, error: str | None}`, never throw, LRU cached, with rate limiting.

### Available Services
- **PubChem**: Name/CAS/CID → SMILES, InChI, formula, mass
- **Cellosaurus**: Accession (CVCL_xxxx) → name, species, disease, site, sex
- **AOP-Wiki**: AOP ID → full pathway graph (AOP, events, relationships)
- **BAO / OLS**: Free-text query → best-matching ontology term with IRI
- **ORCID**: ORCID iD → name, affiliation, affiliation ROR
- **ROR**: Organization name → ROR ID, website URL
- **Crossref**: DOI → title, authors, journal, year

### Multi-Strategy Lookups
For chemicals: try by name, then CAS, then ChEBI. If all fail, ask user for SMILES/InChI.

### Anti-Hallucination
The agent **never fabricates identifiers**. Every identifier is verified against its source. If verification fails, the field is cleared and the agent tries alternatives or asks the user.

## 11. Key Design Decisions

### D1: Toolbox over Graph
The agent decides what to call rather than following a predefined graph. Validation and HITL feedback can send the process to any earlier stage. Mitigated by max iterations and HITL escalation.

### D2: Three-Tier Validation
MUST = blocking, SHOULD = recommended, MAY/MIT/FAIR = suggestions.

### D3: Crate as Persistence Format
Partial `ro-crate-metadata.json` is valid. Missing fields = not completed. `crate_state.json` adds tracking metadata.

### D4: Format-Agnostic Core
Readers convert input → `CrateState`. Writers convert `CrateState` → output. Agent doesn't care about input format.

### D5: Verify, Don't Trust
Every identifier verified against source. Never fabricate.

### D6: Field-Level Completion Tracking
Per-field, per-entity completion using MIT YAML as reference. Enables precise resume and accurate scoring.

### D7: ARC Scaffold as Mandatory First Structure
Every session scaffolds the ARC folder tree up front and sorts scanned files into it (binding each to its `LabProcess`). This is not optional output — it is the first structure imposed on unstructured input and the substrate for all downstream drafting, classification, and validation. ARC is the working layout, not an input format.

## 12. Project Structure

Annotated with where new components would live:

```
vitro-crate/
├── AGENTS.md                    This file
├── pyproject.toml
├── profiles/                    Existing — domain profiles
│   ├── context.py               JSON-LD context
│   ├── validator.py             3-pass SHACL validation
│   ├── models/isa.py, tox.py    Entity classes
│   ├── schemas/isa.yaml, tox.yaml
│   └── shapes/isa/, tox/        SHACL shapes
├── lookups/                     Existing — external API clients
│   ├── cellosaurus.py, pubchem.py, aopwiki.py, bao.py
│   └── orcid.py, ror.py, crossref.py
├── mit/invitro_tox.yaml         Existing — Minimum Information Table
├── fair/                        Existing — FAIR indicators
├── arc/                         Existing — ARC template/spec
├── input/                       Existing — example inputs
├── builder/                     NEW — core builder system
│   ├── state.py                 CrateState dataclass
│   ├── engine.py                Agent orchestrator
│   ├── tools/                   Tool implementations
│   │   ├── scanner.py, drafters.py, management.py
│   │   ├── lookups.py, verification.py, builder.py
│   │   ├── validation.py, mit_assessment.py
│   │   ├── fair_assessment.py, session.py
│   ├── readers/                 Input readers
│   │   ├── metadata_files.py, existing_crate.py
│   │   ├── directory.py
│   ├── writers/                 Output writers
│   │   ├── rocrate_writer.py, arc_writer.py
│   └── agents/                  LLM config
│       ├── system_prompt.py, tools_spec.py
├── sessions/                    NEW — persisted sessions
└── tests/                       NEW — test suite
```

## 13. Future Considerations

### MCP Integration
Toolbox architecture is MCP-ready. External MCP servers can be registered as additional tools.

### Multi-User Workflows
Provenance model supports single-user now. Could be extended for multiple personas.

### Web API
Builder is a Python library. FastAPI/Streamlit frontend can call into it without changes.

### Custom Profiles
Schemas are YAML-defined. Additional profiles could be loaded at runtime.

### Batch Processing
State is isolated per session. Parallel sessions are straightforward.

---

*This document is a living design artifact. Update as architectural decisions evolve.*
