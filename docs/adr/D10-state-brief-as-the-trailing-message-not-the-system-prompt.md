# D10: State Brief as the Trailing Message, Not the System Prompt

The per-turn state brief goes **last**, after the history, never into the system prompt or a user
message: `_assemble_model_messages` puts `SYSTEM_PROMPT` first and the brief from
`_build_system_prompt_with_state()` at the tail.

Why: providers cache the stable `tools + system + history` prefix; volatile state in the system
message breaks that cache and leaves the growing history uncached (#60). `SYSTEM_PROMPT` must
therefore stay byte-stable. Neither the system message nor the brief is persisted into
`MemorySaver`; both are rebuilt each invocation, so per-turn metadata never accumulates (#66).
`get_status` gives the LLM full details.
