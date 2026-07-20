"""Metric extraction for the evaluation harness.

Three orthogonal concerns live here, all pure and offline:

* :func:`mine_profile_metrics` — aggregate token / iteration / tool-call counts
  from parsed ``profile.ndjson`` records (the schema written by
  :class:`builder.tools.profiler.ProfilingLogger` and the LangGraph node wrappers
  in :mod:`builder.agents.react.agent_loop`).
* :func:`crate_graph_hash` — a stable content hash of the crate a state assembles
  to, used as the determinism signal across repeated runs.
* :func:`evaluate_success` — run ``build_and_validate`` and report the per-layer
  conformance map (the corpus success predicate's building block).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from builder.state import CrateState

logger = logging.getLogger(__name__)

# Auto-generated, wall-clock-derived node properties that ro-crate-py stamps onto
# the root Dataset at build time. They carry no agent-decision content, so they
# are stripped before hashing — otherwise two identical builds run seconds apart
# would falsely register as non-deterministic.
_VOLATILE_NODE_KEYS: frozenset[str] = frozenset({"datePublished", "dateModified"})


@dataclass(frozen=True)
class ProfileMetrics:
    """Token / iteration / tool-call counts mined from ``profile.ndjson``.

    Attributes:
        input_tokens: Sum of ``input_tokens`` across model ``node_end`` events.
        output_tokens: Sum of ``output_tokens`` across model ``node_end`` events.
        tool_calls: Number of ``tool_call`` events.
        iterations: The highest ``iteration`` counter observed (0 if none).
        model_name: The model that produced the run (the last non-empty
            ``model_name`` seen on a model ``node_end`` event), or ``None``. Used to
            price the run (:func:`compute_cost`).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    model_name: str | None = None

    @property
    def total_tokens(self) -> int:
        """Combined input + output token count."""
        return self.input_tokens + self.output_tokens


# USD per 1,000,000 tokens as ``(input, output)``. These are list prices at the
# time of writing and are **not authoritative** — extend the table or pass a
# ``price_override`` to :func:`compute_cost` for the run's actual model. A model
# absent here yields ``cost_usd = None`` (the harness never guesses a price). The
# gpt-5.6-luna model used for the paper re-run is deliberately NOT listed here (it
# is priced at run time via the ``--price-input`` / ``--price-output`` override) so
# no work-issued model's pricing lives in this public repo.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
}


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: str | None,
    *,
    price_override: tuple[float, float] | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """Return the USD cost of a run, or ``None`` when the model is unpriced.

    Args:
        input_tokens: Total prompt tokens for the run.
        output_tokens: Total completion tokens for the run.
        model_name: The run's model (e.g. mined via :func:`mine_profile_metrics`).
        price_override: ``(input_per_mtok, output_per_mtok)`` in USD. When given it
            prices *any* model and takes precedence over the table — the path the
            live gpt-5.6-luna re-run uses.
        prices: Price table to consult (defaults to :data:`MODEL_PRICES`).

    Returns:
        The cost in USD, or ``None`` when neither an override nor a table entry
        prices *model_name* (never a guessed ``0.0``).

    A dated/variant model name (e.g. ``"gpt-4o-2024-08-06"``) matches its base by
    **longest** prefix, so ``"gpt-4o-mini-*"`` is never mispriced as ``"gpt-4o"``.
    """
    if price_override is not None:
        in_price, out_price = price_override
    else:
        table = MODEL_PRICES if prices is None else prices
        key = (model_name or "").strip().lower()
        pair = table.get(key)
        if pair is None and key:
            for name in sorted(table, key=len, reverse=True):
                if key.startswith(name):
                    pair = table[name]
                    break
        if pair is None:
            return None
        in_price, out_price = pair
    return input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price


def _as_int(value: Any) -> int:
    """Coerce a possibly-missing / null token field to a non-negative int."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def mine_profile_metrics(records: list[dict[str, Any]]) -> ProfileMetrics:
    """Aggregate token, iteration, and tool-call counts from profile records.

    Token usage is summed over ``node_end`` events for the ``"model"`` node only
    (the ``"tools"`` node never carries token usage). The iteration count is the
    maximum ``iteration`` seen across all events — robust to the per-event
    ordering. ``records`` is the output of
    :func:`builder.tools.dashboard.read_profile`.

    Args:
        records: Parsed ``profile.ndjson`` event dicts.

    Returns:
        A :class:`ProfileMetrics` with the aggregated counts.
    """
    input_tokens = 0
    output_tokens = 0
    tool_calls = 0
    max_iteration = 0
    model_name: str | None = None

    for rec in records:
        event = rec.get("event")
        iteration = _as_int(rec.get("iteration"))
        if iteration > max_iteration:
            max_iteration = iteration
        if event == "tool_call":
            tool_calls += 1
        elif event == "node_end" and rec.get("node") == "model":
            input_tokens += _as_int(rec.get("input_tokens"))
            output_tokens += _as_int(rec.get("output_tokens"))
            # Keep the last non-empty model name seen — a run is single-model, and
            # this is robust to early events that omit it.
            name = rec.get("model_name")
            if name:
                model_name = str(name)

    return ProfileMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls=tool_calls,
        iterations=max_iteration,
        model_name=model_name,
    )


def crate_graph_hash(state: CrateState) -> str:
    """Return a stable SHA-256 hash of the crate *state* assembles to.

    The crate is assembled in memory (no disk write, no payload materialization)
    and its JSON-LD ``@graph`` is canonicalized — each node sorted by ``@id`` and
    the whole document dumped with ``sort_keys=True`` — before hashing. Two states
    that produce the same crate therefore hash identically regardless of insertion
    order, which is exactly the determinism signal the harness needs.

    Falls back to hashing the serialized :class:`CrateState` if assembly fails, so
    the determinism check degrades gracefully rather than raising into the runner.

    Args:
        state: The crate state to fingerprint.

    Returns:
        A 64-character hex SHA-256 digest.
    """
    try:
        from builder.tools.builder import assemble_crate

        crate = assemble_crate(
            state,
            output_dir=None,
            materialize_payload=False,
            include_all_scanned=False,
        )
        metadata_doc = crate.metadata.generate()
        graph = metadata_doc.get("@graph", metadata_doc)
        if isinstance(graph, list):
            # Drop volatile build-time stamps, then sort nodes by @id so neither
            # the build clock nor insertion order can perturb the hash.
            scrubbed = [
                {k: v for k, v in node.items() if k not in _VOLATILE_NODE_KEYS}
                if isinstance(node, dict)
                else node
                for node in graph
            ]
            canonical: Any = sorted(
                scrubbed,
                key=lambda node: json.dumps(
                    node.get("@id", "") if isinstance(node, dict) else node,
                    sort_keys=True,
                ),
            )
        else:
            canonical = graph
        payload = json.dumps(canonical, sort_keys=True, default=str)
    except Exception as exc:  # noqa: BLE001 — never let hashing abort the run
        logger.warning("crate_graph_hash: assembly failed (%s); hashing raw state", exc)
        payload = json.dumps(state.to_dict(), sort_keys=True, default=str)

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_success(
    state: CrateState,
    *,
    profile: str = "all",
    severity: str = "required",
) -> dict[str, Any]:
    """Run ``build_and_validate`` and return its conformance result.

    Args:
        state: The crate state produced by an agent build.
        profile: Validation scope (``"all"`` runs base -> isa -> tox).
        severity: Gate severity (``"required"`` is the conformance gate).

    Returns:
        ``{"ok": bool, "conformance": {layer: bool}, "issues": [...]}`` exactly as
        :func:`builder.tools.validation.build_and_validate` returns it.
    """
    from builder.tools.validation import build_and_validate

    return build_and_validate(state, severity=severity, profile=profile)
