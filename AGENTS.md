# ISA-Tox RO-Crate Builder

An LLM agent that turns a researcher's *in vitro* toxicology deposit into an RO-Crate that
conforms to RO-Crate 1.2, the ISA profile and the ISA-Tox profile, then scores it for MIT
coverage, FAIR maturity and AI-readiness.

This file holds what every session needs: how to work here, the rules that must hold, and
where everything else is written down. The design itself lives in
[`docs/design/`](docs/design/) and the reasoning behind it in [`docs/adr/`](docs/adr/).
Read the part you are about to change.

## Working here

```bash
uv sync --dev --extra langchain   # what CI installs; without the extra the LLM tests fail
uvx ruff check
uv run ty check                   # gates CI: the tree has zero diagnostics
uv run pytest -n auto             # the whole suite; CI shards it 16 ways, nothing is skipped
```

- **Tests come first, and `tests/` is read-only to agents.** A `PreToolUse` hook
  (`.claude/hooks/guard-test-edits.py`) refuses edits under `tests/` unless
  `CLAUDE_ALLOW_TEST_EDITS=1` is set, so changing a test is a deliberate act. Never weaken a
  test to get green. [CONTRIBUTING.md](CONTRIBUTING.md) says what a test must be able to catch.
- **CI never touches the network.** The live lookup checks run only with
  `VITRO_LIVE_LOOKUPS=1` (`tests/test_lookups_live.py`); keep them out of the workflow.
- **Behaviour changes update README.md; design changes update `docs/design/`; a new decision
  gets a record in `docs/adr/`.** This file changes only when a rule or the map does.

## The system in one page

There is **one build: the ReAct loop** ([D15](docs/adr/D15-the-react-loop-is-the-build.md)).
`main.py --interactive` (or `--unattended`, or the eval harness) calls `run_build`
(`builder/agents/build.py`), which hands an initialized `AgentEngine` to a LangGraph
`StateGraph` whose model chooses the tool calls.

1. **Initialization**, before the loop: scan the approved input folder, classify every file
   (`metadata` / `protocol` / `raw_data_file` / `processed_data_file`), rank the readable
   documents, read the licence, DOI, title, accession, funding, ORCIDs and ROR ids the deposit
   itself declares, and read its studies and assays from the folder tree.
   → [initialization](docs/design/initialization.md)
2. **The loop** drafts entities through the [toolbox](docs/design/toolbox.md) into
   `CrateState`, the single serializable source of truth ([CrateState](docs/design/crate-state.md)).
   Identifiers come only from lookups ([lookups](docs/design/lookups.md)).
3. **`build_and_validate`** assembles the crate in memory and runs the SHACL passes for every
   layer the crate claims (RO-Crate 1.2 → ISA → ISA-Tox). Findings route back to an entity and
   field. → [building](docs/design/building.md), [validation](docs/design/validation.md)
4. **`export_crate`** writes the crate directory with its payload, its preview and an embedded
   maturity report. It runs automatically after every build that passes base conformance.
   → [assessments](docs/design/assessments.md), [report](docs/design/report.md)

The loop itself (nodes, routing, prompt layout, history trimming, model tiering, the
entrypoint) is in [the ReAct loop](docs/design/react-loop.md); sessions, resume and the
`HumanInterface` are in [sessions and HITL](docs/design/sessions-and-hitl.md). The overall
architecture is in [architecture](docs/design/architecture.md).

## Rules that must hold

Each rule names what enforces it. A rule with nothing enforcing it is the first one to drift;
when you add a rule, add its test.

### Identifiers and claims — never fabricate ([D5](docs/adr/D5-verify-dont-trust.md))

- An identifier (ORCID, ROR, CAS, PubChem CID, DTXSID, Cellosaurus accession, DOI, ontology
  IRI) comes from a lookup that returned it, or it is not written. `verify_identifier` clears a
  value its source does not confirm. — `tests/test_tools_verification.py`
- What the deposit does not contain is reported, never manufactured: a step whose output was
  not deposited keeps no `result` and the profile's Violation stands
  ([wiring contracts](docs/design/building.md#build-path-wiring-contracts-buildertools_crate_mappingpy)). —
  `tests/test_process_chain_deposited_outputs.py`
- A licence the depositor did not state is the `#licence-not-stated` entity, never a guessed
  or restrictive licence; a declared one is read from the deposit and outranks a drafted one
  ([D13](docs/adr/D13-isa-haspart-hierarchy--investigation-is-the-root.md)). — `tests/test_deposit_licence.py`
- Synthetic answers (`--smoke-test`) are announced on the console and never written into the
  crate. Unanswered questions stay unanswered (`--unattended`). — `tests/test_smoke_test_mode.py`,
  `tests/test_unattended_build.py`

### Filesystem access fails closed ([D9](docs/adr/D9-approved-scan-roots-security-guard-rail.md))

- Nothing is scanned or read outside an approved root; with no approved root nothing is. Roots
  come only from the user's input path or a real approval, never from the agent's own call, and
  `/`, the home directory and OS trees are refused even when listed. Symlinks are resolved first.
  — `tests/test_scanner.py`, `tests/test_path_traversal.py`
- No payload byte is written outside the crate, and no source outside the input tree is packaged.
  — `tests/test_path_traversal.py`, `tests/test_crate_mapping_references.py`
- Only a real person approves a scan root: the simulator and the smoke-test interface refuse
  outright, and no headless or eval interface reports `is_interactive`. —
  `tests/test_smoke_test_mode.py`, `eval/tests/test_headless_engine.py`

### Validation is offline and honest

- Validation never reaches the network: bundled RO-Crate contexts, every other dereference
  refused; a transport failure raises rather than becoming a REQUIRED finding. —
  `tests/test_offline_validation.py`, `tests/test_validation_ssrf.py`
- `profile="all"` means the layers the crate claims (`CrateState.layers`, [D18](docs/adr/D18-a-crate-claims-the-profile-its-study-can-meet.md)), and every surface
  that shows conformance composes it cumulatively over the layer chain
  (`conformance_by_layer`): no layer passes a tier a layer beneath it fails. —
  `tests/test_profile_isa.py`, `tests/test_maturity_report.py`
- A rule the profiles cannot evaluate is checked on our side and recorded as checked: the
  payload (`verify_payload`) and ISA reachability (`verify_isa_reachability`). An unassessed tier
  reads *not assessed*, never clean. — `tests/test_payload_verification.py`, `tests/test_isa_reachability.py`
- The ISA-Tox shapes mint no classes or terms, and a shape's `sh:pattern` is derived from the
  vendored Bioregistry record it cites. — `tests/test_profile_crate.py`,
  `tests/test_identifier_namespace_shapes.py`, `tests/test_shape_constraint_coverage.py`

### Assessments score the crate a reader receives ([assessments](docs/design/assessments.md))

- A check reads the assembled `@graph`, not `CrateState`, and must be able to fail: a crate of
  empty entities scores `False` on every indicator about the data. — `tests/test_fair_metrics_can_fail.py`
- Indicator definitions are generated from the vendored published instruments, never written
  by hand. — `tests/test_fair_indicators_source.py`, `tests/test_dsm_indicators_source.py`,
  `tests/test_air_criteria_source.py`

### The toolbox has one registry

- A new LLM tool is registered in four places: `TOOL_REGISTRY`, `TOOL_SPECS`, the system
  prompt's "Your Tools" list and [the toolbox doc](docs/design/toolbox.md). A tool advertised
  but not runnable, or runnable but not advertised, fails fast. — `tests/test_tools_spec.py`,
  `tests/test_agents_doc_toolbox.py`

### The loop stays cheap and well-formed ([D10](docs/adr/D10-state-brief-as-the-trailing-message-not-the-system-prompt.md), [D12](docs/adr/D12-bounded-message-history-trim--prune-before-each-model-call.md))

- `SYSTEM_PROMPT` is byte-stable; per-turn state goes in a trailing brief, so the provider's
  prompt cache holds. History trimming never orphans a tool message and never returns an empty
  history; an unanswered tool call is dropped, never answered with an invented result. —
  `tests/test_agent_loop.py`, `tests/test_interrupted_tool_call.py`
- The export is idempotent on a **content** fingerprint, never an entity count. —
  `tests/test_validation_debounce.py`, `tests/test_agent_loop.py`
- A console notice at INFO or above is written for the person running the build: plain
  English, no issue numbers or module tags. — `tests/test_log_lines_are_user_copy.py`

### The report travels inside the crate ([report](docs/design/report.md))

- It loads nothing: vendored bundles are inlined and verified against
  `builder/writers/vendor/manifest.json` at render time, and no content from the crate becomes
  a link or anchor. — `tests/test_entity_explorer.py`, `tests/test_html_xss.py`
- Every term in the crate's `@context` resolves to its vocabulary's canonical IRI, from a
  declared allow-list of namespaces. — `tests/test_context_namespaces.py`

### Generated files are never edited by hand

Change the generator or its source, then regenerate.

| File | Generator |
|---|---|
| `fair/indicators.yaml` | `scripts/gen_fair_indicators.py` |
| `fair/dsm_indicators.yaml` | `scripts/gen_dsm_indicators.py` |
| `fair/dsm_nanopub_refs.yaml` | `scripts/fetch_dsm_nanopub_refs.py` (network, by hand) |
| `air/criteria.yaml` | `scripts/gen_air_criteria.py` |
| `profiles/vocabulary/identifier_namespaces.json` | `scripts/refresh_identifier_namespaces.py` |
| `profiles/vocabulary/type_supertypes.json` | `scripts/refresh_type_vocabulary.py` (network, by hand) |
| `profiles/shapes/tox/14_identifier_namespaces.ttl` | `scripts/gen_identifier_shapes.py` |
| The published profile crate (not committed) | `scripts/gen_profile_crate.py` |

## Where things are written down

| You are changing… | Read |
|---|---|
| The overall shape, entity model, layer order | [architecture](docs/design/architecture.md) |
| `CrateState` and what persists | [CrateState](docs/design/crate-state.md) |
| Scanning, file classification, document ranking, deposit structure | [initialization](docs/design/initialization.md) |
| A tool's contract | [toolbox](docs/design/toolbox.md) |
| Drafting, crate assembly, `_crate_mapping`, output layout | [building](docs/design/building.md) |
| SHACL passes, verification, identifier namespaces, the profile crate | [validation](docs/design/validation.md) |
| MIT / FAIR / DSM / AIR scoring | [assessments](docs/design/assessments.md) |
| The maturity report, entity explorer, assay lanes | [report](docs/design/report.md) |
| Registry clients, caching, rate limits | [lookups](docs/design/lookups.md) |
| The ReAct graph, prompt, history, models, `run_build` | [the ReAct loop](docs/design/react-loop.md) |
| Sessions, resume, `HumanInterface`, smoke-test and unattended modes | [sessions and HITL](docs/design/sessions-and-hitl.md) |
| Why something is the way it is | [decision records](docs/adr/README.md) (`D1`–`D18`; code cites them by id) |

## Project structure

```
vitro-crate/
├── AGENTS.md                    This file: rules and the map
├── CONTEXT.md CONTRIBUTING.md README.md
├── .github/workflows/ci.yml     CI (ruff, ty, pytest on push/PR)
├── .claude/                     Agent settings; the read-only-tests hook
├── pyproject.toml
├── main.py                      CLI entry point: build dispatch, output-path resolution
├── profiles/                    Domain profiles + validation
│   ├── context.py               JSON-LD context builder
│   ├── validator.py             3-pass SHACL validation (base / isa / tox)
│   ├── identifier_namespaces.py Identifier patterns derived from the Bioregistry records
│   ├── ontology_iris.py         Canonical ontology term IRIs
│   ├── models/isa.py, tox.py    Entity classes
│   ├── shapes/tox/              ISA-Tox SHACL shapes (base + ISA ship with rocrate_validator)
│   ├── vocabulary/              Vendored type supertypes + Bioregistry identifier records
│   ├── contexts/                Vendored RO-Crate 1.1 / 1.2 JSON-LD contexts
│   └── docs/                    The profile specification
├── lookups/                     External API clients
│   ├── cellosaurus.py, pubchem.py, comptox.py, aopwiki.py, bao.py
│   └── orcid.py, ror.py, crossref.py, cordis.py, iuclid.py, _http.py
├── mit/invitro_tox.yaml         Minimal Information Table
├── fair/                        FAIR indicators (RDA FDMM + FAIRplus DSM, vendored)
├── air/                         Bridge2AI AI-readiness criteria (vendored)
├── input/                       Example inputs
├── builder/                     Core builder system
│   ├── state.py                 CrateState dataclass
│   ├── engine.py                AgentEngine: run_tool, gating, approved scan roots
│   ├── config.py, pricing.py    Provider/model config; token pricing
│   ├── tools/                   Tool implementations (the shared toolbox)
│   │   ├── scanner.py, drafters.py, composites.py, management.py
│   │   ├── lookups.py, verification.py, builder.py, validation.py
│   │   ├── repair.py, mit_assessment.py, fair_assessment.py
│   │   ├── air_assessment.py, assessment_graph.py  Bridge2AI axis + shared verdicts
│   │   ├── data_content.py, file_readers.py, hitl.py, session.py
│   │   ├── document_discovery.py, deposit_structure.py, rehome.py, remediation.py
│   │   ├── field_kinds.py        Shared field-kind vocabulary (reference / identifier fields)
│   │   ├── registry.py, _crate_mapping.py, dashboard.py, provenance.py
│   │   ├── profiler.py, _resolve_cache.py
│   │   └── directory.py, existing_crate.py, metadata_files.py
│   ├── writers/                 Output writers
│   │   ├── rocrate_writer.py
│   │   ├── provenance_dag.py    Entity-graph model + inventories
│   │   ├── maturity_report.py   Maturity / FAIR HTML report
│   │   ├── entity_explorer.py   Interactive React Flow entity graph
│   │   ├── entity_explorer.js   …its browser half (no build step)
│   │   ├── entity_explorer_layout.js  …where its nodes go
│   │   ├── explorer_inspector.js  …the panel and vocabulary both viewers share
│   │   ├── assay_lane.py        Assay-lane section
│   │   ├── assay_lane_view.js   …where one assay's nodes go
│   │   ├── assay_lane_app.js    …its browser half: SVG, chips, folds, inspector
│   │   ├── payload_codec.js     …the wire format its data island carries
│   │   └── vendor/              Pinned UMD builds inlined into the report
│   └── agents/                  Orchestration + LLM config
│       ├── build.py             run_build: reads the deposit's structure, then runs the ReAct loop
│       ├── structure.py         derive_deposit_structure: one drafter-tier call labels the folder tree
│       ├── llm.py               Shared model construction + usage mining
│       ├── ui.py                Interactive UI: status bar, reply, banners, boxed prompt
│       ├── progress_spinner.py  Live progress spinner
│       └── react/               The ReAct StateGraph loop (the build)
│           ├── agent_loop.py      The loop
│           ├── system_prompt.py   Its system prompt
│           └── tools_spec.py      TOOL_SPECS + the registry-parity contract
├── eval/                        Eval harness: runs the build over a corpus and scores it
├── scripts/                     Generators, vocabulary refreshers, profile validator
├── docs/
│   ├── design/                  How each subsystem works and what it must guarantee
│   ├── adr/                     Why: one record per design decision (D1–D18)
│   └── superpowers/specs/       Specs for work in progress; distil into design/adr on merge
├── sessions/                    Persisted sessions
├── output/                      Built crates (versioned)
└── tests/                       Test suite (read-only to agents; see above)
```

## Keeping the docs true

- **State what must hold, in the present tense.** Implementation detail belongs in docstrings,
  which the design docs point to. No changelogs, migration stories or dated audits: history
  is git's, and the reasoning is an ADR's.
- **Prefer generated over written.** The toolbox doc is held to the registry by a test; where
  a fact can be derived from code or data, derive it.
- **Cite decisions by id** (`D5`), not by section number: ids are stable, positions are not.
