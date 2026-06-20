# Issue Templates for vitro-crate

This directory contains templated issue descriptions for each vertical slice of the ISA-Tox RO-Crate Builder project. Each file follows a standard template.

To publish to the GitHub issue tracker, run from a machine with `gh` installed:

```
for f in docs/.github/issues/*.md; do
  title=$(head -1 "$f" | sed 's/^# //')
  body=$(tail -n +2 "$f")
  gh issue create --title "$title" --body "$body" --label ready-for-agent
done
```

## Dependency order (publish blockers first)

1. slice-01-cratestate.md (no blocker)
2. slice-03a-chemical-lookups.md (no blocker)
3. slice-03b-biological-lookups.md (no blocker)
4. slice-03c-identity-lookups.md (no blocker)
5. slice-02-scan-files.md (blocked by 1)
6. slice-04a-structural-entities.md (blocked by 1, 3a-c)
7. slice-04b-domain-entities.md (blocked by 1, 3a-b)
8. slice-04c-supporting-entities.md (blocked by 1, 3c)
9. slice-05-identifier-verification.md (blocked by 1, 3a-c)
10. slice-06a-crate-assembly.md (blocked by 1, 4a-c)
11. slice-06b-validation.md (blocked by 6a)
12. slice-07-mit-fair-assessment.md (blocked by 1, 6a)
13. slice-08-hitl-tools.md (blocked by 1)
14. slice-09-agent-engine.md (blocked by 2, 4a-c, 5, 6a-b, 7, 8)
15. slice-10-arc-writer.md (blocked by 1, 6a)