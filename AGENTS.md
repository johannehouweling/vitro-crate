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
The agent's `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set that records user-confirmed paths. The guard **fails closed** (#197): with no approved roots, *nothing* is scannable. When the agent calls `scan_files(path)`:
1. The path is resolved to an absolute canonical form
2. If `approved_scan_roots` is empty (or the scanner receives `None`/empty `approved_roots`), the scan is **refused without walking** — the agent's own scan call never auto-approves a new root
3. It is checked against the approved set — if the target is not equal to, nor a subdirectory of, an approved root, scanning is denied
4. A hard denylist (`_is_forbidden_root`) refuses the filesystem root `/`, the user's home directory itself, and OS/system trees (`/System`, `/Library`, `/private`, `/var`, `/etc`, `/usr`, bare `/Users`, `/Volumes`) **even if such a path appears in `approved_scan_roots`**. Legitimate *subdirectories* (e.g. `~/Desktop/project`) are still allowed — only the bare roots are blocked
5. New roots are added **only** from a user-provided input path (`AgentEngine.initialize()` / `read_directory()`) or an explicit real approval — never from the agent's own scan call. The non-interactive `SimulatedHumanInterface` **denies** any `present(..., purpose="scan_root")` escalation, so it can never widen filesystem access on its own

The **same boundary now covers the file *read* and *write* tools** (#167), not just `scan_files`:
- **Reads.** `AgentEngine.run_tool` gates every file-reading tool — `read_file`, `read_excel`, `read_docx`, `read_file_sample`, `read_multiple_files`, `extract_pdf_text`, `preview_archive`, `unzip_file` — through the shared `scanner._contain(path, approved_roots)` helper. A path outside an approved root is refused before the file is opened; with no approved roots **every** read is refused (fail-closed). `read_multiple_files` filters out-of-root paths into its `skipped` list so an in-tree batch still works. This closes the prompt-injection vector where an injected metadata file made the agent read `~/.ssh/id_rsa`, `/etc/passwd`, or a `.env` of secrets.
- **Writes (export).** `_crate_mapping._file_dest` contains a File's `dest_path` to the crate output dir — an absolute path or a `..` that climbs out is refused and replaced with the safe `data/<slug>` fallback, so no payload byte is ever written outside the crate. `_file_source` refuses any source whose **realpath** escapes `input_path` (symlink-escape containment), so injection cannot package an arbitrary local file into the shareable crate.
- **Symlink escape.** `_contain` resolves the realpath before matching, so a symlink that lives inside an approved root but points outside it is refused for both reads and export sources. The `_is_forbidden_root` denylist also matches the *unresolved* path so symlinked OS trees (`/etc`→`/private/etc`, `/var`→`/private/var`) are still caught.

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
- **Base RO-Crate 1.2** must pass before ISA validation is meaningful
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

**MIME detection (Issue #148):** `_detect_mime_type` resolves in order — stdlib
`mimetypes`, then a scientific-format registry (`_SCIENTIFIC_MIME_TYPES`) covering
MS/microscopy/flow extensions the stdlib does not know (`.mzML` →
`application/x-mzml`, `.fcs` → `application/vnd.isac.fcs`, vendor binaries
`.raw/.wiff/.czi/.nd2/.lif/.d/...` → `application/octet-stream`), consulted BEFORE
the text-content sniff so binaries are never mislabeled `text/plain`, with
`application/octet-stream` as the true default for unknown binary. A NUL byte in
the header now reliably forces the binary default. `encoding_format_for_name`
exposes the same extension→media-type derivation (no disk read) for entity
drafting.

**Size ceilings (Issue #148):** the dedicated readers in `file_readers.py` share
the scanner's 100 MB ceiling (`_MAX_BYTES`), not the old 1 MB cap that silently
returned `None` for ordinary mid-size files; row/line caps keep memory bounded.
The agent loop turns a bare `None` from any file reader
(`read_file_sample`/`read_file`/`read_excel`/`read_docx`) into an actionable
"unreadable/too-large — skip it" message so a weak model stops re-calling it.

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

- **Pinned local contexts, deny-by-default for everything else (SSRF guard, #168).**
  `profiles/contexts/ro-crate-1.1-context.jsonld` and
  `ro-crate-1.2-context.jsonld` are committed copies of the RO-Crate JSON-LD
  contexts. `_install_offline_context_loader()` (run at import) intercepts the
  `HttpRequester` GET/HEAD proxy (and `fetch_fresh`) and serves these well-known
  context URLs from disk, so both resolution paths get the bundled copy and never
  touch the wire. Any **other** outbound dereference — a crafted `@context` (or any
  crate-controlled IRI) in an *untrusted* crate pointing at e.g. cloud metadata
  `169.254.169.254` or an internal host — is **refused, not fetched**:
  `_blocked_remote_response()` returns a benign synthetic-200 empty JSON-LD document
  (`{"@context": {}}`) for every non-allowlisted URL. Failing closed with a valid
  200 (rather than raising) is deliberate: rocrate_validator's JSON-LD document
  loader (`_patched_source_to_json`) catches a fetch *exception* and falls back to
  rdflib's own `urllib` opener — which would perform the very request we are
  blocking — so serving an empty context keeps resolution on our intercept (no
  urllib fallback, no network), injects no term mappings from the crafted context,
  and raises no spurious REQUIRED content issue. It also sets
  `ROCRATE_VALIDATOR_AUTO_WARM=0` to suppress rocrate_validator's best-effort cache
  warm-up (pure network traffic we don't need, since the context is bundled and the
  warm-up's other artifact — the spec HTML page — is unused by any check). Refresh
  the bundled files only when the pinned RO-Crate context version changes.
- **Transport failure ≠ content violation.** If a remote resource genuinely can't
  be dereferenced, rocrate_validator swallows the connection error inside the
  check and re-emits it as a REQUIRED *content* issue. `validate_crate` and
  `validate_crate_dict` detect those (a connection-error message on a
  remote-resolving check) and raise `ValidationTransportError` instead, so a
  network failure surfaces as a clear error — never a spurious REQUIRED issue and
  never a false negative in `build_and_validate` (which maps the exception to
  `{"ok": False, "error": ...}`). The regression test
  `tests/test_offline_validation.py` runs validation with the HTTP transport hard-
  blocked and asserts green + no spurious REQUIRED issue;
  `tests/test_validation_ssrf.py` validates a crate whose `@context` points at an
  attacker URL and asserts **no** outbound request reaches it (deny-by-default);
  the #59 e2e harness also runs with the network disabled to prove the path is
  offline-safe.

#### MIT & FAIR Assessors (`builder/tools/mit_assessment.py`, `builder/tools/fair_assessment.py`)
Score against `mit/invitro_tox.yaml` and `fair/indicators.yaml`. Both produce
scores, not pass/fail.

### External RO-Crate Packages

This project builds on the existing RO-Crate Python ecosystem rather than reinventing crate assembly, validation, or entity models:

| Package | PyPI | What it provides | How we use it |
|---------|------|-----------------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) | `uv add rocrate`<br>(import `rocrate`) | Official Python SDK for creating and manipulating RO-Crates. Provides `ROCrate`, `ContextEntity`, `File`, and other base entity classes. | The entity model classes in `profiles/models/isa.py` and `profiles/models/tox.py` subclass `rocrate.model.ContextEntity` and `rocrate.model.File`. The builder uses `ROCrate` to assemble the crate and serialise `ro-crate-metadata.json`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) | `uv add roc-validator`<br>(import `rocrate_validator`) | Official SHACL-based validation library. Supports multi-profile validation (base RO-Crate → ISA → domain extensions) with severity levels. | `profiles/validator.py` wraps this in three passes (RO-Crate 1.2, ISA, ISA-Tox), suppressing inherited-profile duplicates so each pass reports only its own layer. |
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

### File Tools
*The scanner/sampler triad below is engine-routed (not in `TOOL_SPECS`); the
full readers are specced and LLM-callable during the agent loop.*
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20, mode: str = "content") → str | None
  mode: "content" (first N lines), "summary" (file-type-aware), "overview" (metadata + summary)
read_multiple_files(paths: list[str], lines: int = 50, mode: str = "content") → dict
  mode: same options as read_file_sample
read_file(path: str) → str | None             # full read by extension (txt, csv, json, xlsx, docx, md, pdf)
read_excel(path: str) → str | None            # .xlsx → pipe-delimited text
read_docx(path: str) → str | None             # .docx → plain text
extract_pdf_text(path: str) → str             # structured PDF: [Page N] text, tables, image metadata
preview_archive(path: str) → dict             # list a .zip's members + metadata without extracting
unzip_file(path: str, output_dir: str | None = None) → str   # extract a .zip, returns extraction path
```
`scan_files`, `read_file_sample`, and `read_multiple_files` run during session
initialization to classify inputs and feed the state brief. The full readers
(`read_file`/`read_excel`/`read_docx`/`extract_pdf_text`) and the archive tools
(`preview_archive`/`unzip_file`) are dispatchable so the agent can pull a file's
full contents on demand. There is **no `scaffold_arc` tool** — ARC is an *output*
format only (D7); the ARC folder tree is materialised at export time by
`builder/writers/arc_writer.py::write_arc`, not assembled from scanned inputs.

### Entity Drafting Tools
```
scaffold_isa_backbone(investigation=None, study=None, assay=None, validate_base=False) → dict  # composite: linked Investigation→Study→Assay in one call (idempotent-WITH-merge: a reused layer's EMPTY fields are filled from the supplied hints, fill-don't-clobber), the fast path to a BASE-passing crate
materialize_aop_subgraph(aop_id: str, study_id: str | None = None) → dict  # composite: one AOP-Wiki id → AdverseOutcomePathway + KeyEvent[] + KeyEventRelationship[] subgraph, cross-linked deterministically; optionally wired onto a Study
resolve_compound(name: str, hints: dict | None = None, verify=None) → {entity_id, name, identifiers, verifications, verified, source}  # composite: chemical name → lookup_compound → draft_molecular_entity → verify_identifier, in one idempotent call; carries the looked-up CAS + PubChem CID and never keeps an unverified id (D5)
resolve_publication(title: str, verify=None) → {ok, doi, entity_id, title, score} | {ok: False, reason, title}  # composite: publication title → Crossref title-search → confidence gate → draft_publication_with_authors(doi=…), in one idempotent call; commits a DOI only on a high-confidence match (score floor AND near-exact title) and never fabricates one (D5)
draft_publication_with_authors(doi: str) → {publication_id, doi, authors:[{name, person_id, orcid, resolution}], hitl}  # composite (engine-routed, HITL-capable): publication + every author wired as a Person, each author's @id harmonized to their ORCID via a verify-first cascade
draft_investigation(hints: dict) → Entity
draft_study(investigation_id: str, hints: dict) → Entity
draft_assay(study_id: str, hints: dict) → Entity
draft_molecular_entity(name: str, hints: dict) → Entity
draft_cell_line_sample(name: str, hints: dict) → Entity
draft_sample(hints: dict) → Entity                       # material input/output in the derivation chain
draft_process(assay_id: str, process_type: str, hints: dict) → Entity
draft_protocol(hints: dict) → Entity                     # LabProtocol a LabProcess can follow
draft_person(name: str, hints: dict) → Entity            # hints accept givenName/familyName; when neither is given they are derived by a deterministic split of `name` (comma-form "Last, First" inverted; a lone token kept as a family-name candidate, never mis-placed into givenName) so EVERY Person path is ISA-conformant (non-empty schema:givenName). ORCID stays empty (D5)
draft_organization(name: str, hints: dict) → Entity
draft_publication(doi: str, hints: dict) → Entity
draft_defined_term(name: str, hints: dict) → Entity
draft_property_value(name: str, hints: dict) → Entity
draft_file(name: str, path=None, role=None, encoding_format=None, additional_types=None, programming_language=None) → Entity
```
`draft_defined_term` persists a looked-up ontology / AOP / Key-Event term as a
`schema:DefinedTerm` contextual entity (Issue #141): pass the looked-up IRI as the
`url`/`@id` hint so the node gets a dereferenceable `@id`, and it then round-trips
into the `@graph` and is referenceable (via `set_fields`/`link`) as a `mentions` /
`measurementMethod` / `sampleType` target. `draft_property_value` creates a typed
`schema:PropertyValue` node (`value` + optional `propertyID` + `unitText`/`unitCode`).
Both back the previously-unfilled `defined_terms` / `property_values` CrateState
collections the mapping already rendered.
`materialize_aop_subgraph` (Issue #180) is the AOP counterpart of
`scaffold_isa_backbone`: from the single model-supplied numeric `aop_id` it calls
`lookup_aop` and materialises the entire pathway as typed contextual entities —
one `aopwiki:AdverseOutcomePathway` carrying its `has_molecular_initiating_event`
/ `has_key_event` / `has_adverse_outcome` / `has_key_event_relationship` link
arrays, one `aopwiki:KeyEvent` per MIE/KE/AO (all share `@type KeyEvent`,
discriminated only by the `eventType` string), and one
`aopwiki:KeyEventRelationship` per relation (`upstream_event` /
`downstream_event` by `@id`). Every node is keyed by its resolvable AOP-Wiki IRI,
so all wiring is deterministic and idempotent and no id is ever fabricated (D5).
These three types live in the shared `aop_entities` CrateState collection and
build via `_crate_mapping` as `ContextEntity` nodes typed by their own AOP class.
With `study_id`, the AOP is wired onto that Study via the `aop` reference (an
alias of `schema:mentions`), closing the largest gold-crate fidelity gap.

`resolve_compound` (Issue #179, task 3) is the chemistry counterpart of
`scaffold_isa_backbone`: from the single model-supplied compound `name` it fuses
the recurring `lookup_compound` → `draft_molecular_entity` → `verify_identifier`
chain into ONE deterministic call. (1) `lookup_compound` resolves the chemical
(PubChem, then a ChEBI fallback) — a miss returns `{ok: False, error}` and creates
no entity; (2) `draft_molecular_entity` mints (or, idempotently, reuses) the
`MolecularEntity` carrying the looked-up `cas` / `pubchem_cid` (and `smiles` /
`inchikey` / …) — the build's shared `_identifier_pv` path turns `cas` +
`pubchem_cid` into the `[CAS, PubChem CID]` identifier PropertyValues, so this
composite never hand-rolls that wiring; (3) `verify_identifier` confirms each
minted identifier against source. **D5:** `verify_identifier` *clears* any value
that does not resolve, so a failed identifier never lingers as a fabricated id —
the per-field verdicts are surfaced in `verifications` and `verified` is the AND of
them. Looked-up identifier fields win over same-named caller `hints`, and the
entity id is derived deterministically from the name so re-running reuses the
entity rather than duplicating it.

`resolve_publication` (Issue #179) is the citation counterpart of
`resolve_compound`, closing the gap PR #217 deferred: a plan carries a
publication *title* only (D5 — no DOI), but ISA REQUIRES a `ScholarlyArticle` with
an identifier (and BASE requires the auto-wired root `citation` `@id` to be an
absolute URI) — both unreachable from a title alone. From the single
model-supplied `title` it (1) runs a Crossref `query.bibliographic` title-search
(`lookups.crossref.search_works_by_title`) for candidate works ranked by
Crossref's relevance `score`; (2) applies a **D5 confidence gate** — a candidate
is committed ONLY when it clears BOTH the Crossref score floor AND a
normalized-title near-exact match (token-overlap threshold), so a high score on a
*different* paper, a weak score on the right title, or no candidate all return
`{ok: False, reason: "no confident DOI match", title}` and create NO entity (a DOI
is never fabricated from a title); (3) on a confident match delegates to
`draft_publication_with_authors(doi=…)`, reusing its DOI→`ScholarlyArticle`+authors
path (the ORCID cascade is handled there). It is idempotent (keyed by the resolved
DOI). It is invoked by code (materialize / guidance), not chosen by the weak
model, but is registered four-place for consistency with `resolve_compound`.

`draft_publication_with_authors` (Issue #180, deferred item) is the citation
counterpart of the composites above and the **only engine-routed, HITL-capable
drafter** (registered `takes_human=True`; the engine injects the active
`HumanInterface` as a `human_interface` kwarg). It calls `lookup_doi`, ensures the
`ScholarlyArticle` exists in state, and for EACH author creates/reuses a `Person`
wired as the article's `author`, harmonizing the author's `@id` to their **ORCID**
via a verify-first cascade (stop at first success): **(a)** the Crossref ORCID on
the author; **(b)** an in-crate `Person` with a *verified* ORCID matching the
author's family name + given/initial (affiliation-preferred) — this resolves the
gold case where citation `Fabian Wagenaars` reuses root `F.M.A. Wagenaars`'s ORCID
`0000-0003-4766-7358`; **(c)** a public ORCID search
(`lookups.orcid.lookup_orcid_by_name`, the `/v3.0/expanded-search` endpoint via the
shared rate-limited HTTP layer); **(d)** fallback to a synthesized
`#CitationAuthor_<Given>_<Family>` Person (the legacy behavior). **Confidence rule
for (c):** auto-accept **only** a *single* candidate that is a STRONG match (family
+ full — not initial-only — given name) and that passes name verification;
*anything else* — multiple candidates, a weak / initial-only match, or a sole
strong match that fails verification — **escalates to HITL** (`present` the ranked
candidates plus a none/skip option, then optionally `request_input` for a pasted
ORCID). **D5:** an ORCID from (a) or (c), and an HITL-chosen one, is attached only
after `lookup_orcid` resolves it and the family name matches; (b) is already
verified; an ORCID-resolved Person also carries its ORCID `identifier`
PropertyValue at build via the shared `_identifier_pv` path. HITL fires **only** on
genuine ambiguity — never when an author is confidently resolved or confidently
absent — and when no `human_interface` is available an ambiguous author falls back
to a synthesized id rather than guessing.
The `hints` parameter is **typed per entity type** (Issue #90). Each `draft_*`
tool advertises a JSON-Schema built by `_crate_mapping.draft_hints_schema(type)`
from the single source of truth `_crate_mapping.ENTITY_DRAFT_SCHEMA` — allowed
scalar keys plus reference keys, the latter a strict subset of `_REF_FIELDS`
(asserted by test) so the advertised reference vocabulary and the crate-mapping
resolver cannot drift. The schema is open (`additionalProperties: true`), so a
weak model sees the high-value keys without the long tail being forbidden.

`LabProcess` hints additionally advertise a `units` map plus the optional
subtype parameters (`assay_kit`/`substrate` for EndpointReadout,
`acceptance_criteria`/`evaluation_criteria` for DataAnalysis) (Issue #143):
`_build_process` threads `units=f.get('units')` into the Exposure /
EndpointReadout / DataAnalysis constructors so each `ParameterValue` carries its
`unitText`, and threads the optional params into the matching subtype. A
`CellLineSample`'s `passage` / `growth` / `organ` / `tissue` hints are promoted to
ISA Sample Characteristics — `schema:additionalProperty` PropertyValue nodes
carrying the value and, when known, the property's ontology IRI (`organ` / `tissue`
mirror the gold crate's `Organ` / `Tissue` characteristics with the ISA-Tox
`param/{organ,tissue}` `propertyID`; Issue #180). A `LabProcess`'s
`additionalProperty` field is likewise resolved to its in-state PropertyValue
reference(s) at build time (gold `#report_analysis` → `[#pv_repro_score]`) — only
PropertyValues already present in state (or bare IRIs) are wired; a score is never
computed or fabricated here (D5).

**Looked-up identifiers round-trip as `schema:PropertyValue` nodes** (Issue #180,
deterministic build path — no new LLM tools). `_crate_mapping._identifier_pv(name,
value, property_id_url=None)` mints an identifier PropertyValue with a stable id
mirroring rocrate-wizard's `param_id` (`#param_<slug(name)>_<sha1("name|value")[:10]>`)
and emits `propertyID` as an `{"@id": …}` node when a url is given. At build time:
a Person carrying `orcid` gains an `ORCID` PropertyValue identifier
(`propertyID {"@id": https://orcid.org}`); a MolecularEntity carrying
`cas`/`casrn`/`cas_number` and/or `pubchem_cid` gains `[CAS, PubChem CID]`
identifiers in that order (CAS has no `propertyID`; PubChem CID's is
`{"@id": https://pubchem.ncbi.nlm.nih.gov/compound}`). These source fields are
consumed structurally (kept off the node as raw literals). A Person's
`affiliation` and a Publication's `author` are resolved to `{"@id"}` references
(via `_wire_reference` / `_resolve_many`) rather than emitted as literals, so
`Person.affiliation` points at its Organization and `ScholarlyArticle.author`
is an array of `Person` references. No identifier is ever fabricated — only values
already in state (from a lookup) are wired (D5). `draft_property_value` defaults
and `@id`-wraps the `propertyID` for a PropertyValue named `DOI`
(`OBI_0002110`) or `PubMedID` (`OBI_0001617`), the IRIs the tox
`{10,11}_*_property_value.ttl` shapes require as `sh:hasValue` nodes — so a
DOI/PubMedID PropertyValue passes the tox pass instead of silently failing.

`draft_file` auto-derives `encodingFormat` from the file extension (`name`, then
`path`) when the caller omits it (Issue #148), via the same scientific-format-aware
MIME registry the scanner uses — so `run.mzML` becomes `application/x-mzml` and
`acquisition.fcs` becomes `application/vnd.isac.fcs` rather than being left blank
or mislabeled `text/plain`. An explicit `encoding_format` always wins; an
extensionless name leaves the field unset. `additional_types` co-types the node
beyond plain `File` (Issue #180) — passing `["SoftwareSourceCode"]` plus
`programming_language="Python"` makes an analysis script a `@type:[File,
SoftwareSourceCode]` data entity with `schema:programmingLanguage` (gold
`plot.py`); both are consumed structurally (`File` is always the leading type and
duplicates are dropped), and a plain File keeps its scalar `@type`.

### Entity Management Tools
```
set_fields(entity_id: str, fields: dict, source="llm") → Entity
set_crate_metadata(title=None, description=None, accession=None, release_date=None, date_modified=None) → {title, description, accession, release_date, date_modified}  # set Root Data Entity (crate-level) scalar metadata
remove_entity(entity_id: str, cascade: bool = False) → bool
list_entities(entity_type: str | None) → [Entity]
list_scanned_files(name_contains=None, mime_contains=None, offset=0, limit=200) → {total_scanned, matched, files:[{path, filename, size, mime_type}]}
```
`set_crate_metadata` (Issue #180) is the single setter for **crate-level**
metadata — the scalar properties of the Root Data Entity (`./`), as opposed to
`set_fields` which mutates a graph *entity*. It writes onto `CrateState.metadata`:
`title` / `description` / `accession` map to the root's `name` / `description` /
`identifier`, and `release_date` / `date_modified` add `release_date` /
`date_modified` to `CrateMetadata` (defaulting to `None`, so sessions saved
before these fields existed still load) and emit `schema:releaseDate` /
`schema:dateModified` on the root at build time (the gold S-VHPS21 root carries
both alongside the auto-set `datePublished`). Only the arguments actually passed
(non-empty) are written — a date is never fabricated (D5) — and ro-crate-py's
auto-set `datePublished` is left untouched unless explicitly overridden.
`list_scanned_files` retrieves the **full** raw scan inventory from
`CrateState.scanned_files`. `scan_files` only surfaces a ~15-file sample and its
output is later pruned from history (D12), so this is how the agent re-reads the
complete file list to decide which files to place/annotate — paginated
(`offset`/`limit`) and filterable (`name_contains`/`mime_contains`) so it stays
token-bounded, and compact (no `first_rows` preview).
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
draft_process_chain(assay_id: str, chain: [{process_type, hints?, object?, result?}], validate_after=None) → {assay_id, process_ids, steps, synthesized}  # composite: create + wire the whole CellCulture→Exposure→EndpointReadout→DataAnalysis chain in one idempotent call, synthesizing the EndpointReadout/DataAnalysis outputs the build has no fallback for
link(from_id: str, relation: str, to_id: str) → {from_id, relation, to_id}
attach_files(to: str, name_contains=None, mime_contains=None, paths=None, role=None) → {attached, file_ids, to}
check_provenance() → {ok, issues:[{entity_id, property, message, fix, severity, profile}]}
```
`draft_process_chain` (Issue #179, task 3) is the `link` composite — the
proactive counterpart of `fix_required_issues`' missing-output repair rule. It
fuses the recurring `draft_process` + `link` sequence that wires the gold
S-VHPS21 derivation chain into ONE idempotent call. `chain` is an ordered list of
step dicts (`process_type` + optional `hints` + optional explicit `object` /
`result` ids); any **subset** of the four subtypes is allowed (partial chains
work) and steps are always wired in the canonical order regardless of input order,
so a weak model cannot mis-sequence the provenance. Each producing step's output
is threaded into the next step's input, so the chain is fully connected and
referenceable. **Its load-bearing job (§14.3):** `EndpointReadout`/`DataAnalysis`
have **no build-time output fallback**, so a process with no explicit `result`
(and, for DataAnalysis, no `object`) fires a tox REQUIRED Violation. The composite
**synthesizes** the missing output — a placeholder `Sample` (via `draft_sample`)
for a material producer (CellCulture) or a placeholder `File` (via `draft_file`)
for a data producer (Exposure/EndpointReadout/DataAnalysis) — and `link`s it, so
the chain never dangles. **Requires:** an existing `assay_id` + each step's
`process_type`. **Synthesizes (only when not supplied/derivable):** the produced
output entity (and a DataAnalysis input `File` when it has no upstream step).
**Respects:** any explicit `object`/`result` you pass — those win over synthesis.
Placeholders carry only structural metadata (name, crate path, role); they
**never fabricate measurement values or identifiers** (D5) — they are header-less
stubs to be filled with `populate_condition_table` / `set_fields`. Idempotent:
placeholder/process ids are derived deterministically from the step, so re-running
reuses them rather than duplicating.
`attach_files` is the bulk *placement* verb (#177): it associates a **group** of
scanned files with a Study or Assay in one call (select by `name_contains` /
`mime_contains` / explicit `paths`, stamp an optional `role`). For each match it
finds-or-creates a `File` entity (deduped by on-disk source, so it is never
duplicated and drops out of the #175 root fallback) and appends it to the
target's `hasPart`, which `_add_structural` resolves to nest the file under that
dataset. It is the scalable counterpart to per-file `draft_file` and complements
the #175 auto-include fallback (inclusion) with agent-driven placement
(association). Process inputs/outputs stay with `link`.
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
lookup_compound(name: str) → CompoundData | None   # PubChem (→ ChEBI fallback)
lookup_dtxsid(query: str) → DtxsidData | None       # EPA CompTox (DTXSID)
lookup_cell_line(accession: str) → CellLineData | None  # Cellosaurus
lookup_aop(aop_id: str) → AOPData | None            # AOP-Wiki
lookup_bao_term(query: str) → TermData | None       # OLS/BAO
lookup_ontology_term(query: str, ontology: str) → TermData | None  # OLS (any ontology)
lookup_unit(unit_string: str) → TermData | None     # OLS/UO (units)
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
fix_required_issues(severity="required", profile="all") → {ok, fixed:[{issue, rule, action}], remaining:[{issue, reason}]}
export_crate(output_path: str) → CrateBuildResult
build_crate(output_path: str) → CrateBuildResult     # back-compat alias of export_crate
validate(crate_path: str) → ValidationReport
validate_table(file: str, table_schema: dict, foreign_keys: dict | None = None, entity_id: str | None = None) → {ok, issues}
populate_condition_table(exposure_id: str, rows_or_csv_path: list[dict] | str, output_dir: str | None = None) → {ok, path, rows}
```

`validate_table` is the **data-content (payload) layer** (#95): it validates a
CSV's rows against a Frictionless `tableSchema` — separate from the SHACL
metadata passes (see §6, Data-Content Layer). Issues use the same routable shape
with `profile="data"`.

`populate_condition_table` (Issue #144) writes the per-well rows into an
Exposure's CSVW condition table — replacing the header-only placeholder #94
materialises — either from a list of row dicts (keyed by the condition-table
column titles) or by attaching a user-supplied plate-map CSV. The condition
table's typed schema is the gold S-VHPS21 crate's full **10 columns** (#180,
Lane D): `well_id`, `assay`, `cell_line`, `compound`, `concentration_value`,
`concentration_unit`, `exposure_duration`, `experiment`, `technical_replicate`,
`control` — each with a `datatype` + ontology `propertyUrl` (and a `valueUrl`
resolving the cell-line/compound columns to their in-crate Sample /
MolecularEntity id). Population fills whatever columns the data provides; the
schema describes all 10 and missing cells are written empty. It targets the
exact path the build wires (`_crate_mapping._condition_table_rel`), so the #94
CSVW typing (`tableSchema`) stays attached to the populated table. The companion
bridge `data_content.csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)` converts
those CSVW column descriptors into the Frictionless `{fields:[...]}` shape (the
single source of truth — `_CONDITION_TABLE_HEADER` is also derived from the
column constant, so the placeholder header and the typed schema cannot drift),
so `validate_table` needs no hand-authored schema for the populated table.

An `EndpointReadout` that already emits result file(s) additionally emits a typed
`raw_measurements.csv` `csvw:Table` (#180, Lane D) — 3 columns (`well_id`,
`measured_value`, `measured_unit`), typed the same way the condition table is
(`datatype` + `propertyUrl`). It is **appended** to the readout's results, never
substituted, so a resultless readout still fires the "MUST have a result" issue
for `fix_required_issues`. The CSV is header-only — no measurement rows are
fabricated (D5). Both tables emit `propertyUrl`/`valueUrl` as `{@id}` references
(not bare strings): RO-Crate 1.2's base profile flags an IRI value used as a
string when that IRI is also a described entity (e.g. the cell-line `NCIT_C16403`,
which a `CellLineSample` materialises as a `cell line` `DefinedTerm`).

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

`fix_required_issues` is the **deterministic repair loop** — the keystone of the
§14 pipeline (Issue #179, task 1). `build_and_validate` *routes* each issue to
`{entity_id, property, fix, …}` but nothing mapped an issue back to a *repair*;
this closes that gap. It runs `build_and_validate`, dispatches each issue through a
small, ordered table of issue-shape → repair rules (`builder/tools/repair.py`,
`RepairRule`), then **re-validates** to confirm what actually cleared (it trusts the
validator's verdict, not a rule's optimism). A repair runs **only when the correct
value is already determined by state** — e.g. an `EndpointReadout`/`DataAnalysis`
missing its `result` where exactly **one** un-wired `File` already exists in state
is auto-wired via `link` (the §14.3 "no output fallback" trap). Anything needing
**new content, a new entity, or a fabricated identifier — or a genuinely ambiguous
target (2+ candidates) — is out of scope (D5)** and returned under `remaining` for a
bounded LLM leaf. It is **idempotent and side-effect-safe**: if nothing is
deterministically fixable it mutates nothing and returns every issue in `remaining`.
It maps a validation focus-node `@id` (e.g. `./#LabProcess_er1`) back to its state
entity by inverting `_crate_mapping._mint_id`. Each `fixed` item carries
`{issue, rule, action}`; each `remaining` item `{issue, reason}`.

`export_crate` is the **only** tool that touches disk — call it once the crate
is conformant to materialise the on-disk RO-Crate directory (payload included).
`build_crate` remains as a back-compat alias. The in-memory assembly path
(`assemble_crate(..., materialize_payload=False)`) skips writing the Exposure
condition-table placeholder CSV so validation stays a zero-disk operation.

**Auto-included scanned files (#175).** `assemble_crate(..., include_all_scanned=True)`
(the default, used by `export_crate`) packages *every* scanned file that the agent
has **not** already drafted as a `File` entity, attaching it to the root `hasPart`
as a plain `File` leaf — an honest *fallback* so the exported crate never silently
drops a data file. It is **inclusion only, not placement**: files the agent has
explicitly placed (under a Study/Assay, wired as a process `result`/`object`, or
given a role) already have a `File` entity and are deduped out by resolved source
path, so the agent's semantic association always wins. Reserved RO-Crate filenames
are skipped. The hot `build_and_validate` path passes `include_all_scanned=False` —
plain leaves don't change the validation verdict, so skipping them keeps the ReAct
loop fast. Semantic placement of file *groups* (which assay, which role) stays an
agent task (bulk placement tool — follow-up).

### Assessment Tools
```
assess_mit_coverage() → MITReport
assess_fair_maturity() → FAIRReport
```

### Session & HITL Tools
```
present_to_human(context: str, options: [str]) → HumanResponse
request_input(prompt: str, field_type: str | None = None) → HumanResponse
save_session(label: str) → SessionInfo
list_sessions() → [SessionInfo]
load_session(session_id: str) → SessionStatus
get_status() → SessionStatus
get_hint() → str
```
`present_to_human` offers a choice between `options`; `request_input` asks the
human for a single free-form value (e.g. a compound name, CAS number, or cell
line accession) when a lookup needs a missing identifier. `list_sessions` and
`load_session` drive the resume flow (§7); `present_to_human`/`request_input`
are engine-routed HITL tools (not in `TOOL_REGISTRY`), the rest are specced.

### Profiling
Every tool call and graph node execution is automatically timed and recorded by `ProfilingLogger` (see [docs/profiling.md](docs/profiling.md)). Profile data is written to `sessions/<session_id>/profile.ndjson` as newline-delimited JSON with event types including `tool_call`, `node_start`, `node_end`, and `hitl_wait`. This file is the primary input for timing analysis, debugging, and live status in future web UIs.

The dashboard derives a live **▶ / ⏸ / ⏹** agent-status badge from these events via the pure helper `determine_agent_status(records)` (`builder/tools/dashboard.py`, issue #193): **▶ driving** when a node/tool is in flight, **⏸ awaiting input** when blocked on a human, **⏹ idle** otherwise. Because a `tool_call` event is only logged *after* the tool returns, a pending HITL call would be invisible to inference — the agent is blocked *inside* `present_to_human`/`request_input` before the event is written. The engine therefore emits an explicit `hitl_wait` event immediately before invoking either HITL tool (`run_tool` in `builder/engine.py`); the subsequent `tool_call` for that tool marks the human's response. The badge renders in the CrateState overview panel title across both the live (`run_dashboard`) and static (`run_static_dashboard`) paths.

## 6. Validation Layers

| Layer | Severity | Meaning | Agent Action |
|-------|----------|---------|--------------|
| Base RO-Crate 1.2 | REQUIRED | Structural validity | MUST fix before proceeding |
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
- **CompTox (EPA)**: Name/CAS/InChIKey → DTXSID (the DSSTox anchor identifier)
- **Cellosaurus**: Accession (CVCL_xxxx) → name, species, disease, site, sex
- **AOP-Wiki**: AOP ID → full pathway graph (AOP, events, relationships)
- **OLS4 (generic)**: Free-text query + ontology short name → best-matching
  term IRI with a relevance score. Backs `lookup_bao_term` (BAO),
  `lookup_unit` (UO units), and `lookup_ontology_term` for any OLS-hosted
  vocabulary (EFO/OBI/NCIT/UBERON/ChEBI/…).
- **ORCID**: ORCID iD → name, affiliation, affiliation ROR
- **ROR**: Organization name → ROR ID, website URL
- **Crossref**: DOI → title, authors, journal, year

### Multi-Strategy Lookups
For chemicals, `lookup_compound` tries by name, then CAS, then **ChEBI** (via
OLS4) — a PubChem miss now falls back to resolving a ChEBI IRI rather than a
hard not-found. If all fail, ask the user for SMILES/InChI. The ChEBI fallback's
identity rides on **context-declared keys** so the `MolecularEntity` compacts
cleanly under RO-Crate 1.2 (Issue #243): the ChEBI CURIE on `chebiId`
(`schema:identifier`, mirroring `cas`/`pubchemCid`) and the dereferenceable
ontology IRI on `sameAs` as an `{"@id": …}` node. The legacy bare
`chebi_id`/`chebi_iri` keys were absent from the `@context` and failed the base
pass (and leaked into the HITL loop as unanswerable gaps), so they are gone.

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
The `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set. When the agent calls `scan_files(path)`, the path is resolved to an absolute canonical form and checked against approved roots — if not found or within a subdirectory of one, scanning is denied. New roots are added only through user approval (a user-provided input path at `initialize()`/`read_directory()`, or a real HITL approval). This prevents the LLM agent from accessing arbitrary filesystem locations and provides a clear audit trail. On macOS, this same mechanism protects user files. On Linux, it prevents scanning into `/proc`, `/sys`, or other system paths.

**Fail-closed (#197).** The guard previously failed *open*: when `approved_scan_roots` was empty the engine passed `approved_roots=None`, which the scanner treated as "no guard", and the first path the agent scanned was auto-approved. The agent could therefore scan the entire filesystem by naming any path. The guard now fails **closed**:
- The engine always passes a concrete allowlist (an empty `set()`, never `None`); the scanner refuses (returns `[]` without walking) whenever `approved_roots` is `None` or empty.
- The auto-approve-of-first-scan was removed: the agent's own `scan_files` call can never add a root. Roots enter the allowlist only from a user-provided input path or a real approval.
- A hard denylist (`scanner._is_forbidden_root`) refuses `/`, the user's home directory itself, `/System`, `/Library`, `/private`, `/var`, `/etc`, `/usr`, bare `/Users`, and `/Volumes` even if explicitly present in `approved_roots`; it is also enforced in `engine._directory_to_approve` so a forbidden directory can never *become* an approved root. Legitimate subdirectories are unaffected.
- `SimulatedHumanInterface.present(..., purpose="scan_root")` returns a `rejected` action, so the non-interactive default can never approve a new scan root (benign checkpoints still auto-approve).
- Follow-up: sandboxing the eval harness so it cannot scan outside its fixtures is tracked separately.

**Extended to read + write tools (#167).** The approved-roots boundary previously guarded only `scan_files`, so prompt injection could still escape it via the read tools (arbitrary local file read, e.g. `read_file('/etc/passwd')` or a secrets `.env`) and the export writer (a `..` traversal `dest_path`, or a symlinked source escaping the input tree). The fix adds one shared containment primitive, `scanner._contain(candidate, approved_roots) -> Path | None` (resolve realpath, reject when not inside any approved root, apply the `_is_forbidden_root` denylist, fail closed on empty/None roots), applied at three choke points: the read-tool dispatch in `engine.run_tool` (gates `read_file`/`read_excel`/`read_docx`/`read_file_sample`/`read_multiple_files`/`extract_pdf_text`/`preview_archive`/`unzip_file`), `_crate_mapping._file_dest` (contains `dest_path` under the crate output dir, else `data/<slug>`), and `_crate_mapping._file_source` (refuses sources whose realpath escapes `input_path`). The scanner read functions themselves stay unguarded so `scan_files` can still sample files internally; the gate lives at the orchestration layer.

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

### D13: ISA hasPart Hierarchy — Investigation is the Root

The Investigation **is** the Root Data Entity (`./`); the ISA RO-Crate profile mandates this and
the SHACL shapes forbid alternatives (a Study carrying `additionalType "Study"` MUST be `hasPart`
of the root via `StudyMustBeReferencedFromInvestigation`, so the root cannot itself be a Study).
`_add_structural` (`builder/tools/_crate_mapping.py`) therefore:

- **Folds a single Investigation entity onto the root** instead of emitting a duplicate
  `#Investigation_*` node, and indexes it to `./` so `investigation_id` references resolve there.
  (0 or 2+ Investigations keep separate nodes — out of scope, rare.)
- Keeps **Study and Assay as their own `Dataset` + `additionalType` nodes** (required by the ISA
  shapes); an Assay MAY be `hasPart` of either its Study or the Investigation (`2_assay.ttl`
  `AssayMustBeReferencedFromInvestigation`).
- Mints **distinct hierarchical identifiers** per level via `_isa_identifier`
  (`FAB-2026` → `FAB-2026/study-<id>` → `…/assay-<id>`). The `@id` (the path) is the true unique
  key; the `identifier` *property* is the ISA descriptor and must not collide across levels.
- Attaches each **result `File` to its producing Assay's `hasPart`** (de-duped via `_append_unique`)
  and removes it from the root's auto-added `hasPart` (`_remove_child`) — raw/processed data are the
  data of an assay. Files stay reachable from the root transitively (File → Assay → Study → `./`).
- Re-emits the Assay's **`dataFiles` / `resources` PageTab aliases** (both expand to `schema:hasPart`
  via `profiles/context.py`) as resolved File references *and* nests those Files under the Assay's
  `hasPart`, un-parenting them from the root — same move as result Files, so the gold-crate JSON keys
  round-trip without breaking reachability (`_wire_dataset_aliases`, #180 Lane C).

Round-trip is symmetric: `read_existing_crate` (`builder/readers/existing_crate.py`) recovers the
**bare** entity_id (stripping the type-qualifier so `#Study_study_1` → `study_1`, not the unbounded
`#Study_Study_…` double-prefix), reconstructs the `study_id`/`assay_id` linkages the crate encodes
structurally via `hasPart`/`about`, and folds the root back into an Investigation entity — so
build → read → build is idempotent and structure-preserving. `_build_process` reads the
`input`/`output` aliases as well as `object`/`result` so I/O survives the round-trip.

### D14: Entity-Graph Visualization (`builder/writers/provenance_dag.py`, Issue #130)

A dependency-light **Mermaid** renderer turns a built crate's `@graph` into a node-link diagram for
visual exploration. `build_crate_graph(metadata, *, layer, all_edges)` is the deterministic model
(nodes classified into the three paper layers — packaging / ISA / ISA-Tox — with each referenced
`@id` marked *in-crate* / *external-identifier-backed* / *dangling*, orphans flagged, cumulative
`--layer` filter); `render_crate_graph` formats it as a layered flowchart (node colour/shape =
functional category, subtle per-layer box wash, "Outside the crate" group, legend).
`render_provenance_mermaid` is the focused LabProcess-derivation view. The CLI exposes both:
`python -m main --graph [--view crate|provenance] [--layer crate|isa|isa-tox] [--format html|mermaid]`
— `html` renders in the browser, `mermaid` prints the source.

**Embedded in the crate.** `export_crate` (the disk-writer) writes the entity graph as
`ro-crate-graph.mmd` into the crate and registers it as a `File` + `CreativeWork` `about` the Root
Data Entity (so ro-crate-py links it from `./`'s `hasPart`) — the diagram travels with the data and
is reachable from the root. It is generated from the `@graph` *before* its own File node is added
(never self-depicting) and is in `_EXCLUDED_IDS` so re-rendering an exported crate ignores it.
Embedding is automatic (no separate agent tool — a free byproduct of building the crate) and can be
turned off with `export_crate(..., embed_graph=False)`. Inline-rendering the Mermaid in the #86
`ro-crate-preview.html` is the natural next step.

**Maturity report (`ro-crate-maturity.html`, #85).** `export_crate` also embeds a human-readable
maturity report as a `File` + `CreativeWork` `about` `./` (same mechanism as the graph). It is
rendered by `builder/writers/maturity_report.py` (`build_maturity_html`) and covers four axes:
profile adherence (rendered from the crate's existing `state.validation` — REQUIRED/RECOMMENDED
issues surfaced as suggestions; it does **not** re-run the SHACL validator, so the embed adds no
validation cost to export — validation stays a separate step), FAIR indicators + DSM level
(`assess_fair_maturity`), OECD MIT coverage (`assess_mit_coverage`), and a derived
reproducibility-readiness checklist. Embedding is automatic, best-effort (a reporting failure never
fails the export), and can be turned off with `export_crate(..., embed_report=False)`.

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
│   │   ├── provenance_dag.py     Mermaid entity-graph / provenance DAG (#130)
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

## 14. Architecture Evolution: Deterministic Pipeline (planned — Issue #179)

> **Status:** Cutover DONE for the interactive build (Issue #179). The A/B gate
> (task 6) is decided: on the 3 shared corpus cases the deterministic pipeline
> reached **3/3** ISA-Tox conformance vs ReAct **1/3** (the weak LLM stalled at
> 0–1 iterations on the hard cases), so the **deterministic pipeline + HITL
> guidance tail is now the DEFAULT interactive architecture** (`main.py
> --interactive` → `run_interactive_build`). The prose-prompt ReAct StateGraph of
> §4 / D1 is **retained behind `--legacy-react`**, not deleted — the system-prompt
> strip is a separate follow-up (task 7). This supersedes the earlier "keep flat
> ReAct, structure in the tool layer only" stance.

### 14.1 Decision

Move the *workflow orchestration* out of the LLM system prompt and into **code**.
Today the whole sequence — `scan → scaffold ISA backbone → draft entities →
build_and_validate → fix REQUIRED bottom-up → enrich → export` — is encoded as
**prose in `builder/agents/system_prompt.py`** and re-derived by the model every run.
The agent runs on a weak model (DeepSeek-flash) that collapses on exactly that
multi-turn orchestration but is fine at bounded extraction. The target architecture is a
**deterministic pipeline with the LLM confined to bounded leaves**, and a **small agent
retained only for the conversational / unstructured-input tail**.

**Scope of the claim (honest):** a deep-research pass refuted the broad "plan-and-execute
beats ReAct on reliability" claim — ReAct often has a higher final pass rate. The
defensible win *for this system* (known step ordering + rigid SHACL-validated output +
weak executor) is **cost, latency, reproducibility, testability, predictability** — not
blanket correctness. The cutover was **gated on an in-repo A/B** (task 6) and that gate
has now passed for the interactive build (3/3 vs 1/3 ISA-Tox conformance on the shared
corpus — the pipeline ALSO won on correctness for these cases because the weak executor
stalled), so the pipeline is the default interactive path with ReAct retained behind an
opt-in flag.

### 14.2 Target shape

```
INPUT (dir / zip / conversation)
   │
   ▼  DETERMINISTIC PIPELINE (code, not model-driven)
 scan ─ scaffold ISA backbone ─ draft entities ─ build_and_validate ─ fix loop ─ enrich ─ export
                                     │  (bounded LLM leaf: extract→entity)     │ (deterministic
                                     ▼                                          │  dispatch over
                              cheap drafter model                               │  routed issues;
                                                                                ▼  LLM only for
   small TAIL AGENT (strong model) ── only for: no-metadata conversational build,   content repairs)
                                       genuine ambiguity, HITL
   │
   ▼ OUTPUT: ARC RO-Crate dir + payload + embedded graph/maturity/preview
```

- **Spine = code.** The Priority 1–4 heuristic (§4) becomes control flow, not prose.
- **Leaves = cheap model.** Drafting/disambiguation only (binds the §4.4 drafter tier).
- **Leaf context = bounded file BODIES, not just filenames (#231).** The single
  `extract`/`draft` leaf is fed by `pipeline.py::_gather_context`, which now reads a
  bounded BODY excerpt of each non-tabular rich file (`.json` / `.docx` / `.pdf` …)
  via the existing `builder/tools/file_readers.read_file` — preferring the cheap
  tabular `first_rows` preview the scanner already captured. Reads are **fail-closed
  to `state.approved_scan_roots`** (same `_contain` guard as `engine.py`), never
  raise out of the spine (reader errors are logged + skipped), and are capped per
  file AND in total by `pipeline.py::_MAX_CONTEXT_CHARS` so the one bounded call
  stays token-safe. Before #231 the leaf saw filenames + tiny previews only, so
  `extract_plan` returned an empty plan and the backbone fell back to the literal
  default names; now document titles/abstracts/SOP headings reach the leaf. The
  empty-context path is still a strict no-op (`""` ⇒ no provider call), preserving
  the no-provider determinism guarantee.
- **Fix loop = deterministic.** `build_and_validate` already returns issues pre-routed to
  `{entity_id, property, fix, severity, profile}`; a code loop dispatches each to a
  lookup / `set_fields` / `link`, calling the LLM only for "draft new content" repairs.
- **Tail = small strong-model agent.** The one place open-ended judgement is irreducible.

### 14.3 Gate audit outcome (two multi-agent audits, 2026-06-25)

"Do we have the tools to build an ISA-Tox crate from code?" was answered against both the
SHACL/MIT requirements (267 elements, 82 tools mapped) **and** a real gold crate
(`crates_out/S-VHPS21_rocrate`, built by the external `rocrate-wizard`):

- **Validity:** **YES today** — empirically, one `scaffold_isa_backbone(...)` call yields
  `{base, isa, tox}` all true with **zero issues**. No REQUIRED element lacks a code path.
- **Full fidelity:** **Not yet** — reproducing a rich real crate needs ~17 deterministic
  tools/extensions, *every one proven feasible by rocrate-wizard*. Tracked in the
  toolbox-completion issue (companion to #179).

**Two conditional Violation traps** (the "wiring contract" — fire only when those
entities exist, both code-fixable; document for callers):
1. A `PropertyValue` named `DOI`/`PubMedID` is SHACL-duck-typed and MUST carry
   `propertyID` as an **`@id` IRI node** (the OBI IRI: `DOI`→`OBI_0002110`,
   `PubMedID`→`OBI_0001617`). **Fixed (#180):** `draft_property_value` now defaults
   the IRI by name and `@id`-wraps it (was a silent string-literal Violation).
2. `EndpointReadout`/`DataAnalysis` have **no `result`/`object` build-time fallback**
   (unlike CellCulture/Exposure); a process with no explicit output fires a Violation.
   Fix: synthesize/`link` outputs (fold into `draft_process_chain`).

**Highest-leverage gap:** `materialize_aop_subgraph` — closes ~36 items (1 AOP + N
KeyEvents + M KeyEventRelationships) in one tool. Fully deterministic: port
`rocrate-wizard` `lookups/aopwiki.py` (returns the whole subgraph pre-wired from AOP-Wiki
JSON) + `builder.py:265-278` (`crate.add(ContextEntity(...))` per node, then wire AOP to
Study via `mentions`/`aop`). The only LLM-supplied input is the numeric `aop_id`.

### 14.4 Migration tasks (each its own `jh-*` branch + PR, TDD)

Pipeline (#179): (1) `fix_required_issues` deterministic repair loop — *the keystone*;
(2) drafters as real LLM leaves — **done** (see [the drafter-leaf](#the-drafter-leaf-leavespy)
below), and the leaf is now **wired into the spine's `_draft_entities` hook** (§14.5
step 2 — was a deferral), gated to a strict no-op when no LLM provider is configured;
(3) composite meta-tools incl. `draft_process_chain`
— **done**: it synthesizes the EndpointReadout/DataAnalysis outputs the build has no
fallback for (closing the §14.3 Violation trap) and wires the whole chain in one
idempotent call (see §5 Derivation Chain Tools); (4) pipeline spine — **done and now
the DEFAULT interactive path** (`builder/agents/pipeline.py::run_pipeline`, see §14.5):
a deterministic, code-driven orchestrator. Post-A/B-gate (task 6) it **replaces** the
`should_continue` ReAct loop as the default `main.py --interactive` build (via
`run_interactive_build`, §14.6.1); ReAct is retained behind `--legacy-react` (the
`should_continue` graph itself is untouched pending the task-7 prompt strip). The spine
is also selectable in the eval harness (`python -m eval --arch pipeline`); (5) the tail
— **done**: the gap engine
(`assess_gaps` #215, §14.6) feeds the deterministic HITL guidance loop
(`run_guidance` #218, §14.6.1), now **wired into the interactive build path**
(`run_interactive_build`, `builder/agents/build.py`) so a real user gets the
guidance tail after the automated pipeline while the A/B path (simulated /
headless) runs the guidance-free pipeline alone; (6) **A/B eval harness — decision
gate — done & PASSED**: the harness compared the two architectures on the shared
corpus and the pipeline won (3/3 vs 1/3 ISA-Tox conformance), triggering the
interactive cutover above; (7) prompt + docs — **pending** (strip the orchestration
prose from `system_prompt.py` now that code owns control flow; the ReAct loop stays
reachable via `--legacy-react` until then).

#### The drafter-leaf (`leaves.py`)

Task 2's "Leaves = cheap model" primitive (§14.2). `builder/agents/leaves.py`
exposes a single pure function — **`draft_entity_fields(entity_type: str,
context: str, *, model: str | None = None) -> dict`** — the smallest unit of LLM
work in the pipeline: free-text/context in → a structured dict of one entity's
fields out, in a **single bounded model call**. It is a **library leaf, not an
LLM-callable tool** — the deterministic spine (task 4) imports and calls it; the
ReAct agent never sees it, so it needs **no four-place tool registration**. It
does **not** mutate `CrateState` and does **not** orchestrate; the spine feeds
its result into the deterministic `draft_*` state mutators.

Contract:
- **Drafter tier.** The call goes through `_build_chat_model(role="drafter")`
  (§4.4), so a cheap model does the extraction. With no drafter model configured
  this resolves to the primary model — a strict no-op.
- **Structured output.** The output is constrained by the entity's typed hint
  schema `_crate_mapping.draft_hints_schema(entity_type)` via the model's
  structured-output / function-calling, so the returned dict validates against
  that schema. The schema gets a top-level `title` (`<EntityType>Fields`) so
  langchain can use it directly as a function spec.
- **D5 — no fabricated identifiers.** Identifier-bearing scalar fields (CAS /
  InChIKey / SMILES / PubChem CID / ORCID / ROR / DOI / Cellosaurus accession /
  ontology codes / IRIs) and all entity-reference fields (`_REF_FIELDS`) are
  **removed from the schema the model sees** — it is never even asked for an
  identifier — *and* defensively stripped from the result. Those fields are left
  empty for a downstream **lookup** to fill, never guessed.

Toolbox completion (companion issue, parallel disjoint lanes): `materialize_aop_subgraph`;
~~the identifier-PropertyValue family~~ **done (#180)** — landed as deterministic
build-path wiring (shared `_crate_mapping._identifier_pv`), **not** new LLM tools, per
§14's "prefer the deterministic build path": Person `orcid` → ORCID PropertyValue,
MolecularEntity `cas`/`pubchem_cid` → `[CAS, PubChem CID]`, plus `Person.affiliation`
and `Publication.author` resolved as `{@id}` references and the
`draft_property_value` DOI/PubMed `propertyID` fix; a dedicated
`draft_publication_with_authors` composite (synthesizing `#CitationAuthor_*` Person
nodes from Crossref author lists) remains deferred follow-up. The
**reference-wiring resolver extensions** lane is also **done (#180 Lane C)** —
likewise deterministic build-path wiring (`_crate_mapping._wire_dataset_aliases`),
**not** new LLM tools: root `funder` → Organization ref(s), root `about` → the
DataAnalysis LabProcess (mirroring the Assay `about`→LabProcess wiring), Assay
`measurementMethod` → BAO `DefinedTerm` ref, and the hasPart-family aliases
`dataFiles`/`resources` re-emitted as resolved File refs while staying nested under
the assay's `hasPart` (un-parented from the root). All five new alias keys
(`funder`, `measurementMethod`, `studies`, `assays`, `resources`, `dataFiles`,
`labProcesses`) joined `_REF_FIELDS` so `_scalar_props` strips them as resolver
inputs rather than leaking the raw id onto the node (D5: an unresolvable, non-IRI
value is dropped, never guessed). The **CSVW payload** lane is also **done (#180
Lane D)** — deterministic build-path wiring, **not** new LLM tools: the Exposure
condition table grows from 5 to the gold crate's full **10 typed columns**
(`_crate_mapping._CONDITION_TABLE_COLUMNS`), and an EndpointReadout that already
emits result file(s) additionally emits a typed `raw_measurements.csv`
`csvw:Table` (3 columns, `_crate_mapping._synth_raw_measurements`), appended (never
substituted) so the "MUST have a result" repair contract is untouched. Both
schemas are built by a shared `_build_csvw_schema` and emit `propertyUrl`/`valueUrl`
as `{@id}` references (RO-Crate 1.2 flags an IRI-as-string when it is also a
described entity, e.g. the cell-line `NCIT_C16403` `DefinedTerm`). The header-only
placeholders never fabricate measurement/well rows (D5). The
**characteristics/properties fidelity** lane is also **done (#180 Lane E)** —
deterministic build-path enhancements (no new LLM tools, plus one existing-tool
signature extension): (1) a `CellLineSample`'s `organ` / `tissue` hints join
`passage` / `growth` as `schema:additionalProperty` PropertyValue characteristics,
carrying the ISA-Tox `param/{organ,tissue}` `propertyID` (gold
`#SampleCell_MDCK1`); (2) a `LabProcess`'s `additionalProperty` field is resolved
to its in-state PropertyValue reference(s) (gold `#report_analysis` →
`[#pv_repro_score]`) via `_wire_references` — only PropertyValues already in state
(or bare IRIs) are wired, never a fabricated score (D5); (3)
`extend_draft_file_for_source_code` landed as new `draft_file` params
`additional_types` + `programming_language`, co-typing an analysis script as
`@type:[File, SoftwareSourceCode]` with `schema:programmingLanguage` (gold
`plot.py`). **Deferred:** `set_crate_metadata` (`releaseDate` / `dateModified` on
the root) — it requires new `CrateState.metadata` fields **and** a new four-place
LLM tool, and it is a fidelity nicety, **not** a validity blocker
(`datePublished` is auto-set by ro-crate-py); deferred to keep this a cohesive,
build-path-only change.

> **Tool-registration contract:** every new LLM tool must be registered in **four**
> lockstep places — `TOOL_REGISTRY`, `TOOL_SPECS`, the system-prompt "## Your Tools"
> catalogue, and §5 of this doc (guarded by `tests/test_agents_doc_toolbox.py`) — plus
> the import lists in `tests/test_tools_spec.py`, or CI fails.

### 14.5 The pipeline spine (task 4 — `builder/agents/pipeline.py`)

`run_pipeline(engine: AgentEngine) -> dict` is the deterministic, code-driven
orchestrator of §14.2 — the Priority 1-4 heuristic (§4) expressed as **control
flow, not prose**, with **no LLM deciding control flow**. It operates on an
already-`initialize()`-d engine (so scanning + approved-roots happened in the
engine) and routes every step through `engine.run_tool(...)` (so each is profiled
and validation is cached); it never re-implements tool logic, only orchestrates the
existing toolbox. The sequence:

1. **Scaffold** the ISA backbone via `scaffold_isa_backbone` — always, and
   idempotent (existing layers are reused). The spine supplies deterministic
   backbone **names** (from `state.metadata.title` when present, else stable
   defaults) because a bare `draft_study` populates only the entity_id, not the
   `name` field, and the ISA profile REQUIRES a non-empty Study `name`. With names
   supplied this alone yields `{base, isa, tox}` on an empty crate (§14.3).
2. **Draft entities** — the §14.2 bounded **drafter-leaf is now wired in here**
   (was a deferral): `_draft_entities` gathers a free-text context from what the
   engine carries (crate `title`/`description` + a scanned-file digest that now
   includes **bounded BODY excerpts** of non-tabular rich files — `.json` / `.docx`
   / `.pdf` — read fail-closed to `approved_scan_roots` and capped by
   `_MAX_CONTEXT_CHARS`, #231) and, for
   each draftable entity missing descriptive fields, calls `draft_entity_fields`
   (`leaves.py`) and applies only the returned **non-identifier descriptive**
   fields (fill, don't clobber). It is a **strict no-op when no LLM provider is
   configured** (detected via `config.get_provider()`, the same check the rest of
   the code uses) *and* when there is no usable context — so the deterministic
   spine, its tests, and the deterministic A/B path are unchanged (the leaf is
   never even imported on that path). **D5-safe:** identifier / `@id` / `entity_id`
   fields are never set or overwritten — those come from lookups. Returns
   `{drafted: [<ids>], fields_applied: <n>}`.
3. **build_and_validate** in memory (no disk write).
4. **Fix loop** — `fix_required_issues` + re-validate, **bounded to ≤3 rounds**,
   stopping when no REQUIRED issue remains *or* a round fixes nothing (deterministic
   dispatch only; the loop is monotone over the rule set, so a no-progress round
   means the rest needs the LLM leaf).
5. Returns `{ok, conformance, issues, scaffold, materialized, drafted, fix_rounds}`.

`run_pipeline` is the **automated** build and stays **guidance-free** — the HITL
guidance tail is invoked *around* it by the interactive entrypoint
(`run_interactive_build`, §14.6.1), never inside the spine, so the A/B eval can
drive the spine non-interactively. Post-A/B-gate (3/3 vs 1/3 ISA-Tox conformance)
this spine is the **default** `main.py --interactive` build; ReAct is opt-in via
`--legacy-react`.

**Determinism contract:** with **no LLM provider configured** the drafter-leaf
step (2) is a strict no-op, so every step is deterministic and the same input
state ⇒ an identical built `@graph` — the headline win the deterministic A/B path
of the eval harness asserts (`crate_graph_hash` equal across runs, zero tokens in
CI). When a provider *is* configured, step 2 makes a bounded, D5-safe extraction
call, trading strict graph-hash determinism for richer drafted content.

**Measurable via the same harness.** `eval/pipeline_factory.py`
(`make_pipeline_agent_factory` → `PipelineBuildAgent`) implements the same
`BuildAgent` contract as the ReAct factory: it builds a headless engine
(`SimulatedHumanInterface`), `initialize(input_path=case.input_path)` (which
approves the input dir under the fail-closed guard), runs `run_pipeline`, and
returns the final `CrateState`/`session_id` exactly like `ReActBuildAgent`.
`eval/__main__.py` adds `--arch react|pipeline` (DEFAULT `react`) selecting the
factory, so `python -m eval --arch pipeline --label pipeline` runs the same
corpus/metrics/report against the spine — diffable vs the frozen `react-baseline`.
The spine calls **no model**, so it runs in CI for real (zero tokens).

The pre-migration ReAct baseline is frozen at git tag **`react-baseline`** for the A/B.

### 14.6 The hybrid build loop and the gap engine (`builder/tools/gap_analysis.py`)

The full hybrid ISA-Tox build loop runs in five stages — the first four are the
**automated pipeline** (`run_pipeline`, §14.5) and the fifth is the **interactive
HITL tail** (`run_guidance`). Every stage is deterministic *code* except the two
explicit bounded LLM leaves (Extract's `extract_plan`, and the drafter the
guidance tail uses to *suggest* a value the user must confirm):

```
        ┌───────────── AUTOMATED PIPELINE (run_pipeline) ──────────────┐
INPUT → Extract → Materialize → Assess → Auto-resolve →  …  →  Guidance (run_guidance)
        (leaf)    (deterministic   │      (deterministic         (deterministic HITL loop:
                  composites)      │       fix loop)              ask-user / draft+confirm;
                                   ▼                              INTERACTIVE ONLY)
                                gap engine
```

- **Extract** (`extract_plan`, leaf #213, §14.4) — the bounded whole-document
  extractor pulls a *candidate plan* (names/titles only, no identifiers — D5)
  from the input context in a single model call.
- **Materialize** (`_materialize_plan` via the idempotent composites #217, §14.5)
  — deterministically turns each plan section into linked ISA-Tox entities through
  `scaffold_isa_backbone` / `resolve_compound` / `draft_cell_line_sample` /
  `draft_process_chain` / `materialize_aop_subgraph` / `draft_person`. Identifiers
  come from the composites' own lookups, never from the plan. The backbone is
  scaffolded BEFORE the plan is materialized, so the plan's Study name/description
  are merged onto the already-scaffolded Study (fill-don't-clobber: only when the
  field is empty or still the generic placeholder), and each `people[]` is split
  into `givenName`/`familyName` so the Person is ISA-conformant (#232).
- **Assess** (`assess_gaps`, the gap engine #215, this section) — one
  prioritized `GapReport` unifying SHACL + MIT + FAIR.
- **Auto-resolve** (`fix_required_issues`, §5, the keystone) — clears every
  `auto_fixable` gap deterministically from state alone, no prompt.
- **Guidance** (`run_guidance` #218, §14.6.1) — the **deterministic, code-driven
  HITL loop** that walks the remaining `auto_fixable=False` gaps with the user in
  the loop. It is NOT a ReAct/LLM-orchestrated agent: CODE owns control flow, the
  LLM only *drafts* a suggested value, and the user confirms every uncertain
  commit (D5). It is invoked **only for a real interactive user** (see §14.6.1).
  The loop **advances over un-progressable gaps** rather than aborting on the
  first one (#230): each round it draws the next *actionable* gap (`report-only`
  gaps are never drawn — see below), and a gap it cannot progress (e.g. the user
  skips it) is added to a **per-report skip-set** so the loop moves on to the gap
  behind it. It only stops once the whole report is exhausted with no progress —
  one cryptic, uncommittable gap can no longer abandon the 200 behind it — and is
  still hard-bounded by `max_rounds`. The skip-set is cleared on every commit (the
  re-assessed report is fresh). ask-user prompts are **human-readable** (a direct
  question naming the field + entity, the reason, any suggestion, and the expected
  format), never the raw failed-check `message`.

**The deliberate split — automated vs interactive.** Stages 1–4 are the
**automated** build: `run_pipeline` (§14.5) runs them with **no HITL**, so it
never blocks on a user and the A/B eval can drive it non-interactively
(`--arch pipeline`, a clean automated-vs-automated comparison vs ReAct).
`run_pipeline` therefore stays **guidance-free** — the Guidance tail (stage 5) is
HITL and lives **outside** the spine, in the interactive entrypoint
(`run_interactive_build`, §14.6.1). A headless / simulated run is exactly
`run_pipeline` alone; a real user gets `run_pipeline` *then* `run_guidance`.

#### 14.6.1 The interactive entrypoint (`builder/agents/build.py`)

`run_interactive_build(engine, *, pipeline_runner=None, guidance_runner=None,
exporter=None, output=None) -> dict` joins the two halves into the end-to-end
sequence a real user runs. It:

1. runs the **automated** pipeline (`run_pipeline`) — always;
2. runs the **HITL guidance tail** (`run_guidance(engine, engine.human_interface)`)
   **iff the engine's `HumanInterface` is interactive**;
3. surfaces a concise summary of the guidance results
   (`format_guidance_summary` — gaps resolved / asked / remaining per tier, plus
   final base/isa/tox conformance) via the injected `output` channel (e.g. the
   CLI's `print` / console writer);
4. **exports the crate to disk LAST** (`export_crate(engine.state)`, #233) — after
   guidance, so the *enriched* crate is what lands — and surfaces the resolved
   **absolute** crate path via `output`;
5. returns `{"pipeline": <run_pipeline result>, "guidance": <run_guidance result
   or None>, "export": <export_crate result>}` — `guidance` is `None` exactly when
   the path was non-interactive; `export` is the (successful) export result dict.

**The on-disk export (#233).** Before #233 the pipeline path built + validated in
memory and exited **without writing anything** — `export_crate`
(`builder/tools/builder.py`, the only disk writer) was never called on this path
(only the legacy ReAct loop exported, because the LLM chose to). The export is now
the deterministic **final step** of `run_interactive_build`, on **every** completed
build (interactive *and* headless), so the user always gets a crate on disk and
`--output` has an effect. The destination is resolved by `export_crate` from
`state.metadata.output_path` (the CLI-resolved path, see below) with the session
`working_crate/` fallback. An export failure is **never silently swallowed**: it is
logged, surfaced via `output`, and re-raised as `CrateExportError` so the CLI
signals a non-zero exit. The exporter is injectable so the wiring is unit-tested
with no ro-crate-py / disk (`tests/test_agents_build.py`).

**Output location (CLI, `main.py`).** The on-disk destination is resolved at
dispatch time with this precedence (#233):

1. `--output` / `-o` always wins (sets `state.metadata.output_path`);
2. `--output` omitted **and** `--input` given => a **sibling of the input folder**,
   `<input_parent>/<input_name>-ro-crate/`, so the crate lands next to the data it
   describes;
3. no `--input` (conversation mode) => leave `output_path` unset so `export_crate`
   falls back to the session `working_crate/` directory.

**The interactive signal.** "Interactive vs headless" is read from a single,
optional `HumanInterface.is_interactive` attribute via the fail-closed helper
`builder.tools.hitl.is_interactive(human)`: a `None` interface, one that omits the
attribute, or one that sets it falsy is **non-interactive**; only a frontend
backed by a real user sets it `True`. The default `SimulatedHumanInterface`
(used by the A/B eval, batch runs, and the test suite) is `is_interactive = False`,
so behind it `run_interactive_build` degrades to `run_pipeline` + export alone and
`run_guidance` is **never invoked** — guidance can never block a headless build,
but the headless build is **still written to disk**. The pipeline, guidance, and
exporter runners are all injectable so the wiring is unit-tested with no SHACL /
no LLM / no disk / no network (`tests/test_agents_build.py`).

**Stage C — the gap engine.** `assess_gaps(state: CrateState) -> GapReport`
(`builder/tools/gap_analysis.py`) unifies the three assessors into ONE
prioritized gap list the Guidance stage consumes. It is a **pure, deterministic,
idempotent library function** (no LLM, no network, never mutates `state`) — and a
**library function only, NOT a four-place LLM tool**: the spine/guidance *code*
imports and calls it. It calls (does not re-implement) the three assessors:

- `build_and_validate(state, severity="optional", profile="all")` — one widest
  sweep yields REQUIRED + RECOMMENDED + OPTIONAL SHACL issues, each already
  routed to `{entity_id, property, message, fix, severity, profile}`.
- `assess_mit_coverage`'s underlying YAML logic — every unfilled MIT parameter is
  a domain-enrichment gap.
- `assess_fair_maturity` — every *failing* indicator is a gap.

**Tiering** mirrors the §6 validation layers (MUST = blocking, SHOULD =
recommended, MAY = optional):

| Source | Gap → tier |
|--------|------------|
| SHACL `required` (Violation) | **MUST** |
| SHACL `recommended` (warning) | **SHOULD** |
| SHACL `optional` | **MAY** |
| MIT param, `additional: false` (core) | **SHOULD** |
| MIT param, `additional: true` | **MAY** |
| FAIR failing indicator, `essential` | **SHOULD** |
| FAIR failing indicator, otherwise | **MAY** |

Each `Gap` carries `{tier, source, entity_id, entity_type, property, message,
suggestion, fix_hint, auto_fixable}`: `suggestion` is the expected propertyID
IRI / ontology term / parameter description from the profile or MIT YAML;
`fix_hint` is a deterministic tool name (`"fix_required_issues"`), `"draft"`,
`"ask-user"`, or **`"report-only"`** (#230). A `report-only` gap is one the
guidance loop can *not* commit deterministically from the gap's own fields — it
has no settable entity field (`entity_id is None`) and its property maps to no
Root Data Entity metadata slot (`_CRATE_SETTABLE_FIELDS` — which mirrors
guidance's `_apply_value`). **Every FAIR gap is `report-only`** (its property is
an indicator id like `RDA-F1-02M`, not a field), as are **crate-level MIT gaps
whose field is not a crate slot**; they stay in the report for context but the
guidance loop never spends an ask-user/draft turn on them. `GapReport` adds
`{gaps, conformance, mit_overall, fair_summary, counts}`, with `gaps` sorted
**all MUST, then SHOULD, then MAY**, then **committable before `report-only`**
within a tier, stable secondary by `(source, entity_id, property)` — so the loop
always reaches the gaps it can act on first.

**`auto_fixable` is the load-bearing field** — it is `True` iff
`fix_required_issues` can clear the gap deterministically from state alone. The
engine decides this by re-using the repair loop's **own** rule predicates
(`builder/tools/repair.py` `_RULES`, `_resolve_state_entity`,
`_unique_unwired_file`) read-only, so the gap engine and the repair loop can
never drift on what "deterministically fixable" means: today the single
`missing_process_output` rule makes an `EndpointReadout`/`DataAnalysis` missing
its `result`/`output` auto-fixable **iff exactly one un-wired `File` is in state**
(unambiguous target); two-or-more (ambiguous) or zero (needs new content, D5)
files, and every non-SHACL or non-REQUIRED gap, are `auto_fixable=False` →
`"ask-user"`/`"draft"`. The Auto-resolve stage runs `fix_required_issues`; what
remains is exactly the `auto_fixable=False` set the Guidance stage works through.

---

*This document is a living design artifact. Update as architectural decisions evolve.*
