# Sessions and human-in-the-loop

> Session persistence and resume, and how questions reach a person (or deliberately do not).

## Session Persistence & Resume

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
The ReAct loop saves through the agent-callable `save_session` tool and an
end-of-loop autosave. There is no save on a HITL answer, and no context-limit
trigger exists.

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
3. The loop is told `resumed=True` explicitly — the caller's fact, never inferred
   from how populated the state looks (#410) — and prints the resume summary.
4. The resume panel's next steps are derived from the crate's actual state:
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

## Human-in-the-Loop (HITL)

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
   the console, **Edit** is the free-text row every choice prompt ends with ([the toolbox](toolbox.md))
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
field_type) → InputResponse`. The default `SimulatedHumanInterface` answers
nothing for headless runs; a frontend (Streamlit,
FastAPI, CLI) supplies its own adapter without monkeypatching the tool
functions. The module-level `present_to_human` / `request_input` functions
remain as thin wrappers over a shared default simulator.

Three implementations ship in `builder/tools/hitl.py`:

| Interface | `is_interactive` | Wired by | Answers with |
| --- | --- | --- | --- |
| `SimulatedHumanInterface` | `False` | the headless default (`--unattended`, eval, tests) | nothing: every question is left unanswered; **deny** scan roots |
| `ConsoleHumanInterface` | `True` | `main.py --interactive` | a real person, on stdin |
| `SmokeTestHumanInterface` | `True` | `main.py --smoke-test` | itself — see below |

**`SmokeTestHumanInterface` (`--smoke-test`) is a TEST harness, not a frontend.**
It exists so the interactive path can be driven end to end with nobody at the
keyboard: every `present` confirms the **pre-selected**
choice (via the shared `_default_choice_index`, never a re-implemented "first
option") and every `request_input` returns the literal `SMOKE_TEST_ANSWER`,
`"yes, continue"`. It reports `is_interactive = True` *because that is the point* —
the loop's questions are gated on that one signal ([the build entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy)), so the simulator cannot
exercise them. Four rules bind the harness; the mechanism lives in the docstrings of
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
- **The conversation is bounded.** The ReAct loop's conversational "what next?"
  read goes through the `HumanInterface` (`CONVERSATION_FIELD_TYPE`) when the
  answers are synthetic, so the harness answers it for a bounded number of turns
  (`conversation_turns`) or, with `--smoke-test MINUTES`, until the wall-clock
  budget is spent, then ends the session the way Ctrl+D does — between turns,
  never mid-question — and the crate it has is exported.

`--smoke-test` implies `--interactive` (normalised once, in `parse_args`).

**`--unattended` builds with nobody at the keyboard.** It runs the same loop
behind the headless `SimulatedHumanInterface`, with `interactive=False` and the
`UNATTENDED_INSTRUCTION` (`builder/agents/build.py`), which tells the agent that
nobody answers. A question it asks anyway comes back unanswered: nothing is
approved, chosen or typed on a person's behalf (D5), so what the deposit and the
lookups do not state stays empty and remains a finding in the maturity report.
The engine keeps each such question on `CrateState.unanswered_questions`, saved
with the session; the brief of a later session lists them, and a person's answer
closes each one (`record_user_answer`). A person who skips a question at the
keyboard has answered it, so nothing is kept then. The run ends with the export
(the finish backstop), says how many questions went unanswered and how to answer
them (`--resume <id> --interactive`), and exits 1 when no crate was written. It
needs `--input` or `--resume`, and is not combined with `--interactive` or
`--smoke-test`.
