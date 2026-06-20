# Slice 1: CrateState Foundation + Session Persistence

## What to build

The `CrateState` dataclass — the single source of truth for the entire builder. It holds all entity collections, field-level completion status (`_completion`), provenance tracking (`_provenance`), scanned file inventory, validation results, MIT/FAIR assessment scores, and the checkpoint reasoning log.

Implement JSON serialization/deserialization so the state can be saved to disk and resumed. Session persistence auto-saves to `sessions/<session_id>/crate_state.json` at key milestones (scan, draft, validate, HITL, explicit save). The reasoning log is a structured array of `{step, action, tool, result, timestamp}` events.

This slice includes no tools, no agent loop — just the data model that everything else builds on.

## Acceptance criteria

- [ ] `CrateState` dataclass declared with all sections: session_id, metadata, entities (by type with _completion and _provenance), scanned_files, validation, mit_assessment, fair_assessment, checkpoint (with reasoning_log), iteration_count, stuck
- [ ] Entity schema includes `_completion: {field_name: {status, source}}` and `_provenance: {created_by, reviewed_by, lookups_used}`
- [ ] JSON round-trip: `CrateState -> JSON file -> CrateState` preserves all data
- [ ] `save_session(label)` writes `crate_state.json` to `sessions/<id>/`
- [ ] `resume_session(id)` loads state from disk and returns CrateState
- [ ] Auto-save triggers at: after scan, after each entity draft, after validation, after HITL checkpoint
- [ ] Reasoning log events include: step number, action description, tool name, result summary, ISO timestamp
- [ ] Tests: serialization round-trip, auto-save milestones, reasoning log append

## Blocked by

None — can start immediately.