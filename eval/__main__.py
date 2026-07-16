"""``python -m eval`` — run the A/B harness and write a labeled report.

Live by default: it runs the current ReAct engine over the corpus (real LLM calls,
your configured DeepSeek-flash / OpenAI / Anthropic credentials) and writes a
labeled ndjson report under ``eval_reports/``. Capture the baseline with::

    python -m eval --label react-baseline

then freeze the baseline at git tag ``react-baseline`` (see ``eval/README.md``).
When the deterministic pipeline lands, run the same command with its factory under
a ``--label pipeline`` and diff the two reports with :func:`eval.report.compare_reports`.

The ``agent_factory`` / ``profile_reader`` parameters of :func:`run_main` exist so
the offline tests drive the whole flow with a mock — they are never used live.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from builder.agents.build import BuildMode
from builder.config import load_config, merge_with_env
from eval.agent_api import BuildAgent
from eval.corpus import DEFAULT_CORPUS
from eval.report import write_report
from eval.runner import run_eval

logger = logging.getLogger(__name__)

_DEFAULT_OUT_DIR = "eval_reports"


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser for ``python -m eval``."""
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Run the agent-agnostic A/B evaluation harness over the corpus.",
    )
    parser.add_argument(
        "--arch",
        choices=("react", "pipeline"),
        default="react",
        help=(
            "Architecture under test: 'react' (DEFAULT) drives the live ReAct "
            "engine; 'pipeline' runs the deterministic spine (AGENTS.md §14)."
        ),
    )
    parser.add_argument(
        "--label",
        default="react-baseline",
        help="Run label recorded in the report (default: react-baseline).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Builds per case; >1 enables the determinism signal (default: 2).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output ndjson path (default: <out-dir>/<label>.ndjson).",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="LLM provider override (openai|anthropic); auto-detected if omitted.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override for the live ReAct agent.",
    )
    parser.add_argument(
        "--api-base",
        dest="api_base",
        default=None,
        help="Custom OpenAI-compatible base URL for the live ReAct agent.",
    )
    return parser


def select_agent_factory(
    mode: BuildMode,
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None,
) -> Callable[[], BuildAgent]:
    """Return the live agent factory for the chosen build *mode*.

    :attr:`~builder.agents.build.BuildMode.REACT` (the harness default) builds the
    live ReAct factory (which reads provider/model/base_url);
    :attr:`~builder.agents.build.BuildMode.PIPELINE` builds the deterministic-spine
    factory (which ignores those — it calls no model). The CLI's ``--arch`` string
    maps straight onto the shared enum via ``BuildMode(arch)``, so ``main.py`` and
    this harness flip A/B through the *same* switch (#309). Keeping selection here
    means :func:`run_main` stays mode-agnostic and the choice is unit-testable.

    Args:
        mode: The :class:`~builder.agents.build.BuildMode` to build.
        provider: LLM provider override (ReAct only).
        model: Model name override (ReAct only).
        base_url: Custom OpenAI-compatible base URL (ReAct only).

    Returns:
        A zero-arg factory producing fresh :class:`~eval.agent_api.BuildAgent`s.

    Raises:
        ValueError: If *mode* is unrecognised.
    """
    if mode is BuildMode.PIPELINE:
        from eval.pipeline_factory import make_pipeline_agent_factory

        return make_pipeline_agent_factory()
    if mode is BuildMode.REACT:
        from eval.react_factory import make_react_agent_factory

        return make_react_agent_factory(provider=provider, model=model, base_url=base_url)
    raise ValueError(f"Unknown build mode: {mode!r}")


def run_main(
    argv: list[str] | None = None,
    *,
    agent_factory: Callable[[], BuildAgent] | None = None,
    profile_reader: Callable[[str], list[dict[str, Any]]] | None = None,
    out_dir: str = _DEFAULT_OUT_DIR,
) -> int:
    """Run the harness and write a report; return a process exit code.

    Args:
        argv: CLI args (defaults to ``sys.argv[1:]``).
        agent_factory: Override the agent factory (the tests inject a mock). When
            ``None``, the **live** ReAct factory is used.
        profile_reader: Override the profile reader (tests inject a canned one).
        out_dir: Directory for the default ``<label>.ndjson`` output path.

    Returns:
        ``0`` on success, ``1`` on a configuration error (e.g. no LLM provider).
    """
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if agent_factory is None:
        # LIVE path only — and BOTH arches need creds. The ReAct factory reads
        # provider/api_key/base_url/model from the environment
        # (builder.agents.llm); the deterministic pipeline now does too —
        # post-#211 its drafter-leaf (builder/agents/leaves.py, via
        # builder.agents.llm._build_chat_model) reads the same env vars.
        # So bridge any creds kept solely in ~/.config/vitro-crate/config.toml
        # into os.environ first — mirroring how the interactive CLI hydrates via
        # merge_with_env(). Without this, creds-in-config.toml make a live ReAct
        # run raise "No LLM provider configured", and a live --arch pipeline run
        # silently no-op the drafter-leaf -> a false-negative A/B comparison
        # (#179). When no provider is configured both arches still no-op
        # gracefully. The offline/mock path (injected agent_factory) skips this
        # entirely so its tests stay config-free.
        merge_with_env(load_config())

        agent_factory = select_agent_factory(
            BuildMode(args.arch),
            provider=args.provider,
            model=args.model,
            base_url=args.api_base,
        )

    out_path = Path(args.out) if args.out else Path(out_dir) / f"{args.label}.ndjson"

    logger.info(
        "Running eval: label=%s repeats=%s cases=%d",
        args.label,
        args.repeats,
        len(DEFAULT_CORPUS),
    )
    try:
        report = run_eval(
            agent_factory,
            DEFAULT_CORPUS,
            repeats=args.repeats,
            label=args.label,
            profile_reader=profile_reader,
        )
    except RuntimeError as exc:
        # e.g. no LLM provider configured for the live factory.
        logger.error("Eval run failed: %s", exc)
        return 1

    write_report(report, out_path)
    summary = report.summary()
    logger.info(
        "Done: success_rate=%.2f mean_tokens=%.0f determinism_rate=%.2f -> %s",
        summary["success_rate"],
        summary["mean_total_tokens"],
        summary["determinism_rate"],
        out_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.exit(run_main())
