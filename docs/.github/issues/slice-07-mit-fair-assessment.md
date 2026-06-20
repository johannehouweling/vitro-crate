# Slice 7: MIT + FAIR Assessment Tools

## What to build

Two assessment tools that produce scores (not pass/fail):

- `assess_mit_coverage()` — walks CrateState field-level completion data, maps each field to its MIT module using the `crate_slot` mappings in `mit/invitro_tox.yaml`, computes per-module completion percentages (e.g., Chemical Information: 6/12 fields filled = 50%), and an overall score
- `assess_fair_maturity()` — checks the assembled crate against indicators in `fair/indicators.yaml` and `fair/dsm_indicators.yaml`, returns indicator-level results and a DSM level

Both return structured reports that the agent can present to the user as improvement suggestions.

## Acceptance criteria

- [ ] `assess_mit_coverage()` returns `{module_scores: {module_name: {completed, total}}, overall_score: float}`
- [ ] Uses `mit/invitro_tox.yaml` crate_slot mappings to map fields to modules
- [ ] `assess_fair_maturity()` returns `{indicator_results: [{indicator, passed, detail}], dsm_level: str}`
- [ ] Uses `fair/indicators.yaml` and `fair/dsm_indicators.yaml`
- [ ] Both produce scores only — no blocking, no pass/fail
- [ ] Tests: assess a half-complete CrateState (verifies partial scores), assess empty CrateState (verifies 0% scores)

## Blocked by

- Slice 1 (CrateState — field-level completion data for MIT)
- Slice 6a (assembled crate needed for FAIR indicator checks)