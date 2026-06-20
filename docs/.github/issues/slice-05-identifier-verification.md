# Slice 5: Identifier Verification Tool

## What to build

Tools to verify that every identifier in the crate resolves at its source:

- `verify_identifier(entity_id, field)` — given an entity and a specific field (e.g., `MolecularEntity.identifier` with value "33889-69-9"), call the relevant lookup to confirm it exists
- `verify_all_identifiers()` — walk all entities in CrateState, check every field with `status: "filled"`, return a list of VerificationResults

Verification is REQUIRED — if an identifier doesn't resolve, the field is cleared (status -> "missing") and the agent tries alternatives or asks the user. Leaving a field empty is always acceptable.

## Acceptance criteria

- [ ] `verify_identifier(entity_id, field)` calls the appropriate lookup based on entity type and field name
- [ ] Returns `{verified: bool, field: str, value: str, error: str | None}`
- [ ] On failure, field cleared in CrateState (`_completion.status` -> "missing")
- [ ] `verify_all_identifiers()` returns list of VerificationResults for all filled identifier fields
- [ ] No-op for fields that are already "missing" or "verified"
- [ ] Tests: verify known-good identifier (passes), verify fabricated identifier (fails and clears field)

## Blocked by

- Slice 1 (CrateState — reads entity _completion and _provenance)
- Slices 3a, 3b, 3c (lookup clients for verification calls)