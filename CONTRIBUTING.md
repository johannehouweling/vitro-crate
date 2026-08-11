# Contributing to vitro-crate

## Development Environment

- **Python:** >=3.12
- **Package manager:** [uv](https://docs.astral.sh/uv/) — use `uv` for all dependency and virtual environment management. The `.venv/` directory is created implicitly by `uv sync` and must not be committed to the repo.
- **Type checking:** `uv run ty` (uses [`ty`](https://docs.astral.sh/ty/), the Rust-based type checker from Astral).
- **Linting:** `ruff` (configured in `pyproject.toml`).

## Commands

```bash
uv sync                        # install all dependencies (including dev)
uv sync --dev --extra langchain  # with LLM agent support
uv add <package>               # add a production dependency
uv add --dev <package>         # add a dev dependency
uv run <command>               # run a command in the venv
uvx ruff check                 # lint (uv-managed ruff)
uv run ty                      # type checking (Rust-based)
uv run pytest                  # run tests
uv run pytest --cov=builder    # with coverage
```

## Testing

This project uses **test-driven development (TDD)**. Write the test before the implementation.

- **Framework:** `pytest`
- **Run tests:** `uv run pytest`
- **Run typechecker:** `uv run ty`
- **Run linter:** `uvx ruff check`
- **Run with coverage:** `uv run pytest --cov=builder`
- **Test location:** `tests/` directory, mirroring the `builder/` structure.

**TDD workflow:**
1. Write a failing test that describes the expected behavior
2. Confirm it fails — **for the right reason** (because the behavior is missing, not
   because a fixture value you haven't set yet is absent)
3. Write the minimal implementation to pass
4. Confirm it passes
5. Refactor if needed

### Writing tests that actually test

A test must be able to fail *because the code under test is broken*. If deleting the
function under test leaves the test green, it isn't testing that function. The
recurring anti-pattern in this repo (see #343) is the **tautological test**: it
hand-builds the expected `CrateState` (or hard-codes values from the golden spec) and
asserts them back, bypassing the producer entirely. Guard against it:

- **Drive the real entry point over real/recorded input** — for build-path coverage,
  scan a real fixture and run `run_pipeline` (see `tests/test_pipeline_real_input.py`),
  don't assert back a hand-built state. Prefer real recorded fixtures over hand-built
  intermediate state.
- **Avoid mocks; test observable state at the public entry point.** When you must
  stand in for a non-deterministic seam (LLM/network), gate the stub on **content-only
  tokens** from the real input so a scan/extraction regression still reddens the test.
- **Oracle from the spec, not observed behavior.** Don't read expected values off the
  current implementation — a test written against buggy code just ratifies the bug.

Three mechanisms enforce this:

- **Read-only tests** — a `PreToolUse` hook (`.claude/hooks/guard-test-edits.py`, wired
  in `.claude/settings.json`) blocks agent edits to `tests/` so a test is never silently
  weakened to make code pass. Authoring/updating tests is a deliberate act: set
  `CLAUDE_ALLOW_TEST_EDITS=1`.
- **Mutation testing** — the objective "would this catch a regression" signal (coverage
  does not measure it). Run locally with `uv run mutmut run` then `uv run mutmut results`;
  CI runs it weekly + on demand (`.github/workflows/mutation.yml`, informational). Scope
  is in `[tool.mutmut]` in `pyproject.toml` — seeded on one fast module; expand
  `source_paths` to the producer modules as the budget allows (#346).
- **Review lens** — the `Claude Code Review` workflow flags assert-back, bypasses-producer,
  over-mock, and name-claims-more-than-body smells on every PR.

## CI

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push/PR to `main`:
1. `lint` job — `uvx ruff check` and `uv run ty check`. **Both gate**: `ty` has been
   enforcing since the ty-clean sweep, so a type error reddens the PR before any test
   shard runs. Run it locally before pushing — it is cheap, and it catches things a
   passing test suite will not (a stub whose signature no longer matches what it
   replaces, a `dict` literal ty narrows more tightly than you meant).
   `ruff format` is **not** a gate.
2. `test` job — a **16-way matrix** that shards the suite across sixteen independent
   `ubuntu-latest` runners with [`pytest-split`](https://github.com/jerry-git/pytest-split).
   Each shard runs serially (no `pytest-xdist`), and the shards run concurrently
   as separate jobs.

Why matrix sharding instead of in-process `pytest-xdist`? GitHub's standard
runner is 2 vCPU / 1 physical core, so `pytest-xdist -n auto` resolves to a
single worker there (`created: 1/1 worker`) — i.e. no parallelism. Forcing a
fixed `-n N` risks OOM: the runner has ~7 GB and every xdist worker re-loads
torch + langchain + rocrate. Sharding across jobs gives each shard the whole
runner's RAM and provides real wall-clock parallelism without OOM risk.

Shards are balanced by recorded test timings in the committed `.test_durations`
file (`--splitting-algorithm least_duration`), so the heavy SHACL-validation and
e2e build tail is spread evenly (a few minutes per shard) rather than piling into one
group. Regenerate `.test_durations` after large test-suite changes with:

```bash
uv run pytest \
  --ignore=tests/test_validator_wiring.py \
  --ignore=tests/test_lookups_contract.py \
  --ignore=tests/test_dashboard.py \
  --store-durations -p no:cacheprovider
```

**Local dev:** the suite is still wired for `pytest-xdist`, so locally (where
machines have many physical cores and more RAM) you can parallelise in-process:

```bash
uv run pytest -n auto --maxprocesses=4   # cap workers so build/validate don't OOM
```

Merges to `main` are gated on a green CI run.

## Code Style

- Follow **PEP 8** conventions (enforced by `ruff`).
- Use **type hints** on all public functions and methods.
- Prefer **dataclasses** over plain dicts for structured data.
- Use **pathlib** over `os.path`.
- Avoid bare `except` — catch specific exceptions.
- Logging via the `logging` module, not `print`.

## Project Conventions

- **Entity model:** All entity types live in `builder/state.py` as dataclasses.
- **Tools:** Each tool is a standalone function in `builder/tools/`. One file per tool category.
- **Lookups:** External API clients live in `lookups/` and return `{found: bool, data: dict, error: str | None}`.
- **Validation:** SHACL shapes in `profiles/shapes/`. Three-pass validation in `profiles/validator.py`.
- **Sessions:** Persisted to `sessions/<session_id>/`. Never commit session data to the repo.
- **Input data:** Example inputs live in `input/`. Never commit real experimental data.
- Commits using conventional commit style with keywords like: `feat: <commit msg>`, `docs: <commit msg>`, `fix: <commit msg>`, `chore: <commit msg>` or `feat(<part>): <commit msg pertaining to part>`. Breaking changes are indicated with `feat!:` exclamation mark before the colon. 

## Pull Requests

- One feature or fix per PR.
- Include tests for new functionality.
- Ensure all tests pass before requesting review.
- Update `AGENTS.md` if the architecture changes.

## AI Coding Agents

This repo is designed to be worked on by both humans and AI coding agents. The `AGENTS.md` file contains the system architecture and design rationale — read it first if you are an AI agent onboarding to this codebase.

**`AGENTS.md` is a design document, not a changelog or an implementation manual** (see its "Maintaining this document" note). State contracts and invariants in the present tense; put line-level algorithm detail in **docstrings** and point to them. Keep out migration narratives, dated audit snapshots, "task N — done/withdrawn", and A/B run logs — that history lives in git and PRs. Cite an issue number only when it names a durable contract.

When an AI agent makes changes, it should:
1. Read `AGENTS.md` to understand the architecture.
2. Follow the guidelines of the tdd (test-driven development) skill for any new functionality.
   1. Start with a failing test first
   2. Implement the minimal feature that makes the test pass
   3. And incrementally build the full feature in this fashion
3. Update `AGENTS.md` if the design changes.
4. Leave `CONTRIBUTING.md` conventions intact.
5. Update `README.md` if anything of note is added or needs changing.