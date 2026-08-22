# Triage Labels

## Canonical roles

| Role | Label | Description |
|------|-------|-------------|
| Needs triage | `needs-triage` | Maintainer needs to evaluate |
| Needs info | `needs-info` | Waiting on reporter for more information |
| Ready for agent | `ready-for-agent` | Fully specified; an AFK agent can pick it up with no human context |
| Ready for human | `ready-for-human` | Requires human implementation |
| Won't fix | `wontfix` | Will not be actioned |

Of these, only `ready-for-agent` and `wontfix` exist on the tracker, and only `ready-for-agent` is in use; `needs-triage`, `needs-info` and `ready-for-human` have never been created. Do not apply a label from this table without checking `gh label list` first.

## Custom overrides

Three project labels exist alongside the GitHub defaults:

- `blocked-upstream` — the issue is gated on a fix in an external project (`crs4/rocrate-validator`). Do not pick one up as agent work.
- `security` — security vulnerability or hardening.
- `performance` — performance work.