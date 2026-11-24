# The ReAct loop

> The LangGraph StateGraph that is the build: nodes, routing, prompt injection, checkpointing, model tiering and the entrypoint.

## Agent Graph (LangGraph / StateGraph)

The build `main.py --interactive` runs (D15) is an explicitly constructed `StateGraph`,
built by `_build_agent_graph()` in `builder/agents/react/agent_loop.py`; its entrypoint is
[`run_build`](#the-build-entrypoint-builderagentsbuildpy).

- **Topology.** Four nodes: `__start__` → `model` (assembles messages, binds tools,
  invokes the LLM) → `tools` (a `ToolNode`) → back to `model`, and `__end__`. The state
  is `AgentState`, a single `messages` field with the `add_messages` reducer.
- **Routing.** One conditional router, `should_continue`: to `tools` when the last
  message has `tool_calls`, else to `__end__`.
- **Prompt assembly.** `call_model` delegates to `_assemble_model_messages`: the
  byte-stable `SYSTEM_PROMPT` first, the bounded history (`_trim_history`, D12), the
  per-turn state brief last (D10). Both are rebuilt on every invocation.
- **Tool binding.** Tools are bound inside `call_model`, per turn (#156):
  `_tools_for_state` narrows the *advertised* set while the `ToolNode` keeps the full set —
  advertise narrow, execute wide, so a narrow advertisement never blocks execution.
  Binding is not optional: without `bind_tools` the model never emits a tool call,
  `should_continue` always ends, and the agent becomes a chatbot that narrates work it
  never does (#71).
- **Checkpointing.** `graph.compile(checkpointer=MemorySaver())` snapshots the agent
  state after each node, keyed by the `thread_id` in `RunnableConfig` (the session id), so
  later `invoke()`s on that thread continue the conversation. It affects neither routing
  nor topology.

## Model Tiering (Issue #96)

A cheap model can do bounded extraction while the primary model drives orchestration,
with no change to the graph. Construction is centralised in `_build_chat_model`
(`builder/agents/llm.py`), shared by the ReAct model node and the drafter call that
labels the deposit's folder tree
([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)),
so a change made "for ReAct" also lands there. An explicit `model` wins; otherwise
`role` selects the tier:

- `role="orchestrator"` (default) → `VITRO_OPENAI_MODEL` / `VITRO_ANTHROPIC_MODEL`.
- `role="drafter"` → `VITRO_OPENAI_DRAFTER_MODEL` / `VITRO_ANTHROPIC_DRAFTER_MODEL`
  when configured, else the primary model — a strict no-op for a single-model setup.

Precedence is env var → `[openai]`/`[anthropic] drafter_model` config key (folded in by
`config.merge_with_env`). `config.get_drafter_model()` only *reports* the drafter on the
generator record; it is not on the construction path. The one production drafter-tier
call is `builder/agents/structure.py`; the `draft_*` tools are pure state mutations
([entity drafters](building.md#entity-drafters-buildertoolsdrafterspy)) and spend
nothing on it.

**OpenAI reasoning models (`gpt-5.x`, `o`-series)** reject a non-default `temperature`
and cannot bind tools on `/v1/chat/completions`, so they go through the Responses API
(`use_responses_api=True`) with no temperature. Detection is by name
(`_is_openai_reasoning_model`); `VITRO_OPENAI_USE_RESPONSES_API` forces it for an
unrecognised deployment name. `VITRO_OPENAI_REASONING_EFFORT` forwards
`reasoning_effort`; `none` opts back to the standard path.

**`VITRO_TEMPERATURE`** (default `0`) applies to both providers, resolved once in
`_resolve_temperature()` (#402). Blank reads as unset; a non-numeric value raises, because
a silently ignored control is the defect. A Responses-API model still receives no
temperature at all — the API 400s on any explicit value.

**Decision gate (future work):** upgrade the *orchestrator* only if `profile.ndjson`
(iterations per task, recursion-limit hits, REQUIRED-issue fix success) shows
reasoning/recovery-shaped failures, not malformed output, which schemas and SHACL already
catch. Guardrails are a one-time cost; a stronger model is recurring per token.

## The Build Entrypoint (`builder/agents/build.py`)

`run_build(engine, *, provider, model, base_url, output, resumed, initial_prompt,
verbose, interactive)` is what `main.py --interactive` and the eval call (D15). It reads
the deposit's structure once, sets `generator.architecture = ARCHITECTURE` (`"react"`,
#772) before the loop starts — the loop exports from inside itself — and hands the
engine to `run_interactive_agent`, which returns `{"stop_reason": ...}`.
`interactive=False` says nobody can answer (the eval): the seeded turn and its autonomous
continuation are the whole session, ended as Ctrl+D ends it.

**Session provenance is passed, never inferred (#410).** `resumed` comes from the CLI
(`--resume`). `engine.initialize(--input)` populates state before the loop, so a
populated `CrateState` is not evidence of a resume. Everything that branches on it (the
banner title, the greeting and its offline fallback) takes the flag, and
`ui.print_resume_summary(engine, *, resumed)` makes it a required keyword. Whether the
banner appears at all is still decided by content.

**The loop needs a kickoff (#412).** The greeting invoke sits outside the
autonomous-continuation loop, which is keyed on a user message, so without one the loop
greets and blocks on stdin. `initial_prompt` seeds that first turn (blank = absent). The
CLI supplies `build.DEFAULT_INSTRUCTION` whenever an input folder is given, unless
`--prompt/-P` overrides it (#846); without a folder the session waits for a message.

**The on-disk export (#233, #287).** `export_crate` is the only crate writer, and the
loop never depends on the model choosing it. `_auto_export_after_build` fires after a
`build_and_validate` that passes **base** over a non-empty crate, exports to
`state.metadata.output_path` (fallback: the session `working_crate/`), stamps
`_EXPORTED_FLAG` and prints the path; `_finish_backstop` (#251) builds and exports on the
quit/EOF path. Failures are logged and surfaced, never swallowed. Both are idempotent on
`CrateState.export_fingerprint()`, a **content** hash of entities, crate metadata and the
scanned-file inventory — never an entity count (#380), which no field-level tool
(`set_fields`, `set_crate_metadata`, `fix_required_issues`, `link`) changes. It is wider
than `validation_fingerprint()`, which must stay narrow or the #155 debounce stops
hitting. `_finish_backstop` gates on and stamps the same fingerprint, so it catches a crate
changed after its auto-export and quit and EOF cannot double-export.

**Console notices are user copy (#813).** `main.py` installs `ui.NoticeHandler` at INFO,
so every INFO+ record is shown to the person running the build: plain English, no issue
numbers, hedged plurals (`file(s)`) or module tags, and a count after a label
(`Files attached: 91`). Developer detail goes to DEBUG (`-v`).
`tests/test_log_lines_are_user_copy.py` holds the rule.

**Output location (`main.py`, #233, #315).**

1. `--output` / `-o` always wins (sets `state.metadata.output_path`);
2. otherwise, with `--input`: `output/<name>_crate`, versioned `_v2`/`_v3`… on re-run
   (`_default_output_dir`), `<name>` being the input folder minus a trailing
   `_extracted` — never a sibling of the input tree. Payload is materialised, so each
   version is self-contained;
3. no `--input` (conversation mode): `output_path` stays unset and `export_crate` falls
   back to `working_crate/`.

**The interactive signal.** Interactive vs headless is read only through the fail-closed
`builder.tools.hitl.is_interactive(human)`: a `None` interface, a missing attribute or a
falsy `is_interactive` is non-interactive. `SimulatedHumanInterface` (eval, batch, tests)
is `False`, so a scan-root escalation is denied and the RECOMMENDED/OPTIONAL validation
escalation is never offered (D9). That is why `--smoke-test` needs its own
`SmokeTestHumanInterface`
([human-in-the-loop](sessions-and-hitl.md#human-in-the-loop-hitl)).
