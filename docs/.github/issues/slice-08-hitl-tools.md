# Slice 8: HITL Tools

## What to build

The human-in-the-loop interface that lets the agent present checkpoints and collect user feedback:

- `present_to_human(context, options)` — presents a message to the user with a set of allowed responses. Returns `HumanResponse` which is one of: `{type: "approve"}`, `{type: "edit", replacement: dict}`, `{type: "reject", reason: str}`, or `{type: "skip"}`.
- Stuck detection — after N iterations without progress (same entity count, same validation failures, repeated HITL rejections), the agent escalates to the user with a "stuck" checkpoint.

All user feedback is logged to the relevant entity's `_provenance` (who reviewed/edited what, when).

## Acceptance criteria

- [ ] `present_to_human(context, options)` returns a HumanResponse
- [ ] User can Approve, Edit (with replacement data), Reject (with reason), or Skip
- [ ] Feedback logged to entity `_provenance.reviewed_by` and `_provenance.review_notes`
- [ ] Stuck detection triggers after N iterations (configurable, default 10) without state changes
- [ ] Stuck escalation presents a special checkpoint: "I'm stuck trying to X. Can you help?"
- [ ] Tests: each response type, stuck detection with mock state, feedback logging

## Blocked by

- Slice 1 (CrateState — provenance tracking in entities)