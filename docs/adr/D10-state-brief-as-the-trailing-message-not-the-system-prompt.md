# D10: State Brief as the Trailing Message, Not the System Prompt

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
