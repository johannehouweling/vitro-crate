# Issue Tracker

## Tracker type

- **Platform:** GitHub Issues
- **Repository:** `johannehouweling/vitro-crate`
- **CLI:** `gh`

## Triage surface

External pull requests are **not** treated as a request surface for triage.

## Workflow

1. Incoming issues are triaged using the label vocabulary defined in `triage-labels.md`.
2. In practice an issue gets a kind label (`bug` / `enhancement` / `documentation`) and, once it is fully specified, `ready-for-agent`. `wontfix` is available as a terminal state but is currently unused.
3. The wider `needs-triage` → `needs-info` / `ready-for-agent` / `ready-for-human` → `wontfix` ladder is not adopted: three of those labels do not exist on the tracker. See `triage-labels.md` for which do.