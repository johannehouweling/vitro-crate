# D8: Observability via Reasoning Log

Every tool call, state change and reasoning step is recorded in
`CrateState.checkpoint.reasoning_log` as `{"step", "action", "tool", "result", "timestamp"}`.
The log is persisted with the session and survives resume. It backs live status (`get_status()`:
phase, entity counts, MIT scores, iteration count, last action), progress tracking and
diagnostics (failed lookups, rejected HITL checkpoints, stuck agents); a web UI can tail it
without changing the builder's internals.
