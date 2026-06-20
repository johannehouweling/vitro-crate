# Issue Templates for vitro-crate

This directory contains templated issue descriptions for each vertical slice of the ISA-Tox RO-Crate Builder project. Each file follows a standard template.

To publish to the GitHub issue tracker, run from a machine with `gh` installed:

```bash
for f in docs/.github/issues/slice-*.md; do
  title=$(head -1 "$f" | sed 's/^# //')
  body=$(tail -n +2 "$f")
  gh issue create --title "$title" --body "$body" --label ready-for-agent
done
```

**Important**: Publish in dependency order (blockers first, see below) so that the "Blocked by" fields can reference real GitHub issue numbers.

## Dependency order (publish blockers first)

| Order | File | Blocked by |
|-------|------|------------|
| 1 | slice-01-cratestate.md | None |
| 2 | slice-03a-chemical-lookups.md | None |
| 3 | slice-03b-biological-lookups.md | None |
| 4 | slice-03c-identity-lookups.md | None |
| 5 | slice-08-hitl-tools.md | #1 |
| 6 | slice-02-scan-files.md | #1 |
| 7 | slice-04a-structural-entities.md | #1, #2, #3, #4 |
| 8 | slice-04b-domain-entities.md | #1, #2, #3 |
| 9 | slice-04c-supporting-entities.md | #1, #4 |
| 10 | slice-05-identifier-verification.md | #1, #2, #3, #4 |
| 11 | slice-06a-crate-assembly.md | #1, #7, #8, #9 |
| 12 | slice-06b-validation.md | #11 |
| 13 | slice-07-mit-fair-assessment.md | #1, #11 |
| 14 | slice-10-arc-writer.md | #1, #11 |
| 15 | slice-09-agent-engine.md | #6, #7, #8, #9, #10, #11, #12, #13 |