# D8: Observability via Reasoning Log

Every tool call, state change, and reasoning step is recorded in `CrateState.checkpoint.reasoning_log` as a structured event: `{"step": int, "action": str, "tool": str, "result": str, "timestamp": datetime}`. This log enables:
- **Live status** for web UIs (`get_status()` returns current phase, entity counts, MIT scores, iteration count, last action)
- **Session replay** for debugging — re-run the tool calls from the log against the same state
- **Progress tracking** — number of entities drafted, fields filled vs total, validation pass/fail counts
- **Diagnostics** — which lookups failed, which HITL checkpoints were rejected, how often the agent got stuck

The reasoning log is persisted with the session and survives resume. A future web UI can tail or stream this log without changing the builder's internals — the data structure is already there.
