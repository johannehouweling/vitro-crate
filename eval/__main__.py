"""``python -m eval`` — run the harness over the ReAct build and write a labeled report.

Live by default: it runs the ReAct build (:func:`builder.agents.build.run_build`)
over the corpus — real LLM calls with your configured DeepSeek-flash / OpenAI /
Anthropic credentials — and writes a labeled ndjson report under ``eval_reports/``.
Runs made under different labels (another model, prompt or revision) diff with
:func:`eval.report.compare_reports`::

    python -m eval --label react
    python -m eval --label react-gpt-4o --model gpt-4o

See ``eval/README.md``; the ReAct baseline is frozen at git tag ``react-baseline``.

The ``agent_factory`` / ``profile_reader`` parameters of :func:`run_main` exist so
the offline tests drive the whole flow with a mock — they are never used live.

For a defensible comparison (#331) the report also records, per case: the
**stop-reason** (``completed`` / ``cap_hit`` / ``error`` — a ReAct "win" at the
recursion cap is not a clean win), the **cost** in USD (from
``eval.metrics.MODEL_PRICES`` or the ``--price-input`` / ``--price-output``
override for an unlisted model), and how many **transient** network/API failures
were re-run before the result counted (``--max-transient-retries``). ``--repeats``
defaults to 3 so variance is reported over more than one or two samples.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable

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
        description="Run the evaluation harness over the corpus with the ReAct build.",
    )
    parser.add_argument(
        "--label",
        default="react-baseline",
        help="Run label recorded in the report (default: react-baseline).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help=(
            "Builds per case; >1 enables the determinism signal. Default 3 so "
            "variance is reported over more than one or two samples (trap 4)."
        ),
    )
    parser.add_argument(
        "--price-input",
        dest="price_input",
        type=float,
        default=None,
        help=(
            "USD price per 1M INPUT tokens for the run's model. With --price-output "
            "this prices any model (e.g. one not in eval.metrics.MODEL_PRICES) so the "
            "report carries a per-case cost_usd (trap 5)."
        ),
    )
    parser.add_argument(
        "--price-output",
        dest="price_output",
        type=float,
        default=None,
        help="USD price per 1M OUTPUT tokens for the run's model (see --price-input).",
    )
    parser.add_argument(
        "--max-transient-retries",
        dest="max_transient_retries",
        type=int,
        default=2,
        help=(
            "Re-run a case up to N times on a transient network/API failure before "
            "it counts (trap 1). 0 disables retries (default: 2)."
        ),
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
        # LIVE path only. The ReAct build reads provider/api_key/base_url/model
        # from the environment (builder.agents.llm), so creds kept solely in
        # ~/.config/vitro-crate/config.toml are bridged into os.environ first —
        # as the interactive CLI does via merge_with_env(). Without it a live run
        # raises "No LLM provider configured" despite valid creds on disk (#179).
        # The offline/mock path (injected agent_factory) skips this so its tests
        # stay config-free.
        merge_with_env(load_config())

        from eval.react_factory import make_react_agent_factory

        agent_factory = make_react_agent_factory(
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
    price_override: tuple[float, float] | None = None
    if args.price_input is not None and args.price_output is not None:
        price_override = (args.price_input, args.price_output)
    elif args.price_input is not None or args.price_output is not None:
        # One without the other cannot price a run — warn and fall back to the table.
        logger.warning("Ignoring --price-input/--price-output: BOTH are required to price a run.")

    try:
        report = run_eval(
            agent_factory,
            DEFAULT_CORPUS,
            repeats=args.repeats,
            label=args.label,
            profile_reader=profile_reader,
            price_override=price_override,
            max_transient_retries=args.max_transient_retries,
        )
    except RuntimeError as exc:
        # e.g. no LLM provider configured for the live factory.
        logger.error("Eval run failed: %s", exc)
        return 1

    write_report(report, out_path)
    summary = report.summary()
    # total_spend is every repeat of every case — what the API actually billed —
    # and cost_per_repeat is that divided by --repeats, so runs made with different
    # repeat counts stay comparable (#401). Naming both keeps either unmistakable.
    cost = summary["total_cost_usd"]
    per_repeat = summary["mean_cost_usd_per_repeat"]
    logger.info(
        "Done: success_rate=%.2f (all_repeats=%d/%d any_repeat=%d/%d) "
        "not_applicable=%d mean_tokens=%.0f determinism_rate=%.2f "
        "completed=%d cap_hit=%d error=%d total_spend=%s cost_per_repeat=%s -> %s",
        summary["success_rate"],
        # A gap between these two is flakiness — the thing #405 made visible.
        # The denominator is the cases the agent ATTEMPTED, which is what every
        # rate above is computed over; printing it against the corpus size would
        # read as failures on cases it reported not_applicable (#609).
        summary["num_success_all_repeats"],
        summary["num_cases_compared"],
        summary["num_success_any_repeat"],
        summary["num_cases_compared"],
        summary["num_not_applicable"],
        summary["mean_total_tokens"],
        summary["determinism_rate"],
        summary["num_completed"],
        summary["num_cap_hit"],
        summary["num_error"],
        f"${cost:.4f}" if cost is not None else "n/a",
        f"${per_repeat:.4f}" if per_repeat is not None else "n/a",
        out_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.exit(run_main())
