# The ReAct loop

> The LangGraph StateGraph that is the build: nodes, routing, prompt injection, checkpointing, model tiering and the entrypoint.

## Agent Graph (LangGraph / StateGraph)

> This section describes the ReAct loop, the build `main.py --interactive` runs
> (D15); [the build entrypoint](#the-build-entrypoint-builderagentsbuildpy) describes its entrypoint, `run_build`.

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

## Node Topology

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

## Edge Routing (Tool-Calling Loop)

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

## How `system_prompt` Gets Injected

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

## How `MemorySaver` Integrates

The `MemorySaver` checkpointer is passed to `graph.compile()`. It is a **checkpointing layer** that snapshots the full agent state (`messages` list, etc.) after each node execution. LangGraph uses the `thread_id` from `RunnableConfig` to key these checkpoints. On subsequent `invoke()` calls with the same `thread_id`, the graph resumes from the last checkpoint, providing conversational memory across turns.

The `MemorySaver` does not affect routing or the node topology — it is purely a persistence mechanism for state snapshots.

## Model Tiering (Issue #96)

The weak model the agent runs on (e.g. DeepSeek-flash) collapses on multi-turn
orchestration and error recovery — the build→validate→re-draft loop — but stays
fine at bounded extraction. Model tiering lets a stronger model drive the
orchestration node while a cheap model does the bounded drafting work, without
any change to the graph topology.

Construction is centralised in `_build_chat_model(provider, model, base_url,
max_retries, role, timeout, streaming)` (`builder/agents/llm.py`) — shared by the
ReAct model node and the bounded drafter call that reads the deposit's structure
([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)), so a change made "for ReAct" also lands on that call. An explicit `model`
argument wins over role-based
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

The tier is bound in production by one call: `builder/agents/structure.py` builds
the model that labels the deposit's folder tree with `role="drafter"` ([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)), so
setting `VITRO_OPENAI_DRAFTER_MODEL` changes the model behind that bounded call. The
`draft_*` tools (`builder/tools/drafters.py`) are not on the tier — they are pure
state mutations (see [entity drafters](building.md#entity-drafters-buildertoolsdrafterspy)), so the loop itself spends nothing on
it.

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
Anthropic silently did nothing. Blank/whitespace reads as unset
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

## The Build Entrypoint (`builder/agents/build.py`)

`run_build(engine, *, provider=None, model=None, base_url=None, output=None,
resumed=False, initial_prompt=None, verbose=False, interactive=True)` is what
`main.py --interactive` and the eval harness call (D15). It reads the deposit's
structure once ([deposit structure](initialization.md#deposit-structure-buildertoolsdeposit_structurepy-builderagentsstructurepy)), names the build on the generator record
(`generator.architecture = "react"`, `ARCHITECTURE`, #772) — before the loop starts,
because the loop exports from inside itself — and hands the engine to
`run_interactive_agent` (`builder/agents/react/agent_loop.py`), which returns
`{"stop_reason": ...}`. `interactive=False` is the caller's statement that nobody can
answer (the eval): the seeded turn and its autonomous continuation are then the whole
session, ended the way Ctrl+D ends it.

**Session provenance is passed, never inferred (#410).** `resumed` states whether
the run was loaded from a saved session (`--resume`) or started fresh. Only the CLI
knows: `engine.initialize(--input)` populates `scanned_files` — and the drafters may
populate entities — before the loop begins, so a populated `CrateState` is *not*
evidence of a resume. Anything that branches on resume-ness (the session banner's
title, the ReAct greeting prompt and its offline fallback) takes this flag;
`ui.print_resume_summary(engine, *, resumed)` makes it a **required** keyword so no
call site can quietly re-derive it from state content. Content emptiness still
decides whether the banner appears at all — that is a separate question from where
the content came from.

**The loop needs a kickoff (#412).** The ReAct loop's greeting invoke sits *outside*
the autonomous-continuation loop ([work in layers](architecture.md#agent-priority-heuristic-work-in-layers)), which is keyed on a user message — so the
loop greets and then blocks on stdin having done no work. `initial_prompt` seeds
that first turn in place of the first stdin read; blank is treated as absent, and
control passes to the ordinary autonomous loop immediately afterwards. The CLI
supplies it whenever an input folder is given (`build.DEFAULT_INSTRUCTION`, unless
`--prompt/-P` says otherwise; #846): a folder on the command line already says what
to do. Without a folder the session waits for a first message: it is a conversation.

**The on-disk export (#233, #287).** `export_crate` (`builder/tools/builder.py`) is
the only disk writer, and the loop must never depend on the model *choosing* it.
`_auto_export_after_build` in `builder/agents/react/agent_loop.py` fires after a
`build_and_validate` that passes **base** conformance over a non-empty crate, calls
`export_crate` with no explicit path, stamps `_EXPORTED_FLAG` and surfaces the
absolute crate path; `_finish_backstop` (#251) builds and exports on the quit/EOF
exit path. The destination is resolved by `export_crate` from
`state.metadata.output_path` (the CLI-resolved path, see below) with the session
`working_crate/` fallback. An export failure is never silently swallowed: it is
logged and surfaced, and the exit backstop tries again. The auto-export is
idempotent via `CrateState.export_fingerprint()` — a **content** hash over entities
+ crate metadata + the scanned-file inventory — so it re-exports exactly when the
crate changed and an unchanged repeat build is a no-op. The fingerprint must be
content, **never an entity count** (#380): a count is invariant under every
field-level tool the agent is told to use for the rest of the session
(`set_fields`, `set_crate_metadata`, `fix_required_issues`, `link`), so counting
keeps all of that work off disk — see the `export_fingerprint` docstring.
`export_fingerprint()` is strictly **wider** than `validation_fingerprint()` because
`export_crate` packages scanned files (`include_all_scanned=True`) the validation
path never sees, and `validation_fingerprint()` must stay narrow or the #155
debounce stops hitting. `_finish_backstop` gates on the same fingerprint rather than
on "something exported this session": it is the last chance to catch a crate that
changed after its auto-export, and it stamps the fingerprint too so the two exit
paths (quit and EOF) cannot double-export.

**Console notices are user copy (#813).** An interactive session routes every log
record at INFO or above to the console as a dim `·` line (`main.py` installs
`ui.NoticeHandler` at INFO), so such a record is written for the person running the
build: plain English, with no issue numbers, hedged plurals (`file(s)`) or module
tags, and a count after a label (`Files attached: 91`). What only a developer needs
is logged at DEBUG and shown with `-v`. `tests/test_log_lines_are_user_copy.py`
holds the rule.

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
(used by the eval, batch runs, and the test suite) is `is_interactive = False`, so
behind it nothing waits on an answer nobody can give: a scan-root escalation is
denied and the loop's RECOMMENDED/OPTIONAL validation escalation is never offered
(D9). This gate is also why `--smoke-test` needs its own `SmokeTestHumanInterface`
([human-in-the-loop](sessions-and-hitl.md#human-in-the-loop-hitl)) rather than reusing the simulator: to exercise the loop's questions an
interface must report `is_interactive = True`, and the simulator reports `False` by
design.
