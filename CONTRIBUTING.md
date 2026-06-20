# Contributing to vitro-crate

## Development Environment

- **Python:** >=3.12
- **Package manager:** [uv](https://docs.astral.sh/uv/) — use `uv` for all dependency and virtual environment management. The `.venv/` directory is created implicitly by `uv sync` and must not be committed to the repo.
- **Type checking:** `uv run ty` (uses [`ty`](https://docs.astral.sh/ty/), the Rust-based type checker from Astral).
- **Linting:** `ruff` (configured in `pyproject.toml`).

## Commands

```bash
uv sync                        # install all dependencies (including dev)
uv add <package>               # add a production dependency
uv add --dev <package>         # add a dev dependency
uv run <command>               # run a command in the venv
```

## Testing

This project uses **test-driven development (TDD)**. Write the test before the implementation.

- **Framework:** `pytest`
- **Run tests:** `uv run pytest`
- **Run typechecker:** `uv run ty`
- **Run with coverage:** `uv run pytest --cov=builder`
- **Test location:** `tests/` directory, mirroring the `builder/` structure.

**TDD workflow:**
1. Write a failing test that describes the expected behavior
2. Confirm it fails (`uv run pytest`)
3. Write the minimal implementation to pass
4. Confirm it passes
5. Refactor if needed

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

## Pull Requests

- One feature or fix per PR.
- Include tests for new functionality.
- Ensure all tests pass before requesting review.
- Update `AGENTS.md` if the architecture changes.

## AI Coding Agents

This repo is designed to be worked on by both humans and AI coding agents. The `AGENTS.md` file contains the system architecture and design rationale — read it first if you are an AI agent onboarding to this codebase.

When an AI agent makes changes, it should:
1. Read `AGENTS.md` to understand the architecture.
2. Follow the guidelines of the tdd (test-driven development) skill for any new functionality.
   1. Start with a failing test first
   2. Implement the minimal feature that makes the test pass
   3. And incrementally build the full feature in this fashion
3. Update `AGENTS.md` if the design changes.
4. Leave `CONTRIBUTING.md` conventions intact.
5. Update `README.md` if anything of note is added or needs changing.