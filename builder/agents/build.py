"""The build's entrypoint: read the deposit's structure, then run the ReAct loop.

:func:`run_build` is what ``main.py --interactive`` and the eval harness call. It
reads the deposit's studies and assays from its folder tree once
(:func:`read_deposit_structure`, #852), names the build on the state (#772), and
hands the engine to the ReAct agent loop
(:func:`builder.agents.react.agent_loop.run_interactive_agent`), which drives the
toolbox, validates and exports from inside itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from builder.agents.llm import ModelOverrides, UsageSink, make_usage_logger
from builder.config import get_provider
from builder.tools.deposit_structure import (
    check_structure,
    describe_structure,
    fallback_structure,
    normalise_structure,
    tree_summary,
)

if TYPE_CHECKING:
    from builder.engine import AgentEngine

logger = logging.getLogger(__name__)

__all__ = ["ARCHITECTURE", "DEFAULT_INSTRUCTION", "read_deposit_structure", "run_build"]

# Default no-op output channel: discard. The CLI passes ``print`` (or a console
# writer); tests pass a list's ``append`` to capture the surfaced line.
OutputChannel = Callable[[str], Any]


# What a run started on an input folder is told first, unless --prompt says
# otherwise: a folder on the command line already says what to do (#846).
DEFAULT_INSTRUCTION = "Build the crate and validate it."

# The build named on the generator record (#772): the ReAct loop.
ARCHITECTURE = "react"


def derive_deposit_structure(
    tree: str,
    *,
    overrides: ModelOverrides | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, Any]:
    """Lazy shim over :func:`builder.agents.structure.derive_deposit_structure`.

    The leaf imports ``langchain_core`` at module load and this module must stay
    importable without it; the shim is also the monkeypatch target for tests.
    """
    from builder.agents.structure import derive_deposit_structure as _leaf

    return _leaf(tree, overrides=overrides, usage_sink=usage_sink)


def read_deposit_structure(
    engine: AgentEngine,
    *,
    overrides: ModelOverrides | None = None,
    output: OutputChannel | None = None,
) -> dict[str, Any] | None:
    """Read the deposit's studies and assays from its folder tree, once (#852).

    :func:`run_build` calls it before the loop starts. With no
    scanned files there is no deposit and nothing is said. A structure already
    on the state (a resumed session) is kept and only described again. Otherwise
    the tree is summarised, one drafter call labels it, and the labelling is
    stored when :func:`~builder.tools.deposit_structure.check_structure` accepts
    it; a failed check, a model error or no configured provider stores the
    one-study, one-assay fallback instead. Either way one line says what the
    deposit was read as. The call's token usage lands on the generator record
    like every leaf's.
    """
    state = engine.state
    if not state.scanned_files:
        return None
    if state.deposit_structure is None:
        state.deposit_structure = _derive_structure(engine, overrides)
    line = describe_structure(state.deposit_structure)
    if output is not None:
        output(line)
    else:
        logger.info("%s", line)
    return state.deposit_structure


def _derive_structure(engine: AgentEngine, overrides: ModelOverrides | None) -> dict[str, Any]:
    state = engine.state
    if get_provider() is None:
        return fallback_structure()
    approved = sorted(state.approved_scan_roots)
    root = approved[0] if approved else state.metadata.input_path or ""
    summary = tree_summary(state.scanned_files, input_root=root, documents=state.documents)
    totals = {"input_tokens": 0, "output_tokens": 0}
    try:
        labelling = derive_deposit_structure(
            summary, overrides=overrides, usage_sink=make_usage_logger(engine, totals)
        )
    except Exception as exc:  # noqa: BLE001 — the build goes on with the fallback
        logger.warning("Reading the deposit's structure failed: %s", exc)
        return fallback_structure()
    problems = check_structure(labelling, state.scanned_files, input_root=root)
    if problems:
        logger.warning("The deposit's structure was not accepted: %s", "; ".join(problems))
        return fallback_structure()
    return normalise_structure(labelling)


def run_build(
    engine: AgentEngine,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    output: OutputChannel | None = None,
    resumed: bool = False,
    initial_prompt: str | None = None,
    verbose: bool = False,
    interactive: bool = True,
) -> dict[str, Any] | None:
    """Read the deposit's structure, then run the ReAct loop over *engine*.

    The loop (:func:`builder.agents.react.agent_loop.run_interactive_agent`)
    mutates ``engine.state`` in place, exports from inside itself and returns
    ``{"stop_reason": ...}``.

    ``resumed`` is the caller's fact — in the CLI, ``bool(args.resume)`` — and
    must never be re-derived from how populated ``engine.state`` looks:
    ``initialize(--input)`` scans files before the loop starts, so content-based
    inference reports every fresh run as a resume (#410).

    Args:
        engine: An initialized :class:`~builder.engine.AgentEngine`.
        provider: LLM provider override (auto-detected when ``None``).
        model: Model-name override.
        base_url: Custom OpenAI-compatible base URL.
        output: Sink for the line that says what the deposit was read as.
        resumed: True iff this run was started from a saved session
            (``--resume``), as opposed to a fresh scan.
        initial_prompt: The opening message, so the loop starts working instead
            of blocking on stdin (#412).
        verbose: Show bounded diagnostics for model errors.
        interactive: False for a run nobody answers (the eval): an engine
            defaults to a :class:`~builder.tools.hitl.SimulatedHumanInterface`, so
            the loop cannot tell a batch session from a person's.

    Returns:
        The loop's ``{"stop_reason": ...}``.
    """
    from builder.agents.react.agent_loop import run_interactive_agent

    overrides = ModelOverrides(provider=provider, model=model, base_url=base_url)
    read_deposit_structure(engine, overrides=overrides, output=output)
    # The loop exports from inside itself, so the build must be named on the state
    # before it starts (#772).
    engine.state.generator.architecture = ARCHITECTURE
    react_kwargs: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "resumed": resumed,
        "initial_prompt": initial_prompt,
    }
    # Only pass the caller's fact when it is not the default, so an injected
    # narrow double still works.
    if not interactive:
        react_kwargs["interactive"] = False
    # Preserve the original call shape for callers that omit the verbose flag;
    # the runner's default remains False.
    if verbose:
        react_kwargs["verbose"] = True
    return run_interactive_agent(engine, **react_kwargs)
