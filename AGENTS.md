**The legend names the class, not the category and not a census (#623).** A colour key labelled in
category prose — "Sample / material", "Term / parameter" — explains the canvas in a vocabulary the
reader can see nowhere else. Each key is therefore the **class that puts an entity in that
category**, which is `_entity_category`'s own rule and the word the profile, the shapes and the
crate's `@type` all use: `LabProcess`, `LabProtocol`, `Sample`, `MolecularEntity`, `File`. It was a
census of the type tags a crate's own nodes carried, and on a real deposit that made the protocol key
read `File, HowTo +1` — true of the entities, and not a word anything else in the system says. The
census is not lost: it rides on the `title`, which is where "what does this crate actually put in
that bucket" belongs. A category no single class defines — the fallback bucket, and the off-crate
reference, which names a provenance status rather than a type — keeps its prose. The wording is
decided **once in Python** (`_legend_wording`) because two sections draw this legend with two
different renderers, and a second copy of the rule in a second browser app is how two legends over
one crate come to disagree.

# ISA-Tox RO-Crate Builder — System Design

> **Purpose:** This document describes the architecture, component design, and design rationale for the LLM-assisted RO-Crate builder backend. It serves as both a developer guide and an orientation document for AI coding agents working on this codebase.
>
> **Maintaining this document.** AGENTS.md is a **design document** — contracts and
> invariants, described in the present tense as the system *is*. Keep it that way:
> - State **what must hold**, not the code's line-level algorithm — implementation
>   detail lives in **docstrings**, which this doc points to.
> - **No changelog or logbook.** No migration narratives, dated audit snapshots,
>   "task N — done/withdrawn", or A/B run logs; that history belongs in git and PRs.
>   Cite an issue number only when it names a durable contract.
> - Keep §5 (toolbox) and §12 (structure) faithful to the code — prefer generating
>   them from the registry / tree over hand-maintenance.
> - Update it when the **design** changes; update README when **behavior** changes.

## Table of Contents

- [1. Architecture Overview](#1-architecture-overview)
- [2. Core Concepts](#2-core-concepts)
- [3. CrateState — The Central Data Model](#3-cratestate--the-central-data-model)
- [4. Agent Priority Heuristic (Work in Layers)](#4-agent-priority-heuristic-work-in-layers)
- [5. The Agent Toolbox](#5-the-agent-toolbox)
- [6. Validation Layers](#6-validation-layers)
- [7. Session Persistence & Resume](#7-session-persistence--resume)
- [8. Human-in-the-Loop (HITL)](#8-human-in-the-loop-hitl)
- [9. Input & Output Formats](#9-input--output-formats)
- [10. Lookup Services](#10-lookup-services)
- [11. Key Design Decisions](#11-key-design-decisions)
- [12. Project Structure](#12-project-structure)
- [13. Future Considerations](#13-future-considerations)
- [14. The Deterministic Pipeline & Guidance Loop](#14-the-deterministic-pipeline--guidance-loop)

## 1. Architecture Overview

The ISA-Tox RO-Crate Builder is a **toolbox-based agent system** that assists researchers in creating profile-conformant RO-Crates for *in vitro* toxicology data.

> **Two first-class build variants.** The builder ships **two supported,
> actively-explored architectures over the same toolbox** (see §14): a
> **deterministic pipeline + HITL guidance tail** (the `--interactive` default —
> code owns the step ordering, the LLM is confined to bounded leaves) and the
> **ReAct agent loop** (`--react` — the LLM orchestrates tool calls). Both
> are maintained; this is an ongoing A/B exploration, **not** a migration that ends
> in deleting ReAct. The rest of §1–§4 describes the ReAct loop; §14 describes the
> deterministic pipeline and the relationship between the two.

The ReAct variant gives an LLM agent a set of tools and lets it decide the order of operations based on the current state, rather than following a rigid pipeline with predefined steps.

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
   │   → read declared licence        │
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
3. The declared-licence read — a licence the deposit itself states is recorded on `metadata.license` with `license_from_deposit` set, before anything drafts one.

Steps 2 and 3 are best-effort: a failure leaves their state empty rather than aborting initialization. What the loop is handed is therefore the inventory, a classification per file, the ranked document context, and any declared licence — it never has to bootstrap those itself. The agent uses the inventory during entity drafting to bind files to `LabProcess` instances as annotations emerge. The crate's output layout is not scaffolded upfront — it is produced by `export_crate` once entity annotations are complete.

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
| **Packaging** | RO-Crate 1.2 base | `Dataset`, `File`, `Person`, `Organization` |
| **Structural** | ISA hierarchy | `Investigation`, `Study`, `Assay`, `LabProcess`, `LabProtocol`, `Sample` |
| **Domain** | Toxicology extension | `MolecularEntity`, `CellLineSample`, `LabProcessExposure`, `LabProcessEndpointReadout`, `LabProcessCellCulture`, `LabProcessDataAnalysis` |

### Entity Provenance

> **Status: the entity-level `status` ladder below is declared but not implemented.**
> The `EntityStatus` enum carries exactly these four values and is exported from
> `builder`, but nothing assigns or reads it — an `Entity` carries only the
> per-field completion and the provenance record.

Every entity tracks:
- `status`: `draft` | `enriched` | `reviewed` | `verified`
- `_provenance.created_by`: `scanner` | `llm` | `user` | `lookup`
- `_provenance.reviewed_by`: `user` | `null` — declared, but no code writes it
  today (§8)
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

## 3. CrateState — The Central Data Model

`CrateState` is the single source of truth. It is serializable to JSON and persists to disk for session resume.

```
CrateState {
    session_id: str, created_at: str, updated_at: str,  # ISO-8601 strings
    metadata: {
        title: str | None, description: str | None, accession: str | None,
        release_date: str | None, date_modified: str | None,
        publisher: str | None, creator: str | None, contact: str | None,
        license: str | None,
        # the licence was READ from the deposit descriptor, not drafted (#535):
        # a depositor's statement is a fact and outranks a drafted guess, so
        # `set_crate_metadata` will not overwrite it
        license_from_deposit: bool,
        input_path: str | None, output_path: str | None,
        exported_at: str | None,  # None until the crate has been written
    },
    # what produced this crate: application, provider, model(s), architecture,
    # and the run's token / cost / duration usage. Captured through an
    # ALLOWLIST, never a blocklist — never an API key, never a raw `base_url`;
    # `api_host` is the hostname alone, because the crate is shareable
    generator: {
        name, version, url, provider, model, drafter_model, api_host,
        architecture: "react" | "pipeline", settings, usage/cost fields,
    },
    entities: {
        investigations: [Entity], studies: [Entity], assays: [Entity],
        lab_processes: [Entity], protocols: [Entity], samples: [Entity],
        molecular_entities: [Entity], people: [Entity], organizations: [Entity],
        publications: [Entity], defined_terms: [Entity],
        property_values: [Entity], files: [Entity], aop_entities: [Entity],
    },
    scanned_files: [{ path, filename, size, mime_type, first_rows,
        reviewed_by_user, classification }],
    approved_scan_roots: set[str],  # user-approved directory roots for file scanning
    # scientific documentation ranked at initialization, read by both arms
    documents: [{ kind, classification, filename, relative_path, score,
                  reasons, preview }],
    # bounded content captured from documents that were successfully read,
    # keyed by path relative to the approved root. Session evidence, not a
    # general-purpose result cache
    document_evidence: { relative_path: { tool, path, content, truncated, args } },
    validation: {
        base_passed: bool, isa_passed: bool, tox_passed: bool,
        required_issues: [str], should_issues: [str], may_issues: [str],
        # the same findings with structure intact (#510) — the flat lists above
        # are their display projection and stay byte-stable (the ReAct loop
        # parses them); empty on a verdict recorded before the field existed
        issue_records: [{ profile, severity, entity_id, message }],
        assessed_tiers: set[str],
        # whether anything actually looked at the crate's FILES (#530) — the
        # in-memory gate validates a document, where payload checks emit nothing
        payload_checked: bool,
        # whether anything asked which entities the ISA backbone reaches (#537) —
        # the profile's own rules cannot, so a detached entity is skipped not failed
        isa_reachability_checked: bool,
        input_fingerprint: str,
    },
    mit_assessment: { module_scores: { m: { completed, total } }, overall_score,
        # the same parameters bucketed by guidance document (documents overlap,
        # so these do not sum to the checklist total), and each document's
        # bucket split by module
        standard_scores: { doc: { completed, total } },
        standard_module_scores: { doc: { m: { completed, total } } } },
    fair_assessment: { indicator_results, dsm_level },
    checkpoint: { next_actions: [str], completed_checkpoints: [str],
                  reasoning_log: [{"step", "action", "tool", "result", "timestamp"}] },
    # standing answers to "run the broader validation tiers?". A recorded
    # answer means the user has DECIDED — never ask that tier again, and the
    # decision survives `--resume`
    validation_preferences: { "recommended": bool, "optional": bool },
    # the durable half of the HITL record. A tool-result answer lives only in
    # the graph checkpoint, so a rotated thread loses it and the agent re-asks
    # a question already answered; answers belong in state
    user_answers: [{ question, answer }],
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
        "MolecularEntity:identifier": {"status": "filled", "source": "lookup"},
        "MolecularEntity:smiles": {"status": "missing", "source": "lookup"},
    },
    "_provenance": {
        "created_by": "llm", "reviewed_by": null, "lookups_used": ["pubchem"],
    }
}
```

`source` records the *tier* a value came from, never the service: which service
answered is recorded once, in `_provenance.lookups_used`.

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

### 4.1 Validation Gate Ordering

The three profile passes in `profiles/validator.py` are **independent**: a
`profile="all"` sweep always runs base, ISA and ISA-Tox, fanned out to a process
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
   (`export_crate`/`build_crate` is the only disk-writing step; see §5 for how
   often each arm calls it)
2. Fix base RO-Crate issues first (structural integrity)
3. Then iterate on ISA issues
4. Then iterate on ISA-Tox issues
5. Report MIT/FAIR scores last, as optional improvements

This avoids the common pitfall of perfecting one entity type before verifying
the crate can assemble at all.

### 4.2 Core Components (shared by both build arms)

The components both build arms drive. ("Pipeline" is reserved for the
deterministic build arm and its composition, §14 — these modules are shared, not
arm-specific.)

#### 4.2.1 Scanner (`builder/tools/scanner.py`)
Examines an input directory (or zip archive) and builds a raw file inventory
(path, size, mime type, first lines of readable files). Never reads entire
large files into context. The inventory is not the whole of initialization — file classification,
document ranking and the declared-licence read all run before the agent loop
(§1) — but it is what the agent uses during entity drafting to bind files to
`LabProcess` instances as annotations emerge. Restricted by approved scan
roots (see [Guard Rails](#guard-rails-approved-scan-roots) above).

**Bounded results.** `max_files` and `max_line_length` cap result size and preview
length; `read_file_sample` takes `precomputed_size`/`already_text` so a caller that
already knows them skips a second `stat()`/MIME syscall. `_safe_walk` prunes
hidden/`.git`/`__MACOSX` directories **in place** during `os.walk`
(`dirnames[:] = [...]`) — the in-place mutation is what stops them being descended
at all; a refactor into a post-hoc filter loses that.

**MIME detection.** `_detect_mime_type` resolves in order: stdlib `mimetypes`, then
a scientific-format registry (`_SCIENTIFIC_MIME_TYPES`) covering MS/microscopy/flow
extensions the stdlib does not know (`.mzML` → `application/x-mzml`, `.fcs` →
`application/vnd.isac.fcs`, vendor binaries `.raw/.wiff/.czi/.nd2/.lif/.d/…` →
`application/octet-stream`), then a text-content sniff. The registry is consulted
**before** the sniff so binaries are never mislabeled `text/plain`;
`application/octet-stream` is the default for unknown binary; and a NUL byte in the
header forces that default even when the bytes decode as UTF-8.
`encoding_format_for_name` exposes the same extension→media-type derivation with no
disk read, for entity drafting.

**Size ceilings and the read contract.** The dedicated readers in `file_readers.py`
share the scanner's 100 MB `_MAX_BYTES` ceiling; row/line caps keep memory bounded.
`read_file` returns plain-text/JSON **in full** up to `_TEXT_BUDGET_BYTES` (64 KiB),
because a line-clipped return makes weak models loop "let me read the rest". Over
budget it returns the content shown plus an explicit, machine-stable marker
(`[truncated: showing first 64 KiB of N KiB; this is the maximum for this tool —
do not re-read]`) so the model knows re-reading yields nothing more; that marker is
a verbatim contract, duplicated in the tool description and the system prompt, so it
changes in all three places or none. The 100 MB `_MAX_BYTES` hard cap still skips
genuinely huge binaries entirely. A *directory* handed to
`read_file`/`read_file_sample` returns "`<path>` is a directory … use
list_scanned_files …", **never a silent `None`**, and a bare `None` from any reader
(`read_file_sample`/`read_file`/`read_excel`/`read_docx`) is turned into an
actionable "unreadable/too-large — skip it" message — silence is what makes a weak
model re-call the same reader. `read_file_sample`'s `lines` argument controls how
much 'content' mode returns. Reasoning-log entries embed a compact, bounded repr of
each tool's call args (`run_tool: read_file(path='…')`) so the recorded action shows
*which* path/hints a tool ran with, not just its result.

**Repeated non-progress loop-breaker (ReAct only).** An identical non-progress call
re-issued by a weak model burns millions of tokens, so the `_run` wrapper in
`_build_langchain_tools` tracks the last tool-call signature (name + sorted args)
and the consecutive count of the **same non-progress result** (a directory message,
an unreadable/`None` message, or an `{"error": …}` dict —
`_is_non_progress_result`) on the engine. After `_LOOP_BREAKER_THRESHOLD` (3)
identical non-progress repeats it **refuses to run the call again** and returns a
forceful corrective message carrying the live `list_scanned_files` inventory
(concrete file paths to read instead). Any *distinct* call or any *progress* result
resets the counter, so legitimately-repeated different calls and a single normal
retry never trip it.

#### 4.2.2 File classification & document discovery (`builder/tools/document_discovery.py`)

Two questions about the same file, answered once and read by everything. The
line-level rules and the fixture measurements behind them live in the module's
docstrings (`classify_file`, `classify_scanned_files`, `_allocate_slots`,
`_fair_shares`, `_context_body`); what follows is what must hold.

**What a file IS — one classification, four values** (#591). `classify_file`
returns exactly one of `metadata` / `protocol` / `raw_data_file` /
`processed_data_file`, plus the signal that decided it. `metadata` covers the
deposit record, assay-metadata workbooks, publications and plate maps;
`protocol` covers SOPs, lab protocols and analysis scripts; the other two are
the data tiers a derivation chain is wired from. A plate map is `metadata` — it
states the design that was intended, not a value that was measured.

The order is **content → filename → path**, and it is load-bearing in both
directions: letting the extension outrank the content files an instrument
printout under `raw data/` as a protocol because it is a `.pdf`, and letting the
path outrank the filename files `assay1_rawdata/README.txt` as a measurement.
Within the filename step, *what a file is* outranks *which tier it would be* — a
paper titled "Normalization of Data for Viability…" is a publication, not
processed data. Terms are matched at a word boundary and open at the end, so one
spelling covers a family (`normali` catches "normalised") while `raw` still
refuses "drawings" and `process` refuses "unprocessed", which names the opposite
tier. A word-processor format is never placed by its folder: nothing but a person
writes a `.docx`, so bench notes filed beside the measurements are not
measurements.

The classification is **stamped on every scanned file** (`classify_scanned_files`
→ `FileClassification.classification`), not on the ranked subset — what the crate
is built from must not depend on what fits in a context window.
`classification_of` answers for a record that was never stamped (a resumed
session saved before the classification existed, or any caller holding the
inventory without the deposit mounted) from the record alone, without touching
the disk.

Stamping every file does not mean OPENING every file. A deposit is a handful of
homogeneous folders, not N distinct things, so files are grouped by `(directory,
extension)`, a *spread* sample of each group is read — spread, because a folder
sorted by run date puts its exception at the end as often as at the start — and a
group whose sample agrees takes that verdict across the rest. A group is opened in
full when it is smaller than four times the sample, when its sample DISAGREES
(which is what says the folder is heterogeneous and cannot be summarised at all),
or when the sample is anything but `raw_data_file` — instrument output is the only
tier whose files are interchangeable. A propagated file has no preview, and a
workbook with no preview is ranked on its filename alone (#587). The standing
limitation: a file whose CONTENT alone makes it an exception in the interior of a
large uniform folder is invisible to this, because nothing about its name,
extension or directory sets it apart.

Which file is the design table is **not** what a file IS, and is deliberately not
folded into the classification: the same content is `metadata` as
`plate_map.csv`, `raw_data_file` as `conditions.csv`, and `processed_data_file`
when it carries the measured value alongside the design. The spine asks the rows
instead, through `data_content.condition_table_fit` — a well key AND columns that
map onto the canonical schema. That is the same predicate
`populate_condition_table` must satisfy to write anything, so what is detected and
what is writable cannot disagree.

Consumers: `composites._deposited_outputs` / `_deposit_evidences` (§5, *Derivation
Chain Tools*), `provenance.attach_files` (which stamps it as the File's `role`
unless the caller names one), the pipeline spine's `_attach_scanned_files`, the
ReAct gap engine, and the maturity report's "data files included" row.

`File.role` is the *stamp*, not the classification. It is free text — `draft_file`
records whatever the agent passes, and the spine stamps `raw_data`/`processed_data`
labels that a resumed session then carries for the rest of its life. A reader
deriving a class from it therefore accepts only a value in `FILE_CLASSES` and
classifies the file otherwise; **nothing may take the field at its word.** The role
never reaches the crate in any case — `_crate_mapping` drops it, as it is absent
from the RO-Crate `@context`.

**What reaches the prompt — ranking.** `discover_documents` screens and ranks
readable documentation into a bounded context, reusing the previews classification
already read. It reports `kind` (descriptor / tabular / narrative / opaque)
alongside the classification, because form decides how a file is *rendered* — a
data table contributes its shape, prose contributes its text — and the two
questions have different answers for the same file. The cap is stated in the
context rather than applied silently (#587).

Both the character budget and the slot cap are split **max-min fair**
(`_fair_shares`), not spent in rank order: rank order lets a few long READMEs
consume the whole ceiling and delete every cheaper entry behind them, and a file
that is never named is a file the agent cannot read. A share too small to carry
even the entry's `[kind/class] path` header buys nothing, so that entry is dropped
and its share returned rather than emitted as a fragment.

**The context leads with the deposit's SHAPE, then the sample (#599).** A ranked sample is only
meaningful against what it is a sample of: 40 files of 1468 is 2.7% of a submission, and
"1428 not surfaced" cannot tell a tail of 1352 instrument printouts apart from one hiding sixteen
unread protocols. Since #591 every scanned file carries a classification, so the census is free and
complete — it was simply discarded in favour of the count. `summarise_deposit` states the tally per
class, then the folders that hold the files with their own tallies, and the "not surfaced" line is
broken down the same way.

The folder listing starts at `_branch_point` rather than the top: a submission is routinely one
folder deep before anything differs (svhps22 puts all 1468 files under
`study_01_TH-DNT_Tier1_NeuralCellLines/`), so listing the root reports one folder holding everything.
Descend while there is exactly one child DIRECTORY, whatever files sit beside it. Everything not
inside a listed folder — at the trunk and *above* it — is counted under `(top level)`, because the
descent otherwise walks straight past the root descriptor, the one file that states the study's own
identity: svhps22 showed 1467 of its 1468 files and said nothing about the one it dropped. The rows
sum to the total, or the shape is a picture of a different deposit. A deposit that does not branch
at all — flat, or a single deep chain — gets the tally and no tree. The tree is bounded to `_MAX_SHAPE_FOLDERS` with the remainder stated,
and is omitted entirely below `_MIN_SHAPE_BRANCHES`: a tree of one limb repeats the tally above it,
and a census must never be longer than the file list it summarises. It is charged to the same
character budget, taken off the top, and bounded by construction so it cannot crowd out the
documents it exists to introduce.

**Both arms render through `format_document_context`, and the cap has one home (#675).**
None of the above reached a model for a while: the engine built the bounded context and
used the string for its LENGTH in a log line, while each arm re-rolled its own from
`state.documents` and sliced it to a hardcoded `[:20]`. Measured on S-VHPS22, discovery
produced 40 candidates balanced 13 metadata / 13 processed / 12 protocol / 2 raw and the
pipeline sent 26 587 characters — against an 18 000 budget — carrying 4 / 13 / **1** / 2.
The budget, the header guard and #595's allocation all stopped at the engine. So
`MAX_DOCUMENT_CANDIDATES` is the only cap, `DocumentationCandidate.from_dict` turns the
stored dicts back into candidates, and a renderer that wants a different PRESENTATION
(the ReAct arm's numbered markdown list, which is also the user-facing reply) still takes
its membership from the same ranking rather than re-deciding what fits.

**The slots are allocated the same way, over the classification (#595).** The cap is
the agent's whole view of the submission, and it used to be spent on one axis — "how
document-like is this?" — over a population #591 can classify into four, so whole
tiers vanished: svhps26 named 14 interchangeable plate readouts and *none* of its 8
GraphPad analysis files, each carrying a kilobyte of readable content. Every class
present now takes a floor, so a tier is never wholly absent, and the surplus goes
through the same `_fair_shares`. `raw_data_file` takes its floor and stands out of
the redistribution — #598 established it is the one tier whose members are
interchangeable, so a sixth gamma-counter printout says nothing the first five did,
while a sixth protocol is a different experiment — but it still absorbs whatever the
other classes leave unspent, because an empty slot helps nobody.

The 40 slots and the 18 000-character budget are one trade, not two: at 40 slots a
smaller budget squeezes every entry down to "more files named, each saying less",
and the class floor is what stops the extra slots going to more of the same
instrument output.

#### 4.2.3 Entity Drafters (`builder/tools/drafters.py`)
Generate metadata entities from files, conversation, or existing metadata.
Each drafter is a **pure state mutation** over hints its caller supplies — the
model's contribution is recorded as provenance (`created_by="llm"`,
`source="llm"`) rather than made from inside the drafter — and **identifiers come
from lookups, never fabricated**: `_resolve_person_orcid` verifies a single
full-name ORCID search hit through the record endpoint before using its URL as an
entity id, and leaves the deterministic local id in place for anything ambiguous,
unavailable or weak.

**Entity types:** Investigation, Study, Assay, MolecularEntity, CellLineSample,
LabProcess (CellCulture/Exposure/EndpointReadout/DataAnalysis), Person,
Organization, Publication.

#### 4.2.4 Crate Builder (`builder/tools/builder.py`)
Assembles the RO-Crate using [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py)
(`profiles/models/isa.py`, `profiles/models/tox.py`, `profiles/context.py`).
Can produce partial crates at any point.

Every term the crate's `@context` declares resolves to a real vocabulary's own canonical IRI — the
policy `profiles/ontology_iris` states for lookup terms, applied to the context too. AOP terms are
**AOPO**'s (`http://aopkb.org/aop_ontology#`), the ontology AOP-Wiki RDF itself annotates with; they
spent months under an `aopwiki.org` path AOP-Wiki does not serve, so a whole vocabulary resolved to
nothing while looking familiar (#644). A test holds every namespace in the context to a declared
allow-list, so an invented one fails CI instead of shipping inside crates, and a second drives the
real validator over a crate carrying the real context — a shape whose `sh:class` and the crate's
`@context` disagree does not fail, it finds no target and never runs.

#### 4.2.5 Validator (`builder/tools/validation.py`, `profiles/validator.py`)
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

**The in-memory path must not walk the working directory.**
`services.validate_metadata_as_dict` builds the crate through
`rocrate_validator`'s `ROCrate.from_metadata_dict`, which hardcodes the crate URI
to `"./"` and then resolves the metadata-descriptor id by `rglob`-ing it — a
recursive walk of the entire current working directory on every pass, every call,
and pure waste on a path where no crate exists on disk. `profiles/validator.py`
installs `_patch_in_memory_descriptor_id()`, an idempotent module-level patch
(alongside the offline-context and ISA-ontology patches) that pre-seeds the cached
`_metadata_descriptor_id` with the canonical `ro-crate-metadata.json` constant —
the same value the upstream walk falls back to, so results are byte-identical.
**The patch stays scoped to `from_metadata_dict`**, which only the dict path uses,
so the on-disk `validate_crate` (which legitimately discovers a descriptor in a
real crate directory) is untouched. Its docstring carries the upstream bug it
works around and the condition for deleting it.

**Cost levers.** The residual cost is profile *composition*, not inference:
`rocrate_validator` recomposes the shapes/ontology graph and re-resolves check
overrides on every `validate()` call with no reuse hook, so a long-lived worker
does not amortize it. Profiling detail is in
[docs/validator-profiling-115.md](docs/validator-profiling-115.md). The supported
levers are to gate the inner loop at `required` severity (`validate_crate_dict`'s
default) and to scope `profile` to a single pass when the full sweep isn't needed;
the full 3-pass sweep is run only as a final gate. Switching the disk path to
`required` and reserving `OPTIONAL` for the final report is a further modest win,
not taken here because it changes which issues the loop sees. The only
order-of-magnitude path is an upstream injectable pre-composed shapes/ontology
graph.

Two things are settled; do not re-open them. **Caching the parsed shapes was
explored and deliberately abandoned** — the `.ttl` parse is negligible (~10–130 ms)
and `rocrate_validator` exposes no hook to reuse the compiled graph without a
fragile internals monkeypatch. **Pass-folding — one `tox` pass reporting all
layers, attributing issues by originating profile id — is the only large lever and
is NOT result-equivalent:** the bundled `tox → isa → ro-crate` chain is
RO-Crate **1.1** lineage while the dedicated base pass validates against **1.2**
(#110), so folding would downgrade the base layer and change the issue set. That
split is by design, not drift — `profiles/shapes/tox/profile.ttl` bumps to 1.2 only
if/when the upstream `isa-ro-crate` profile does, guarded by
`tests/test_profile_lineage.py`. Folding becomes safe only after that upstream bump
*and* a byte-identical issue-set test.

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

#### 4.2.6 Maturity Assessors (`builder/tools/{mit,fair,air}_assessment.py`)
Score against `mit/invitro_tox.yaml`, `fair/indicators.yaml`,
`fair/dsm_indicators.yaml` and `air/criteria.yaml`. All produce scores, not
pass/fail, and share one verdict shape (`builder/tools/assessment_graph.py`).

**Indicator definitions are generated from vendored published instruments, never
hand-written**, so a definition cannot silently drift into a paraphrase of the model
the paper claims to score:

| Axis | Instrument | Vendored | Generator |
|---|---|---|---|
| FAIR | RDA FAIR Data Maturity Model (doi:10.15497/rda00050, CC-BY-4.0) | `fair/rda_fdmm.xlsx` | `scripts/gen_fair_indicators.py` |
| DSM | FAIRplus **Dataset** Maturity Model v1.2 (FAIRplus D2.6, doi:10.5281/zenodo.7464523, model text CC-BY-4.0) | `fair/fairplus_dsm_v1.2.xlsx` | `scripts/gen_dsm_indicators.py` |

Each generator carries **all** the published indicators — the DSM's full 83, not the
subset we can assess — with verbatim text and the model's own columns, and keeps the
repo-specific decision (which indicators are assessable from one crate, and with which
check) in its own `LOCAL_SCOPE`. An indicator scoped for assessment whose check is not
registered **raises**; it is a wiring bug, not a pass. That trap is why DSM-4-R6 was
silently skipped while its level was awarded for free. `tests/test_dsm_indicators_source.py`
and `tests/test_fair_indicators_source.py` pin each committed YAML to its generator's
output and to the source workbook.

Note the DSM model text is **CC-BY-4.0**; the FAIRplus repository's MIT `LICENSE.txt`
covers only its Jekyll theme and does not license the model.

**Where an instrument's own arithmetic is not reproduced, the YAML says so.** The RDA
workbook computes a maturity level per FAIR area (`calc!C13:F13`, Level 0-5 gated on
essential / important / useful thresholds); this tool publishes a met/failed count and
no level. `fair/indicators.yaml`'s `scoring:` block records both, with the formulas
**read from the sheet** rather than restated, and states the reason: every level above
0 requires all of an area's essential indicators to be met, an `out_of_scope` indicator
can never be met, and Accessibility is 12 of 12 `out_of_scope` — so the ladder would
report the hosting repository's properties as the crate's failure. The per-indicator
boolean is a different case and the block says so too: the sheet's own column J
collapses its five-way metric all-or-nothing, so met/failed *is* the instrument's, not
a coarsening of it. AIR carries the same shape of declaration; the DSM reproduces its
instrument's grid outright and needs none.

**A check reads the crate, not the session.** An indicator is scored against the
assembled `@graph` — the bytes a reader receives — so a third party scoring the
published crate reaches our published number. Given no graph a check answers *not
assessed* (`None`) rather than guessing from `CrateState`, and where that answer lands is
the instrument's own arithmetic: the RDA count carries not-assessed as a bucket beside met
and failed, while a DSM cell drops it from `pct` and scores it 0 in `published_pct`, because
the published sheet has no such state; `_GRAPH_AWARE_FAIR_CHECKS` names the RDA checks that read the graph, and
every DSM check registered unwrapped in `DSM_CHECKS` reads it too;
`assessment_graph.needs_graph` states why the alternative is a lie. The licence predicates
are the one deliberate exception: `_effective_license` falls back to
`state.metadata.license`, because a licence the deposit itself declared (#535) is a crate
fact wherever it is read from, so the three of them — five registry entries, the DSM reusing
two — answer a bool with no graph rather than `None`. `_state_check`
marks what is left — a **burn-down**, enumerated with the reason each rewrite was
refuted in `tests/test_fair_metrics_can_fail.py`, which may shrink and may not grow.
Two indicators asking one question share one function, so the axes cannot disagree
about one crate.

**A check must be able to fail, and must ask the published question.** `len(entities)
> 0` is not an assessment: it scores that the builder ran. The floor is pinned by
`tests/test_fair_metrics_can_fail.py`, which scores a crate holding two empty entities
and no payload and requires every indicator whose published text names *the data* to
read `False`; the packaging indicators it may legitimately meet are enumerated there
with reasons. Moving a check to the graph without rewriting its predicate inflates the
score — porting DSM-1-C1 and DSM-1-R0 as-is lifts twelve crates a level for free —
so **a migration ships with a real crate that fails it, and published scores are
expected to go down** (#670).

### 4.3 External RO-Crate Packages

This project builds on the existing RO-Crate Python ecosystem rather than reinventing crate assembly, validation, or entity models:

| Package | PyPI | What it provides | How we use it |
|---------|------|-----------------|---------------|
| [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py) | `uv add rocrate`<br>(import `rocrate`) | Official Python SDK for creating and manipulating RO-Crates. Provides `ROCrate`, `ContextEntity`, `File`, and other base entity classes. | The entity model classes in `profiles/models/isa.py` and `profiles/models/tox.py` subclass `rocrate.model.ContextEntity` and `rocrate.model.File`. The builder uses `ROCrate` to assemble the crate and serialise `ro-crate-metadata.json`. |
| [`rocrate-validator`](https://github.com/crs4/rocrate-validator) | `uv add roc-validator`<br>(import `rocrate_validator`) | Official SHACL-based validation library. Supports multi-profile validation (base RO-Crate → ISA → domain extensions) with severity levels. | `profiles/validator.py` wraps this in three passes (RO-Crate 1.2, ISA, ISA-Tox), suppressing inherited-profile duplicates so each pass reports only its own layer. |
| [`rocrate-wizard`](https://github.com/johannehouweling/rocrate-wizard) *(external frontend)* | TBD | Frontend/UI layer that uses this backend (vitro-crate) to provide a user-facing RO-Crate builder. | This repo is the dependency — `rocrate-wizard` imports from `vitro-crate` and adds the web UI/CLI on top. |

These packages are imported directly — we do not fork or vendor them. Version requirements are declared in `pyproject.toml`.

### 4.4 Agent Graph (LangGraph / StateGraph)

> This section describes the **ReAct variant** (`--interactive --react`),
> one of the two first-class build paths (§14). It is a **supported, maintained**
> architecture, not a deprecated one; the deterministic pipeline (the
> `--interactive` default) is described in §14.

The agent loop uses an **explicitly constructed StateGraph** built by `_build_agent_graph()` in `builder/agents/react/agent_loop.py`. This replaces the earlier `create_agent()` factory pattern (Issue #37), giving us full control over node names, routing logic, and middleware integration.

```python
graph = StateGraph(AgentState)
graph.add_node("model", call_model)
graph.add_node("tools", tool_node)
graph.add_conditional_edges("model", should_continue)
graph.add_edge("tools", "model")
graph.add_edge(START, "model")
return graph.compile(checkpointer=MemorySaver())
```

#### 4.4.1 Node Topology

The compiled graph has exactly **four nodes**:

| Node | Purpose |
|------|---------|
| `__start__` (built-in) | Entry point — LangGraph's standard pseudo-node. Transitions unconditionally to `model`. |
| `model` | Assembles the message list, binds the state-relevant tools, and invokes the LLM. |
| `tools` | Executes any tool calls produced by the model (via `ToolNode`). |
| `__end__` (built-in) | Terminal node — agent terminates here. |

The state is typed as `AgentState`, a `TypedDict` with a single `messages` field using the `add_messages` reducer for automatic concatenation:

```python
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
```

#### 4.4.2 Edge Routing (Tool-Calling Loop)

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

#### 4.4.3 How `system_prompt` Gets Injected

The `call_model` node prepends the system prompt on **every invocation**:

```python
def call_model(state):
    messages = state.get("messages", [])
    system_msg = SystemMessage(content=SYSTEM_PROMPT)
    model_messages = [system_msg, *messages]
    active_tools = _tools_for_state(tools, ...)   # narrowed per turn (#156)
    model = llm.bind_tools(active_tools) if active_tools else llm
    response = model.invoke(model_messages)
    return {"messages": [response]}
```

This means the system prompt appears at the front of the messages every time the loop iterates back to the model, ensuring the LLM always has its full context: `[system, human, ai(tool_calls), tool, system, ai(answer)]`.

In the live code `call_model` delegates the message assembly to `_assemble_model_messages`, which keeps the byte-stable system prefix and trailing per-turn state brief (D10) but **bounds the history in between** via `_trim_history` — pruning consumed state-backed tool outputs and trimming to a token budget so per-turn input stays bounded over a long session, without ever orphaning a tool message (D12, Issue #61).

Tools are bound **inside** `call_model`, per turn (#156): `_tools_for_state` narrows
the advertised set to what the current state can act on, while the `ToolNode` keeps
the full set — advertise narrow, execute wide, so a narrow advertisement never
blocks execution. **Binding at all is not optional.** Without `bind_tools` the model
is never told the tools exist, `should_continue` therefore always routes to
`__end__`, and the agent degrades to a text-only chatbot that narrates "let me
scan…" but never executes a tool (the #71 regression).

#### 4.4.4 How `MemorySaver` Integrates

The `MemorySaver` checkpointer is passed to `graph.compile()`. It is a **checkpointing layer** that snapshots the full agent state (`messages` list, etc.) after each node execution. LangGraph uses the `thread_id` from `RunnableConfig` to key these checkpoints. On subsequent `invoke()` calls with the same `thread_id`, the graph resumes from the last checkpoint, providing conversational memory across turns.

The `MemorySaver` does not affect routing or the node topology — it is purely a persistence mechanism for state snapshots.

#### 4.4.5 Model Tiering (Issue #96)

The weak model the agent runs on (e.g. DeepSeek-flash) collapses on multi-turn
orchestration and error recovery — the build→validate→re-draft loop — but stays
fine at bounded extraction. Model tiering lets a stronger model drive the
orchestration node while a cheap model does the bounded drafting work, without
any change to the graph topology.

Construction is centralised in `_build_chat_model(provider, model, base_url,
max_retries, role, timeout, streaming)` (`builder/agents/llm.py`) — shared code
that **both** build arms import, so a change made "for ReAct" also lands on every
pipeline drafter leaf. An explicit `model` argument wins over role-based
resolution; otherwise `role` selects the tier:

- `role="orchestrator"` (default) → the primary model
  (`VITRO_OPENAI_MODEL` / `VITRO_ANTHROPIC_MODEL`).
- `role="drafter"` → the cheap drafter model
  (`VITRO_OPENAI_DRAFTER_MODEL` / `VITRO_ANTHROPIC_DRAFTER_MODEL`) **when
  configured**.

The drafter tier is provider-agnostic. `_build_chat_model` reads those two
environment variables directly; `config.merge_with_env` folds the
`[openai]`/`[anthropic] drafter_model` config keys into them when the env var is
absent, so the observable precedence is env var → config key, mirroring the
primary model's. (`config.get_drafter_model()` is the *reporting* resolver — it
records `drafter_model` on the crate's generator provenance; it is not on the
construction path.) **Default = single model:** when no drafter model is set,
`role="drafter"` resolves to the same primary model as the orchestrator, so
behaviour is identical to a single-model setup — a strict no-op.

The tier is bound in production by the deterministic pipeline: every drafter leaf
in `builder/agents/pipeline/leaves.py` builds its model with `role="drafter"`
(§14.4), so setting `VITRO_OPENAI_DRAFTER_MODEL` changes the model behind each
bounded extraction call. The ReAct arm's `draft_*` tools
(`builder/tools/drafters.py`) are not on the tier — they are pure state mutations
(see *Entity Drafters* above), so the ReAct arm spends nothing on it.

**OpenAI reasoning models (`gpt-5.x`, `o`-series).** These reject a non-default
`temperature` and cannot bind function tools on `/v1/chat/completions` (the API
400s on tools + `reasoning_effort`), so `_build_chat_model` routes them through
the **Responses API** (`use_responses_api=True`) and does not send a temperature.
Detection is by model name (`_is_openai_reasoning_model`); a custom/Azure
deployment name the heuristic can't recognise can force it with
`VITRO_OPENAI_USE_RESPONSES_API`. Standard models keep chat/completions with a
deterministic `temperature=0` (override via `VITRO_TEMPERATURE`).
`VITRO_OPENAI_REASONING_EFFORT` forwards a `reasoning_effort` (a lever on
reasoning-token spend); the explicit value `none` disables reasoning and opts
back to the standard `temperature=0` path.

`VITRO_TEMPERATURE` applies to **both** providers, resolved once in
`_resolve_temperature()` (#402). It previously read on the OpenAI path only while
the Anthropic branch hard-coded `temperature: 0`, so a temperature sweep on
Anthropic silently did nothing — and an A/B asked to compare two architectures at
one temperature was comparing two temperatures. Blank/whitespace reads as unset
(matching `VITRO_OPENAI_REASONING_EFFORT`); a non-numeric value raises rather than
resolving to 0, because a silently-ignored control is the defect this fixed. A
Responses-API reasoning model still receives no temperature at all — "no opinion"
must be absence, since the API 400s on any explicit value.

**Decision gate (future work):** upgrading the *orchestrator* to a stronger
model is a separate, profiling-gated decision. Instrument `profile.ndjson` for
iterations-per-task, recursion-limit hits, and REQUIRED-issue fix success;
upgrade the orchestrator only if failures are reasoning/recovery-shaped
(looping, mis-sequencing), not malformed output (which schemas + SHACL already
catch). Guardrails are a one-time cost; a stronger model is recurring per token.

## 5. The Agent Toolbox

### File Tools
*The scanner/sampler triad and the archive tools below are engine-routed — the
engine dispatches them itself, so they are advertised in `TOOL_SPECS` but are not
in `TOOL_REGISTRY`; the full readers are registry tools. All of them are
LLM-callable during the agent loop.*
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20, mode: str = "content") → str | None
  mode: "content" (first `lines` lines — `lines` controls how much is returned), "summary" (file-type-aware), "overview" (metadata + summary)
  a directory path returns an actionable "use list_scanned_files" message, never a silent None (#240)
read_multiple_files(paths: list[str], lines: int = 50, mode: str = "content") → dict
  mode: same options as read_file_sample
read_file(path: str) → str | None             # full read by extension (txt, csv, json, xlsx, docx, md, pdf); text/JSON returned COMPLETE up to a 64 KiB budget, over-budget files carry an explicit "[truncated … do not re-read]" marker; a directory returns the list_scanned_files guidance (#240)
read_excel(path: str) → str | None            # .xlsx → pipe-delimited text
read_docx(path: str) → str | None             # .docx → plain text
extract_pdf_text(path: str) → str             # structured PDF: [Page N] text, tables, image metadata
preview_archive(path: str) → ArchivePreview   # list a zip/tar(.gz) archive's members + metadata without extracting
unzip_file(path: str, output_dir: str | None = None) → {extracted_to, entry_count, message} | {error, message}   # extract a zip/tar(.gz)
```
`scan_files`, `read_file_sample`, and `read_multiple_files` run during session
initialization to classify inputs and feed the state brief. The full readers
(`read_file`/`read_excel`/`read_docx`/`extract_pdf_text`) and the archive tools
(`preview_archive`/`unzip_file`) are dispatchable so the agent can pull a file's
full contents on demand. `unzip_file` refuses an unsafe archive with an error dict
rather than raising — a member escaping the destination (Zip-Slip), or a total
uncompressed size over `_MAX_UNCOMPRESSED_BYTES` (zip-bomb) — and the agent calls
`present_to_human` before extracting a large archive so the user can confirm.
Every tool named in this section exists in the code —
**never document a tool that does not exist**: the parity guard in
`tests/test_agents_doc_toolbox.py` fails the build on a phantom tool name in
this section (Issue #145).

### Entity Drafting Tools
```
scaffold_isa_backbone(investigation=None, study=None, assay=None, validate_base=False) → dict  # composite: linked Investigation→Study→Assay in one call (idempotent-WITH-merge: a reused layer's EMPTY fields are filled from the supplied hints, fill-don't-clobber), the fast path to a BASE-passing crate
materialize_aop_subgraph(aop_id: str, study_id: str | None = None) → dict  # composite: one AOP-Wiki id → AdverseOutcomePathway + KeyEvent[] + KeyEventRelationship[] subgraph, cross-linked deterministically; optionally wired onto a Study
link_assay_to_key_event(assay_id: str, event_name: str | list[str]) → {ok, assay_id, key_event_ids, matched_names, unmatched?} | {ok: False, error, candidates}  # composite: link an Assay to the AOP Key Event(s) it MEASURES (schema:mentions via keyEvent), each name matched INDEPENDENTLY against the KeyEvents already in state; commits the in-state AOP-Wiki id, never one built from the name, and writes NOTHING for a name matching zero or several Key Events because which Key Event an assay measures is a scientific claim (D5); links ACCUMULATE deduplicated rather than replace — removing one is `set_fields`' job (`key_event_id`/`matched_name` are kept as the first match for callers written against the one-event form)
resolve_compound(name: str, hints: dict | None = None, verify=None) → {entity_id, name, identifiers, verifications, verified, source}  # composite: chemical name → lookup_compound → draft_molecular_entity → verify_identifier (+ best-effort CompTox DTXSID), in one idempotent call; carries the looked-up CAS + PubChem CID + EPA DTXSID and never keeps an unverified id (D5)
resolve_cell_line(name: str, hints: dict | None = None, catalog_name: str | None = None, verify=None) → {entity_id, name, accession, match, query, verifications, verified, source}  # composite: cell-line name → lookup_cell_line_by_name (full name, then catalog_name) → draft_cell_line_sample → lookup_cell_line (which IS the verification), in one idempotent call; a miss is NOT a failure (no `ok` key) — the Sample is always minted and the accession is enrichment
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
one `aopo:AdverseOutcomePathway` carrying its `has_molecular_initiating_event`
/ `has_key_event` / `has_adverse_outcome` / `has_key_event_relationship` link
arrays, one `aopo:KeyEvent` per MIE/KE/AO (all share `@type KeyEvent`,
discriminated only by the `eventType` string), and one
`aopo:KeyEventRelationship` per relation (`upstream_event` /
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
them. **DTXSID enrichment (Issue #179):** after resolution it also runs a
best-effort CompTox `lookup_dtxsid` — querying by the strongest EXACT key
available (`cas` → `inchikey` → name) — and stores the EPA **DTXSID** on the
entity when found, so the build appends it as a third `DTXSID` identifier
PropertyValue after `[CAS, PubChem CID]`. It is D5-safe (the value comes straight
from CompTox, never fabricated) and **non-fatal** — a CompTox miss or outage never
sinks an already-resolved compound. `resolve_compound` is the deterministic arm's
ONLY producer of DTXSID, so dropping the CompTox call drops the identifier.
Looked-up identifier fields win over same-named caller `hints`. **Dedup is
by resolved chemical IDENTITY, not by name** (Issue #179): after a successful
lookup it computes an identity key in priority order `pubchem_cid` → `inchikey` →
`cas` → `chebiId` and reuses any existing `MolecularEntity` whose same identity
field matches — so two DIFFERENT names for one molecule (e.g. `Indocyanine green`
and `ICG`, same CID/InChIKey) collapse to ONE node (the synonym is recorded as a
`schema:alternateName`) instead of minting a second `chem_<name>` node, and two
names resolving to the same `pubchem_cid` can no longer mint the same `@id` (which
ro-crate-py silently overwrites — data loss). When the record carries no identity
field, it falls back to name-keyed reuse so re-running the same name still reuses
the entity rather than duplicating it.

`resolve_cell_line` (Issue #372) is the cell-line counterpart of
`resolve_compound`, and the deterministic arm's **only** name→Cellosaurus path.
Minting a cell line straight through `draft_cell_line_sample(name=…, hints={})`
skips the lookup and leaves the crate with no accession at all. Two steps, both
Cellosaurus: (1)
`lookup_cell_line_by_name` on the full normalized name and then on
`catalog_name`, each gated by that lookup's **unmodified** exact+unique D5 rule —
first unique-exact hit wins, and the tier is reported as `match`; (2)
`lookup_cell_line` on the accession, which **is** the verification —
`_select_verifier("CellLineSample", "accession")` already resolves to exactly
that function, so a following `verify_identifier` would re-issue the same
`lru_cache`d call, and the status is set directly (mirroring
`_verify_compound_identifier`). A *transient* step-2 failure keeps the accession
unverified; a *definitive* step-2 miss clears it.

**A miss is NOT a failure** — the one deliberate divergence from
`resolve_compound`, which returns `{ok: False}` and mints nothing. A
`CellLineSample` with only a name is a valid ISA Sample and is what the arm
produced before this composite, so returning `{ok: False}` would delete the cell
line from every crate whose line is not catalogued, taking the
`CellCulture.cell_line` input and the Study's `cell_lines` mention with it.
**Always mint; the accession is enrichment** — hence no `ok` key: read
`accession` / `match`. `name` stays the name **as the source documents word it**;
the Cellosaurus label goes to `alternateName`. The plan's short `catalog_name`
(a catalogue *name* is a name, so it is D5-clean and `_strip_plan_identifiers`
leaves it) is what lets a descriptive phrase such as "FRTL-5 TPO-overexpressing
rat thyroid follicular cells" reach CVCL_0265 at all; one shaped like an
accession is **refused**, as is any identifier-bearing `hints` key, so every id
it commits came back from Cellosaurus inside the call. Idempotency lives in the
composite, not the drafter: reuse is tried by accession first — one line often
appears under two names in one submission, and once the accession drives the
`@id` (`_crate_mapping._mint_id`) two such entities would collide onto one node
— then by the deterministic name-derived id. Only `accession` /
`alternateName` / `url` / `sameAs` are persisted; `_CELL_LINE_DROPPED_FIELDS`
records what else the record offers and the failure each would cause (the
record's own `identifier` is a full URL that `verify_all_identifiers` would
re-query percent-encoded, miss, and pop; `taxonomicRange`/`disease`/
`anatomicalSite` are `DefinedTerm` node objects that `_scalar_props` emits
un-flattened, failing base conformance).

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
to a synthesized id rather than guessing. An ORCID-resolved author's
**`affiliation` becomes an `schema:Organization` reference, never a literal
string** (Issue #179 — the ISA shape flags a literal-string affiliation as a
Violation): the ORCID record's `affiliation_name` (+ `affiliation_ror`) is
find-or-drafted into an Organization (preferring the ROR so its `@id` resolves to
the ROR IRI; D5 — a ROR is set only when the lookup returns one, never fabricated)
and the Person's `affiliation` is wired to that Organization's `@id`. Authors
sharing an affiliation reuse ONE Organization (deduped by name), so the build's
`_wire_reference` resolves each `Person.affiliation` to the shared node.
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
`cas`/`casrn`/`cas_number` and/or `pubchem_cid` and/or `dtxsid` gains
`[CAS, PubChem CID, DTXSID]` identifiers in that order (CAS has no `propertyID`;
PubChem CID's is `{"@id": https://pubchem.ncbi.nlm.nih.gov/compound}`; DTXSID's is
`{"@id": https://comptox.epa.gov/dashboard/chemical/details}`). These source fields
are consumed structurally (kept off the node as raw literals). A Person's
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
set_crate_metadata(title=None, description=None, accession=None, release_date=None, date_modified=None, publisher=None, creator=None, contact=None, license=None) → {title, description, accession, release_date, date_modified, publisher, creator, contact, license, note?}  # set Root Data Entity (crate-level) scalar metadata
remove_entity(entity_id: str, cascade: bool = False) → {removed, entity_id, detached, discarded_fields, warning}
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
Attribution — `publisher` / `creator` / `contact` — must be a resolvable
reference: a Person/Organization already in state, or a verified ORCID/ROR IRI.
A bare name is REFUSED, because a name string credits nobody a registry can
resolve (D5). `license` yields to a licence READ from the deposit descriptor
(`license_from_deposit`, #535) — a depositor's statement is a fact and a drafted
guess does not replace it — and the call reports that under `note`. A call with
every field empty is REFUSED rather than answered with a success-shaped summary:
it can write nothing, so it is always a mistake; `get_status` reads the current
metadata.
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
orphaned. It returns a REPORT, not a bare boolean: `cascade=True` is right for a
stray reference and quietly destructive for a parent link — clearing an Assay's
id off its processes detaches every experiment in the crate, and a parentless
process violates no shape, so the crate still passes all three profiles with zero
REQUIRED issues. `detached` and `discarded_fields` name what came loose and what
was thrown away, so the caller can re-point the children instead of finding the
hole at export.

### Derivation Chain Tools
```
draft_process_chain(assay_id: str, chain: [{process_type, hints?, object?, result?}], validate_after=None) → {assay_id, process_ids, steps, synthesized, skipped?}  # composite: create + wire the whole CellCulture→Exposure→EndpointReadout→DataAnalysis chain in one idempotent call; EndpointReadout/DataAnalysis have NO build-time output fallback, so each step is wired to the output the deposit actually holds — a data producer with no deposited file keeps no `result` and the tox Violation reports the gap; only a CellCulture's `Sample` is synthesized
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
closes that trap, but **asks the deposit first** (#589): the scanned files
classified `raw_data_file` are the `EndpointReadout`'s result and the
`processed_data_file`s the `DataAnalysis`'s, found by `_deposited_outputs` and
wired whole (`schema:result` takes "at least one"). Only when the deposit holds
no file of that class is the step left **without a result**, so the tox Violation
fires and the gap is reported (#592). Nothing is
manufactured to make the shape pass: an empty stand-in bought a green profile at
the cost of a 0-byte CSV a consumer reads as data, with nothing telling the
depositor what was missing. A material producer (CellCulture) still gets a
`Sample` via `draft_sample` — that is modelled material, not a stand-in for a
file nobody deposited.

**Whether the step exists at all is a separate question** (`_deposit_evidences`),
asked of the whole deposit: any data file, or any document describing the
procedure. Either makes it real; a deposit holding neither leaves nothing to
model, and drafting a step there would invent it exactly as an empty output file
used to invent its result — so it is skipped and reported under `skipped`. A
protocol counts on its own because the step carries more than its output: on
svhps26 the SOP yields `Detection Instrument = "gamma counter"`, which reaches
the crate only because an EndpointReadout exists to hold it. Not class-specific,
deliberately: a deposit with only processed data still evidences that a
measurement happened — its raw output was simply not submitted, and that gap is
the Violation's to report.

Scoped to the assay: one Assay owns the whole deposit, several means only what is
already attached counts, so no assay is given its neighbour's measurements. That
scope made the answer depend on call order — the real agent drafts the chain
before it attaches files — so `attach_files` re-asks the question for any step
still unwired (`composites.wire_deposited_outputs`). Evidence arriving is when
the answer can change; asking once at draft time silently answered "nothing
deposited" for 5 of 8 steps whose output was in the deposit all along.

Asking first is what keeps the chain pointing at evidence: a composite that
synthesizes regardless produces a crate whose every chain ends at an empty stub
while the deposit's measurements sit beside it, referenced by nothing — and a
crate that reports no problem, because the stub satisfied the shape.

**The Exposure is the deliberate exception (#285, #650):** it is
NOT given a generic placeholder result here, because its build-time fallback is the
*semantically-correct* output — the **exposed Sample**, the cells after treatment,
deriving from the cultured sample it consumed. Synthesizing a generic result File
would populate `result` and pre-empt it, leaving the crate with no exposed-sample
entity at all and every downstream step hanging off the culture instead. So the
Exposure step is left output-less in state and the build emits the exposed sample;
the material flow still passes downstream via the step's inputs.

The compounds do not ride on that output. They are **reagents of the per-well
condition table**, which the build attaches as a protocol the exposure *executes* —
the plate layout a procedural SOP leaves out — not as something it produces. That is
the only route ISA permits: `schema:object` is restricted to File/Sample/BioSample at
Violation severity, and Bioschemas `LabProcess` has no other input slot.
**Requires:** an existing `assay_id` + each step's
`process_type`. **Reads from the deposit (before synthesizing):** the raw /
processed files that are the step's real output. **Synthesizes:** only a
CellCulture's output `Sample`; the Exposure's output is the build's exposed
`Sample`. A CellCulture grows **one** cell line: a step naming several is built as
one culture per line, each executing that line's own protocol and producing its
own cultured `Sample`, so no `Sample` derives from more than one line and none
stands for a mixture the lab never made. The Exposure consumes every cultured
`Sample` of its assay and emits **one exposed `Sample` per cultured one**, so the
split is not undone a hop later. A co-culture is the explicit exception — asserted
by the step, typed `NCIT:C93168` on the material it yields, and never inferred
from a step that merely names several lines. **Skips (and reports under
`skipped`):** a data producer nothing in the deposit evidences. **Reports rather than fills:** a data producer that IS
evidenced but whose output the deposit lacks keeps no `result`, and the tox
Violation carries that to the report.
**Respects:** any explicit `object`/`result` you pass — those win over both.
The `Sample` carries only structural metadata (name, crate path, role) and
**never fabricates measurement values or identifiers** (D5).
Idempotent: placeholder/process ids are derived deterministically from the step,
so re-running reuses them rather than duplicating; deposited files are found-or-
created by `provenance.find_or_create_file`, deduped on destination and source so
`attach_files` and the chain converge on one entity per file rather than two.
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
output (the build has no fallback for those), File entities produced by no
process, and broken derivation-chain continuity — a process consuming a `Sample`
that no process produces and that is not a CellCulture seed (#140; the guards
that keep primary-cell, data-only and multi-assay crates from being false-flagged
live in the `check_provenance` docstring). Issues come back in the same routable
shape as `build_and_validate` (#87).

### Lookup Tools
```
lookup_compound(name: str) → {found, data, error}   # PubChem (→ ChEBI fallback)
lookup_dtxsid(query: str) → {found, data, error}       # EPA CompTox (DTXSID)
lookup_cell_line(accession: str) → {found, data, error}  # Cellosaurus (accession CVCL_*)
lookup_cell_line_by_name(name: str) → {found, data, error}  # Cellosaurus name → accession (confidence-gated; found: False on ambiguous/partial, D5)
lookup_aop(aop_id: str) → {found, data, error}            # AOP-Wiki
lookup_bao_term(query: str) → {found, data, error}       # OLS/BAO
lookup_ontology_term(query: str, ontology: str) → {found, data, error}  # OLS (any ontology)
lookup_unit(unit_string: str) → {found, data, error}     # OLS/UO (units)
lookup_orcid(orcid_id: str) → {found, data, error}     # ORCID
lookup_ror(name: str) → {found, data, error}              # ROR
lookup_doi(doi: str) → {found, data, error}       # Crossref
```
Every lookup returns that one dict shape and NEVER `None` (§10), so a miss is a
truthy result to read, not a falsy one to branch on. A failure adds `transient:
True` for a timeout / 429 / 5xx — distinct from a definitive not-found, so a
caller keeps the user's value and retries instead of clearing it — and a `fix`
naming the next action when the failure is definitive.

### Verification Tools
```
verify_identifier(entity_id: str, field: str) → VerificationResult
verify_all_identifiers() → [VerificationResult]
```

### Crate Assembly & Validation Tools
```
build_and_validate(severity="required", profile="all") → {ok, conformance, issues}
set_validation_preference(recommended: bool | None = None, optional: bool | None = None) → {validation_preferences, tiers_that_will_run}
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
schema describes all 10 and missing cells are written empty.

A column's `valueUrl` is a claim about **every row in that column**, so it holds
only while the column carries at most one distinct value. Once rows exist,
`data_content.condition_table_multivalued_columns` reads the populated CSV back
and any column with ≥2 distinct values **drops** its `valueUrl` rather than assert
an unverified per-value mapping (D5, #408). The guard is per-column — a
single-compound plate keeps its claim; a multi-compound one loses only `compound`.
Per-value entity mapping is out of scope. It targets the
exact path the build wires (`_crate_mapping._condition_table_rel`), so the #94
CSVW typing (`tableSchema`) stays attached to the populated table. The companion
bridge `data_content.csvw_to_frictionless(_CONDITION_TABLE_COLUMNS)` converts
those CSVW column descriptors into the Frictionless `{fields:[...]}` shape (the
single source of truth — `_CONDITION_TABLE_HEADER` is also derived from the
column constant, so the placeholder header and the typed schema cannot drift),
so `validate_table` needs no hand-authored schema for the populated table.

An `EndpointReadout`'s results are the files it measured, and nothing is appended
to them. **The deposit decides what a step produced** (#589): `raw_data_file`s
are the `EndpointReadout`'s `schema:result` and `processed_data_file`s the
`DataAnalysis`'s, read off the file classification
(`composites._deposited_outputs`). The folder a file sits in cannot decide its
tier: two of the three real deposits file both tiers under one `Raw data +
individual processed data/` (#591).

A step whose output the deposit does **not** contain gets nothing — no entity, no
file, no `result` — and the tox Violation reports it (#592). Writing a file for a
file nobody produced is a claim about data that does not exist; dressing it in a
column contract made that claim machine-readable and vacuously true over zero
rows (#473), and the columns came from a module constant identical in every
crate.
The condition table is the deliberate contrast: its schema resolves `valueUrl` to
this crate's own `Sample` / `MolecularEntity` ids, so it states which compounds at
which doses *this* experiment expected — worth declaring before a row lands.

The condition table emits `propertyUrl`/`valueUrl` as `{@id}` references
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
(`all`|`base`|`isa`|`tox`) scopes the passes, so the inner loop can validate a
single profile at REQUIRED severity and run the full 3-pass sweep only as a gate.
Which pass costs most is crate-dependent and NOT tox by default: on a real
293-entity crate the BASE pass is 22.9s of a 36.9s optional sweep (62%) against
9.2s for tox — measured in `builder/tools/validation.py`. Scoping to `tox` to
save time is therefore backwards on a large crate. The three passes mirror
`profiles/validator.validate_crate`, fed the metadata dict instead of a path.

`fix_required_issues` is the **deterministic repair loop** — the keystone of the
§14 pipeline, and what maps a routed issue back to a *repair*. It runs
`build_and_validate`, dispatches each issue through a
small, ordered table of issue-shape → repair rules (`builder/tools/repair.py`,
`RepairRule`), then **re-validates** to confirm what actually cleared (it trusts the
validator's verdict, not a rule's optimism). A repair runs **only when the correct
value is already determined by state**. Two symmetric rules are wired today:
`missing_process_output` — an `EndpointReadout`/`DataAnalysis` missing its `result`
where exactly **one** un-wired `File` already exists in state is auto-wired as its
`result` via `link` (the §14.3 "no output fallback" trap); and
`missing_process_input` — a `DataAnalysis` missing its required `schema:object`
(input) where exactly **one** free-floating `Sample`/`File` (one wired as no
process input or output) already exists is auto-wired as its `object`. Anything
needing **new content, a new entity, or a fabricated identifier — or a genuinely
ambiguous target (2+ candidates) — is out of scope (D5)** and returned under
`remaining` for a bounded LLM leaf. It is **idempotent and side-effect-safe**: if nothing is
deterministically fixable it mutates nothing and returns every issue in `remaining`.
It maps a validation focus-node `@id` (e.g. `./#LabProcess_er1`) back to its state
entity by inverting `_crate_mapping._mint_id`. Each `fixed` item carries
`{issue, rule, action}`; each `remaining` item `{issue, reason}`.

`export_crate` is the **only** tool that materialises the on-disk RO-Crate
directory (payload included). The deterministic pipeline calls it **once**, after
guidance; the ReAct arm calls it **repeatedly** — automatically after every
base-passing build, and again from the exit backstop — and
`state.export_fingerprint()` is what keeps the repeat idempotent. It is not
the only tool that writes at all: `populate_condition_table` writes the condition
table's CSV, `unzip_file` extracts an archive, and `save_session` persists the
session.
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
agent task — the bulk `attach_files` verb (#177) documented above.

### Assessment Tools
```
assess_mit_coverage()   → MITReport
assess_fair_maturity()  → FAIRReport
assess_air_readiness()  → AIRReport
```
All three score published instruments and share one shape: a tri-state verdict per
item carrying the evidence behind it (`builder/tools/assessment_graph.Verdict`), read
from the assembled `@graph` rather than from `CrateState` — the domain content exists
only after assembly. An item the tool cannot assess from a crate is reported *not
assessed*, never failed, and leaves the denominator of what was assessed — never the
instrument's own. Where a published formula has no *not assessed* state, the report carries
both numbers: `pct` over the criteria assessed, `published_pct` over every criterion
(`AIRReport`, `fair_assessment.dsm_grid`).

`AIRReport` carries **no aggregate score**. The Bridge2AI authors state that
AI-readiness is not scored pass/fail overall, so the axis is seven per-dimension
percentages; a single number would be an invented metric wearing a citation. Each
dimension reports the published denominator (every criterion) beside the local one
(criteria assessed), because the published formula has no "not assessed" state and
substituting ours silently would misstate the instrument.

### Session & HITL Tools
```
present_to_human(context: str, options: [str]) → HumanResponse
present_to_human(context: str, questions: [{question: str, options: [str]}]) → {action: "answered", answers: [{question, answer}]}
request_input(prompt: str, field_type: str | None = None) → InputResponse
save_session(label: str) → SessionInfo
list_sessions() → [SessionInfo]
load_session(session_id: str) → SessionStatus
get_status() → SessionStatus
get_hint() → str
```
`present_to_human` asks one decision: `context` says what was found and
`options` are the rows the user picks between, the first pre-selected. The
console appends a final "Something else — let me type an answer" row to every
prompt except a scan-root escalation, so an answer the caller did not foresee is
still possible; it comes back as `action: "edited"` with the text in `comments`
and `edits.value`. With `questions` the tool asks several in turn — each with its
own `options`, or as a free-text field when it has none — records every answered
exchange in `state.user_answers` (a skip is reported, not recorded), and returns
`{action: "answered", answers: [{question, answer}]}`; an entry not of that shape
is an error and nothing is asked. `request_input` asks the human for a single free-form value (e.g. a
compound name, CAS number, or cell line accession) when a lookup needs a missing
identifier. `list_sessions` and `load_session` drive the resume flow (§7);
`present_to_human`/`request_input` are engine-routed HITL tools (not in
`TOOL_REGISTRY`), the rest are specced.

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
BioHackEU25 report by Chadwick et al. (biohackrxiv `zah28`).

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

**Invocation (Issue #409).** The layer cannot be reached through
`build_and_validate`: that call takes `profile="all"`, `profiles/validator.py`
accepts only `all|base|isa|tox`, and `DATA_CONTENT_PROFILE = "data"` sits
deliberately outside that set. So the pipeline spine calls it directly —
`_validate_populated_tables`, after `_run_fix_loop`, and **only when population
actually landed rows** (the header-only placeholder #94 materialises is valid by
construction). Its verdict is returned by `run_pipeline` under its own
`data_issues` key and is **not** folded into `ok`: a cell contradicting its
`tableSchema` is a different defect from a SHACL conformance failure, and merging
them would make `success` in the eval harness mean two different things. It is
kept out of the fix loop for the same reason — that loop terminates on
`build_and_validate`'s `ok`, and `fix_required_issues` is keyed on SHACL rules
and cannot repair a data cell.

The `compound` / `cell_line` foreign-key allow-lists carry entity **names as well
as ids**, because that is what the cells hold: `propose_condition_rows` writes
`name` and falls back to `entity_id` only for an entity that has none, and a
depositor's plate map names compounds the way a bench scientist writes them. An
id-only allow-list would flag every row of a correct table.

### Verification Layer
Checks that identifiers resolve at their source. Verification failures are REQUIRED — the identifier must be corrected or removed. Leaving a field empty is acceptable (shows up in MIT/FAIR scores but does not block).

**Derivation (Issue #64).** `_VERIFIERS` maps each `(entity_type, field_name)` pair to the
lookup that serves it, and `_VERIFIABLE_FIELDS` is that table's keys — so a pair cannot be
declared verifiable without wiring the verifier that answers for it.
`verify_all_identifiers` decides what to queue from those keys and `verify_identifier`
dispatches through the same table, so the two cannot drift apart. Fields like
`casrn`/`cas_number`/`inchikey` on `MolecularEntity` are included, while `ror` on
`Organization` — which has no verifier — is excluded and so is never queued. That exclusion
is the point: a pair declared verifiable with no verifier behind it comes back
`No verifier configured`, which §6 treats as a REQUIRED blocking failure on a perfectly
valid identifier. `tests/test_tools_verification.py` pins the invariant and fails on
injected drift.

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
The pipeline saves at **phase boundaries**: after scaffold, after materialize, and
after each `build_and_validate` in the fix loop. `run_interactive_build` then does a
final `save_session(state, always_write=True)` after guidance + export, which
bypasses change-detection so a populated overview and a resumable session are
guaranteed written. Entity drafting is deliberately **not** a save point — the fix
loop's first `build_and_validate` saves immediately after it, so a late-recovered
identifier reaches `sessions/` either way; do not add one back. The ReAct arm saves
through the agent-callable `save_session` tool and an end-of-loop autosave. There is
no save on a HITL answer, and no context-limit trigger exists.

**Durability (Issue #53):** Session saves use an atomic-write strategy: write to a temp
file in the same directory, `fsync` it, then `os.replace()` over the target. A SHA-256
hash of the serialised state is computed before saving; if the hash is unchanged from the
previous save, the write is skipped entirely (no-op). Failures are logged and surfaced
to the agent loop, never silently swallowed.

### Resume Flow
1. `load_session(<session_id>)` reads `crate_state.json` and replaces
   `engine.state`; the profiler is re-attached (`engine.ensure_profiler()`), which a
   resumed session would otherwise lack because it bypasses `initialize()`.
2. Unless `--output` is given, the restored `output_path` is versioned, so a
   resumed export never overwrites the crate the previous run produced.
3. Both arms are told `resumed=True` explicitly — the caller's fact, never inferred
   from how populated the state looks (#410) — and print the resume summary.
4. The pipeline arm re-runs the spine (scaffold → materialize → draft → fix loop),
   so validation is re-established from the crate itself. The ReAct arm prints a
   resume panel whose next steps are derived from the crate's actual state:
   blocking issues first, then the next unmet step of the BASE → ISA → TOX climb,
   then export.

`checkpoint.completed_checkpoints` is real and load-bearing — it drives
`_determine_phase` and the dashboard. `checkpoint.next_actions` has no production
writer, so the batch-mode hint always takes its fallback; do not build resume
guidance on it.

**Not reconciled on resume:** nothing compares `working_crate/` on disk against the
restored state, and the ReAct resume panel reads the *restored* `state.validation`
snapshot. A crate directory that changed since the last save can therefore be
announced as passing all three profiles on the strength of stale flags.

**Recovery:** `load_session` reads only `sessions/<session_id>/crate_state.json`. A
missing or corrupt state file ends the run with `Session not found` and a non-zero
exit (pinned by `tests/test_main_graph.py::test_graph_missing_session_errors`);
nothing is reconstructed from a crate's `ro-crate-metadata.json`.

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
2. User can: **Approve**, **Edit**, **Reject with explanation**, or **Skip**. On
   the console, **Edit** is the free-text row every choice prompt ends with (§5)
3. Agent incorporates feedback and continues
4. Feedback is applied to the entity in place; a standing answer to a validation
   escalation is recorded on `state.validation_preferences` and asked at most once
   per session. Entity `_provenance` records only how the entity was created
   (`created_by`) and which lookup services contributed (`lookups_used`) — its
   `reviewed_by` field has no writer, so there is no per-entity audit trail of
   human review today.

### Interface Adapter
HITL requests go through a `HumanInterface` protocol (`builder/tools/hitl.py`)
injected into the engine via `AgentEngine(human_interface=...)`. It defines
`present(context, options) → HumanResponse` and `request_input(prompt,
field_type) → InputResponse`. The default `SimulatedHumanInterface`
auto-approves and skips input for headless/batch runs; a frontend (Streamlit,
FastAPI, CLI) supplies its own adapter without monkeypatching the tool
functions. The module-level `present_to_human` / `request_input` functions
remain as thin wrappers over a shared default simulator.

Three implementations ship in `builder/tools/hitl.py`:

| Interface | `is_interactive` | Wired by | Answers with |
| --- | --- | --- | --- |
| `SimulatedHumanInterface` | `False` | the headless default (batch, eval, tests) | auto-approve; skip every input; **deny** scan roots |
| `ConsoleHumanInterface` | `True` | `main.py --interactive` | a real person, on stdin |
| `SmokeTestHumanInterface` | `True` | `main.py --smoke-test` | itself — see below |

**`SmokeTestHumanInterface` (`--smoke-test`) is a TEST harness, not a frontend.**
It exists so the interactive path — the guidance tail included — can be driven end
to end with nobody at the keyboard: every `present` confirms the **pre-selected**
choice (via the shared `_default_choice_index`, never a re-implemented "first
option") and every `request_input` returns the literal `SMOKE_TEST_ANSWER`,
`"yes, continue"`. It reports `is_interactive = True` *because that is the point* —
the tail is gated on that one signal (§14.6.1), so the simulator cannot exercise
it. Four rules bind the harness; the mechanism lives in the docstrings of
`builder/tools/hitl.py`, and the outcomes are pinned by
`tests/test_smoke_test_mode.py`:

- **Scan roots fail closed (#197).** A `purpose="scan_root"` escalation is refused
  outright — an explicit early return in `present`, before any choice is consulted
  — and `SimulatedHumanInterface` refuses the same way. Fail-closed must be
  written per interface and must never be inherited from the pre-selection rule:
  `_default_choice_index` falls back to the LAST option when nothing reads as a
  refusal, which had this mode approving `["Show me the folder first", "Yes, allow
  this folder"]`. An unattended mode that silently widened filesystem access would
  be the worst bug here.
- **The run says the answers are synthesised.** `SYNTHETIC_ANSWER_NOTICE` is
  printed at the start by `main.py` and again beside the exported crate path,
  gated on the fail-closed `answers_are_synthetic(human)` (an interface that does
  not declare `synthesizes_answers` is assumed to be relaying a real person).
  Nothing is written **into** the crate — a "this was a smoke test" marker in the
  metadata would be fabricating metadata, which D5 forbids.
- **`select_many` skips rather than picking a subset.** A multi-select has no
  pre-selection to confirm, so choosing one would be inventing an answer.
- **`is_done()` is `False` unless a budget was given.** A mode that volunteered
  "done" would return before asking anything, exercising nothing. Without a budget,
  termination is left to `run_guidance`'s own guards (report exhausted / no
  progress / `max_rounds`), which never depend on a cooperating frontend.
  `--smoke-test MINUTES` gives the run a wall-clock budget: once it is spent
  `is_done()` is `True`, and `run_guidance` consults it at the top of each round so
  the run winds down between gaps (never mid-question) and exports what it has.

`--smoke-test` implies `--interactive` (normalised once, in `parse_args`) and
**drives both arms**: the ReAct loop's conversational "what next?" read goes through
the `HumanInterface` (`CONVERSATION_FIELD_TYPE`) when the answers are synthetic, so
the harness answers it for a bounded number of turns (`conversation_turns`) or, with
`--smoke-test MINUTES`, until the budget is spent, then ends the session the way
Ctrl+D does.

## 9. Input & Output Formats

### Input Formats

Input comes in tiers of readiness. The agent should prefer the most structured form available:

| Format | Curation level | Description |
|--------|---------------|-------------|
| **Directory with metadata files** | Medium — partial structure | A research folder that contains some metadata files (README, `.json`, `.yaml`, `.csv`, or other records) alongside raw data. The scanner identifies these by role and the agent drafts entities from whatever structured content they hold — **regardless of the metadata file's format or schema**. Any such file is treated as a generic metadata source, not a special-cased input type. |
| **Unstructured directory** | Low — raw data only | The worst case: a folder of research data with no accompanying metadata. All entities must be drafted from scratch through conversation with the user (file scanning, lookups, and HITL checkpoints). This is the most common real-world scenario. |

**Guiding principle:** Meet the input where it is. Read every metadata file present and reuse every field it can, whatever its structure; if nothing is present, build everything from conversation and lookups. Never discard curated metadata.

### Output Format

The output is a single self-describing RO-Crate directory: `ro-crate-metadata.json`
at its root, describing everything beside it. `export_crate`
(`builder/tools/builder.py`) is the only step that materialises the crate
directory — `build_and_validate` assembles and validates entirely in memory.

```
<output_dir>/                        RO-Crate root directory
├── ro-crate-metadata.json           the RO-Crate metadata descriptor
├── ro-crate-preview.html            browsable preview, no tooling needed (#86)
├── ro-crate-metadata-maturity.html  maturity report + entity explorer (#85)
└── <payload>                        the described files, copied in
```

Payload files land at their path **relative to the input tree**, so an exported
crate mirrors the folder the depositor handed over. A `File` entity's
`dest_path` is contained to the crate root — an absolute or traversing path is
refused and replaced with a `data/<slug>` fallback, so no payload byte is ever
written outside the output directory (#167). Generated tables land under `data/`
(an exposure's condition table at `data/<exposure>_condition_table.csv`). Every
scanned file the agent did not explicitly place is still attached to the root
`hasPart` as a plain `File` leaf, so an export never silently drops a file
(#175).

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
A ChEBI-only `MolecularEntity` (no `pubchem_cid`) takes that resolvable ChEBI
**PURL** as its `@id` (`http://purl.obolibrary.org/obo/CHEBI_<n>`, derived from
the `sameAs` IRI or the `chebiId` CURIE by `_crate_mapping._chebi_purl`) rather
than a `#MolecularEntity_<eid>` fragment — preferring an externally resolvable
identifier the lookup actually produced over a local fragment (Issue #179, D5:
never fabricated; a compound with no `pubchem_cid` and no ChEBI identity still
falls back to the fragment). `pubchem_cid` (the PubChem compound URL) still wins
when both are present.

### Anti-Hallucination
The agent **never fabricates identifiers**. Every identifier is verified against its source. If verification fails, the field is cleared and the agent tries alternatives or asks the user.

**PubChem CID — verify against the authority's own answer (Issue #261).** A PubChem
CID is the *primary key of the PubChem record itself*. PubChem's
`/compound/name` endpoint (which `verify_identifier` re-queries) resolves *names*
and CAS synonyms but **not** a bare numeric CID, so routing a CID back through it
always missed — and D5 then cleared the very CID the authoritative name→CID lookup
had just returned, on *every* compound. `resolve_compound` therefore confirms a
`pubchem_cid` that equals the CID its primary `lookup_compound` returned directly
against that resolution (`composites._verify_compound_identifier`), marking it
`verified` rather than re-querying the wrong endpoint. A CID that is **not** the
lookup's own answer (a hint-supplied / stale value) — and every non-CID field such
as `cas` — still goes through the normal `verify_identifier`, which confirms
against source and clears an unconfirmable value (D5 preserved, CAS unchanged).
This is correct independent of the #252 warm cache, which previously only *masked*
the false negative on the single-call happy path.

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

### Compound Resolution Performance (Issue #252)
Left to itself, `resolve_compound` fans a single compound out to up to **six**
PubChem round-trips — name→JSON + synonyms for the lookup, then a *fresh*
re-resolution of the same compound for each of the CAS and PubChem-CID
verifications — so a concurrent burst multiplies 429 retry/backoff across all of
them. Three in-process levers in `builder/tools/_resolve_cache.py` bound that cost
without weakening D5 (identifiers still come from the authority and are verified):

- **Shared in-process cache** keyed by *normalized* name (strip + collapse
  whitespace + casefold), warmed with the resolved CAS / `CID <cid>` alias keys.
  `lookup_compound` consults it before any network work, so the two verify
  re-resolutions read the already-fetched authoritative record (6 round-trips →
  ~2) and a repeated compound is instant (0 round-trips). The alias keys hold the
  exact record PubChem returned, so verification still confirms against the
  authority's own answer.
- **Bounded concurrency gate** (`_ResolveConcurrency`, default 4) admits only a
  few resolves at once, so a burst does not all storm PubChem and trip its rate
  limiter — complementing the per-host throttle, which spaces each request.

> **Status: the shipped default no longer matches the bound stated below.**
> `DEFAULT_RESOLVE_TIMEOUT` (`builder/tools/_resolve_cache.py`) is currently
> `240.0` seconds — four times the stall the bound exists to cut short — so the
> 20s figure is the intended budget, not the one in force.

- **Per-compound timeout** (`run_with_timeout`, default 20s) bounds the lookup; on
  expiry `resolve_compound` returns a graceful `{"ok": False, …timeout…}` partial
  result and creates no entity, rather than hanging ~60s on a stuck round-trip.

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

### D8: Observability via Reasoning Log
Every tool call, state change, and reasoning step is recorded in `CrateState.checkpoint.reasoning_log` as a structured event: `{"step": int, "action": str, "tool": str, "result": str, "timestamp": datetime}`. This log enables:
- **Live status** for web UIs (`get_status()` returns current phase, entity counts, MIT scores, iteration count, last action)
- **Session replay** for debugging — re-run the tool calls from the log against the same state
- **Progress tracking** — number of entities drafted, fields filled vs total, validation pass/fail counts
- **Diagnostics** — which lookups failed, which HITL checkpoints were rejected, how often the agent got stuck

The reasoning log is persisted with the session and survives resume. A future web UI can tail or stream this log without changing the builder's internals — the data structure is already there.

### D9: Approved Scan Roots (Security Guard Rail)
The `scan_files` tool is restricted to directories the user has explicitly approved. Every session has a `CrateState.approved_scan_roots` set. When the agent calls `scan_files(path)`, the path is resolved to an absolute canonical form and checked against approved roots — if not found or within a subdirectory of one, scanning is denied. New roots are added only through user approval (a user-provided input path at `initialize()`/`read_directory()`, or a real HITL approval). This prevents the LLM agent from accessing arbitrary filesystem locations and provides a clear audit trail. On macOS, this same mechanism protects user files. On Linux, it prevents scanning into `/proc`, `/sys`, or other system paths.

**Fail-closed (#197).** Nothing is walked unless an approved root says so:
- The engine always passes a concrete allowlist (an empty `set()`, never `None`); the scanner refuses (returns `[]` without walking) whenever `approved_roots` is `None` or empty. `None` must never be reintroduced as the "no roots" value — a nullable allowlist is what a reader treats as "no guard".
- The agent's own `scan_files` call can **never** add a root, and no path is auto-approved by being the first one scanned. Roots enter the allowlist only from a user-provided input path or a real approval.
- A hard denylist (`scanner._is_forbidden_root`) refuses `/`, the user's home directory itself, `/System`, `/Library`, `/private`, `/var`, `/etc`, `/usr`, bare `/Users`, and `/Volumes` even if explicitly present in `approved_roots`; it is also enforced in `engine._directory_to_approve` so a forbidden directory can never *become* an approved root. Legitimate subdirectories are unaffected.
- `SimulatedHumanInterface.present(..., purpose="scan_root")` returns a `rejected` action, so the non-interactive default can never approve a new scan root (benign checkpoints still auto-approve).
- `SmokeTestHumanInterface` (`--smoke-test`) is `is_interactive = True`, so unlike the simulator it *does* reach the `present` escalation — and **refuses a `scan_root` purpose outright, before any choice is consulted**. That refusal is stated in the class itself and deliberately *not* inherited from the console frontend's `deny_by_default` pre-selection: `_default_choice_index` falls back to the LAST option when no choice reads as a refusal, so a caller offering `["Show me the folder first", "Yes, allow this folder"]` turned the inherited rule into an approval. **An unattended mode must never be the approver for filesystem access**, so it is stated directly rather than inherited from a rule written for a human at a keyboard; `tests/test_smoke_test_mode.py` pins it, including the case where no option reads as a refusal.
- The A/B eval is **no** exception: both arms meet the corpus behind the production `SimulatedHumanInterface`, and `eval/tests/test_arm_symmetry.py` asserts that neither arm's interface reports `is_interactive`. An eval-only interface must never claim `is_interactive = True`: beyond letting a scan-root escalation be approved, that signal also un-gates the ReAct loop's RECOMMENDED/OPTIONAL validation escalation, so one arm would silently pay for extra full SHACL sweeps.

**Extended to read + write tools (#167).** The approved-roots boundary previously guarded only `scan_files`, so prompt injection could still escape it via the read tools (arbitrary local file read, e.g. `read_file('/etc/passwd')` or a secrets `.env`) and the export writer (a `..` traversal `dest_path`, or a symlinked source escaping the input tree). The fix adds one shared containment primitive, `scanner._contain(candidate, approved_roots) -> Path | None` (resolve realpath, reject when not inside any approved root, apply the `_is_forbidden_root` denylist, fail closed on empty/None roots), applied at three choke points: the read-tool dispatch in `engine.run_tool` (gates `read_file`/`read_excel`/`read_docx`/`read_file_sample`/`read_multiple_files`/`extract_pdf_text`/`preview_archive`/`unzip_file`), `_crate_mapping._file_dest` (contains `dest_path` under the crate output dir, else `data/<slug>`), and `_crate_mapping._file_source` (refuses sources whose realpath escapes `input_path`). The scanner read functions themselves stay unguarded so `scan_files` can still sample files internally; the gate lives at the orchestration layer.

### D10: State Brief as the Trailing Message, Not the System Prompt
The per-turn state brief (session id, file/entity/iteration counts) is **not** appended to user
messages — and not to the system prompt either. `call_model` delegates message assembly to
`_assemble_model_messages`, which puts `SYSTEM_PROMPT` first and the brief built by
`_build_system_prompt_with_state()` **last**, after the history. `SYSTEM_PROMPT` is therefore kept
**byte-stable**: no volatile state may be appended to it, because a provider caches the stable
`tools + system + history` prefix, and volatile state in the system message breaks that cache
immediately after the prompt and leaves the growing, expensive history uncached (Issue #60).
Volatile state at the tail cannot bust a prefix.

Neither the system message nor the brief is persisted into MemorySaver — both are rebuilt fresh on
every invocation, so per-turn metadata never accumulates across turns (Issue #66). The LLM can
still query full details via `get_status`.

### D11: CI Workflow (GitHub Actions)
A `.github/workflows/ci.yml` workflow runs on every push/PR to `main` (Issue #58). It executes
`uv sync`, `ruff check`, `ty` and `pytest`. `ty` **gates** — the tree reports zero diagnostics, and
it is never to be put back behind `continue-on-error`: run advisory, it quietly accumulated dozens
of real diagnostics (a wrong function signature, an LSP-violating override) that nothing was ever
going to force anyone to fix. `pytest` runs the **whole** suite (`testpaths = tests`, `eval/tests`),
sharded 16 ways by pytest-split rather than filtered — no marker, ignore or deselect excludes
anything. The one carve-out is the live-network lookup checks, opt-in behind `VITRO_LIVE_LOOKUPS`
so **CI never touches the network**; do not wire them into the workflow. This prevents regressions
from landing on `main` and keeps the SHACL validator-wiring test gated.

### D12: Bounded Message History (Trim + Prune Before Each Model Call)
`MemorySaver` accumulates the full transcript across turns, so without intervention every
`app.invoke()` replays the entire conversation — including large tool outputs — making per-turn
input tokens grow linearly (cumulative cost quadratically) until the context window overflows
(Issue #61). The history is therefore **bounded before each model call** inside
`_assemble_model_messages` (`builder/agents/react/agent_loop.py`) via `_trim_history`, which runs two
layers in order:

1. **Prune consumed verbose tool outputs** (`_prune_state_backed_outputs`). A `ToolMessage` over
   `_PRUNE_CONTENT_THRESHOLD` chars is replaced by a short stub — but **only once the model has
   consumed it**, i.e. a later `AIMessage` (the model responded) or `HumanMessage` (a new turn
   began) exists. The predicate is load-bearing, not an optimization: the graph edge is
   `tools → model`, so the node running immediately after a tool result is `call_model`, and
   pruning on name and length alone destroyed the result *before any model saw it* (#376). Only
   the newest tool-result block is replayed verbatim; everything older is stubbed, which is where
   #61's savings come from. The `ToolMessage` is **rewritten, not dropped**, so the
   `AIMessage(tool_call)` → `ToolMessage` pairing is never broken.

   The stub is **truthful per tool class**, because the two classes differ in what is recoverable:
   `_STATE_BACKED_TOOLS` (`scan_files`) genuinely persists to `CrateState.scanned_files` and its
   stub points at `list_scanned_files`; `_REPLAYABLE_READER_TOOLS` (`read_file_sample`,
   `read_multiple_files`) persist **nothing** — `CrateState` has no body store — so their stub
   never claims the text is in state and never tells the model not to re-run. It points at
   `read_file`, which returns the body in full up to its 64 KiB budget.
2. **Token-budget trim** via `langchain_core.messages.trim_messages` with `strategy="last"` and
   `start_on=("human", "ai")`. Keeping the most recent turns within the budget bounds per-turn
   input; the window may begin on a human **or** an AI message, and either way never *begins* with
   a dangling `ToolMessage` (or an `AIMessage` whose tool_call lost its answer), i.e. **trimming
   never produces orphaned tool messages** — providers reject those. Anchoring on `"human"` alone
   is not a stricter form of that rule but a defect: a `HumanMessage` enters the graph once per
   *invocation*, so once the AI/Tool tail outgrows the budget the window can no longer reach that
   single anchor and `trim_messages` returns **the empty list** instead of a short one. See
   `_trim_history`'s docstring.

   An AI-anchored window is safe only because **`_drop_unanswered_tool_calls`** runs first, over
   the pruned history: an interrupted turn leaves an `AIMessage` whose `tool_calls` nobody
   answered, the provider rejects the whole request for it (`No tool output found for function
   call …`), and the saved history carries it into the next run. The call is **dropped, never
   answered with a synthesised result** — the tool never ran, and inventing an outcome would tell
   the model something false about the crate.

Trimming is applied only to the *history* between the stable system prefix and the trailing state
brief, so the cache-friendly #60 layout (D10) is preserved: the cacheable prefix shifts only when
the history actually rolls over the budget, far less often than it grew before. The budget is the
`get_max_history_tokens()` knob — `VITRO_MAX_HISTORY_TOKENS` env var → `[agent] max_history_tokens`
config key → default `12000` — mirroring the `max_iterations` precedence. `_trim_history` never
raises into the loop: a trimming edge case falls back to the pruned (untrimmed) history and logs a
warning, so the heaviest payloads are still removed. A trim that keeps **nothing** of a non-empty
history falls back the same way, and that invariant outranks whatever anchor rule a future edit
lands on: the untrimmed history is always a better answer than no history, and the budget is a
target, not a reason to send nothing.

### D13: ISA hasPart Hierarchy — Investigation is the Root

The Investigation **is** the Root Data Entity (`./`); the ISA RO-Crate profile mandates this and
the SHACL shapes forbid alternatives (a Study carrying `additionalType "Study"` MUST be `hasPart`
of the root via `StudyMustBeReferencedFromInvestigation`, so the root cannot itself be a Study).
**A declared licence is read, in whatever convention the deposit states it (#535).** Nothing used
to read it: `set_crate_metadata` — an LLM-callable tool — was its only writer, so the licence was
whatever the model supplied, and on S-VHPS26 the guess inverted the depositor's (CC-BY-4.0 declared,
all-rights-reserved asserted) in the one direction that suppresses reuse. `extract_deposit_licence`
reads a NAMED field, which is not guessing, across the two conventions deposits actually use: the
BioStudies *attribute* (a node naming the field, usually qualified with a canonical URL) and the
*field* every other record uses — RO-Crate `license`, CodeMeta, Frictionless `licenses[].path`,
DataCite `rightsList[].rightsUri`. Gating on the BioStudies shape, as it first did, answered for one
repository's export and left every other deposit on the fabricated fallback. An IRI wins wherever it
sits; without one the declared value is returned **verbatim**, because mapping "CC-BY" onto a 4.0
URI states a version the depositor did not (D5).

Two conventions live outside a metadata record and are read as well. `SPDX-License-Identifier:` is a
formal declaration that can sit in any text, so it is honoured wherever it appears; and a file *named*
`LICENSE` / `LICENCE` / `COPYING` declares by its name that its whole content is the licence, which is
what permits reading a URI out of it — in any other file that would be a URL appearing in prose. The
document formats widen with them: `.json`, `.jsonld`, `.yaml`, `.yml`, `.cff` (CITATION.cff is YAML,
and `license` is one of its standard keys) and `.xml`.

XML carries the same field convention in element form — DataCite keeps the machine-actionable value
in an attribute (`<rights rightsURI="…">CC BY 4.0</rights>`) and the label in the text, Dublin Core
puts the whole thing in `<dc:rights>` — so tags and attributes are matched on their **local** name,
namespace stripped. It is parsed through `defusedxml`, not stdlib `ElementTree`: a deposit is
untrusted input, and one crafted file would otherwise expand a billion-laughs entity during a scan.
A refused or malformed document is simply not a declaration.

Prose is never a source, and the guard is load-bearing rather than defensive: YAML parses a README's
bullets into a list of mappings, so `- License: see the LICENSE file` would otherwise be filed as
legal terms, and every real deposit's README ships the unfilled placeholder `[Default CC-BY 4.0 for
data, CC0 for metadata unless specified otherwise]` — two licences named, neither declared. Only a
document whose top level is a **mapping** is a metadata record, and a `LICENSE` holding only legal
text names no identifier: reading "Creative Commons Attribution 4.0 International Public License" off
its first line would invent a machine-actionable claim out of a heading. Which file is the DEPOSIT's is
decided rather than left to directory order: shallowest first (a root descriptor describes the
deposit, a bundled `package.json` four levels down describes itself), then a machine-actionable IRI,
then the path. Depth outranks the IRI preference deliberately — a nested SPDX URI must not beat the
root descriptor's own label. `license_from_deposit` marks the value as read, and `set_crate_metadata`
cannot overwrite one that was.

**An unstated licence says so, and claims nothing (#540).** `license` is a base MUST, so the field
cannot be left empty — but answering it with `ALL RIGHTS RESERVED BY THE AUTHORS`, as it once did,
converts an unanswered question into the most restrictive claim available, asserted by machine over
someone else's data by a tool whose purpose is FAIR outputs. RO-Crate allows `license` to be a
`CreativeWork` rather than a URL, so a crate with no declared terms carries `LICENCE_NOT_STATED_ID`
(`#licence-not-stated`) — an entity whose name and description record that the depositor stated
nothing, which is neither a grant nor a restriction. It satisfies the requirement, keeps the crate
conformant, and is machine-readable: a consumer branches on the `@id` rather than string-matching
prose. A licence the depositor *did* state always wins, and is emitted as a described entity when
`describe_license` recognises it (never renamed when it does not). The gap engine and MIT scorer
discount the entity by id, so an unstated licence never reads as filled — see §Gap Analysis.

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
  **in addition to** the root's reference, never instead of it (#532) — raw/processed data are the
  data of an assay, and RO-Crate lets a data entity be `hasPart` of more than one `Dataset`. The
  root's reference is what keeps the file in the crate's **file tree**: that tree is walked from
  `./` through *directory* Datasets, and an ISA container is a contextual `#Study_…` / `#Assay_…`
  node, not a directory. Re-parenting therefore stranded every payload file — ro-crate-py refuses to
  open such a crate, while all three SHACL profiles pass it, because none of them asks.
- Re-emits the Assay's **`dataFiles` / `resources` PageTab aliases** (both expand to `schema:hasPart`
  via `profiles/context.py`) as resolved File references *and* nests those Files under the Assay's
  `hasPart` — same move as result Files, so the gold-crate JSON keys round-trip (`_wire_dataset_aliases`,
  #180 Lane C).

`read_existing_crate` (`builder/readers/existing_crate.py`) reads a built crate back into a
`CrateState`: it recovers the **bare** entity_id (stripping the type-qualifier so
`#Study_study_1` → `study_1`, not the unbounded `#Study_Study_…` double-prefix), reconstructs the
`study_id`/`assay_id` linkages the crate encodes structurally via `hasPart`/`about`, and folds the
root back into an Investigation entity. `_build_process` reads the `input`/`output` aliases as well
as `object`/`result`. It has **no production caller** — no build arm, no CLI flag, no tool — and the
round trip is **not** idempotent on a real crate: measured on a built S-VHPS22, build → read → build
recovers 180 of 329 node ids and does not reach a fixed point. Treat it as a test-only helper until
something wires it up or it goes (#711).

### D14: Entity-Graph Visualization (`builder/writers/provenance_dag.py`, Issue #130)

`build_crate_graph(metadata, *, layer, all_edges)` is the deterministic model of a built crate's
`@graph`: nodes classified into the three paper layers (packaging / ISA / ISA-Tox), each referenced
`@id` marked *described* / *external-identifier-backed* / *dangling*, orphans flagged and split by
repair shape (isolated vs stranded), and a cumulative `layer` filter.

**`status` and `residence` are two facts and must never be read for each other (#687).** `status`
answers whether the crate *describes* an id; `residence` answers where its bytes are, read off the
`@id` with no heuristics — `carried` (a relative path: the bytes belong in the crate directory),
`record` (a `#fragment`: a description, no bytes), `elsewhere` (an absolute IRI), `named` (nothing
describes it). One field carried both, so a Cellosaurus IRI, a `#fragment` PropertyValue and a PDF on
disk were indistinguishable, and anything drawn from it would claim a compound's bytes were in the
crate. The shape is the whole rule: it is decidable, needs no disk and no network, and a report must
render the same wherever it is read. It is also the only rule available where this runs — the
maturity report is rendered *into* the crate before `crate.write()` materialises anything, so no
relative path exists on disk at that moment. A `File` declared and never materialised therefore reads
as carried; that is a defect in the crate, and validation is where it belongs. Node labels carry the
generated-file badge (`_display_name`), since a label is ellipsised long before a 16-character
`AUTOGENERATED` prefix ends; each node also carries the crate's own `name` for the places that have
room for it. Every view of a crate reads this one model.

It is drawn by the **entity explorer** (see below), and by nothing else:
`python -m main --graph [--view crate|labprocesses]` writes the explorer as a standalone
page and opens it. The five **inventories** — `build_chemical_inventory`, `build_cellline_inventory`,
`build_people_inventory`, `build_isa_inventory`, `build_citation_inventory` — feed the report's
coverage matrices and the explorer's view membership from one place. Crates built before #618 carry a
`ro-crate-graph.mmd`; on the reader side that name stays **reserved plumbing** — never auto-added as
a payload leaf, never a `CrateState` entity, never a node in a re-rendered graph — so a re-scanned
legacy crate cannot package a generated artifact as the researcher's data.

**Maturity report (`ro-crate-metadata-maturity.html`, #85).** `export_crate` also embeds a human-readable
maturity report as a `File` + `CreativeWork` `about` `./`. It is rendered by
`builder/writers/maturity_report.py` (`build_maturity_html`) as a light-mode evaluation
dashboard and covers four axes: profile adherence (rendered from the crate's existing
`state.validation` — it does **not** re-run the SHACL validator, so the embed adds no validation
cost to export — validation stays a separate step), FAIR indicators + DSM level
(`assess_fair_maturity`, with `dsm_ceiling`'s `blocked_by` naming what stands before the next
level), OECD MIT
coverage (`assess_mit_coverage`), and AI-readiness (`assess_air_readiness`).

The page follows the maturity-report design handoff (PR #607 records it): a header whose headline is the **accession** (subhead: the
publication's name when the crate has one, else the study title), an **About this study** card
(identifier/contact/affiliation/funder/licence/publication+dataset DOI — every value a fact the crate holds or
an honest *not stated*, never a guess) and an **About this RO-Crate** card (the build facts behind
the crate's own `vitro-crate build` CreateAction, plus a provenance note carrying the report id
`MR-<date>-<hash6>`, rendered only when the report is built with the crate's graph — a state-only
render cannot claim its figures come from the crate's metadata). The headline is the accession, because that is what a reader cites — but only while it reads as one
(`state.looks_like_identifier`: one compact token, no whitespace, no longer than a DOI URL). A crate
reached this report headlined a filename slug sitting in the root's `identifier` where a registry
accession belongs (#628), so a value that fails that test is demoted rather than dropped — the title
leads, and the study card states the identifier, since a reader who cannot see it cannot question
it. `set_crate_metadata` applies the same test and warns rather than refusing: the crate carries the
value as `schema:identifier` whatever its shape, and the tool cannot tell a weak identifier from a
real one it has never heard of. Then a **KPI grid**: a profile ×
requirement-level conformance **matrix** (rows the three layers linked to their specs — IRIs pinned
to `_crate_mapping`'s `conformsTo` constants by test — cells ✓/✗/– with counts on `title`; the
Required column is the report's one headline verdict). A `–` carries two different sentences:
*not assessed* where the sweep did not reach that tier, and *no checks defined at this level*
where nothing in the row's layer chain declares a rule there at all, because an empty result
then is the profiles' silence rather than the crate's cleanliness. ISA and ISA-Tox declare no
`sh:Info` shape of their own, but each row is cumulative over `PROFILE_LAYER_CHAIN` and they
extend a profile that declares twelve, so their Optional column is answerable and a clean
crate reads a tick there. Which tiers a layer can report at is
read from the validator's own requirement registry (`profiles.validator.tiers_defined`), never
from a list kept by hand, so only a tier that could have failed is allowed to pass. A finding
outranks that state: one filed at a tier the profile defines no check at still reads ✗, the FAIR ladder (the *next* rung dashed red
and filled to the ratio of indicators met, so a gated 0 never reads as "nothing done"), the **FAIR
principle 1.3** rose (one wedge per MIT module: angle = share of the checklist, radius = fill;
faint full wedges carry the share), a graph tile (linked / total entities) and the AI-readiness
profile (met of assessed, with the seven dimensions as bars; a dimension nothing could be assessed
in is drawn hollow rather than at zero).
Each **Entity coverage** block is a fold (#629): the section is an inventory of a whole crate — the
Files block alone lists 59 on a real deposit — so left open it sits between the reader and
everything below it, and closed it reads as a contents list. The count rides in the summary
because that number is the whole value of a block nobody opens, and `@media print` forces every
fold open so a printed copy keeps the inventory it exists to carry. The Files block's own
per-Dataset folds nest inside unchanged. Findings collapse into **Recommendations** rows — the instrument's own words verbatim in a mono
chip prefixed by its source layer, a badge, then `remediation.describe`'s bold instruction with
`remediation.why`'s one muted consequence clause. **Both instruments use that one shape.** A DSM
indicator blocking the next level arrives as an `Action` like any other
(`remediation.dsm_indicator_actions`), so "this crate is not valid until you do X" and "this crate
does not reach Level 2 until you do Y" are read the same way and ranked against each other — the
`MATURITY` tier sits between `REQUIRED` and `RECOMMENDED`, because a conformance failure means the
crate is not a valid RO-Crate at all while a rung is only the next thing to reach. Its badge names
the rung rather than borrowing a validator severity, and its instruction is the indicator's
`remedy` in `fair/dsm_indicators.yaml` — repo-authored beside `LOCAL_SCOPE`, because the workbook
states the question and never the fix, and the generator refuses to emit an assessable indicator
that has no remedy. The section therefore renders for a crate whose validation is clean and whose
ladder is not, and the FAIR tile's blocker count links into it rather than restating the list.
Numbered **References**
close the page (1: FAIRplus DSM / RDA FDMM; 2: tox-maturity-indicators; 3: the Bridge2AI
AI-readiness criteria, named as a preprint and as quoted verbatim under CC BY-ND). There is no FAIR
pillar detail section and no header verdict pill; the entity explorer, entity coverage, the
profile-adherence breakdown, **the DSM "% complete" grid**, MIT
coverage and the AI-readiness profile keep their sections between the KPI grid and
Recommendations.

**The DSM's published output is the "% complete" grid, and nothing else.** No formula in
any sheet of the assessment workbook computes an achieved maturity level. The report
therefore leads with the grid the sheet computes — level x {content, representation,
hosting, total} (`fair_assessment.dsm_grid`) — and carries the gated level beside it
labelled **derived**, because "how far up the ladder" is the question depositors ask and
it is deliberately harsh: one failing level-1 indicator hides everything above it. The
section heading links the published assessment tool; its URL is recorded once, as the
YAML's `source.assessment_tool`, and the writer reads it from there.

**The grid is the sheet's arithmetic, read from the sheet.** `scripts/gen_dsm_indicators.py`
carries the workbook's own scoring into `fair/dsm_indicators.yaml` under `scoring` —
which indicators each cell counts, each cell's denominator, and the nine promotion rules
— so the scorer reproduces the instrument instead of hand-coding an approximation of it.
Three of its properties are load-bearing and none is obvious:

* **A blank scores 0.** The sheet's validation column is entirely formulas (`=H{row}`),
  so an unanswered indicator evaluates to numeric 0 and `COUNT` counts it. The published
  instrument has no "not assessed" state.
* **Cell membership is not "the indicators at this level".** Higher levels carry lower
  ones forward, and it is a multiset: `DSM-4-H2` sits on two rows of the Level-4 hosting
  cell, which divides by three.
* **Level 0 counts zeros**, because its statements are the pre-FAIRification condition
  in the negative.

Because this tool *can* say "not assessed" and the sheet cannot, every cell publishes
both numbers: `published_pct` is the sheet's, and is what an external assessor
reproduces; `pct` divides by what was actually assessed and is `None` when nothing was.
Reporting only the first would publish a Level-0 row of 100% on the strength of never
having looked; reporting only the second would publish a number nobody can check.
`tests/test_dsm_sheet_parity.py` holds the two together by interpreting the sheet's
formula text and driving the engine over the same answer vectors.

**The sheet's two answer columns are both filled.** `AgentEngine.initialize` scores the
deposit as it arrived — the one moment the state holds nothing but a file inventory and
any licence the deposit declared — and stores the verdicts on `CrateState.pre_assessment`,
because `crate_state.json` is overwritten on every save and that moment is otherwise
unrecoverable. `assessment_graph.as_received_graph` is its evidence: one `File` per
scanned file and a root carrying `hasPart` and, only where the deposit declared one, a
`license`. It **mints nothing** — no
descriptor node, no root identifier, name, description or `conformsTo`, no structure —
because each of those would score the input for work the FAIRification has not done;
`tests/test_fair_metrics_can_fail.py` pins both the shape and the exact set of indicators
a folder of files may honestly meet — and that guard, not a rendered figure, is what the
as-received graph is for: no page shows the intake column. Two limits, both known: `--resume`
does not run `initialize`, and the capture returns early when `pre_assessment` is already set,
so a session that started without a baseline can never acquire one.

**The indicators no crate can evidence are answered by the depositor.** Twenty hosting
and thirteen enterprise-governance indicators describe the environment serving the
dataset, which the published tool puts to a person. `--dsm-answers` reads a flat
`{indicator id: bool}` YAML onto `CrateState.dsm_answers`; `dsm_verdicts` merges it
**only where `scope` is `na`**, so the crate always wins where the crate can answer, and
an id left out stays unassessed rather than defaulting either way. The verdict names its
source, so a cell filled by a person is never mistaken for a measurement.

**Three properties keep a DSM verdict honest.** (i) It is **tri-state**: an indicator
with nothing to read answers `None`, which leaves `pct`'s denominator rather than
counting as a failure (`Verdict.__bool__` raises, so a caller cannot silently collapse
`None` to `False`). (ii) Every verdict carries **evidence** stating what was measured
(`"28 of 59 files are in an open format; proprietary present: …"`), because the published
model is a human assessment instrument and "why did it say no?" must be answerable
without reading the source. (iii) The model's statements **nest**, and the sheet resolves
that by **promoting**: `J4` is `=IF(J5=1,1,H4)`, so meeting the higher rung satisfies the
lower one. `_apply_promotion` reproduces those nine rules to a fixed point and records
which cell licensed each one. Promotion is monotone upward but can never manufacture a
pass, because every rule's source is itself a check that can fail. `dsm_verdicts` is the
single evaluation pass — the level, the grid and the ceiling all read it, so they cannot
disagree, and the report evaluates it once per render. The MIT axis keeps the
aggregate score as the headline and additionally breaks coverage out per guidance document (#491):
each checklist parameter's `standards` map buckets it under the documents that require it
(`MITReport.standard_scores`, labels from `MIT_STANDARD_LABELS`); documents overlap, so the
per-document rows deliberately do not sum to the checklist total. The section says what it scores:
every checklist item is a FAIR maturity indicator as defined in tox-maturity-indicators
(`MIT_INDICATORS_URL`), which the lead links.

**One MIT module, one colour (#606).** `maturity_report.MIT_MODULE_STYLES` is the one registry of
module colours (keyed by the scorer's module name); the stylesheet declares none — a test asserts it
— and derives every state from the one `--mod` token the renderer sets. Every MIT bar speaks one
vocabulary — hue = the module, solid = filled, pale = still missing — so the module rows double as
the key, and a guidance-document bar is split into one span per contributing module, each span that
module's own progress bar, drawn from `MITReport.standard_module_scores` (the document bucket
partitioned by module; see `_score_modules` / `_render_mit_section`). The palette floors (all-pairs
separation under simulated protanopia/deuteranopia and normal vision, ≥3:1 on the page, clear of
every status colour) are pinned by tests, not asserted in prose; the palette is independent of the
entity-category ring below, which it never shares a figure with.

Profile adherence is reported across the three SHACL severity tiers **Required / Recommended /
Optional** (#306). The report must not lie about unassessed tiers: the fast in-loop path
(`build_and_validate`) gates at REQUIRED severity and never populates `should_issues` / `may_issues`,
so an empty SHOULD/MAY list means the tier was *never evaluated*, not that it is clean. Such a tier
renders as an explicit **"not assessed"** neutral state (glyph + label, never colour-only), never as
a green zero; REQUIRED/RECOMMENDED issue text is still surfaced as `Must fix` / `Recommended`
suggestions. Rendering this from `state.validation` alone (no new validation machinery) keeps the
pure/cheap contract.

**Conformance is cumulative, because the profile is a stack.** The packaging (RO-Crate) and
structural (ISA) layers are adopted as published and the domain layer refines them, so
interoperability is inherited rather than rebuilt and a conforming crate is *simultaneously* a valid
RO-Crate and an ISA-structured object. The matrix therefore reports each row over `_LAYER_CHAIN` —
its own checks **and** every layer it extends. Grading each layer in isolation showed the
contradiction that motivated this: a real crate reported ISA failing REQUIRED on 11 findings while
ISA-Tox, judged on its own 35 checks and blind to the 140 it inherits, passed clean. Nothing may now
pass a tier a layer beneath it fails, and a cell whose findings are not its own says so — *"11
findings at this level, 11 inherited"* — so a reader knows which layer to fix.

The rule lives in `state.conformance_by_layer` / `PROFILE_LAYER_CHAIN`, and **every** surface that
paints conformance composes through it rather than reading the three per-pass flags raw: the
matrix, the `prof-card` REQUIRED cards, the TUI status dots (`● base ○ ISA ● Tox` said the crate
failed ISA and passed ISA-Tox), the TUI summary and conformance lines, `dashboard`'s
`✓ Base ✗ ISA ✓ Tox`, and `session`'s `validation_status`. Each raw flag reports only what its own
layer ADDS, which is a fact about the pass and not about the crate; six places restated it and all
six drew the same impossible picture.

This is also what makes the OPTIONAL column answerable. ISA and our tox profile declare no `sh:Info`
shape of their own, so in isolation that column was a permanent dash reading as *"this level does
not apply"* — false for a profile whose conformance includes RO-Crate's twelve MAY checks. It is
composed from the per-layer verdicts rather than by enabling the validator's own inherited
reporting: ISA is 1.1-lineage while the base pass runs 1.2, so inherited reporting mixes two
versions of one spec (measured: 25 findings under `isa` overlapping the base pass's 17 in only 6 —
neither superset nor partition). Upstream tracks the version bump as crs4/rocrate-validator#194;
composing needs no re-validation and cannot mix spec versions.

**A verdict states the ground it stands on (#530).** "Is every declared Data Entity part of the
payload?" is REQUIRED by the base profile and answerable only where the payload exists — the
in-memory gate validates a document, so that check emits *nothing* there, and its silence must never
be read as a pass. `verify_payload` states the invariant the crate itself must satisfy instead of
naming a check id (ids move between upstream releases, and skipping a check that emits nothing
suppresses nothing): every local data entity is backed by a source `crate.write()` will
materialise, since ro-crate-py writes the metadata for a source-less entity and no bytes. Export
runs it against the assembled crate **before** the report is embedded — a verdict reached after the
write could never reach the report shipping inside the crate — and files what it finds as REQUIRED
issues, so the existing rendering flips the Profile-conformance verdict (the matrix's Required column — the report's one headline verdict) without knowing this check exists.
`payload_checked` records that something looked; a verdict where nothing did says so rather than
implying a clean sheet it did not earn.

**A rule that infers its own target cannot fail on a missing edge (#537).** The ISA shapes mint
their target class *from* the very edge whose absence is the defect: `FindISAProcesses` stamps
`isa-ro-crate:Process` only on a `bioschemas:LabProcess` some Dataset already points at, and
`ProcessMustBeReferencedFromDataset` then targets that inferred class — so a process nothing
references never earns the label, the rule written to catch exactly this defect has no target, and
every rule keyed to the class goes silent with it. 11 of the profile's 12 shape files are built this
way, and our `tox/7_assay_key_event.ttl` rides on `isa-ro-crate:Assay`, so the blind spot is
general: a missing structural edge switches off the whole rule-set for that layer, and the crate
reports conformant precisely when its structure is most broken. The upstream shapes are not ours to
restructure, so `verify_isa_reachability` asserts the invariant on our side, the one way an absent
edge cannot game — an entity nothing points at is detached, whatever the profile could evaluate.
Reachability here is **directed**: `provenance_dag.build_crate_graph` already flags orphans, but
over an *undirected* walk, where a process pointing at the files it produced counts as connected
though nothing points at it. Entities named by an absolute URI are described here and live
elsewhere, the same line `verify_payload` draws. `isa_reachability_checked` records that something
asked.

**Every finding folds out of the severity row it belongs to** (#510). Severity is the primary axis
because it is the fix order — REQUIRED blocks the build, the advisory tiers do not — so a tier row
that has findings becomes a `<details>` whose summary is the row itself, listing those findings
grouped by profile layer inside it (base → ISA → ISA-Tox, the gate-ordering contract). A second
per-profile index alongside the rows would restate the same counts twice, so there is none, and the
profile cards stay non-interactive: they assert REQUIRED-gate conformance and nothing else. A row
holding REQUIRED findings is born `open` (a collapsed fold must never hide a blocking issue); a row
with nothing to show does not fold at all, so a disclosure caret always means there is something
there. Print keeps every row AND unfolds it.

The rendering source is chosen **per tier, never once for the report**: `validation.issue_records`
for a tier that has them (grouped by layer, entity ids shown as chips), else that tier's flat
display list (ungrouped — such a verdict carries no attribution to group by). A verdict can hold
records for one tier and only strings for another — a pre-records checkpoint that then takes a
REQUIRED-gate write-back — and deciding globally there would hide the string-only tiers' findings
while the row went on counting them. The count a row advertises is the count of what it unfolds.
Advisory caps apply per profile group, and a cap that bites names how many findings it hid.

When `export_crate` embeds the report it passes the crate's serialized `@graph`
(`build_maturity_html(state, graph=crate.metadata.generate())`), which folds in two sections: the
**entity explorer** (below) and **Entity coverage** — one block per kind of entity, each asking the
question that kind fails at. Can this compound be *obtained* (CAS / PubChem CID / DTXSID plus the
structure fields)? Is this cell line *pinned down* (a Cellosaurus RRID names one stock where a name
names a family; organ / tissue / passage are what let another lab reproduce the culture)? Does this
citation *resolve*, and are its authors entities the crate contains? These are completeness
verdicts, not pictures, and no diagram answers them.

The blocks carry no diagrams of their own — the explorer answers that half better and interactively
— and they stack like every other section. The block names and their order are the owner's, reviewed
on the report artifact: Files / Assays / Chemicals / Biological models / Persons & Organisations /
Citations. A block with nothing to report is omitted rather than shown empty. The `graph` argument
is optional — omitting it (e.g. a bare `build_maturity_html(state)`) skips both sections, so the report stays
useful without a serialized crate. The embedded file is named `ro-crate-metadata-maturity.html`
(sharing the `ro-crate-metadata` stem of the crate's main file).

**The entity explorer (`builder/writers/entity_explorer.py`, #615).** The static views it replaced
each answered one question, and the all-entities view answered "what is in here" without drawing an
edge, because a
node-link picture of a whole crate is a hairball on paper. Given a canvas the reader can pan and
interrogate it stops being one, so the same `@graph` is also shipped to the browser and drawn with
React Flow. `build_explorer_payload(metadata)` is the pure, deterministic model — the
`build_crate_graph` nodes and edges with labels unescaped, the crate document verbatim, the category
registry including each category's `glyph`, and one member list per view; `render_explorer_section`
emits the mount point, the payload as a `<script type="application/json">` data island, and the
vendored bundles.

Views are **toggles, not tabs**: what is drawn is the union of the views that are on, with the edges
induced between whatever that leaves visible, so "the compounds AND the samples" needs no view of
its own. **All entities** is the one that opens — everything the crate describes, which is every
node the model gives a layer. **Assays** draws the ISA backbone plus what its assays are *for*: the adverse outcome pathway a
study serves and the key events an assay measures, which the ISA-Tox profile hangs off
`schema:mentions` (`7_assay_key_event.ttl`, `6_study_aop.ttl`). Followed from the backbone rather
than swept from the crate, and filtered by type — `mentions` is general enough to carry the
build's own action, and a key event nothing in the backbone claims is not one this crate's assays
measure (#627). A domain type also outranks the generic one it refines when a node is captioned,
so a key event reads as a key event rather than as the `DefinedTerm` it also is — and it is drawn in
its own `pathway` category, because what an assay measures takes part in the work rather than
qualifying it, and the fallback bucket paints csvw columns and the build's own action (#643).
`PATHWAY_TYPES` — the pathway, its key events and the `KeyEventRelationship`s that order them — is
the one list all three rules read: which nodes the view follows to, how they are captioned, and what
colour they are drawn in. A relationship is in it although nothing `mentions` one: what a view
*reaches* and what an entity *is* are different questions, and a chain whose every link is drawn as
vocabulary is not a chain.

**Adverse outcome pathway** draws the chain from the other end: every `pathway`-category entity — the adverse
outcome pathway, its key events and the `KeyEventRelationship`s ordering them — plus the ISA entity
that `mentions` one, as context. Assays starts at the backbone and follows outward, so it shows only
what an assay or study points at (5 of 36 on a real deposit); this view starts at the chain, so a
relationship (which nothing mentions) and an event no assay measures directly are drawn too. The
chip counts the chain, not the context (#625), and the view is not offered at all when the crate has
none — `build_explorer_payload` omits any view no entity satisfies, so no per-view guard exists.
It is named for the framework rather than shortened to "Pathways": in this community a *pathway* is a
WikiPathways molecular pathway, so the short label would promise genes and deliver key events (#652).

**LabProcesses** draws the derivation chain plus what each step *is*: the protocol a visible
process executes and the assay whose `about` points at one. Neither edge lies on the material chain
the derivation walk follows, and the two point in opposite directions — outward to the protocol,
inward from the assay — so the view showed every step and every file it touched while never saying
how a step was done or which assay it served (#626). Followed, never collected: context reaches the
canvas only through a step the view already draws. The other toggles are the tabbed section's own
selections, reused rather than re-derived — `_derivation_edges` and `_route_hop_ids` are shared with
the SVG renderers for that reason, and tests hold each toggle to what its panel draws. A view no
entity satisfies is not offered, the way an empty tab is not shown. **A chip counts the view's
subject, not its selection** (#625): a selection carries the context that makes it readable — the
files a step touched, the process and table that link a compound to the work — so counting the
members made every chip overstate its own label, LabProcesses by threefold. The subject comes from
the same source the matching coverage block counts, and a test pins the two numbers to each other
rather than each to a literal; it is counted *as drawn*, so a subject the view cannot show is never
a number the reader has no way to look at. A view whose name covers everything it draws — `All entities` — declares no subject and counts its members. Selecting an entity opens a side
panel with its properties, its links in and out grouped by relation, and its JSON-LD; a toolbar
toggle swaps that for the whole `ro-crate-metadata.json`. Every `@id` in either is a button that
moves the selection — **never a link**: the payload carries the crate verbatim, `javascript:` URLs
and all, so the absence of anchors is load-bearing and pinned by test.

**The navigation is one row, and the inspector is not in the section.** The chips are the whole of
the toolbar — the whole-crate view first, fenced from the questions about parts of it — and search
sits *on* the canvas, because it acts on the drawing. Framing is React Flow's own control and is not
offered twice. The colour key and the count sit **under** the canvas, with the drawing they describe,
and the **entity-coverage inventory folds there too**: it inventories the same entities from the
other side, and as a section of its own it put a six-block contents list between the reader and the
rest of the report. The inspector is a **drawer docked to the window's right edge**, present only
once an entity is chosen — inside the section it cost the canvas a third of its width for a panel
that says nothing until then. Both sections dock to the same edge, so each announces when it opens
one and the other puts its own away; below the breakpoint there is no window to hang off and the
panel stays beside the canvas.

**The assay lanes are a section of their own (#686).** One assay drawn as the chain it is — cell
line, culture, cultured sample, exposure, exposed sample, readout, raw files, analysis, processed
files — one lane at a time, chosen from a chip per assay. As many chips as the crate has assays;
they are minted from the ISA inventory rather than declared, because every other view is a question
about the crate and an assay lane is named for an entity only this crate has. A crate with no assay
gets no section rather than an empty heading. No lede: the drawing is captioned by its own column
headings, and prose explaining a picture is the first thing a reader skips.

It is a section rather than a view of the explorer because the two answer different questions with
different instruments: a lane has a fixed left-to-right order and nine named columns, so it wants a
flat drawing a reader scans, not a pan-and-zoom viewport that has to be framed first. Combining a
lane with any other view also handed the lane's geometry a graph it had no place for, and every node
it could not place reached the canvas without a position.

A lane draws that assay's steps, the materials **one hop** out from them, the protocol under each
step, and the compounds one hop past those protocols. It draws neither the Study, the Investigation,
nor the assay itself — drawn, the assay would connect to every step and rebuild the star #678 took
apart, so it frames the view instead. The material walk is a hop and not a closure, or a shared file
would lead out of the assay and undo the scoping. The key is built from the assay's **name**, because
the key is what a shared link carries and real ids repeat their own kind; names are not unique, so
where two assays share one, every lane with that slug takes an id-derived suffix — decided across all
assays before any key is minted, so keys never depend on the order the graph listed them in.

The section carries **no data island of its own**: it reads the explorer's, which already holds the
nodes, the edges, the vocabulary, the palette and the crate document. One copy of a crate on a page
that ships inside that crate is the accepted cost of self-containment; a second would not be. The
report therefore emits the explorer first.

Two folds and a framing, all the reader's, and all **on the viewer rather than in the bar above it**:
the chips pick *which* assay, these change how that assay is drawn, and a row of identical pills said
the two were the same kind of choice. **Protocols** puts the band away. **Unfold files** opens a
column of files that is otherwise drawn as one stack — a readout that wrote forty files is ordinary,
and forty boxes down one column is a lane no one can read across; a column of anything but files
never folds, because the other columns are the chain and hiding a step would hide the finding. Both
folds are applied by handing the geometry a smaller graph and asking again rather than by editing its
answer, since a fold changes which column is tallest and the chain is centred on that. **Fit** scales
the drawing to the viewer; at rest the boxes are drawn at reading size and the viewer scrolls, since
a nine-column chain of a real deposit is 1,800px wide and neither answer is right for every reader.

The section draws **plain SVG, built by hand** — no React, no React Flow, no layout library. That is
the point of it being a section: the explorer's canvas is a pan-and-zoom viewport with a dagre pass
behind it, and a lane needs neither.

**Two pictures, one of everything else.** The two viewers share the data island, the palette, the
legend, the chips, the footer strip, the overlay controls and the drawer — and, in code, one module:
`explorer_inspector.js` builds the inspector in plain DOM and each viewer mounts it into its own
`<aside>`. DOM rather than either app's framework, because the one thing React and hand-built SVG
both have is an element to fill. The module also owns the **vocabulary** — `term`, `edgeTerm`,
`prop` — for the reason the legend's wording lives in Python: a second copy is how one page comes to
say two things. A box is captioned the same in both, name over the crate's own type, and a lane is
**something to link to**: the chosen assay, the folds and the selection ride in the page's one hash
under this section's keys (`lane`, `fold`, `pick`), each side replacing only what it owns so neither
erases the other's link.

**Compounds are one hop past a protocol.** ISA restricts `schema:object` to File/Sample/BioSample at
Violation severity, so a `MolecularEntity` is never a process input directly; `reagent` is a
LabProtocol property ranging over it, and `Exposure --executes--> table --reagent--> compound` is the
correct representation rather than a detour to shorten (#650). The process views follow that second
hop, anchored on the protocols the drawn steps execute — a compound with no edge to any drawn work is
what the view is not for. The model draws `reagent` reversed, so the arrow points at the step that
consumes the material.

**Where the nodes go (`entity_explorer_layout.js`, #619).** Layout is its own module and its own
`<script>`, holding pure geometry — no DOM, no React, no payload — so a test can run the shipped code
over a crate's graph rather than a Python restatement of it. A layered pass gives every node in a
rank its own row, which a crate defeats by construction: the root `hasPart` every file it carries,
so one rank holds the whole file list and the layout is as tall as the deposit is long (a real
293-entity crate laid out 12,100 px inside a 620 px canvas). Leaves are what makes a rank wide and
they are also what a row says nothing about — nothing hangs off them — so a rank holding more than
`RANK_CAP` leaves is packed into a near-square grid. The packing is expressed to dagre rather than
around it: each block enters the second pass as ONE stand-in node the size of the grid it will hold,
with its members' edges redirected to the stand-in, so rank order, rank spacing and the room a block
needs stay the layered algorithm's answers; the members are dealt into the box afterwards. A rank at
or below the cap keeps its column, because a column is a rank and that reads better than a grid
does. This does not make every view framable — a crate with 80 non-leaf entities over nine ranks is
some 3,400 px tall whatever is done with its leaves, and "all entities" stays a view to navigate
rather than to take in at once.

**What a node and an edge encode (#688).** Selecting an entity lights its edges and names each one
in **the vocabulary the crate is serialized with** — `schema:object`, `bioschemas:executesLabProtocol`
— never the model's internal words (`input`, `executes`), which name nothing a reader can look up.
`relation_terms()` derives the mapping from the relation tables and the `@context` and ships it in the
payload, so the browser holds no second copy: a hand-kept list beside them is a second vocabulary, and
the moment a relation changes predicate the two disagree with nothing to catch it. A relation whose
namespace has no declared prefix falls back to its full IRI, which is long and true; an invented
prefix would be short and a lie. Labels carry no background box — the text takes the edge's colour and
a surface-coloured `paint-order` halo knocks the line out from behind it, so a label reads as part of
its edge. Clicking the selected node clears the selection, and the labels with it.

**What the page carries is not the model (`payload_codec.js`, #694).** `build_explorer_payload` is the
readable model — ids everywhere, one dict per edge — and stays that way, because it is what every
Python consumer and test reads. The **data island** carries a compacted encoding of the same thing:
an edge becomes `[src_index, dst_index, label]`, a view's members become indices, and a node's `name`
is omitted where it equals its `label`. A mean `@id` in a real crate is 53 characters and the model
repeats one per edge endpoint and one per view membership — some 1,800 copies of strings already in
`nodes` — and the report ships *inside* the crate and is opened from disk, so no transfer encoding
ever squeezes them. On S-VHPS22 that is 301 KB → 182 KB, a 15% cut to the whole section. A category's
`glyph` is not shipped at all: nothing has drawn one since #688. `expand` runs on the app's first
line, so no other line knows the wire format exists; the two halves are inverses and the round trip is
tested by running the **shipped JavaScript** over what Python produced, rather than each against its
own mirror. Deriving `nodes`/`edges` in the browser from `document` would save more and is
deliberately not done: it would be a second implementation of `build_crate_graph`, and the two would
drift.

**The inspector's Overview (#688).** The first tab listed an entity's raw JSON keys, which are the
serializer's shorthand: `input` and `object` are one predicate, `studies` and `assays` and `hasPart`
are another, and a reader had to know the `@context` to see it. The Overview names each property as
the crate expands it (`property_terms()`, derived from that context; a key the context does not name
expands under its `@vocab`, which is the crate's own rule rather than a guess) and shows **one row per
predicate, not one per spelling** — the crate's own keys are on the row's tooltip. The Links tab names
relations from the same table the edges do, so a relation is not one word on the canvas and another in
the panel. `parameter` and `parameterValue` stay two rows: they are two predicates, emitted
deliberately because the two profiles the crate claims ask for parameters under different ones.

A URL is offered **for copying, never as a link**. The payload carries the crate verbatim,
`javascript:` URLs and all, so the explorer writes no anchor and no link target — that absence is
load-bearing and pinned by a test that greps the app for the attribute names. A clipboard write
navigates nowhere and executes nothing, so the reader gets the URL without the crate getting a way to
run anything.

A node encodes two orthogonal facts: **the border is its category, the fill is its residence.** A
tinted fill means the bytes are in the crate directory (#687) — the one thing a reader can act on,
since it separates a record from a file they can open. Category glyphs are **dropped; colour only.**
Shape was the redundant channel that survived greyscale, print and colour vision deficiency, and
dropping it leaves eleven categories on a colour ring `CATEGORY_STYLES` itself calls full at ten. The
cost is knowingly taken, with the inspector naming the type in words and the legend stating the
mapping as its mitigations; the glyph data and its uniqueness rule are kept so restoring the channel
is a one-line change rather than a re-derivation.

**Where an assay's nodes go (`assay_lane_view.js`, #686).** Ranking by dependency puts a protocol in
a rank to the *right* of the step that executes it, so the material chain a reader is following is
interrupted by what is not material. A lane splits the two directions instead: **horizontal is the
material chain, vertical is what qualifies a step.** Rank is decided by what a node **is** in the
ISA-Tox chain, never by a layered pass: a step by its `additionalType`, a material by the step whose
`result` produced it, and a material nothing produced by the step that consumes it. So a rank is a
COLUMN — two CellCultures stack and cannot coincide — and a missing step is an **empty column**, not
a declined graph, which is the finding a maturity report exists to show. `derivesFrom` is excluded
from the ranking edges though it is material: a cultured sample derives from the line its culture
consumed, so the edge points back up the chain.

**Rows follow the chain, and connectors run beside the column.** A rank ordered by id is a rank
ordered by nothing a reader can see: an assay culturing three lines draws three parallel tracks, and
ids sorting differently from their cultures braid them together so that every crossing claims a
relationship none of them have. Each rank therefore takes its order from the rank before it, ties by
id so two builds of one deposit draw alike. Where the chain genuinely fans — three samples into one
exposure and out again — nothing in the crate says which came from which, and the crossing that
remains is the deposit's rather than the drawing's. Band connectors run down the gap to the **left**
of the column and bracket into both boxes' sides, one vertical per anchor: dropped from the anchor's
own box, a step in the top row of a three-row rank would draw its line straight through the two
steps below it, and a line crossing a box reads as an edge to that box.

Both tiers of the band are *dealt* into a grid, because a step may execute several protocols and a
table may list a dozen reagents, so substances cost height and never width. The module returns `null`
for a graph with no step it recognises, and the section says so where the drawing would have been.
The page loads it as a plain `<script>` tag, which is the UMD branch `require()` never exercises, so
a test evaluates the page's own script bodies with no `module` in scope.

**The legend names types, not categories (#623).** A colour key labelled in category prose — "Sample
/ material", "Term / parameter" — explains the canvas in a vocabulary the reader can see nowhere
else, while every node on it is captioned with its type. Each key is therefore labelled from the
crate's own census: the distinct type tags its nodes carry in that category, commonest first, the
first two spelled out and the rest counted away with the full list on `title`. Derived, never a
hand-kept map, so a category that gains a type is labelled with it the day it does, and the fallback
bucket — which has no single type to name — is labelled as honestly as the rest. A refinement folds
into its base (`Dataset · Assay` counts as `Dataset`): the colour is the base type's, and the
refinement is what the node itself spells out. The one key the census cannot supply is the off-crate
reference, which keeps its wording because it names a provenance status rather than a type.

**Script, but nothing loaded.** The report's contract was "carries no script"; it is now *loads
nothing*. React, React Flow, dagre and htm are vendored UMD builds under `builder/writers/vendor/`,
pinned by `manifest.json` (name, version, licence, origin, sha256) and verified against it at render
time — a bundle that no longer matches fails the render rather than shipping inside every crate
built afterwards. They are inlined with a credit banner, and React Flow's stylesheet joins the
report's single `<style>` rather than opening a second one in the body. This costs the report about
450 KB of library plus a copy of the crate's own metadata: a JSON panel cannot `fetch` a sibling
file from `file://`, and a report that only works where it was built is not an artifact that travels
inside a crate. Tabs stay CSS-only — they are a different mechanism with a different failure mode —
and print keeps them: the canvas is a screen affordance, so `@media print` hides it and shows a note
saying where the interactive version lives.

The page is **self-contained** (inline CSS, inlined scripts, no external assets) so it renders
offline. The styling
and document shell live in sibling assets — `maturity_report.css` and `maturity_report.html` (with
`__STYLE__` / `__TITLE__` / `__BODY__` placeholders) — which `build_maturity_html` reads (cached) and
**inlines** at render time; only the data-driven markup is assembled in Python. Embedding is
automatic, best-effort (a reporting failure never fails the export), and can be turned off with
`export_crate(..., embed_report=False)`.

**One entity type, one colour and one shape — `CATEGORY_STYLES`.** Colour that changes between
views teaches the reader that colour carries no meaning — which costs them a channel the whole-crate
view has nothing to replace with. Every view therefore takes its colour and its glyph from one
registry, and no view decides a palette of its own.

`provenance_dag.CATEGORY_STYLES` is the single registry: one entry per functional category holding
its **colour**, its **legend wording** and its **glyph** (14×14 path data), keyed by the string
`_entity_category` assigns. Everything downstream is generated from it —

- `category_css()` emits the `--cat-*` custom properties plus each category's tile, node and tag
  rules into `maturity_report.css` at the `__CATEGORY_STYLES__` placeholder. CSS cannot iterate, so
  these rules are **generated, never hand-written** — hand-written rules are how a category ends up
  with a colour in one view and none anywhere else. The stylesheet declares no palette of its own; a
  test asserts that.
- `build_explorer_payload` generates the explorer's palette into the data island, so the canvas
  takes its colours and glyphs from the same registry rather than a second copy in the stylesheet.
- `_node_class` / `_node_class_for_brief` both classify through `_entity_category`, so a coverage
  matrix and the explorer cannot disagree about what an entity is.

Colours are a constant-lightness ring in CIE Lab (L\* 47, chroma 44, hues 36° apart), with process
and container split on lightness instead because sRGB is narrow in the blues. Worst pair dE 24;
every stroke clears 3:1 on the page. The ring holds **ten**, and it is full — against a frozen palette the best eleventh colour anywhere in the
sRGB gamut reaches dE 22.7, under that floor. So **saturation is the second channel**: the ring is
for entities that take part in the work, and `annotation` — the bucket for an entity that *qualifies*
another — is drawn muted (chroma 19) beside `ctx`'s near-grey for an entity the crate never typed.
An eleventh category therefore costs a demotion rather than a new hue, and which entities earn a
saturated colour is a design decision, not an accident of registry order. Shape is
the channel that survives greyscale, print and colour vision deficiency, so **no two categories may
share a glyph** — `TestCategoryRegistry` pins that, the palette separation, the contrast floor, and
the generated-CSS coverage.

### D15: Deterministic Pipeline as the Default Build Path
The `--interactive` default is the deterministic pipeline (§14), not the ReAct loop
(D1): code owns the step ordering and the LLM is confined to bounded leaves. The
rationale is **efficiency, predictability, and clean termination**, not raw
capability — a capable model reaches SHACL conformance on either path. The default is
gated on the in-repo A/B (`eval/`), whose standing finding is qualitative: the pipeline
is cheaper per build and terminates on its own, where ReAct runs to a recursion cap.
ReAct stays a supported variant (`--react`) for flexible conversational exploration.
The success metric is profile conformance (base + isa + tox) plus an entity-count
quota — **not** scientific accuracy.

> **No measured figure from that A/B is published here, and no figure from it may be
> quoted until it has been re-measured on the fixed harness (#636)** — the
> conformance result as much as the cost, token and wall-clock multipliers. The runs
> predate the harness fixes in #609 and gave the two arms different environments;
> the biases do not all point the same way, so the net direction is unknown until a
> re-run.

### D16: ISA-Tox Specialization via `additionalType`, Not `@type` Arrays

Every ISA-Tox specialization is expressed as `@type: <bare base token>` +
`additionalType: <discriminator string>` — **not** a JSON-LD `@type` array:

- A cell-line sample is `@type: "Sample"` (`bioschemas:Sample`) + `additionalType:
  "CellLine"` + a `sampleType` DefinedTerm (`profiles/shapes/tox/1_cell_line_sample.ttl`,
  isa_tox.md §Sample - Cell-based Test System).
- LabProcess steps are `@type: "LabProcess"` + `additionalType:
  "CellCulture"|"Exposure"|"EndpointReadout"|"DataAnalysis"`; ISA backbone nodes are
  `@type: "Dataset"` + `additionalType: "Investigation"|"Study"|"Assay"`.

The specialized `tox:` class is **inferred by the validator**, not asserted in the crate:
each tox shape carries a SHACL `TripleRule` that adds `rdf:type tox:CellLineSample` (etc.)
when the discriminator matches (`FindCellLineSamples`). A generic RO-Crate consumer sees
the base type; the ISA-Tox validator sees the specialization — this is why `@type` stays
the bare token.

Consequences the builder respects: `_crate_mapping` emits the bare base `@type` plus the
discriminator (never a `@type` array), and a single conceptual entity is ONE node — a
`CellLineSample` already IS a Sample, so modelling it as a *separate* `Sample` +
`CellLineSample` (two entities sharing a bare `entity_id`) is discouraged by RO-Crate 1.2
(§Contextual entities) and warns at `CrateState.add_entity` (#366).

**A node may still carry more than one type — for reasons that are not
discriminators.** D16 forbids expressing an ISA-Tox *specialization* as a `@type`
array; it does not forbid a node being genuinely two things:

- **Its published schema.org supertype.** RO-Crate RECOMMENDS a type in the
  schema.org namespace, and the shape checking it is syntactic — it looks for an
  IRI beginning `http(s)://schema.org/`. Our domain types resolve to
  `https://bioschemas.org/…`, so a well-typed entity would fail it. `add_schema_org_types`
  therefore appends the supertype: `LabProcess → schema:Action`, `LabProtocol →
  schema:HowTo`, `Sample → schema:Thing`, `MolecularEntity → schema:BioChemEntity`.
  Every one is READ from the vendored `profiles/vocabulary/type_supertypes.json`,
  never decided in code — a published alignment, not our claim. See that function's
  docstring for the rules; a type absent from the vocabulary simply gains nothing.
- **Cross-vocabulary co-typing**, where one artefact really is two kinds of thing:
  a deposited procedure document is `File` + `LabProtocol` (#646), and the generated
  per-well condition table is `File` + `csvw:Table` + `LabProtocol` (#650) — a CSV,
  a typed table, and the layout the exposure follows.

The domain type stays FIRST in every case: it is the specific, meaningful one, and
the rest are what a generic consumer can follow.

## 12. Project Structure

Where each component lives:

```
vitro-crate/
├── AGENTS.md                    This file — authoritative system design
├── CONTEXT.md CONTRIBUTING.md README.md
├── .github/workflows/ci.yml     CI (ruff, ty, pytest on push/PR)
├── pyproject.toml
├── main.py                      CLI entry point — mode dispatch, output-path resolution (§14)
├── profiles/                    Domain profiles + validation
│   ├── context.py               JSON-LD context builder
│   ├── validator.py             3-pass SHACL validation (base / isa / tox)
│   ├── models/isa.py, tox.py    Entity classes
│   ├── shapes/tox/              Custom tox-ro-crate SHACL shapes
│   │                            (base + isa shapes ship with rocrate_validator)
│   ├── contexts/               Vendored RO-Crate 1.1 / 1.2 JSON-LD contexts
│   └── docs/
├── lookups/                     External API clients
│   ├── cellosaurus.py, pubchem.py, comptox.py, aopwiki.py, bao.py
│   └── orcid.py, ror.py, crossref.py, iuclid.py, _http.py
├── mit/invitro_tox.yaml         Minimum Information Table
├── fair/                        FAIR indicators (RDA FDMM + FAIRplus DSM, vendored)
├── air/                         Bridge2AI AI-readiness criteria (vendored)
├── input/                       Example inputs
├── builder/                     Core builder system
│   ├── state.py                 CrateState dataclass
│   ├── engine.py                AgentEngine — run_tool, gating, approved scan roots
│   ├── config.py, pricing.py    Provider/model config; token pricing
│   ├── tools/                   Tool implementations (the shared toolbox)
│   │   ├── scanner.py, drafters.py, composites.py, management.py
│   │   ├── lookups.py, verification.py, builder.py, validation.py
│   │   ├── repair.py, gap_analysis.py, mit_assessment.py, fair_assessment.py
│   │   ├── air_assessment.py, assessment_graph.py  Bridge2AI axis + shared verdicts
│   │   ├── data_content.py, file_readers.py, hitl.py, session.py
│   │   ├── document_discovery.py, file_descriptions.py, rehome.py, remediation.py
│   │   ├── field_kinds.py        Shared field-kind vocabulary (both arms)
│   │   ├── registry.py, _crate_mapping.py, dashboard.py, provenance.py
│   │   ├── profiler.py, reachability.py, _resolve_cache.py
│   ├── readers/                 Input readers
│   │   ├── directory.py, existing_crate.py, metadata_files.py
│   ├── writers/                 Output writers
│   │   ├── rocrate_writer.py
│   │   ├── provenance_dag.py     Entity-graph model + inventories (#130)
│   │   ├── maturity_report.py    Maturity / FAIR HTML report
│   │   ├── entity_explorer.py   Interactive React Flow entity graph (#615)
│   │   ├── entity_explorer.js   …its browser half (no build step)
│   │   ├── entity_explorer_layout.js  …where its nodes go (#619)
│   │   ├── explorer_inspector.js  …the panel and vocabulary both viewers share
│   │   ├── assay_lane.py        Assay-lane section (#686)
│   │   ├── assay_lane_view.js   …where one assay's nodes go
│   │   ├── assay_lane_app.js    …its browser half: SVG, chips, folds, inspector
│   │   ├── payload_codec.js  …the wire format its data island carries (#694)
│   │   └── vendor/              Pinned UMD builds inlined into the report
│   └── agents/                  Orchestration + LLM config
│       ├── build.py             BuildMode switch + run_build dispatch; pipeline entrypoint (run_interactive_build)
│       ├── llm.py               Shared model construction + usage mining, both modes (#309)
│       ├── ui.py                Shared interactive UI: status bar, reply, banners, boxed prompt (both arms)
│       ├── progress_spinner.py  Shared live progress spinner (both arms)
│       ├── pipeline/            Deterministic pipeline mode (--interactive DEFAULT)
│       │   ├── pipeline.py        Pipeline spine (run_pipeline)
│       │   ├── guidance.py        HITL guidance tail (run_guidance)
│       │   └── leaves.py          Bounded LLM extraction leaves (drafter tier)
│       └── react/               ReAct StateGraph mode (--react)
│           ├── agent_loop.py      ReAct StateGraph loop
│           ├── system_prompt.py   ReAct system prompt
│           └── tools_spec.py      TOOL_SPECS advertised to the ReAct LLM + the registry-parity contract (#327)
├── eval/                        A/B eval harness (--arch react|pipeline)
├── scripts/                     Developer tools (indicator/criteria generators, profile validator)
├── docs/                        Profiling notes + agent docs (linked from §4, §5)
├── sessions/                    Persisted sessions
├── output/                      Built crates (versioned)
└── tests/                       Test suite
```

## 13. Future Considerations

Extension points the current design leaves open (not yet built): registering
external **MCP** servers as additional tools (the toolbox is MCP-ready);
**multi-user** provenance (the model is single-user today); a **Web API / frontend**
over the builder library (FastAPI/Streamlit call in unchanged); runtime-loaded
**custom profiles** (schemas are YAML); and **batch processing** (state is
per-session, so parallel runs are straightforward).

## 14. The Deterministic Pipeline & Guidance Loop

The default `main.py --interactive` build is a **deterministic pipeline + HITL
guidance tail** over the shared toolbox (§5): code owns the step ordering and the
LLM is confined to bounded leaves. The ReAct agent loop (§4 "Agent Graph",
`--react`) is the supported alternative — §1 states the two-variant
relationship and **D15** records the A/B evidence for why the pipeline is the
default. This section documents the pipeline and its guidance tail.

### 14.1 Decision

The workflow orchestration lives in **code**, not the LLM system prompt: the
sequence `scan → scaffold ISA backbone → materialize the plan → draft entities →
build_and_validate →
fix REQUIRED bottom-up → enrich → export` is control flow (§14.5), with the LLM
confined to bounded leaves (§14.2) and a small agent for the conversational /
unstructured-input tail. The defensible win is **cost, latency, reproducibility,
testability, and clean termination** — not blanket correctness (a capable model
reaches conformance on ReAct too). Making the pipeline the default was gated on the
in-repo A/B; see **D15** for the evidence and the levers.

### 14.2 Pipeline shape

```
INPUT (dir / zip / conversation)
   │
   ▼  DETERMINISTIC PIPELINE (code, not model-driven)
 scan ─ scaffold ISA backbone ─ materialize ─ draft entities ─ retry compounds ─ fix loop ─ data-content pass
                                     │  (bounded LLM leaf: extract→entity)   │      │ (deterministic
                                     ▼                                       │      │  dispatch over
                              cheap drafter model            provider-gated ─┘      ▼  routed issues;
                                                                                       LLM only for
   small TAIL AGENT (strong model) ── only for: no-metadata conversational build,       content repairs)
                                       genuine ambiguity, HITL
   │
   ▼ export — NOT a spine step: `_run_build_body` calls it after the guidance tail
   │
   ▼ OUTPUT: RO-Crate dir + payload + embedded graph/maturity/preview
```

- **Spine = code.** The Priority 1–4 heuristic (§4) becomes control flow, not prose.
- **Leaves = cheap model.** Drafting/disambiguation only (binds the §4.4.5 drafter tier).
- **Leaf context = bounded file bodies, not just filenames (#231).** The single
  `extract`/`draft` leaf is fed by `pipeline.py::_gather_context`, which gives every
  scanned file **one** content slice under **one** cap — a body excerpt when the file
  is readable, its `first_rows` preview in full otherwise — **fail-closed to
  `state.approved_scan_roots`**. Every emitted slice is charged against the total, so
  the ceiling is honest and the one call stays token-safe. The empty-context path is a
  strict no-op (no provider call), preserving the no-provider determinism guarantee.
- **Priority decides chars, not just order (#378).** Files are read **metadata-first**,
  and each tier draws a *weighted* share (`_TIER_SHARES`) rather than an equal one;
  an absent or under-spending tier flows its headroom down. Equal shares are not
  enough — with them the highest-priority file in a real deposit emitted 298 chars
  while a bulk-data export emitted 2,049. High-priority bodies are read with the
  shared compactors (`file_readers.compact_grid_text` / `compact_attribute_json`),
  which is what makes the whole metadata workbook fit. The algorithm is documented in
  the `_gather_context` docstring.
- **Fix loop = deterministic.** `build_and_validate` already returns issues pre-routed to
  `{entity_id, property, fix, severity, profile}`; a code loop dispatches each to a
  lookup / `set_fields` / `link`, calling the LLM only for "draft new content" repairs.
- **Tail = small strong-model agent.** The one place open-ended judgement is irreducible.

### D17: A protocol entity IS its file — nothing is minted for one

`executesLabProtocol` names a **deposited document**: its `@id` is the file's
crate-relative path, and only `name` and `intendedUse` are derived on top. Where the
drafter names no protocol, the deposit's scan is searched and the real file attached;
where the deposit holds none, the step carries **no protocol at all**.

The build used to synthesize `#protocol_<assay>` for any step that named none. That
claimed a procedure nobody wrote, gave a fragment `@id` to something that should be a
path, and silenced the ISA `Process entity SHOULD have a protocol` warning with a
fabrication — an assertion that cannot fail is not a check (D-#620). The warning is a
SHOULD, so the honest gap costs a recommendation while the stub cost the truth.

Claiming a document requires two independent pieces of evidence, because executing it
asserts it explains how that step turns its input into its output:

1. The scan classified it as a protocol **by content**
   (`document_discovery.classification_of`), not by filename.
2. Its path is about **that kind of step** and names **exactly one** subject.

Ambiguity resolves to nothing: two candidate documents, or one naming two subjects, is
evidence for neither (D5). Culture protocols are keyed on the **cell line**, not the
assay — culturing is study-level, and a deposit ships one document per line — so every
assay growing SK-N-AS points at one entity, a culture of two lines executes two, and no
composite is invented. Each is also linked to its Study: `hasPart` for a deposited
document, which is real payload.

### 14.3 Build-path wiring contracts

An empty crate is already valid from a single `scaffold_isa_backbone(...)` call
(`{base, isa, tox}` all true, zero issues); no REQUIRED element lacks a code path.
A richer build must additionally honour two **conditional Violation traps** — the
build-path *wiring contract* — which fire only when the relevant entities exist and
are both code-fixable (document for callers):

1. A `PropertyValue` named `DOI`/`PubMedID` is SHACL-duck-typed and MUST carry
   `propertyID` as an **`@id` IRI node** (the OBI IRI: `DOI`→`OBI_0002110`,
   `PubMedID`→`OBI_0001617`). `draft_property_value` defaults the IRI by name and
   `@id`-wraps it — a bare string literal is a Violation.
2. `EndpointReadout`/`DataAnalysis` have **no `result`/`object` build-time fallback**
   (unlike CellCulture/Exposure); a process with no explicit output fires a Violation.
   `draft_process_chain` closes it from the DEPOSIT (raw files → the readout,
   processed → the analysis), and `attach_files` completes any step still unwired.
   When the deposit holds no such file the Violation is **left to fire** (#592):
   this trap is closed with evidence or reported, never with a manufactured file.
   A step nothing evidences at all — neither data nor a procedure document — is
   not drafted, so an empty crate keeps conforming without asserting an
   experiment it cannot show.

### 14.4 Pipeline composition

The deterministic pipeline is assembled from the shared toolbox (§5), not a parallel
re-implementation. Its parts:

- **`fix_required_issues`** — the deterministic REQUIRED-severity repair loop (the
  keystone; §5, §14.6).
- **Drafter leaves** — bounded LLM extraction (`leaves.py`, below), wired into the
  spine's `_draft_entities` step and gated to a strict no-op when no LLM provider is
  configured.
- **Composite meta-tools** — e.g. `draft_process_chain`, which wires the
  EndpointReadout/DataAnalysis outputs the build otherwise lacks (closing the §14.3
  Violation trap) and wires a whole chain in one idempotent call (§5 Derivation
  Chain Tools).
- **Plan file roles** — the extraction leaf classifies each plan file
  (`raw`/`processed`/`condition_table`/`other`) and the spine **consumes** that
  classification: the single `condition_table` entry is written into the Exposure's
  typed CSV via `populate_condition_table` (#408). A role the spine cannot act on is
  a bug, not a spare field — the plan is not paid for in drafter tokens to be
  discarded. Plan-named files resolve through `_scanned_path_for_name`: matched by
  **basename** (the leaf only ever sees `f.filename`, never `f.path`) and fail-closed
  to `approved_scan_roots`, since plan paths are LLM free text. An unusable single
  candidate — no path, outside the roots, unreadable, or read-but-unmappable — falls
  back to the propose-from-entities path (#422), so it never yields less table
  content than having no candidate at all; only the several-candidates ambiguity
  refuses without fallback, because choosing among real plate maps is a human call.
- **The spine** — `run_pipeline` (`builder/agents/pipeline/pipeline.py`, §14.5), the
  code-driven orchestrator and the default `main.py --interactive` build (via
  `run_interactive_build`, §14.6.1); also selectable in the eval harness
  (`python -m eval --arch pipeline`).
- **The gap engine + guidance tail** — `assess_gaps` (§14.6) feeds the deterministic
  HITL `run_guidance` loop (§14.6.1), invoked *around* the spine by
  `run_interactive_build` for real interactive users only.

The ReAct loop remains a fully-supported alternative behind `--react`; its
`should_continue` graph and its `system_prompt.py` orchestration prose are kept intact.

#### The drafter-leaf (`leaves.py`)

The "Leaves = cheap model" primitive (§14.2). `builder/agents/pipeline/leaves.py`
exposes a single pure function — **`draft_entity_fields(entity_type: str,
context: str, *, overrides: ModelOverrides | None = None, usage_sink: UsageSink |
None = None) -> dict[str, Any]`** — the smallest unit of LLM
work in the pipeline: free-text/context in → a structured dict of one entity's
fields out, in a **single bounded model call**. It is a **library leaf, not an
LLM-callable tool** — the deterministic spine (§14.5) imports and calls it; the
ReAct agent never sees it, so it needs **no four-place tool registration**. It
does **not** mutate `CrateState` and does **not** orchestrate; the spine feeds
its result into the deterministic `draft_*` state mutators.

Contract:
- **Drafter tier.** The call goes through `_build_chat_model(role="drafter")`
  (§4.4.5), so a cheap model does the extraction. With no drafter model configured
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
- **One definition of what the model is offered.** The pruned property set is
  `field_kinds.drafter_visible_fields(entity_type)`, which lives outside this
  module because the spine needs it too (to skip calls that cannot apply
  anything, §14.5's "Draft entities" step) and must not import `langchain`.
  Deriving the bound schema and the spine's skip rule from the same function is
  what keeps them from drifting apart.
- **One model, one ledger.** Every leaf — `draft_entity_fields`, `extract_plan`,
  `describe_files` (driven by `builder/tools/file_descriptions.describe_payload_files`),
  and the guidance tail's question/answer leaves — takes the run's `ModelOverrides` and `UsageSink`
  from its caller; no leaf resolves the model from the environment on its own.
  `run_pipeline` and `run_guidance` forward both to each call, so `--model X`
  pins the whole interactive build and every leaf's tokens reach the same
  profile ledger the ReAct model node writes (#399, #608).

**Fidelity beyond validity — deterministic build-path wiring, not new LLM tools.**
Reproducing the richer structure of a real gold crate is done through `_crate_mapping`
wiring rather than LLM tools, per §14's "prefer the deterministic build path" (and
D5 — identifiers come from lookups or the value is dropped, never fabricated):

- **Identifier PropertyValues** — Person `orcid` → ORCID PV; MolecularEntity
  `cas`/`pubchem_cid` → `[CAS, PubChem CID]`; DOI/PubMed `propertyID` as OBI `@id`
  IRIs; `Person.affiliation` / `Publication.author` resolved as `{@id}` references.
- **Reference wiring** — root `funder` → Organization, root `about` → the DataAnalysis
  LabProcess, Assay `measurementMethod` → BAO `DefinedTerm`, and the hasPart-family
  aliases re-emitted as resolved File refs nested under the assay.
- **CSVW payload** — the full Exposure condition table. Nothing is appended to an
  EndpointReadout: its result is the deposit's raw tier (#589), never a manufactured
  table standing beside it.
- **Characteristics/properties** — CellLineSample `organ`/`tissue` and LabProcess
  `additionalProperty` as PropertyValue characteristics, and source-code co-typing
  (`@type:[File, SoftwareSourceCode]` with `schema:programmingLanguage`).

AOP subgraphs and publications-with-authors are materialized from the spine by their
own composites (`materialize_aop_subgraph`, `draft_publication_with_authors`). Root
`releaseDate` / `dateModified` are set through `set_crate_metadata` and emitted
**only when explicitly supplied** — never guessed (D5); `datePublished` is auto-set
by ro-crate-py at crate construction, so the builder never sets or fabricates it.

> **Tool-registration contract:** every new LLM tool must be registered in **four**
> lockstep places — `TOOL_REGISTRY`, `TOOL_SPECS`, the system-prompt "## Your Tools"
> catalogue, and §5 of this doc (guarded by `tests/test_agents_doc_toolbox.py`). The
> `TOOL_REGISTRY` ⇄ `TOOL_SPECS` half is enforced automatically (#327): the parity
> contract in `tools_spec.py` — `expected_tool_spec_names()`, the shared registry plus
> a small, *documented* set of engine-routed tools (`_LLM_TOOLS_OUTSIDE_REGISTRY`) —
> is checked at runtime by `assert_tool_spec_parity()` when the ReAct arm builds its
> tools, and in CI by `tests/test_tools_spec.py`. A registered tool with no schema, or
> a schema advertising a tool the engine cannot run, fails fast in either direction, so
> the A/B always compares the *same* toolbox. A genuinely engine-routed tool (present
> to the LLM but not in the registry) is declared once, in `_LLM_TOOLS_OUTSIDE_REGISTRY`.
> That parity assert is also ReAct's *reachability* guard — an advertised tool is one the
> model can always reach. The pipeline half is `PIPELINE_UNREACHED` +
> `assert_pipeline_reachability()` in `builder/tools/reachability.py`, checked in CI by
> `tests/test_tool_reachability.py` (#386): a registered tool with no call site reachable
> from the deterministic arm fails unless it carries a waiver stating why, and a waiver
> naming a tool that has since been wired fails too.

### 14.5 The pipeline spine (`builder/agents/pipeline/pipeline.py`)

`run_pipeline(engine: AgentEngine, *, progress=None, save=None, overrides=None) ->
dict` is the deterministic, code-driven orchestrator of §14.2 — the Priority 1-4
heuristic (§4) expressed as **control flow, not prose**, with **no LLM deciding
control flow**. It operates on an already-`initialize()`-d engine (so scanning +
approved-roots happened in the engine) and routes every step through
`engine.run_tool(...)` (so
each is profiled and validation is cached); it never re-implements tool logic, only
orchestrates the existing toolbox. The keyword-only `progress` sink (a no-op by
default) receives one concise line per phase (#241) and the keyword-only `save`
callback (defaulting to `save_session`) persists CrateState at each phase boundary
so a concurrent dashboard live-updates (#242) — see §14.6.1 "Progress +
persistence". The keyword-only `overrides` carries the run's `ModelOverrides` to
every bounded leaf, so `--model X` pins the whole build (§14.4 "One model, one
ledger", #399). The sequence:

1. **Scaffold** the ISA backbone via `scaffold_isa_backbone` — always, and
   idempotent (existing layers are reused). The spine supplies deterministic
   backbone **names** (from `state.metadata.title` when present, else stable
   defaults) because a bare `draft_study` populates only the entity_id, not the
   `name` field, and the ISA profile REQUIRES a non-empty Study `name`. With names
   supplied this alone yields `{base, isa, tox}` on an empty crate (§14.3).
2. **Materialize the plan** — `_materialize_plan` asks the bounded `extract_plan`
   leaf for a candidate plan (names only, no identifiers) and turns each section
   into linked ISA-Tox entities through the idempotent composites, and describes
   the payload files via `describe_payload_files`. This is where most entities are
   minted; the stage is documented in full in §14.6 ("Materialize"). Identifiers
   come from the composites' own lookups, never from the plan (D5). With **no
   provider configured** `extract_plan` is never called and every *plan-driven*
   section is a no-op, but the deterministic process-chain and file-attachment
   steps still run (#262), so the crate is never structurally hollow and the
   no-provider graph hash stays identical across repeats. Returns the per-section
   counts reported as `materialized`.
3. **Draft entities** — the §14.2 bounded **drafter-leaf**: `_draft_entities`
   gathers a free-text context from what the engine carries (crate
   `title`/`description` + a scanned-file digest that gives every scanned file
   **one** content slice under **one** cap — a bounded BODY excerpt when the file
   is readable, whatever its type, and its `first_rows` preview in full otherwise
   — read fail-closed to `approved_scan_roots` and capped by
   `_MAX_CONTEXT_CHARS`, #231, §14.2) and, for
   each draftable entity missing descriptive fields, calls `draft_entity_fields`
   (`leaves.py`) and applies only the returned **non-identifier descriptive**
   fields (fill, don't clobber). It is a **strict no-op when no LLM provider is
   configured** (detected via `config.get_provider()`, the same check the rest of
   the code uses) *and* when there is no usable context — so the deterministic
   spine, its tests, and the deterministic A/B path are unchanged (the leaf is
   never even imported on that path). **D5-safe:** identifier / `@id` / `entity_id`
   fields are never set or overwritten — those come from lookups. Returns
   `{drafted: [<ids>], fields_applied: <n>}`.

   Two properties bound what the step spends and what it can assert:

   - **A call is made only when it could apply something.** The leaf offers the
     model exactly `field_kinds.drafter_visible_fields(entity_type)`; the spine
     applies only fields that are both descriptive and *missing*. When those sets
     are disjoint the call cannot change state whatever comes back, so it is not
     made — a named `MolecularEntity`, `Organization` or `Publication` (schemas
     that expose no `description`) never reaches the leaf.
   - **Each entity gets its own context.** `_entity_draft_context` folds the
     entity's id, type and known field values into the shared crate digest. The
     leaf's signature carries no entity, so a shared-only context makes every
     entity of one type send an identical prompt — the model cannot tell which
     one it is describing, and one entity's description lands on its siblings.
     Only the entity's *own* fields are folded in; naming a sibling reintroduces
     the confusion from the other side.
4. **Retry unresolved compounds** — `_retry_unresolved_compounds` gives every
   identifier-less `MolecularEntity` one (and only one) more `resolve_compound`
   attempt, **before** the fix loop validates, so a recovered CAS is in the crate
   that gets validated and exported. It is **provider-gated** (`get_provider() is
   not None`) because the spine must not reach the NETWORK unless a provider is
   configured — that gate is what keeps the no-provider path both deterministic and
   offline. Whatever is still missing comes back as `unresolved_compounds` so the
   summary can say so out loud (#338).
5. **build_and_validate** in memory (no disk write).
6. **Fix loop** — `fix_required_issues` + re-validate, **bounded to ≤3 rounds**,
   stopping when no REQUIRED issue remains *or* a round fixes nothing (deterministic
   dispatch only; the loop is monotone over the rule set, so a no-progress round
   means the rest needs the LLM leaf).
7. **Data-content check** — `_validate_populated_tables` runs the Frictionless
   payload layer (§6) over a condition table that actually received rows, *after*
   the fix loop and deliberately outside it: that loop terminates on SHACL `ok` and
   repairs SHACL rules, which cannot touch a data cell. Its findings come back
   under their own `data_issues` key and are **not** folded into `ok` — a cell
   contradicting its `tableSchema` is a different defect from a conformance failure
   (#409).
8. Returns `{ok, conformance, issues, data_issues, scaffold, materialized, drafted,
   unresolved_compounds, fix_rounds, usage}`. `data_issues` and
   `unresolved_compounds` each stay their own key, never folded into `ok`; `usage`
   is the per-run leaf token ledger (§14.4 "One model, one ledger").

`run_pipeline` is the **automated** build and stays **guidance-free** — the HITL
guidance tail is invoked *around* it by the interactive entrypoint
(`run_interactive_build`, §14.6.1), never inside the spine, so the A/B eval can
drive the spine non-interactively. This spine is the **default**
`main.py --interactive` build (**D15**); ReAct is opt-in via `--react`.

**Determinism contract:** with **no LLM provider configured** every LLM-touching
step is a strict no-op — the LLM calls inside Materialize (step 2), the
drafter leaf (step 3) and the provider-gated compound retry (step 4) — so every
step is deterministic and the same input state ⇒ an identical built `@graph`, the
headline win the deterministic A/B path of the eval harness asserts
(`crate_graph_hash` equal across runs, zero tokens in CI). When a provider *is*
configured, those steps make bounded, D5-safe extraction and lookup calls, trading
strict graph-hash determinism for richer drafted content.

**Measurable via the same harness.** `eval/pipeline_factory.py`
(`make_pipeline_agent_factory` → `PipelineBuildAgent`) implements the same
`BuildAgent` contract as the ReAct factory: it builds a headless engine (behind
the production `SimulatedHumanInterface`, the same human the ReAct arm gets — see
D9; the ReAct arm likewise runs its **shipped** loop via
`run_build(BuildMode.REACT, ..., interactive=False)` rather than a bare single graph
invocation, so both arms are measured with the budget they ship with, #609),
`initialize(input_path=case.input_path)` (which approves the
input dir under the fail-closed guard), runs the shipped `run_interactive_build`
(spine, then export + persist — the ReAct arm exports from inside its own loop, so
scoring one arm before export and the other after compared two different stages,
#609), and returns the final
`CrateState`/`session_id` exactly like `ReActBuildAgent`.
`eval/__main__.py` adds `--arch react|pipeline` (DEFAULT `react`) selecting the
factory, so `python -m eval --arch pipeline --label pipeline` runs the same
corpus/metrics/report against the spine — diffable vs the frozen `react-baseline`.
The spine calls a model only through its bounded leaves, and only when a provider is
configured — with none set it is a strict no-op, so it runs in CI for real.

The ReAct run used for the original A/B is frozen at git tag **`react-baseline`**.

### 14.6 The hybrid build loop and the gap engine (`builder/tools/gap_analysis.py`)

The full hybrid ISA-Tox build loop runs in five stages — the first three are the
**automated pipeline** (`run_pipeline`, §14.5) and the last two are the
**interactive HITL tail** (`assess_gaps` + `run_guidance`, §14.6.1). Every stage is
deterministic *code* except its bounded LLM leaves — `extract_plan`,
`draft_entity_fields` and `describe_files` inside the spine, and the guidance tail's
phrase/interpret pair (the drafter it uses to *suggest* a value the user must
confirm). **No LLM decides control flow.**

```
        ┌──── AUTOMATED PIPELINE (run_pipeline) ────┐ ┌─ HITL TAIL (interactive only) ─┐
INPUT → Extract → Materialize → Auto-resolve →  …  →  Assess → Guidance (run_guidance)
        (leaf)    (deterministic (deterministic        │       (deterministic HITL loop:
                  composites)    fix loop, REQUIRED    ▼       ask-user / draft+confirm)
                                 severity)        gap engine
```

- **Extract** (`extract_plan`, leaf #213, §14.4) — the bounded whole-document
  extractor pulls a *candidate plan* (names/titles only, no identifiers — D5)
  from the input context in a single model call. The plan also carries each
  process step's **descriptive experimental parameters** (exposure duration,
  detection instrument, endpoint …), drawn from the shared LabProcess hint
  vocabulary (`_crate_mapping.LABPROCESS_PARAMETER_FIELDS`) so both arms offer the
  same keys; without that channel the crate publishes ontology-typed
  ParameterValues asserting `"unknown"` that nobody stated (#379). Identifiers
  remain excluded, and the parameter sub-object is closed so an unrecognised key
  cannot reach LabProcess state.
- **Materialize** (`_materialize_plan` via the idempotent composites #217, §14.5)
  — deterministically turns each plan section into linked ISA-Tox entities through
  `scaffold_isa_backbone` / `resolve_compound` / `draft_cell_line_sample` /
  `draft_process_chain` / `materialize_aop_subgraph` / `draft_person`. Identifiers
  come from the composites' own lookups, never from the plan. The backbone is
  scaffolded BEFORE the plan is materialized, so the plan's Study name/description
  are merged onto the already-scaffolded Study (fill-don't-clobber: only when the
  field is empty or still the generic placeholder), and each `people[]` is split
  into `givenName`/`familyName` so the Person is ISA-conformant (#232). When a
  `people[].affiliation_name` is present, an `Organization` is minted via
  `draft_organization` (or an existing one of the same name is **reused**, so a
  shared affiliation yields ONE Organization, not duplicates) and the Person's
  `affiliation` reference is wired onto it via `set_fields` — the build resolves it
  to the Organization's `@id` (`_crate_mapping._wire_reference`). D5-safe: only the
  plan's affiliation *name* is used — no ROR/IRI is fabricated (#179 Lane 1).
  **Entity→provenance wiring (#273).** Resolving a compound / cell line MINTS the
  entity but leaves it a graph orphan unless something references it, so after the
  `compounds[]` / `cell_lines[]` sections run, `_materialize_plan` wires the
  collected ids deterministically through `set_fields` (never hand-rolled JSON-LD)
  using the canonical ISA-Tox reference fields: each resolved `MolecularEntity` →
  the **Exposure** LabProcess via `chemicals` (ISA forbids a MolecularEntity as a
  process object — objects MUST be File/Sample/BioSample — so the build connects
  the compound THROUGH the Exposure's CSVW condition table, `schema:about` →
  MolecularEntity + the `compound` column's `valueUrl`; `_crate_mapping
  ._build_process`/`_synth_condition_table`); the resolved `CellLineSample` → the
  **CellCulture** LabProcess via `cell_line` (its consumed input); and BOTH are surfaced on the
  scaffolded Study via `schema:mentions` (the `chemicals` / `cell_lines`→
  `biologicalModels` aliases) so every resolved entity — PubChem- AND ChEBI-backed
  compounds alike — is reachable from the backbone at a glance (orphan count → 0).
  Idempotent: `set_fields` writes the same deterministic ids, so re-running mints
  no duplicates. **Condition-table link now fires (#285).** `draft_process_chain`
  no longer pre-empts the Exposure's build-time output with a generic placeholder
  File (see §5), so the `chemicals` set here actually build into the Exposure's
  CSVW condition table (`table --about--> MolecularEntity`): the compounds attach
  as the *true conditions of the exposure process*, and the Study `mentions` edge
  is a redundant backstop (still load-bearing for a compound the table cannot reach,
  e.g. one resolved with no Exposure in the chain) rather than the primary link.
  **Assay→Key Event (#382).** Materializing an AOP subgraph used to wire the *Study*
  and stop, so the crate listed a pathway's key events without ever saying which one
  the assay measures. Each `aops[]` item now also carries `measured_event_name` — a
  NAME, never an id (D5) — and after the subgraph lands the spine calls
  `link_assay_to_key_event(assay_id, event_name)`, which matches that name against
  the KeyEvents just materialized and commits THEIR AOP-Wiki IRI onto the Assay's
  `keyEvent` (camelCase: it is the `Assay:keyEvent` MIT slot). A zero or ambiguous
  match writes nothing and is logged, not raised — which key event an assay measures
  is a scientific claim, so an unlinked Assay is a legitimate outcome, and the
  guidance tail (§14.6.1) can still take the answer from the user through the same
  tool. Counted on `_materialize_plan`'s result as `key_events`.
- **Auto-resolve** (`fix_required_issues`, §5, the keystone) — clears every
  `auto_fixable` gap deterministically from state alone, no prompt. The spine runs
  it off `build_and_validate`'s REQUIRED issues, so the automated path needs no
  `GapReport`; the guidance tail runs it again for any `auto_fixable` gap the
  report still carries.
- **Assess** (`assess_gaps`, the gap engine #215, this section) — one
  prioritized `GapReport` unifying SHACL + MIT + FAIR + AI-readiness. It runs in the
  **guidance tail**, not in `run_pipeline`: the spine validates at REQUIRED severity
  only, and the headless path deliberately never calls `assess_gaps` (see "Headless
  gap summary" below).
- **Guidance** (`run_guidance` #218 / #244, §14.6.1) — the **code-driven HITL
  loop** that walks the remaining `auto_fixable=False` gaps with the user in the
  loop. CODE still owns control flow (it is NOT a ReAct/LLM-orchestrated agent),
  but the per-gap ask-user step is a **small bounded LLM exchange** — the #179
  hybrid's "small guidance agent" (#244) — so a cryptic gap becomes a real
  conversation. The user's raw prose is **never** stored verbatim as a field value:
  typing "no idea which file you mean" must not land as the crate `description`.
  It is invoked **only for a real interactive user** (see §14.6.1).
  The loop **advances over un-progressable gaps** rather than aborting on the
  first one (#230): each round it draws the next *actionable* gap (`report-only`
  gaps are never drawn — see below), and a gap it cannot progress (e.g. the user
  skips it) is added to a **per-report skip-set** (indices into the current report)
  so the loop moves on to the gap behind it. It only stops once the whole report is
  exhausted with no progress — one cryptic, uncommittable gap can no longer abandon
  the 200 behind it — and is still hard-bounded by `max_rounds`. The per-report
  index skip-set is cleared on every commit (the re-assessed report is fresh).
  Alongside it the loop keeps a **per-RUN skip-set keyed by gap IDENTITY**
  (`(source, entity_id, property, message)`, #179): a gap surfaced/answered but not
  progressed is recorded here and **never re-drawn for the rest of the run**, even
  after a *different* gap commits and clears the per-report index set and a fresh
  re-assess re-emits it. Without it the always-highest-priority root citation MUST
  gap — which re-emits every round until a `ScholarlyArticle` is wired — is re-asked
  every round (#179).

  **The per-gap LLM exchange (#244).** When a provider is configured (gated on
  `config.get_provider()`, like the pipeline leaves), each ask-user gap runs a
  bounded **phrase → ask → interpret → commit** cycle using two new drafter-tier
  leaves in `builder/agents/pipeline/leaves.py` (internal pipeline calls, NOT four-place
  LLM-advertised tools):
  - **Phrase** (`phrase_gap_question(gap_context) -> str`) turns the gap
    (property, entity_type, tier, MIT/FAIR rationale, suggestion) into ONE clear
    question with a concrete example — never raw SHACL shapes / FAIR indicator
    codes / property IRIs. On an empty/failed result it falls back to the
    deterministic human-readable prompt.
  - **Interpret** (`interpret_gap_reply(question, reply, gap_context) -> dict`)
    parses the free-text reply into a **structured decision** — one of
    `{action: "commit", value}` | `{action: "skip"}` (covers "I don't
    know"/empty) | `{action: "clarify", question}` | `{action: "from_file",
    filename?}`. **Free-text musings never become field values.**
  - **Commit** — only a `commit`'s clean `value` reaches `_apply_value`
    (`set_fields` / `set_crate_metadata`, never hand-rolled JSON-LD). `skip`
    commits nothing; `clarify` asks at most **one** bounded follow-up
    (`_MAX_CLARIFY_FOLLOW_UPS`) then skips, so the clarify path can't loop;
    `from_file` records the filename hint and commits nothing (file extraction is
    a separate bounded reader, not this loop — never store the prose). **D5:**
    identifier-bearing fields are never committed from the user's prose — those
    come from lookups, so an identifier `commit` is refused at `_apply_value`,
    the single chokepoint every commit funnels through (#375). Guarding the
    chokepoint rather than the interpret leaf is what closes the loop's two
    otherwise-unguarded feeders: the no-provider ask-user path and the
    draft-confirm dialog's `edits["value"]`.
    - **A commit must be one the crate will actually carry** (#375). `_apply_value`
      returns `True` only when the value truly lands, because the loop treats
      `True` as progress and `format_guidance_summary` reports it to the user:
      - a **typed** gap (MIT gaps carry `entity_id is None` + an `entity_type`)
        commits to the single in-state instance of that type — the same instance
        `_ground_entityless_gap` phrased the question about, so the prompt and the
        write can never name different entities. Zero or several instances commit
        nothing. Only a genuinely root-level gap (`entity_type` `None` or
        `Investigation`, since `./` folds the Investigation) may reach
        `set_crate_metadata`; without this a question about an Assay overwrote the
        root's `description`, a Base MUST;
      - a **reference-only** field (`_REF_FIELDS`) accepts only a value that
        resolves as a reference. The build strips those keys from an entity's
        scalar properties and `_wire_reference` emits nothing for a
        non-resolvable literal, so storing prose there and reporting success is a
        lie. The shared `builder/tools/field_kinds.py` answers "is this field a
        reference / an identifier, and would this value resolve?" for both arms —
        `set_fields` logs a warning on the same condition, so the ReAct LLM cannot
        make the mistake silently either.
    - **"Progress" means the gap cleared** (#375). After a commit the loop
      re-assesses and, if the gap's identity is still present, records it in
      `tried_identities` and drops it from `resolved` rather than counting it. A
      commit that does not clear its gap would otherwise be re-drawn every round —
      one un-clearable gap consuming the whole `max_rounds` budget while the rest
      of the report is never reached.
    - **Person/agent fields are committed as ENTITY references, not strings
      (#275).** A `creator` / `author` / `publisher` / `editor` / `contributor`
      gap requires an ISA Person reference, so `_apply_value` routes its value to
      `_apply_person_value`: the prose is parsed into a name (plus an optional
      ORCID / affiliation), a Person is minted via the `draft_person` tool, and
      its `@id` is linked onto the gap entity's field as a `{"@id": …}` reference
      (a root/crate-level person gap is satisfied by minting alone — the builder
      auto-wires every Person onto the Root Data Entity as an author). A supplied
      ORCID is attached **only** after `lookup_orcid` confirms its family name
      (D5); an unverified one is dropped (the name still mints a Person).
      Committing such a field as a literal string leaves the "creator MUST be
      of type Person" SHACL shape unsatisfied, so the gap re-emits every
      round and `isa=fail` (#275) — see the `_apply_value` docstring.
    - **The root `citation` gap is resolved through the publication composites,
      not stored as a string (#179).** The Root Data Entity's `citation`
      requirement (BASE: the auto-wired root `citation` `@id` must be an absolute
      URI; ISA: a `ScholarlyArticle` with an identifier) surfaces with
      `entity_id == "./"`, which `_resolve_entity_id` cannot map to a state entity
      and which is not a crate-metadata slot — so a string commit dropped the
      answer and the always-highest-priority gap was re-asked every round. So
      `_apply_value` routes a root `citation` answer to `_apply_citation_value`:
      an answer carrying a DOI → `draft_publication_with_authors(doi=…)`, otherwise
      it is treated as a title → `resolve_publication(title=…)` (both via
      `engine.run_tool`, never hand-rolled JSON-LD). The builder auto-wires the
      resulting `ScholarlyArticle` onto `root_dataset.citation`. D5: the DOI is
      re-looked-up and a title only commits on a confident Crossref match.

  **Entity-less MIT gaps are grounded in the real instance name (#179).** An MIT
  gap is emitted crate-level with `entity_id=None` carrying only `entity_type`
  (e.g. `CellLineSample`). `_resolve_entity_id` short-circuits to `None` for any
  falsy `entity_id`, so without grounding the phrase leaf sees a bare TYPE and no
  name, and the model invents a stock example — or a question no gap rule raises
  at all. `_gap_context`, when `_resolve_entity_id` is `None` but `entity_type`
  is set, looks the type's
  instances up via `state.list_entities(entity_type)` and threads the REAL name in
  (`entity_name` / `known_fields`); with several instances it surfaces their names
  for disambiguation, so the leaf is **never** handed a bare type with no name.
  The `_PHRASE_SYSTEM_PROMPT` reinforces this: with no entity name it must ask
  generically about "the &lt;entity type&gt;" and is explicitly forbidden from
  inventing a specific name, identifier, or example value (D5: no fabrication).

  **Offline / no-provider determinism.** With no provider configured the exchange
  degrades to the original deterministic ask-and-set: phrase = the human-readable
  prompt, interpret = non-empty reply → commit, empty/skip → skip. The same
  deterministic decision is used when a configured leaf is unavailable or raises,
  so a flaky/unreachable LLM never silently drops the user's answer — and offline
  tests and headless runs stay deterministic. ask-user prompts remain
  **human-readable**, never the raw failed-check `message`.

**The deliberate split — automated vs interactive.** Stages 1–3 are the
**automated** build: `run_pipeline` (§14.5) runs them with **no HITL**, so it
never blocks on a user and the A/B eval can drive it non-interactively
(`--arch pipeline`, a clean automated-vs-automated comparison vs ReAct).
`run_pipeline` therefore stays **guidance-free** — the Assess + Guidance tail
(stages 4–5) is HITL and lives **outside** the spine, in the interactive entrypoint
(`run_interactive_build`, §14.6.1). A headless / simulated run is exactly
`run_pipeline` alone; a real user gets `run_pipeline` *then* `run_guidance`.

#### 14.6.1 The interactive entrypoint (`builder/agents/build.py`)

`BuildMode` (`PIPELINE` / `REACT`) is the single switch that selects a variant, and
`run_build(mode, engine, *, provider=None, model=None, base_url=None, output=None,
resumed=False, initial_prompt=None, verbose=False, interactive=True)` dispatches to
it — `PIPELINE` → `run_interactive_build` (below), `REACT` →
`run_interactive_agent` (§4). `main.py` derives the mode from
`--react` (`BuildMode.from_cli`) and the eval harness maps its `--arch`
string onto the same enum (`BuildMode(arch)`), so A/B is chosen in **one** place
(#309).

**Session provenance is passed, never inferred (#410).** `resumed` states whether
the run was loaded from a saved session (`--resume`) or started fresh, and reaches
**both** arms. Only the CLI knows: `engine.initialize(--input)` populates
`scanned_files` — and the drafters may populate entities — before either arm
begins, so a populated `CrateState` is *not* evidence of a resume. Anything that
branches on resume-ness (the session banner's title, the ReAct greeting prompt and
its offline fallback) takes this flag; `ui.print_resume_summary(engine, *, resumed)`
makes it a **required** keyword so no call site can quietly re-derive it from state
content. Content emptiness still decides whether the banner appears at all — that
is a separate question from where the content came from.

**The ReAct arm needs a kickoff; the pipeline does not (#412).** `run_pipeline` is
code-driven and starts unprompted, but the ReAct loop's greeting invoke sits
*outside* the autonomous-continuation loop (§4), which is keyed on a user message —
so the loop greets and then blocks on stdin having done no work. `initial_prompt`
(CLI `--prompt/-P`, `REACT`-only) seeds that first turn in place of the first stdin
read; blank is treated as absent, and control passes to the ordinary autonomous
loop immediately afterwards. It is opt-in by design: auto-continuing every greeting
would erase the conversational character that makes ReAct a distinct arm.

`run_interactive_build(engine, *, pipeline_runner=None, guidance_runner=None,
exporter=None, output=None, overrides=None, resumed=False) -> dict` joins the two
halves into the end-to-end sequence a real user runs. It:

1. emits a leading **progress** line (`Scanning ✓ (N files)`, #241) and runs the
   **automated** pipeline (`run_pipeline`) — always — threading the `output`
   channel in as the spine's progress sink (see "Progress + persistence" below);
2. runs the **HITL guidance tail** (`run_guidance(engine, engine.human_interface)`)
   **iff the engine's `HumanInterface` is interactive**;
3. surfaces a concise summary of the guidance results
   (`format_guidance_summary` — gaps resolved / asked / remaining per tier, plus
   final base/isa/tox conformance) via the injected `output` channel (e.g. the
   CLI's `print` / console writer);
4. **exports the crate to disk LAST** (`export_crate(engine.state)`, #233) — after
   guidance, so the *enriched* crate is what lands — and surfaces the resolved
   **absolute** crate path via `output` (`Crate written to <abs path>`);
5. does a **final `save_session(state, always_write=True)`** (#242) so a populated
   CrateState overview + a resumable session are guaranteed on disk, then
6. returns `{"pipeline": <run_pipeline result>, "guidance": <run_guidance result
   or None>, "export": <export_crate result>}` — `guidance` is `None` exactly when
   the path was non-interactive; `export` is the (successful) export result dict.

**The on-disk export (#233).** `export_crate` (`builder/tools/builder.py`) is the
only disk writer, and it runs as the deterministic **final step** of
`run_interactive_build` on **every** completed build — interactive *and* headless —
after guidance, so the *enriched* crate is what lands and `--output` has an effect.
The destination is resolved by `export_crate` from `state.metadata.output_path` (the
CLI-resolved path, see below) with the session `working_crate/` fallback, and the
resolved **absolute** crate path is surfaced via `output`. An export failure is
**never silently swallowed**: it is logged, surfaced via `output`, and re-raised as
`CrateExportError` so the CLI signals a non-zero exit. The exporter is injectable so
the wiring is unit-tested with no ro-crate-py / disk (`tests/test_agents_build.py`).

**The ReAct loop mirrors this (#287).** The loop must never depend on the model
*choosing* `export_crate`, and `_finish_backstop` (#251) only runs on the quit/EOF
exit path — so the ReAct arm auto-exports on **every** completed in-loop build too:
`_auto_export_after_build` in `builder/agents/react/agent_loop.py` fires after a
`build_and_validate` that passes **base** conformance over a non-empty crate, calls
`export_crate` with no explicit path (same destination resolution as above), stamps
`_EXPORTED_FLAG` and surfaces the absolute crate path. It is idempotent via
`CrateState.export_fingerprint()` — a **content** hash over entities + crate metadata
+ the scanned-file inventory — so it re-exports exactly when the crate changed and an
unchanged repeat build is a no-op. The fingerprint must be content, **never an entity
count** (#380): a count is invariant under every field-level tool the arm is told to
use for the rest of the session (`set_fields`, `set_crate_metadata`,
`fix_required_issues`, `link`), so counting keeps all of that work off disk — see the
`export_fingerprint` docstring. `export_fingerprint()` is strictly **wider** than
`validation_fingerprint()` because `export_crate` packages scanned files
(`include_all_scanned=True`) the validation path never sees, and
`validation_fingerprint()` must stay narrow or the #155 debounce stops hitting.
`_finish_backstop` gates on the same fingerprint rather than on "something exported
this session": it is the last chance to catch a crate that changed after its
auto-export, and it stamps the fingerprint too so the two exit paths (quit and EOF)
cannot double-export.

**Progress + persistence (#241 / #242).** Both are deterministic, need no LLM, and
never perturb the built `@graph`:

- **Progress (#241)** is surfaced through the **existing `output` channel** — one
  concise line per phase: `Scanning ✓ (N files)` (emitted by `run_interactive_build`
  from `state.scanned_files`), then the spine's own threaded lines
  (`Scaffolding ISA backbone…` / `Extracting plan…` / `Materializing N entities…` /
  `Validating base→ISA→ISA-Tox…` / `Resolving gaps…`), then `Crate written to
  <abs path>`. `run_pipeline` takes a keyword-only `progress` sink (defaulting to a
  strict **no-op**); `run_interactive_build` threads `output` in only when the
  injected `pipeline_runner` actually accepts a `progress` kwarg (signature-checked,
  so a narrower injected test double still works). With the default no-op `output`
  (non-interactive / eval / tests) **nothing is emitted**, so determinism and eval
  output stay clean.
- **Persistence (#242)** is driven by `save_session` calls at **phase boundaries**.
  `run_pipeline` takes a keyword-only `save` callback (defaulting to the real
  `builder.tools.session.save_session`) and calls it after **scaffold**, after
  **materialize**, and after **each `build_and_validate`** in the fix loop — the
  incremental, change-detected writes drive the dashboard's mtime-watch refresh so
  a running `--dashboard` reflects the crate converging round by round.
  `run_interactive_build` then does a **final `save_session(state,
  always_write=True)`** after guidance + export (on **both** the interactive and the
  headless path), which bypasses change-detection to guarantee a populated overview
  + a resumable session. Persisting CrateState writes only `crate_state.json`; it
  **never touches the built `@graph`**, so the no-provider determinism guarantee
  (identical graph hash across runs) holds. Both `progress` and `save` are injected
  so the wiring is unit-tested with no disk / no SHACL (`tests/test_agents_build.py`,
  `tests/test_agents_pipeline.py`).

**Output location (CLI, `main.py`).** The on-disk destination is resolved at
dispatch time with this precedence (#233, #315):

1. `--output` / `-o` always wins (sets `state.metadata.output_path`);
2. `--output` omitted **and** `--input` given => **`output/<name>_crate`**, versioned
   `_v2`/`_v3`… on re-run (`_default_output_dir`), where `<name>` is the input folder
   name with a trailing `_extracted` stripped. This keeps builds under a dedicated
   `output/` tree instead of writing an `<input>-ro-crate` sibling (which polluted a
   curated input tree like `input/raw/`). Payload is materialized, so each version is
   self-contained;
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
but the headless build is **still written to disk**. This gate is also why
`--smoke-test` needs its own `SmokeTestHumanInterface` (§8) rather than reusing the
simulator: to exercise the tail an interface must report `is_interactive = True`,
and the simulator reports `False` by design. The pipeline, guidance, and
exporter runners are all injectable so the wiring is unit-tested with no SHACL /
no LLM / no disk / no network (`tests/test_agents_build.py`).

**Headless gap summary (#179, Lane 5; #296).** On the headless path `run_guidance`
is never invoked, so without this the user would never see the build's posture.
After the pipeline build + export, `run_interactive_build` emits a single,
**non-blocking** summary line via the `output` channel — `format_gap_summary`
renders the count of open **MUST** issues plus base/isa/tox conformance.
**It REUSES the validation result the pipeline already computed** (`run_pipeline`
returns `{conformance, issues, …}` from its required-severity fix loop) rather
than calling `assess_gaps` afresh: a fresh `assess_gaps` re-runs the heaviest
`severity="optional"` SHACL + MIT + FAIR + AI-readiness sweep (the #115
validation bottleneck),
which on every headless build is both a real per-build UX regression and a CI
timeout (#296). Because the pipeline validates only at REQUIRED severity, SHOULD/MAY
gaps are not computed on this fast path — the line reports them as *not assessed*
rather than fabricating a count (D5 — read-only reporting of real state). It is
**pure observability**: it never prompts, never runs `run_guidance`, and never
mutates state. Wording is deliberately distinct from `format_guidance_summary`
(no "resolved"/"asked" verbs, since no interactive guidance ran). Tested with no
extra SHACL (`tests/test_agents_build.py::TestHeadlessGapSummary`); the interactive
path is unchanged (it still runs `run_guidance` + `format_guidance_summary`).

**Stage C — the gap engine.** `assess_gaps(state: CrateState) -> GapReport`
(`builder/tools/gap_analysis.py`) unifies the four assessors into ONE
prioritized gap list the Guidance stage consumes. It is a **pure, deterministic,
idempotent library function** (no LLM, no network, never mutates `state`) — and a
**library function only, NOT a four-place LLM tool**: the spine/guidance *code*
imports and calls it. It calls (does not re-implement) the four assessors, all
against the SAME assembled document the SHACL sweep validated — a second
assembly is how two axes come to disagree about one crate (#377):

- `build_and_validate(state, severity="optional", profile="all")` — one widest
  sweep yields REQUIRED + RECOMMENDED + OPTIONAL SHACL issues, each already
  routed to `{entity_id, property, message, fix, severity, profile}`.
- `assess_mit_coverage`'s underlying YAML logic — every unfilled MIT parameter is
  a domain-enrichment gap. Both go through **one** matcher,
  `mit_assessment.slot_matcher` (#377): the `crate_slot` vocabulary describes the
  **assembled crate**, not `CrateState`, so a state-field scan cannot see a
  `LabProcess*` subtype (they are `LabProcess` + an `additionalType`, absent from
  the `EntityType` literal), the `char` characteristic traversal, or a field the
  build *promotes* — a MolecularEntity's `cas` becomes the node's `identifier`,
  a CellLineSample's `accession` likewise. A second copy in the gap engine would
  ask for identifiers the crate already carries. The document assembled for the
  SHACL sweep is threaded into the MIT pass, so a `GapReport` costs **one**
  assembly, not two. A value the build *synthesized* in
  the user's absence (the placeholder root name/description, the "licence not
  stated" entity) never counts as filled — crediting it would stop the loop asking
  for the real one; the values are imported from the build's own constants rather
  than duplicated. The licence discount is keyed on `LICENCE_NOT_STATED_ID`, and
  `_nonempty` unwraps a single-key `{"@id": …}` before checking, so a placeholder
  is not made real by being modelled as an entity rather than a string (#540).
- `assess_fair_maturity` — every *failing* indicator is a gap.
- `assess_air_readiness` — every criterion **assessed and not met** is a gap; a
  criterion the tool cannot assess never becomes one. Each criterion declares a
  remedy in `air/criteria.yaml`, and `_is_committable` has the final veto: a remedy
  naming an entity type with no instance in state is forced `report-only` rather than
  spending a human turn on a value `_apply_value` would discard. Never `MUST` — that
  tier is the SHACL build gate, and no profile requires AI-readiness.

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
| AI-readiness criterion failed, remedy committable | **SHOULD** |
| AI-readiness criterion failed, remedy `report-only` | **MAY** |
| AI-readiness criterion **not assessed** | *no gap* |

An AI-readiness gap is **never MUST and never `auto_fixable`**: MUST is the SHACL
build gate and no RO-Crate profile requires AI-readiness, and `auto_fixable` means
precisely "`fix_required_issues` can clear it", which no repair rule does for a
Bridge2AI criterion.

Each `Gap` carries `{tier, source, entity_id, entity_type, property, message,
suggestion, fix_hint, auto_fixable}`: `suggestion` is the expected propertyID
IRI / ontology term / parameter description from the profile or MIT YAML;
`fix_hint` is a deterministic tool name (`"fix_required_issues"`), `"draft"`,
`"ask-user"`, or **`"report-only"`** (#230). A `report-only` gap is one the
guidance loop can *not* commit — it stays in the report for context, but the loop
never spends a human turn on it. `_is_committable` decides this and **must mirror
`guidance._apply_value`'s success conditions** (#375); a test walks every
actionable gap and asserts `_apply_value`'s preconditions hold, so the two cannot
drift. A gap is committable when it names a field the loop can write: a
person/citation field (their own composite routes), an entity-scoped gap whose
`entity_id` resolves to a state entity (or the root `./` with a
`_CRATE_SETTABLE_FIELDS` field), a **typed** gap (`entity_id is None` +
`entity_type`) whose field is a crate slot **and** whose type has exactly one
instance, or a crate-level gap whose field is a crate slot. Never committable: a
gap naming **no field** (a node shape), and an **identifier-bearing field** — D5
refuses to take an identifier from prose, so asking could only discard the answer
(resolving it is a *lookup*, #338/#372). A **reference-only** field stays
committable on purpose: prose is refused, but naming an entity already in the
crate is exactly the useful answer when a repair rule declined on ambiguous
candidates. **Every FAIR gap is `report-only`** (its property is an indicator id
like `RDA-F1-02M`, not a field), as are **crate-level MIT gaps whose field is not
a crate slot**. Actionability is gated on the **field**, never on the mere
presence of an `entity_id` — MIT gaps deliberately keep `entity_id is None`, and
populating it would flip ~167 report-only pseudo-field slots
(`MolecularEntity:char`, `LabProcessExposure:param`, …) into ask-user turns that
would write literal `"char"` / `"param"` keys. `GapReport` adds
`{gaps, conformance, mit_overall, fair_summary, air_summary, counts}`, with
`gaps` sorted
**all MUST, then SHOULD, then MAY**, then **committable before `report-only`**
within a tier, stable secondary by `(source, entity_id, property)` — so the loop
always reaches the gaps it can act on first.

**`auto_fixable` is the load-bearing field** — it is `True` iff
`fix_required_issues` can clear the gap deterministically from state alone. The
engine decides this by re-using the repair loop's **own** rule predicates
(`builder/tools/repair.py` `_RULES`, `_resolve_state_entity`,
`_unique_unwired_file`, `_unique_unwired_input`) read-only, so the gap engine and
the repair loop can never drift on what "deterministically fixable" means: the two
symmetric rules make a process-edge gap auto-fixable **iff its target is the single
unambiguous candidate in state** — `missing_process_output` (an
`EndpointReadout`/`DataAnalysis` missing its `result`/`output`) iff exactly one
un-wired `File` exists, and `missing_process_input` (a `DataAnalysis` missing its
`schema:object`) iff exactly one free-floating `Sample`/`File` exists. Two-or-more
(ambiguous) or zero (needs new content, D5) candidates, and every non-SHACL or
non-REQUIRED gap, are `auto_fixable=False` → `"ask-user"`/`"draft"`. The
Auto-resolve stage runs `fix_required_issues`; what remains is exactly the
`auto_fixable=False` set the Guidance stage works through.

---

*This document is a living design artifact. Update as architectural decisions evolve.*
