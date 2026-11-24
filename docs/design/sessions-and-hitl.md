# Sessions and human-in-the-loop

> Session persistence and resume, and how questions reach a person (or deliberately do not).

## Session Persistence & Resume

```
sessions/<session_id>/
├── crate_state.json       # Serialized CrateState
├── working_crate/         # Crate directory (may be partial)
├── profile.ndjson         # Profiling events (tool and node timing)
└── session.log            # Agent reasoning trace
```

**Saving.** Only the agent-callable `save_session` tool and an end-of-loop autosave save;
a HITL answer does not. Saves are atomic (temp file in the same directory, `fsync`,
`os.replace()`), skipped when the SHA-256 of the serialised state is unchanged, and a
failure is logged and surfaced to the loop, never swallowed (#53).

**Resume.**
1. `load_session(<session_id>)` replaces `engine.state` from `crate_state.json` and
   re-attaches the profiler (`engine.ensure_profiler()`), since resume bypasses
   `initialize()`.
2. Unless `--output` is given, the restored `output_path` is versioned, so a resumed
   export never overwrites the previous run's crate.
3. The loop is told `resumed=True` explicitly, never inferred from state (#410).
4. The resume panel's next steps come from the crate's state: blocking issues, then the
   next unmet step of BASE → ISA → TOX, then export.

`checkpoint.completed_checkpoints` drives the session phase (`session._determine_phase`) and the
dashboard's (`dashboard._determine_phase_from_state`).
`checkpoint.next_actions` has no production writer; do not build resume guidance on it.

**Not reconciled on resume:** nothing compares `working_crate/` against the restored
state, and the resume panel reads the restored `state.validation`, so a crate changed
since the last save can be announced as passing on stale flags.

**Recovery:** a missing or corrupt `crate_state.json` ends the run with
`Session not found` and a non-zero exit
(`tests/test_main_graph.py::test_graph_missing_session_errors`); nothing is rebuilt from
`ro-crate-metadata.json`.

## Human-in-the-Loop (HITL)

The agent asks at checkpoints such as file-scan review, entity-draft review, identifier
verification, a validation gate, MIT score review, completion review and when stuck. The
user can **Approve**, **Edit** (on the console, the free-text row every choice prompt ends
with, [the toolbox](toolbox.md)), **Reject with explanation** or **Skip**; feedback is
applied to the entity in place. A standing answer to a validation escalation is recorded
on `state.validation_preferences` and asked at most once per session. `_provenance`
records `created_by` and `lookups_used` only — `reviewed_by` has no writer, so there is no
per-entity audit trail of human review.

**Interface adapter.** Requests go through the `HumanInterface` protocol
(`builder/tools/hitl.py`), injected via `AgentEngine(human_interface=...)`:
`present(context, options) → HumanResponse` and
`request_input(prompt, field_type) → InputResponse`. A frontend supplies its own adapter
without monkeypatching; the module-level `present_to_human` / `request_input` wrap a
shared default simulator.

| Interface | `is_interactive` | Wired by | Answers with |
| --- | --- | --- | --- |
| `SimulatedHumanInterface` | `False` | the headless default (`--unattended`, eval, tests) | nothing: every question is left unanswered; **deny** scan roots |
| `ConsoleHumanInterface` | `True` | `main.py --interactive` | a real person, on stdin |
| `SmokeTestHumanInterface` | `True` | `main.py --smoke-test` | itself — see below |

**`SmokeTestHumanInterface` is a test harness, not a frontend.** It drives the
interactive path with nobody at the keyboard: `present` confirms the pre-selected choice
(the shared `_default_choice_index`, never a re-implemented "first option") and
`request_input` returns `SMOKE_TEST_ANSWER`. It reports `is_interactive = True` because the
loop's questions are gated on that signal
([the build entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy)).
Rules, pinned by `tests/test_smoke_test_mode.py` (mechanism in the `hitl.py` docstrings):

- **Scan roots fail closed (#197).** A `purpose="scan_root"` escalation is refused by an
  explicit early return in `present`, as `SimulatedHumanInterface` refuses it. Fail-closed
  is written per interface, never inherited from the pre-selection rule:
  `_default_choice_index` falls back to the LAST option when none reads as a refusal, which
  would approve `["Show me the folder first", "Yes, allow this folder"]`.
- **The run says the answers are synthesised.** `main.py` prints `SYNTHETIC_ANSWER_NOTICE`
  at the start and beside the exported crate path, gated on the fail-closed
  `answers_are_synthetic(human)`. Nothing is written into the crate: such a marker would be
  fabricated metadata (D5).
- **The conversation is bounded.** With synthetic answers the "what next?" read goes
  through the `HumanInterface` (`CONVERSATION_FIELD_TYPE`), answered for
  `conversation_turns` turns or, with `--smoke-test MINUTES`, until the budget is spent;
  the session then ends between turns as Ctrl+D does, and the crate is exported.

`--smoke-test` implies `--interactive` (normalised in `parse_args`).

**`--unattended` builds with nobody at the keyboard.** The same loop runs behind
`SimulatedHumanInterface` with `interactive=False` and `UNATTENDED_INSTRUCTION`
(`builder/agents/build.py`). A question asked anyway comes back unanswered — nothing is
approved, chosen or typed on a person's behalf (D5) — and is kept on
`CrateState.unanswered_questions`, saved with the session, listed in a later session's
brief and closed by a person's answer (`record_user_answer`); a skip at the keyboard is an
answer and keeps nothing. The run ends with the finish-backstop export, reports how many
questions went unanswered and how to answer them (`--resume <id> --interactive`), and exits
1 when no crate was written. It needs `--input` or `--resume` and excludes `--interactive`
and `--smoke-test`.
