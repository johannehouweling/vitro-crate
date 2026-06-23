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
      │          Tools: draft_entity, update_entity, remove_entity,
      │          lookup_*, verify_identifier, build_and_validate,
      │          export_crate, validate, assess_mit, assess_fair,
      │          present_to_human, save_session, get_status
      ▼
   ┌──────────────────────┐
   │    CrateState         │── persists between sessions
   │  (serializable)       │
   └──────────────────────┘

   Initialization (before agent loop):
   ┌──────────────┐
   │  scan_files  │ ──► agent loop (inventory only)
   └──────────────┘

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

One step is **always** run as fixed initialization before the agent loop:
1. `scan_files` — builds a raw file inventory (path, size, mime type, first rows). No role classification, no ARC sorting — just a list of what's in the input directory.

This inventory is the only precondition. The agent uses it during entity drafting to bind files to `LabProcess` instances as annotations emerge. The ARC folder structure is not scaffolded upfront — it is produced as an output by `arc_writer.py` once entity annotations are complete.

### Guard Rails: Approved Scan Roots
The agent's `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set that records user-confirmed paths. When the agent calls `scan_files(path)`:
1. The path is resolved to an absolute canonical form
2. It is checked against the approved set — if not found, scanning is denied
3. If it is a subdirectory of an approved root, scanning is allowed
4. The user can approve new paths through HITL review (via `present_to_human` or the CLI) — this is the only way new roots get added

This prevents the LLM agent from reaching into arbitrary locations on the user's filesystem and provides a clear audit trail of which directories the system has ever accessed.

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
        reviewed_by_user }],
    approved_scan_roots: set[str],  # user-approved directory roots for file scanning
    validation: {
        base_passed: bool, isa_passed: bool, tox_passed: bool,
        required_issues: [str], should_issues: [str], may_issues: [str],
    },
    mit_assessment: { module_scores: { m: { completed, total } }, overall_score },
    fair_assessment: { indicator_results, dsm_level },
    checkpoint: { next_actions: [str], completed_checkpoints: [str],
                  reasoning_log: [{"step", "action", "tool", "result", "timestamp"}] },
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

## 4. Agent Priority Heuristic (Work in Layers)

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

### Validation Gate Ordering

The three-pass validation in `profiles/validator.py` has a strict dependency:
- **Base RO-Crate 1.1** must pass before ISA validation is meaningful
- **ISA Profile** must pass before ISA-Tox validation is meaningful
- **ISA-Tox Profile** depends on both lower layers

If the agent tries to validate and gets `base_passed: false`, there is no point
fixing ISA-Tox issues until the crate builds. The agent should:

1. Call `build_crate` early, even with minimal entities
2. Fix base RO-Crate issues first (structural integrity)
3. Then iterate on ISA issues
4. Then iterate on ISA-Tox issues
5. Report MIT/FAIR scores last, as optional improvements

This avoids the common pitfall of perfecting one entity type before verifying
the crate can assemble at all.

### Pipeline Components

The components used by the agent are:

#### Scanner (`builder/tools/scanner.py`)
Examines an input directory (or zip archive) and builds a raw file inventory
(path, size, mime type, first lines of readable files). Never reads entire
large files into context. This inventory is the only precondition for the
agent loop — the agent uses it during entity drafting to bind files to
`LabProcess` instances as annotations emerge. Restricted by approved scan
roots (see [Guard Rails](#guard-rails-approved-scan-roots) above).

**Performance features (Issues #67–#69):**
- `max_files` and `max_line_length` params limit result size and preview length
- `read_file_sample` accepts `precomputed_size` and `already_text` to avoid redundant stat()/MIME syscalls
- `_safe_walk` prunes hidden/`.git`/`__MACOSX` directories in-place via `os.walk` `dirnames[:]` mutation, avoiding the cost of descending and then filtering

#### Entity Drafters (`builder/tools/drafters.py`)
Generate metadata entities from files, conversation, or existing metadata.
Each drafter collects hints, calls the LLM, and ensures identifiers come from
lookups (never fabricated).

**Entity types:** Investigation, Study, Assay, MolecularEntity, CellLineSample,
LabProcess (CellCulture/Exposure/EndpointReadout/DataAnalysis), Person,
Organization, Publication.

#### Crate Builder (`builder/tools/builder.py`)
Assembles the RO-Crate using [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py)
(`profiles/models/isa.py`, `profiles/models/tox.py`, `profiles/context.py`).
Can produce partial crates at any point.

#### Validator (`builder/tools/validation.py`, `profiles/validator.py`)
Runs three-pass SHACL validation via `profiles/validator.py`, which wraps
[`rocrate_validator`](https://github.com/ResearchObject/rocrate-validator).
Returns issues by severity: REQUIRED (blocking), SHOULD (recommended), MAY
(informational). Two entry points:
- `validate_crate(crate_dir)` validates a crate **on disk** and returns
  prose `ValidationResult`s (used by the `validate(crate_path)` tool).
- `validate_crate_dict(metadata_doc, severity, profile)` validates an
  **in-memory** metadata document (the dict from `crate.metadata.generate()`)
  via `services.validate_metadata_as_dict`, returning `DictValidationResult`s
  whose `RoutableIssue`s carry the focus-node `entity_id`, failing `property`
  IRI, `check_id`, `severity`, and `profile`. This backs `build_and_validate`
  and is the no-disk fast path.

**Performance note — why the tox pass dominates, and why we do *not* cache shapes.**
The three passes are not equal work: `base` (~0.3s) and `isa` (~0.5s) are cheap,
but `tox` (~2.7s of a ~3.4s full sweep) resolves the deepest inheritance chain
(`tox-ro-crate → isa-ro-crate → ro-crate`, i.e. our `SHAPES_DIR` plus the bundled
isa+base profiles) and rocrate_validator runs **SHACL + owlrl inference over that
combined graph on every call**. Caching the parsed shapes was explored
(issue #63 / PR #111) and **deliberately abandoned**: the `.ttl` parse is
negligible (~10–130ms), the dominant ~2.5s is library-internal inference that
rocrate_validator exposes **no hook to reuse**, and the only way to cache the part
we own was to monkeypatch `ValidationContext.__load_profiles__` — a fragile patch
on library internals for an ~11% gain that leaves the real bottleneck untouched.
The supported speed levers instead are: gate the inner loop at `required` severity
(`validate_crate_dict`'s default — fastest), and scope `profile` to a single pass
when the full sweep isn't needed. A full 3-pass sweep is run only as a final gate.

**Offline-safe validation — bundled RO-Crate context, no network on the base pass (#117).**
Every crate's `@context` points at the *remote* IRI
`https://w3id.org/ro/crate/1.2/context`, and the base pass must dereference it to
expand the data graph. `rocrate_validator` resolves that IRI over HTTP through two
paths — rdflib's JSON-LD document loader (feeding check `ro-crate-1.1_2.1`) and the
`FileDescriptorJsonLdFormat` check, which calls `HttpRequester().get(context_uri)`
directly (check `ro-crate-1.1_2.2`). On PR #116 CI that fetch flaked
(`RemoteDisconnected`) and the base pass emitted **spurious REQUIRED issues**,
turning a transient blip into red CI and violating #59's "runs offline" criterion.
`profiles/validator.py` makes validation offline-safe:

- **Pinned local contexts.** `profiles/contexts/ro-crate-1.1-context.jsonld` and
  `ro-crate-1.2-context.jsonld` are committed copies of the RO-Crate JSON-LD
  contexts. `_install_offline_context_loader()` (run at import) intercepts the
  `HttpRequester` GET/HEAD proxy (and `fetch_fresh`) and serves these well-known
  context URLs from disk, so both resolution paths get the bundled copy and never
  touch the wire. It also sets `ROCRATE_VALIDATOR_AUTO_WARM=0` to suppress
  rocrate_validator's best-effort cache warm-up (pure network traffic we don't
  need, since the context is bundled and the warm-up's other artifact — the spec
  HTML page — is unused by any check). Refresh the bundled files only when the
  pinned RO-Crate context version changes.
- **Transport failure ≠ content violation.** If a remote resource genuinely can't
  be dereferenced, rocrate_validator swallows the connection error inside the
  check and re-emits it as a REQUIRED *content* issue. `validate_crate` and
  `validate_crate_dict` detect those (a connection-error message on a
  remote-resolving check) and raise `ValidationTransportError` instead, so a
  network failure surfaces as a clear error — never a spurious REQUIRED issue and
  never a false negative in `build_and_validate` (which maps the exception to
  `{"ok": False, "error": ...}`). The regression test
  `tests/test_offline_validation.py` runs validation with the HTTP transport hard-
  blocked and asserts green + no spurious REQUIRED issue; the #59 e2e harness also
  runs with the network disabled to prove the path is offline-safe.

#### MIT & FAIR Assessors (`builder/tools/mit_assessment.py`, `builder/tools/fair_assessment.py`)
Score against `mit/invitro_tox.yaml` and `fair/indicators.yaml`. Both produce
scores, not pass/fail.

### External RO-Crate Packages

This project builds on the existing RO-Crate Python ecosystem rather than reinventing crate assembly, validation, or entity models:

| Package | PyPI | What it provides | How we use it |
|---------|------|-----------------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) | `uv add rocrate`<br>(import `rocrate`) | Official Python SDK for creating and manipulating RO-Crates. Provides `ROCrate`, `ContextEntity`, `File`, and other base entity classes. | The entity model classes in `profiles/models/isa.py` and `profiles/models/tox.py` subclass `rocrate.model.ContextEntity` and `rocrate.model.File`. The builder uses `ROCrate` to assemble the crate and serialise `ro-crate-metadata.json`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) | `uv add roc-validator`<br>(import `rocrate_validator`) | Official SHACL-based validation library. Supports multi-profile validation (base RO-Crate → ISA → domain extensions) with severity levels. | `profiles/validator.py` wraps this in three passes (RO-Crate 1.1, ISA, ISA-Tox), suppressing inherited-profile duplicates so each pass reports only its own layer. |
| [`rocrate-wizard`](https://github.com/ResearchObject/rocrate-wizard) *(external frontend)* | TBD | Frontend/UI layer that uses this backend (vitro-crate) to provide a user-facing RO-Crate builder. | This repo is the dependency — `rocrate-wizard` imports from `vitro-crate` and adds the web UI/CLI on top. Referenced in the ARC template's conversion workflow. |

These packages are imported directly — we do not fork or vendor them. Version requirements are declared in `pyproject.toml`.

### Agent Graph (LangGraph / StateGraph)

The agent loop uses an **explicitly constructed StateGraph** built by `_build_agent_graph()` in `builder/agents/agent_loop.py`. This replaces the earlier `create_agent()` factory pattern (Issue #37), giving us full control over node names, routing logic, and middleware integration.

```python
graph = StateGraph(AgentState)
graph.add_node("model", call_model)
graph.add_node("tools", tool_node)
graph.add_conditional_edges("model", should_continue)
graph.add_edge("tools", "model")
graph.add_edge(START, "model")
return graph.compile(checkpointer=MemorySaver())
```

#### Node Topology

The compiled graph has exactly **four nodes**:

| Node | Purpose |
|------|---------|
| `__start__` (built-in) | Entry point — LangGraph's standard pseudo-node. Transitions unconditionally to `model`. |
| `model` | Prepends the system prompt and invokes the LLM. |
| `tools` | Executes any tool calls produced by the model (via `ToolNode`). |
| `__end__` (built-in) | Terminal node — agent terminates here. |

The state is typed as `AgentState`, a `TypedDict` with a single `messages` field using the `add_messages` reducer for automatic concatenation:

```python
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
```

#### Edge Routing (Tool-Calling Loop)

The edges form a classic ReAct (Reasoning + Acting) loop — a single conditional router named `should_continue`:

```
   __start__ ──► model ──► should_continue? ──► tools ──► model (loop back)
                        │
                        └──► __end__ (if no tool_calls)
```

The `should_continue` function inspects the last `AIMessage` in the state:

- **Route to `tools`**: If the last message has pending `tool_calls`.
- **Route to `__end__`**: If the last message has no `tool_calls` (the termination condition).

```python
def should_continue(state):
    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    tool_calls = getattr(last_message, "tool_calls", None)
    if tool_calls and len(tool_calls) > 0:
        return "tools"
    return END
```

#### How `system_prompt` Gets Injected

The `call_model` node prepends the system prompt on **every invocation**:

```python
def call_model(state):
    messages = state.get("messages", [])
    system_msg = SystemMessage(content=SYSTEM_PROMPT)
    model_messages = [system_msg, *messages]
    response = llm.invoke(model_messages)
    return {"messages": [response]}
```

This means the system prompt appears at the front of the messages every time the loop iterates back to the model, ensuring the LLM always has its full context: `[system, human, ai(tool_calls), tool, system, ai(answer)]`.

In the live code `call_model` delegates the message assembly to `_assemble_model_messages`, which keeps the byte-stable system prefix and trailing per-turn state brief (D10) but **bounds the history in between** via `_trim_history` — pruning consumed state-backed tool outputs and trimming to a token budget so per-turn input stays bounded over a long session, without ever orphaning a tool message (D12, Issue #61).

#### How `MemorySaver` Integrates

The `MemorySaver` checkpointer is passed to `graph.compile()`. It is a **checkpointing layer** that snapshots the full agent state (`messages` list, etc.) after each node execution. LangGraph uses the `thread_id` from `RunnableConfig` to key these checkpoints. On subsequent `invoke()` calls with the same `thread_id`, the graph resumes from the last checkpoint, providing conversational memory across turns.

The `MemorySaver` does not affect routing or the node topology — it is purely a persistence mechanism for state snapshots.

#### Model Tiering (Issue #96)

The weak model the agent runs on (e.g. DeepSeek-flash) collapses on multi-turn
orchestration and error recovery — the build→validate→re-draft loop — but stays
fine at bounded extraction. Model tiering lets a stronger model drive the
orchestration node while a cheap model does the bounded drafting work, without
any change to the graph topology.

Construction is centralised in `_build_chat_model(provider, model, base_url,
max_retries, role)` (`builder/agents/agent_loop.py`). The `role` parameter
selects the tier when no explicit `model` is passed:

- `role="orchestrator"` (default) → the primary model
  (`VITRO_OPENAI_MODEL` / `VITRO_ANTHROPIC_MODEL`).
- `role="drafter"` → the cheap drafter model
  (`VITRO_OPENAI_DRAFTER_MODEL` / `VITRO_ANTHROPIC_DRAFTER_MODEL`) **when
  configured**.

The drafter model is provider-agnostic and resolved by
`config.get_drafter_model()` (env var → `[openai]`/`[anthropic] drafter_model`
config key, mirroring the primary-model precedence). **Default = single model:**
when no drafter model is set, `role="drafter"` resolves to the same primary
model as the orchestrator, so behaviour is identical to a single-model setup —
a strict no-op. Because drafters are currently pure state-mutation functions
invoked by the orchestration node (they make no LLM call of their own), this
ships the *capability* and config knob; the drafter tier binds when a drafter
path makes its own model call.

**Decision gate (future work):** upgrading the *orchestrator* to a stronger
model is a separate, profiling-gated decision. Instrument `profile.ndjson` for
iterations-per-task, recursion-limit hits, and REQUIRED-issue fix success;
upgrade the orchestrator only if failures are reasoning/recovery-shaped
(looping, mis-sequencing), not malformed output (which schemas + SHACL already
catch). Guardrails are a one-time cost; a stronger model is recurring per token.

## 5. The Agent Toolbox

### File & ARC Tools
*These are called during session initialization, not by the LLM during the agent loop.*
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20, mode: str = "content") → str | None
  mode: "content" (first N lines), "summary" (file-type-aware), "overview" (metadata + summary)
read_multiple_files(paths: list[str], lines: int = 50, mode: str = "content") → dict
  mode: same options as read_file_sample
scaffold_arc(scanned_files: [FileClassification]) → ARCTree
```
`scaffold_arc` creates the ARC folder tree from the template and sorts scanned files into the correct ARC buckets. Called after `scan_files` and before the agent loop starts.

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
draft_file(name: str, path=None, role=None, encoding_format=None) → Entity
```
The `hints` parameter is **typed per entity type** (Issue #90). Each `draft_*`
tool advertises a JSON-Schema built by `_crate_mapping.draft_hints_schema(type)`
from the single source of truth `_crate_mapping.ENTITY_DRAFT_SCHEMA` — allowed
scalar keys plus reference keys, the latter a strict subset of `_REF_FIELDS`
(asserted by test) so the advertised reference vocabulary and the crate-mapping
resolver cannot drift. The schema is open (`additionalProperties: true`), so a
weak model sees the high-value keys without the long tail being forbidden.

### Entity Management Tools
```
set_fields(entity_id: str, fields: dict, source="llm") → Entity
remove_entity(entity_id: str, cascade: bool = False) → bool
list_entities(entity_type: str | None) → [Entity]
```
`set_fields` is the **single consolidated mutation tool** (Issue #90). It
replaced three redundant tools — `update_entity` and `bulk_set_fields` were
byte-identical, and `set_entity_field` was just the single-field (one-key dict)
case. Those names survive as thin deprecated aliases for library callers but are
no longer exposed to the LLM. Pass one field or many in the `fields` dict.
`remove_entity` preserves referential integrity: the builder rebuilds the crate
from state each iteration, so a reference left dangling in state surfaces as a
dangling `{"@id": ...}` in the built graph. It first scans every entity's
reference fields (`_REF_FIELDS`); if the target is still referenced it refuses
with an actionable error naming the referrers, unless `cascade=True` clears those
references first. `entity_id` is the stable key — "renaming" changes the `name`
field, never the `entity_id`, so referrers (which point at `entity_id`) are never
orphaned.

### Derivation Chain Tools
```
link(from_id: str, relation: str, to_id: str) → {from_id, relation, to_id}
check_provenance() → {ok, issues:[{entity_id, property, message, fix, severity, profile}]}
```
`link` adds one provenance edge — `relation` is drawn from `PROVENANCE_RELATIONS`
(`object`/`input`/`samples` = consumed, `result`/`output` = produced,
`derives_from` = sample lineage), a strict subset of the crate mapping's
`_REF_FIELDS` (asserted by test, so the edge vocabulary and the resolver cannot
drift). It is the explicit verb the agent uses to wire the
Sample →[CellCulture]→ Sample →[Exposure]→ table →[EndpointReadout]→ raw
→[DataAnalysis]→ figures chain (those reference keys are otherwise hidden behind
the schema-less `hints` param, so a weak model never sets them). `check_provenance`
is a **report-only** connectivity lint (no auto-chaining — branching assays make a
fixed process order wrong): it flags EndpointReadout/DataAnalysis processes with no
output (the build has no fallback for those) and File entities produced by no
process, returning issues in the same routable shape as `build_and_validate` (#87).

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
/verify_all_identifiers() → [VerificationResult]
```

### Crate Assembly & Validation Tools
```
build_and_validate(severity="required", profile="all") → {ok, conformance, issues}
export_crate(output_path: str) → CrateBuildResult
build_crate(output_path: str) → CrateBuildResult     # back-compat alias of export_crate
validate(crate_path: str) → ValidationReport
validate_table(file: str, table_schema: dict, foreign_keys: dict | None = None, entity_id: str | None = None) → {ok, issues}
```

`validate_table` is the **data-content (payload) layer** (#95): it validates a
CSV's rows against a Frictionless `tableSchema` — separate from the SHACL
metadata passes (see §6, Data-Content Layer). Issues use the same routable shape
with `profile="data"`.

`build_and_validate` is the agent's primary build/fix loop: it assembles the
crate from `CrateState` **in memory** and validates the generated JSON-LD
document directly via `rocrate_validator.services.validate_metadata_as_dict` —
**no crate is written to disk and nothing is re-read** (the old
`build_crate`→`validate` round-trip touched disk on every ReAct iteration). It
returns issues keyed to the entity/property that failed so the agent can route
a fix to a specific field:

```python
{
  "ok": bool,                                  # no issues at the gate severity
  "conformance": {"base": bool, "isa": bool, "tox": bool},  # only the scoped key(s) when profile != "all"
  "issues": [
    {"entity_id", "property", "message", "fix", "severity", "profile"}, ...
  ],
}
```

`conformance` is keyed to the passes actually run: all three layers for
`profile="all"`, or just the scoped layer when a single `profile` is given.

`severity` (`required`|`recommended`|`optional`) is the gate that decides which
SHACL checks run — `required` (the default) is fastest. `profile`
(`all`|`base`|`isa`|`tox`) scopes the passes; since the tox pass dominates
wall-clock, the inner loop can validate a single profile at REQUIRED severity
and run the full 3-pass sweep only as a gate. The three passes mirror
`profiles/validator.validate_crate`, fed the metadata dict instead of a path.

`export_crate` is the **only** tool that touches disk — call it once the crate
is conformant to materialise the on-disk RO-Crate directory (payload included).
`build_crate` remains as a back-compat alias. The in-memory assembly path
(`assemble_crate(..., materialize_payload=False)`) skips writing the Exposure
condition-table placeholder CSV so validation stays a zero-disk operation.

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

### Profiling
Every tool call and graph node execution is automatically timed and recorded by `ProfilingLogger` (see [docs/profiling.md](docs/profiling.md)). Profile data is written to `sessions/<session_id>/profile.ndjson` as newline-delimited JSON with event types including `tool_call`, `node_start`, and `node_end`. This file is the primary input for timing analysis, debugging, and live status in future web UIs.

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
| Data content (Frictionless) | REQUIRED | Payload conformance (CSV rows vs CSVW/Frictionless `tableSchema`) | MUST fix offending cell |
| MIT Coverage | Score | % of recommended fields | Improvement suggestions |
| FAIR Indicators | Score | FAIR maturity | Guidance for improvement |

### Data-Content Layer (Frictionless, Issue #95)

The three SHACL passes (base/ISA/ISA-Tox) validate the **metadata descriptor** —
the structure and semantics of `ro-crate-metadata.json`. They do **not** check
the **payload**: whether the rows of a referenced CSV actually match its declared
schema. The Frictionless layer (`builder/tools/data_content.py`,
`validate_table`) closes that gap, mirroring the metadata/data split in the
BioHackEU25 report "Towards a Robust Validation Service for Data and Metadata in
ARC RO-Crates" (Chadwick et al., biohackrxiv `zah28`).

`validate_table(file, table_schema, foreign_keys=None, entity_id=None)` validates
a CSV's content against a Frictionless `tableSchema` descriptor (column types and
constraints) and, optionally, that designated columns reference only known
in-crate ids (`MolecularEntity` / `Sample`) via `foreign_keys`
(`column -> [allowed_id, ...]`). The obvious payload is the CSVW condition table
emitted by #94 and any raw-measurement tables. Issues come back in the **same
routable shape as #87** (`{entity_id, property, message, fix, severity,
profile}`), with `profile == "data"` so this layer stays cleanly distinct from
the SHACL layers — SHACL = metadata, Frictionless = payload, never entangled. It
never raises into the agent loop: setup errors (missing file, malformed schema)
return `{"ok": False, "issues": [], "error": ...}`.

### Verification Layer
Checks that identifiers resolve at their source. Verification failures are REQUIRED — the identifier must be corrected or removed. Leaving a field empty is acceptable (shows up in MIT/FAIR scores but does not block).

**Derivation (Issue #64):** The set of verifiable fields is no longer a hard-coded flat list.
`verify_all_identifiers` and `_select_verifier` both derive from `_VERIFIABLE_FIELDS` — a
frozenset of `(entity_type, field_name)` pairs — so they can never drift apart.
`_IDENTIFIER_FIELDS` is kept as a legacy re-export derived automatically from the same
source. Fields like `casrn`/`cas_number`/`inchikey` on `MolecularEntity` are now included,
while fields like `ror` on `Organization` (which has no verifier) are correctly excluded.

## 7. Session Persistence & Resume

### Save Format
```
sessions/<session_id>/
├── crate_state.json       # Serialized CrateState
├── working_crate/         # Crate directory (may be partial)
│   ├── ro-crate-metadata.json
│   ├── data/
│   └── protocols/
├── profile.ndjson         # Structured profiling events (tool timing, node timing)
└── session.log            # Agent reasoning trace
```

### When Saving Happens
Automatically at: after file scanning, after each entity draft, after HITL checkpoints, after validation, when approaching context limits, and on explicit user request.

**Durability (Issue #53):** Session saves use an atomic-write strategy: write to a temp
file in the same directory, `fsync` it, then `os.replace()` over the target. A SHA-256
hash of the serialised state is computed before saving; if the hash is unchanged from the
previous save, the write is skipped entirely (no-op). Failures are logged and surfaced
to the agent loop, never silently swallowed.

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

### Interface Adapter
HITL requests go through a `HumanInterface` protocol (`builder/tools/hitl.py`)
injected into the engine via `AgentEngine(human_interface=...)`. It defines
`present(context, options) → HumanResponse` and `request_input(prompt,
field_type) → InputResponse`. The default `SimulatedHumanInterface`
auto-approves and skips input for headless/batch runs; a frontend (Streamlit,
FastAPI, CLI) supplies its own adapter without monkeypatching the tool
functions. The module-level `present_to_human` / `request_input` functions
remain as thin wrappers over a shared default simulator.

## 9. Input & Output Formats

### Input Formats

Input comes in tiers of readiness. The agent should prefer the most structured form available:

| Format | Curation level | Description |
|--------|---------------|-------------|
| **Directory with metadata files** | Medium — partial structure | A research folder that contains some metadata files (README, `.json`, `.yaml`, `.csv`, or other records) alongside raw data. The scanner identifies these by role and the agent drafts entities from whatever structured content they hold — **regardless of the metadata file's format or schema**. Any such file is treated as a generic metadata source, not a special-cased input type. |
| **Unstructured directory** | Low — raw data only | The worst case: a folder of research data with no accompanying metadata. All entities must be drafted from scratch through conversation with the user (file scanning, lookups, and HITL checkpoints). This is the most common real-world scenario. |

**Guiding principle:** Meet the input where it is. Read every metadata file present and reuse every field it can, whatever its structure; if nothing is present, build everything from conversation and lookups. Never discard curated metadata.

### ARC Working Layout & Output

**ARC (Annotated Research Context)** is not an input format and is **not optional** — it *is* the output. The RO-Crate *is* a directory with an `ro-crate-metadata.json` at its root, and that directory follows the ARC layout. The `arc_writer.py` component projects CrateState entities onto the VHP4Safety ARC template at `arc/arc-template/` and populates the ARC tree. Since the ARC tree *is* the crate, the output is a single self-describing directory:

```
<accession_arc>/               RO-Crate root directory
├── ro-crate-metadata.json     RO-Crate metadata (describes everything in the ARC)
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

### Concurrency & Per-Host Rate Limiting (Issue #62)
Independent lookups with no data dependency are issued **concurrently** via a
bounded `concurrent.futures.ThreadPoolExecutor` (max ~6 workers), so cold paths
no longer pay strictly-serial latency:

- `lookups/aopwiki.py:lookup_aop` builds the key-event entities deterministically
  (pathway order: MIE → KE → AO), then fetches each event's `_event_details`
  concurrently and merges them back **by identifier** — the assembled result is
  byte-identical to the serial path and order-independent.
- `builder/tools/verification.py:verify_all_identifiers` collects its work plan
  (entity, field) deterministically, runs the per-field verifications
  concurrently, and returns results in that fixed order. Each task mutates only
  its own field's status, so there is no cross-task contention.

Politeness is preserved by a **single per-host throttle** in `lookups/_http.py`
(`_HostRateLimiter` / `throttle_for_url`), applied inside `http_get_json`. It
enforces a minimum spacing (`_HOST_MIN_INTERVAL`, default 0.1s) between requests
to the *same* host — replacing the old per-client `time.sleep(0.1)` calls — and
honours that cap even when many workers fire at once. Different hosts are
throttled independently, so parallel lookups across services are not serialised.
Both functions remain `lru_cache`d, so this only benefits cold paths.

## 11. Key Design Decisions

### D1: Toolbox over Graph
The agent decides what to call rather than following a predefined workflow graph. Validation and HITL feedback can send the process to any earlier stage. Mitigated by max iterations and HITL escalation.

> **Clarification:** The agent loop uses LangGraph's `StateGraph` as its execution runtime — the `"model"` and `"tools"` nodes and `should_continue` conditional edge form the LLM-calling machinery. The *workflow* (which entities to draft, when to validate, what to look up) is **not** encoded in the graph; it emerges from the LLM's tool choices. The StateGraph provides structured message-passing, checkpointing, and looping — not a predefined pipeline.

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

### D7: ARC as Output, Not Input
The ARC folder structure is not scaffolded upfront. The agent builds a raw file inventory (path, size, mime type, first rows) during initialization, then progressively binds files to `LabProcess` instances as annotations emerge through conversation and HITL. The ARC tree is produced as an output by `arc_writer.py` once entity annotations are complete. ARC is a delivery format, not a working layout.

### D8: Observability via Reasoning Log
Every tool call, state change, and reasoning step is recorded in `CrateState.checkpoint.reasoning_log` as a structured event: `{"step": int, "action": str, "tool": str, "result": str, "timestamp": datetime}`. This log enables:
- **Live status** for web UIs (`get_status()` returns current phase, entity counts, MIT scores, iteration count, last action)
- **Session replay** for debugging — re-run the tool calls from the log against the same state
- **Progress tracking** — number of entities drafted, fields filled vs total, validation pass/fail counts
- **Diagnostics** — which lookups failed, which HITL checkpoints were rejected, how often the agent got stuck

The reasoning log is persisted with the session and survives resume. A future web UI can tail or stream this log without changing the builder's internals — the data structure is already there.

### D9: Approved Scan Roots (Security Guard Rail)
The `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set. When the agent calls `scan_files(path)`, the path is resolved to an absolute canonical form and checked against approved roots — if not found or within a subdirectory of one, scanning is denied. New roots are added only through user approval (HITL or CLI prompt at the `present_to_human` checkpoint). This prevents the LLM agent from accessing arbitrary filesystem locations and provides a clear audit trail. On macOS, this same mechanism protects user files. On Linux, it prevents scanning into `/proc`, `/sys`, or other system paths.

### D10: State Brief Injected via System Prompt, Not Message History
The per-turn state brief (session id, file/entity/iteration counts) is **not** appended to user
messages. Instead, `call_model` calls `_build_system_prompt_with_state()` and prepends the
result to the system prompt on every model invocation (Issue #66). Because the system prompt
is re-created fresh each time rather than persisted in MemorySaver, it never accumulates
duplicate metadata across turns. The LLM can still query full details via `get_status`.

### D11: CI Workflow (GitHub Actions)
A `.github/workflows/ci.yml` workflow runs on every push/PR to `main` (Issue #58). It executes
`uv sync`, `ruff check`, `ty` (continue-on-error), and `pytest` (excluding slow integration
tests). This prevents regressions from landing on `main` and keeps the SHACL validator-wiring
test gated.

### D12: Bounded Message History (Trim + Prune Before Each Model Call)
`MemorySaver` accumulates the full transcript across turns, so without intervention every
`app.invoke()` replays the entire conversation — including large tool outputs — making per-turn
input tokens grow linearly (cumulative cost quadratically) until the context window overflows
(Issue #61). The history is therefore **bounded before each model call** inside
`_assemble_model_messages` (`builder/agents/agent_loop.py`) via `_trim_history`, which runs two
layers in order:

1. **Prune consumed state-backed outputs** (`_prune_state_backed_outputs`). Tool outputs whose
   data already lives in `CrateState` — the scan/read listings from `_STATE_BACKED_TOOLS`
   (`scan_files`, `read_file_sample`, `read_multiple_files`) — are replaced by a short stub once
   they exceed `_PRUNE_CONTENT_THRESHOLD` chars. The `ToolMessage` is **rewritten, not dropped**,
   so the `AIMessage(tool_call)` → `ToolMessage` pairing is never broken.
2. **Token-budget trim** via `langchain_core.messages.trim_messages` with `strategy="last"` and
   `start_on="human"`. Keeping the most recent turns within the budget bounds per-turn input;
   `start_on="human"` guarantees the retained window never *begins* with a dangling `ToolMessage`
   (or an `AIMessage` whose tool_call lost its answer), i.e. **trimming never produces orphaned
   tool messages** — providers reject those.

Trimming is applied only to the *history* between the stable system prefix and the trailing state
brief, so the cache-friendly #60 layout (D10) is preserved: the cacheable prefix shifts only when
the history actually rolls over the budget, far less often than it grew before. The budget is the
`get_max_history_tokens()` knob — `VITRO_MAX_HISTORY_TOKENS` env var → `[agent] max_history_tokens`
config key → default `12000` — mirroring the `max_iterations` precedence. `_trim_history` never
raises into the loop: a trimming edge case falls back to the pruned (untrimmed) history and logs a
warning, so the heaviest payloads are still removed.

## 12. Project Structure

Annotated with where new components would live:

```
vitro-crate/
├── AGENTS.md                    This file
├── .github/workflows/ci.yml     CI workflow (ruff, ty, pytest on push/PR)
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
│   │   ├── scanner.py, scaffolder.py, drafters.py
│   │   ├── management.py, lookups.py, verification.py
│   │   ├── builder.py, validation.py, mit_assessment.py
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

### Profiling Dashboard
The `profile.ndjson` log produced by `ProfilingLogger` is the foundation for a live-status web UI. A frontend could tail this file to show real-time tool timing, node execution times, and iteration counters — without any changes to the builder's internals.

---

*This document is a living design artifact. Update as architectural decisions evolve.*
