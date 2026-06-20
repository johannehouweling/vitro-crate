# Slice 6b: Validation (validate)

## What to build

`validate(crate_path)` — runs the three-pass SHACL validation via the existing `profiles/validator.py` (which wraps `rocrate_validator`). Returns a `ValidationReport` with separate lists for:

- **REQUIRED** issues — blocking, crate must not be considered complete
- **SHOULD** issues — recommended, agent should fix if data is available
- **MAY** issues — informational, noted for the user

The three passes are: RO-Crate 1.1 base, ISA Profile, ISA-Tox Profile. The validator suppresses inherited-profile duplicates so each pass reports only its own layer.

## Acceptance criteria

- [ ] `validate(crate_path)` returns `{required: [str], should: [str], may: [str]}`
- [ ] Runs all three SHACL passes via `profiles/validator.py`
- [ ] REQUIRED issues block further progress (stored in CrateState.validation)
- [ ] SHOULD/MAY issues stored but don't block
- [ ] Duplicates across profiles are suppressed
- [ ] Tests: validate a minimal crate (should have REQUIRED issues), validate a complete crate (should pass)

## Blocked by

- Slice 6a (needs a crate to validate)