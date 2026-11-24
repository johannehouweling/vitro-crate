# D12: Bounded Message History (Trim + Prune Before Each Model Call)

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
