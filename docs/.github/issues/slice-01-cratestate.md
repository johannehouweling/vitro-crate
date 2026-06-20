# Slice 1: CrateState Foundation + Session Persistence

> **Status: ✅ COMPLETE** — Implemented in `builder/state.py` (241 lines) and `builder/tools/session.py` (164 lines). 10 tests pass.

## What to build

The `CrateState` dataclass — the single source of truth for the entire builder. It holds all entity collections, field-level completion status (`_completion`), provenance tracking (`_provenance`), scanned file inventory, validation results, MIT/FAIR assessment scores, and the checkpoint reasoning log.

Implement JSON serialization/deserialization so the state can be saved to disk and resumed. Session persistence auto-saves to `sessions/<session_id>/crate_state.json` at key milestones (scan, draft, validate, HITL, explicit save). The reasoning log is a structured array of `{step, action, tool, result, timestamp}` events.

This slice includes no tools, no agent loop — just the data model that everything else builds on.

## Acceptance criteria

- [x] `CrateState` dataclass declared with all sections: session_id, metadata, entities (by type with _completion and _provenance), scanned_files, validation, mit_assessment, fair_assessment, checkpoint (with reasoning_log), iteration_count, stuck
- [x] Entity schema includes `_completion: {field_name: {status, source}}` and `_provenance: {created_by, reviewed_by, lookups_used}`
- [x] JSON round-trip: `CrateState -> JSON file -> CrateState` preserves all data
- [x] `save_session(label)` writes `crate_state.json` to `sessions/<id>/`
- [x] `load_session(id)` loads state from disk and returns CrateState
- [x] `list_sessions()` enumerates saved sessions with metadata
- [x] `get_status()` returns phase, entity counts, MIT score, validation status
- [x] `get_hint()` returns contextual hint about next action
- [x] Reasoning log events include: step number, action description, tool name, result summary, ISO timestamp
- [x] Tests: serialization round-trip, session save/list/load, get_status, get_hint

## Implementation

| File | Purpose |
|------|---------|
| `builder/state.py` | 12 dataclasses: CrateState, Entity, FieldCompletion, EntityProvenance, FileClassification, CrateMetadata, ValidationReport, MITReport, FAIRReport, ReasoningStep, Checkpoint, EntityStatus |
| `builder/tools/session.py` | save_session, load_session, list_sessions, get_status, get_hint |

## Blocked by

None — can start immediately.
