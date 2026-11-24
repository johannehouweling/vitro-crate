# D11: CI Workflow (GitHub Actions)

A `.github/workflows/ci.yml` workflow runs on every push/PR to `main` (Issue #58). It executes
`uv sync`, `ruff check`, `ty` and `pytest`. `ty` **gates** — the tree reports zero diagnostics, and
it is never to be put back behind `continue-on-error`: run advisory, it quietly accumulated dozens
of real diagnostics (a wrong function signature, an LSP-violating override) that nothing was ever
going to force anyone to fix. `pytest` runs the **whole** suite (`testpaths = tests`, `eval/tests`),
sharded 16 ways by pytest-split rather than filtered — no marker, ignore or deselect excludes
anything. The one carve-out is the live-network lookup checks, opt-in behind `VITRO_LIVE_LOOKUPS`
so **CI never touches the network**; do not wire them into the workflow. This prevents regressions
from landing on `main` and keeps the SHACL validator-wiring test gated.
