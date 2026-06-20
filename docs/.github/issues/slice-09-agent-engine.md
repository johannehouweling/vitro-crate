# Slice 9: Agent Engine Orchestrator

## What to build

The core agent loop in `engine.py` that wires all tools together:

1. **Initialization**: Runs `scan_files(path)` to build the raw file inventory
2. **Agent loop**: Enters a loop where it:
   - Examines current `CrateState` (entities drafted, fields filled, validation results, MIT scores)
   - Decides which tool to call next based on state
   - Calls the tool, processes the result, updates state
   - Records every step in `CrateState.checkpoint.reasoning_log`
3. **Gates**: Checks validation after each draft — REQUIRED failures route back to drafting. Stuck detection escalates to user
4. **Completion**: Continues until validation passes with no REQUIRED issues, or user signals done

The agent uses an LLM (configurable provider) to make decisions. Tools are passed as a registry. The engine is framework-agnostic — it just calls tool functions and passes results to the LLM.

## Acceptance criteria

- [ ] `run_engine(input_path, session_id)` initializes CrateState, calls scan_files, enters agent loop
- [ ] Agent loop examines state and calls appropriate tools (draft, lookup, verify, validate, assess, HITL)
- [ ] Every tool call is recorded in reasoning_log with {step, action, tool, result, timestamp}
- [ ] Validation gate: after each draft, runs validate(); if REQUIRED issues, routes back to drafting
- [ ] Stuck detection: after N iterations without progress, escalates to present_to_human()
- [ ] Max iterations guard (configurable, default 100)
- [ ] On completion: calls build_crate(), assess_mit_coverage(), assess_fair_maturity()
- [ ] Integration test: full cycle with mocked tools (deterministic responses), verify reasoning_log and final state
- [ ] Integration test: stuck detection triggers after repeated identical failures

## Blocked by

- Slice 2 (scan_files — initialization)
- Slices 4a, 4b, 4c (entity drafting)
- Slice 5 (identifier verification)
- Slice 6a (crate assembly)
- Slice 6b (validation)
- Slice 7 (MIT/FAIR assessment)
- Slice 8 (HITL tools)