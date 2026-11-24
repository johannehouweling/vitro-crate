# D11: CI Workflow (GitHub Actions)

`.github/workflows/ci.yml` runs `uv sync`, `ruff check`, `ty` and `pytest` on every push/PR to
`main` (#58).

- `ty` **gates**, at zero diagnostics; never put it back behind `continue-on-error` — advisory, it
  accumulated real defects nobody was forced to fix.
- `pytest` runs the **whole** suite (`testpaths = tests`, `eval/tests`), sharded 16 ways by
  pytest-split; no marker, ignore or deselect excludes anything.
- The live-network lookup checks are opt-in behind `VITRO_LIVE_LOOKUPS`, so **CI never touches
  the network**; do not wire them into the workflow.
