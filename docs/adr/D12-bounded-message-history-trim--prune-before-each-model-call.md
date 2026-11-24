# D12: Bounded Message History (Trim + Prune Before Each Model Call)

The history is bounded before every model call: `_assemble_model_messages`
(`builder/agents/react/agent_loop.py`) runs it through `_trim_history`. Without this,
`MemorySaver` replays the whole transcript and its tool outputs on every invoke, so input grows
each turn until the context overflows (#61). See `_trim_history`'s docstring for the mechanics.

1. **Prune consumed tool outputs** (`_prune_state_backed_outputs`). A `ToolMessage` over
   `_PRUNE_CONTENT_THRESHOLD` chars becomes a stub **only once a later `AIMessage` or
   `HumanMessage` exists**: the node after `tools` is `call_model`, so pruning earlier destroys a
   result before any model sees it (#376). The message is rewritten, never dropped, so the
   tool-call pairing holds. The stub is truthful per tool: `_STATE_BACKED_TOOLS` (`scan_files`)
   point at `list_scanned_files`; `_REPLAYABLE_READER_TOOLS` persist nothing, so their stub never
   claims the text is in state and points at `read_file`.
2. **Token-budget trim** with `trim_messages(strategy="last", start_on=("human", "ai"))`. The
   window never begins with an orphaned `ToolMessage` or unanswered tool call; providers reject
   those. Do not anchor on `"human"` alone: one `HumanMessage` enters per invocation, so a long
   AI/Tool tail makes the trim return an empty list.
3. `_drop_unanswered_tool_calls` runs first, over the pruned history, which is what makes an
   AI-anchored window safe. An unanswered call is **dropped, never answered with a synthesised
   result**: the tool never ran.

Trimming touches only the history between the stable system prefix and the trailing brief, so the
D10 cache layout holds. The budget is `get_max_history_tokens()`: `VITRO_MAX_HISTORY_TOKENS` →
`[agent] max_history_tokens` → `12000`. `_trim_history` never raises into the loop: an edge case,
or a trim that keeps nothing of a non-empty history, falls back to the pruned history with a
warning: an untrimmed history is always better than none.
