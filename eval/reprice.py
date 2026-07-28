"""Re-price an existing eval report from its recorded token counts (#335).

The A/B re-run records per-case ``input_tokens`` / ``output_tokens`` / ``model_name``
but leaves ``cost_usd`` null whenever the run's model is unpriced and no price
override was passed — exactly the gpt-5.6-luna paper re-run, whose price is
deliberately kept out of the public :data:`eval.metrics.MODEL_PRICES`. Re-running the
corpus purely to add a cost column would spend real tokens for no new signal, so this
module re-derives cost **offline** from the numbers already in the report: it reads
the labeled ndjson, recomputes each case's ``cost_usd`` under a supplied
``(input, output)`` price, and rewrites the summary's ``total_cost_usd`` to match.

The price is passed in (never hardcoded) so no work-issued model's pricing enters the
repo. Usage::

    python -m eval.reprice eval_reports/react-luna.ndjson \\
        --price-input 1.10 --price-output 6.60
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from eval.metrics import compute_cost

logger = logging.getLogger(__name__)


def reprice_records(
    records: list[dict[str, Any]], *, price_input: float, price_output: float
) -> list[dict[str, Any]]:
    """Return *records* with cost fields recomputed under the given price.

    Each ``record == "case"`` line is repriced across **every** repeat (#401): its
    ``input_tokens_per_repeat`` / ``output_tokens_per_repeat`` arrays are priced
    into ``cost_usd_per_repeat``, whose sum becomes the case's ``total_cost_usd``.
    ``cost_usd`` stays repeat #1's representative figure, matching the runner. The
    ``record == "summary"`` line's ``total_cost_usd`` becomes the sum of the case
    totals (``None`` only when no case had a known price), plus a
    ``mean_cost_usd_per_repeat`` derived from the summary's ``repeats``.

    A report written before #401 carries no per-repeat arrays; it reprices from
    ``input_tokens`` / ``output_tokens`` alone, exactly as before — one repeat is
    the only figure such a report holds. Ragged arrays (differing lengths) are
    treated the same way rather than zipped into an invented price.

    Every other field is left untouched, and the input list is **not** mutated.

    Args:
        records: Parsed ndjson report lines (case lines followed by one summary).
        price_input: USD price per 1M input tokens.
        price_output: USD price per 1M output tokens.

    Returns:
        A new list of new dicts with the cost fields recomputed.
    """
    override = (price_input, price_output)
    repriced: list[dict[str, Any]] = []
    case_totals: list[float] = []
    for rec in records:
        new = dict(rec)
        if new.get("record") == "case":
            model = new.get("model_name")
            ins = new.get("input_tokens_per_repeat") or []
            outs = new.get("output_tokens_per_repeat") or []
            if not (ins and len(ins) == len(outs)):
                # Pre-#401 or corrupt: repeat #1 is all we can honestly price.
                ins = [new.get("input_tokens", 0) or 0]
                outs = [new.get("output_tokens", 0) or 0]
            per_repeat = [
                compute_cost(int(i or 0), int(o or 0), model, price_override=override)
                for i, o in zip(ins, outs)
            ]
            known = [c for c in per_repeat if c is not None]
            case_total = sum(known) if known else None
            new["cost_usd_per_repeat"] = per_repeat
            new["cost_usd"] = per_repeat[0]
            new["total_cost_usd"] = case_total
            if case_total is not None:
                case_totals.append(case_total)
        repriced.append(new)

    total = sum(case_totals) if case_totals else None
    for new in repriced:
        if new.get("record") == "summary":
            new["total_cost_usd"] = total
            repeats = max(1, int(new.get("repeats", 1) or 1))
            new["mean_cost_usd_per_repeat"] = None if total is None else total / repeats
    return repriced


def reprice_file(
    path: str | Path,
    *,
    price_input: float,
    price_output: float,
    out: str | Path | None = None,
) -> Path:
    """Reprice the ndjson report at *path*, writing to *out* (or in place).

    Args:
        path: The labeled ndjson report to read.
        price_input: USD price per 1M input tokens.
        price_output: USD price per 1M output tokens.
        out: Destination path; defaults to overwriting *path*.

    Returns:
        The :class:`~pathlib.Path` written.
    """
    src = Path(path)
    records = [
        json.loads(line)
        for line in src.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    repriced = reprice_records(records, price_input=price_input, price_output=price_output)
    dest = Path(out) if out is not None else src
    dest.write_text(
        "\n".join(json.dumps(r, default=str) for r in repriced) + "\n", encoding="utf-8"
    )
    logger.info("Repriced %d records -> %s", len(repriced), dest)
    return dest


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser for ``python -m eval.reprice``."""
    parser = argparse.ArgumentParser(
        prog="python -m eval.reprice",
        description="Recompute cost_usd in an existing eval report from its recorded tokens.",
    )
    parser.add_argument("report", help="Path to the labeled ndjson report to reprice.")
    parser.add_argument(
        "--price-input",
        dest="price_input",
        type=float,
        required=True,
        help="USD price per 1M INPUT tokens for the report's model.",
    )
    parser.add_argument(
        "--price-output",
        dest="price_output",
        type=float,
        required=True,
        help="USD price per 1M OUTPUT tokens for the report's model.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output ndjson path (default: overwrite the input report).",
    )
    return parser


def reprice_main(argv: list[str] | None = None) -> int:
    """Reprice a report from the CLI; return a process exit code."""
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    reprice_file(
        args.report,
        price_input=args.price_input,
        price_output=args.price_output,
        out=args.out,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    sys.exit(reprice_main())
