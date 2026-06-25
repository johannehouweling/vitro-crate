"""The interactive hybrid build path (Issue #179 — completes the §14 loop).

This is the **interactive entrypoint** that joins the two halves of the §14.6
hybrid build loop into the end-to-end sequence a real user runs:

    Extract → Materialize → Assess → Auto-resolve   ──►   Guidance
    └──────────── run_pipeline (AUTOMATED) ─────────┘     run_guidance (HITL)

:func:`run_interactive_build` runs the **automated** deterministic pipeline
(:func:`builder.agents.pipeline.run_pipeline`) and then — *only* when a REAL
interactive :class:`~builder.tools.hitl.HumanInterface` is present — runs the
HITL gap-resolution tail (:func:`builder.agents.guidance.run_guidance`) so the
user is guided through the gaps the deterministic path could not close on its own.

**Why the split is deliberate.** ``run_pipeline`` stays *guidance-free*: it is the
automated build the A/B eval drives non-interactively (``--arch pipeline``), so it
must be a clean automated-vs-automated comparison with the ReAct path. Guidance is
HITL and would block on a real user, so it belongs only here, in the interactive
tail, gated on :func:`builder.tools.hitl.is_interactive`. The headless
:class:`~builder.tools.hitl.SimulatedHumanInterface` (the eval, batch runs, tests)
reports ``is_interactive == False``, so this entrypoint then degrades to *exactly*
``run_pipeline`` alone — guidance is never invoked behind a simulated human.

Both the pipeline and guidance runners are **injected** (defaulting to the real
functions) so the wiring is unit-testable with no SHACL / no LLM / no network.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from builder.tools.hitl import is_interactive
from builder.tools.session import save_session

if TYPE_CHECKING:
    from builder.engine import AgentEngine
    from builder.state import CrateState
    from builder.tools.hitl import HumanInterface

logger = logging.getLogger(__name__)

__all__ = ["run_interactive_build", "format_guidance_summary", "CrateExportError"]

# A pipeline_runner runs the automated deterministic spine once over an engine,
# mutating engine.state and returning the spine's result dict. The real
# ``run_pipeline`` additionally accepts a keyword-only ``progress`` sink (#241),
# threaded in only when the runner's signature accepts it (see
# ``_run_pipeline_with_progress``), so the alias is the open ``(...)`` form to
# admit both shapes. A guidance_runner runs the HITL gap-resolution loop over the
# engine + a human, returning its summary dict. An exporter writes the assembled
# crate to disk and returns the export result dict (``{success, crate_path,
# error}``). All three are injected so the wiring is testable without SHACL / LLM /
# disk.
PipelineRunner = Callable[..., dict[str, Any]]
GuidanceRunner = Callable[..., dict[str, Any]]
Exporter = Callable[..., dict[str, Any]]

# Default no-op output channel: discard. The CLI passes ``print`` (or a console
# writer); tests pass a list's ``append`` to capture the surfaced summary.
OutputChannel = Callable[[str], Any]


class CrateExportError(RuntimeError):
    """Raised when the final deterministic crate export fails (#233).

    The interactive build's last step writes the enriched crate to disk. A
    failure here means the user gets nothing on disk, so it must NOT be silently
    swallowed: it is logged, surfaced via the ``output`` channel, and raised so
    the CLI can signal a non-zero exit.
    """


def run_interactive_build(
    engine: AgentEngine,
    *,
    pipeline_runner: PipelineRunner | None = None,
    guidance_runner: GuidanceRunner | None = None,
    exporter: Exporter | None = None,
    output: OutputChannel | None = None,
) -> dict[str, Any]:
    """Run the automated pipeline, the HITL guidance tail, then export to disk.

    The engine MUST already be :meth:`~builder.engine.AgentEngine.initialize`-d.
    The automated pipeline always runs. The HITL guidance loop runs **iff** the
    engine's :class:`~builder.tools.hitl.HumanInterface` is interactive
    (:func:`builder.tools.hitl.is_interactive`) — a real user's frontend — so the
    non-interactive / simulated path (the A/B eval, batch, tests) runs the
    automated pipeline alone and ``run_guidance`` is never invoked. When guidance
    runs, a concise summary of its results is surfaced via *output*.

    Finally — **after** guidance, so the *enriched* crate is what lands — the
    deterministic on-disk export (:func:`builder.tools.builder.export_crate`)
    writes ``ro-crate-metadata.json`` to ``state.metadata.output_path`` (the
    CLI-resolved destination) and the resolved ABSOLUTE crate path is surfaced via
    *output*. This is the missing final step of the pipeline path (#233): before
    it, the default interactive build built + validated in memory and exited
    without writing anything. Export runs on **every** completed build (interactive
    *and* headless). An export failure is logged, surfaced, and re-raised as
    :class:`CrateExportError` — it is never silently swallowed.

    **Progress (#241).** The build surfaces staged progress through *output* — a
    "Scanning ✓ (N files)" line up front, the spine's own per-phase lines threaded
    in via a progress callback (Scaffolding / Materializing / Validating /
    Resolving), and the final "Crate written to <abs path>" line — so the
    ~tens-of-seconds deterministic spine no longer looks frozen. It is a strict
    no-op when *output* is the default (non-interactive / tests).

    **Persistence (#242).** The spine persists ``CrateState`` to
    ``sessions/<id>/crate_state.json`` at each phase boundary (incremental saves
    drive a concurrent ``--dashboard``'s live refresh), and this entrypoint does a
    FINAL ``save_session(state, always_write=True)`` after guidance + export so a
    populated overview + a resumable session are always written — on BOTH the
    interactive and the headless path.

    Args:
        engine: An initialized engine. Its ``human_interface`` decides whether
            the guidance tail runs; ``run_guidance`` mutates ``engine.state`` in
            place through the existing tools (never hand-rolled JSON-LD).
        pipeline_runner: Injected automated-build runner; defaults to the real
            :func:`builder.agents.pipeline.run_pipeline` (kept guidance-free).
        guidance_runner: Injected HITL runner; defaults to the real
            :func:`builder.agents.guidance.run_guidance`.
        exporter: Injected on-disk writer; defaults to the real
            :func:`builder.tools.builder.export_crate` (crate assembly via
            ro-crate-py — never hand-rolled JSON-LD).
        output: Sink for the staged progress lines, the human-readable guidance
            summary, and the final crate path (e.g. ``print`` or a console
            writer). Defaults to a no-op.

    Returns:
        ``{"pipeline": <run_pipeline result>, "guidance": <run_guidance result or
        None>, "export": <export_crate result>}`` — ``guidance`` is ``None``
        exactly when the path was non-interactive and the tail was skipped;
        ``export`` is always the (successful) export result dict.

    Raises:
        CrateExportError: If the final on-disk export fails (surfaced first).
    """
    emit: OutputChannel = output or (lambda _msg: None)

    # Progress (#241): the input is already scanned by engine.initialize(); lead
    # with a concise inventory line so the user sees the build picking up.
    scanned = len(getattr(engine.state, "scanned_files", []) or [])
    if scanned:
        emit(f"Scanning ✓ ({scanned} files)")

    pipeline_runner = pipeline_runner or _default_pipeline_runner()
    pipeline_result = _run_pipeline_with_progress(pipeline_runner, engine, emit)

    human: HumanInterface | None = getattr(engine, "human_interface", None)
    if not is_interactive(human):
        # Headless / simulated: run the automated pipeline ALONE so the A/B stays
        # a clean automated-vs-automated comparison. No guidance, no summary —
        # but the build is still completed, so it must still be written to disk.
        logger.debug("Non-interactive build: skipping the HITL guidance tail")
        export_result = _export_crate_to_disk(engine, exporter, emit)
        _final_save(engine)
        return {
            "pipeline": pipeline_result,
            "guidance": None,
            "export": export_result,
        }

    guidance_runner = guidance_runner or _default_guidance_runner()
    emit("Resolving gaps…")
    guidance_result = guidance_runner(engine, human)

    emit(format_guidance_summary(guidance_result))

    # Export LAST so the guidance-enriched crate is what lands on disk (#233).
    export_result = _export_crate_to_disk(engine, exporter, emit)
    # FINAL persist (#242): always_write guarantees a populated overview + resume.
    _final_save(engine)

    return {
        "pipeline": pipeline_result,
        "guidance": guidance_result,
        "export": export_result,
    }


def _run_pipeline_with_progress(
    pipeline_runner: PipelineRunner,
    engine: AgentEngine,
    emit: OutputChannel,
) -> dict[str, Any]:
    """Call *pipeline_runner*, threading *emit* in as the spine's progress sink.

    The real :func:`builder.agents.pipeline.run_pipeline` accepts a keyword-only
    ``progress`` callback (#241). To stay backward-compatible with injected test
    runners whose signature is ``(engine)`` only, we introspect the runner and pass
    ``progress`` **only** when it is accepted — otherwise the runner is called the
    legacy way. This keeps the spine's per-phase lines flowing to the user through
    the real path while never breaking a narrower injected double.
    """
    if _accepts_kwarg(pipeline_runner, "progress"):
        return pipeline_runner(engine, progress=emit)
    return pipeline_runner(engine)


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    """Return True if *func* accepts a keyword argument *name* (or ``**kwargs``)."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _final_save(engine: AgentEngine) -> None:
    """Final, forced CrateState persist (#242) — never abort the build on failure.

    ``always_write=True`` bypasses the change-detection skip so the session is
    guaranteed written even when an incremental phase save already wrote identical
    content, ensuring the dashboard's CrateState overview is populated and the
    session is resumable. A save failure is logged, not raised — the crate is
    already on disk from the export step, so the build still succeeded.
    """
    try:
        result = save_session(engine.state, always_write=True)
        if not result.get("success", True):
            logger.warning(
                "Final session save failed: %s", result.get("error", "unknown error")
            )
    except Exception:  # noqa: BLE001 - persistence is best-effort; never break the build
        logger.exception("Unexpected error during final session save")


def _export_crate_to_disk(
    engine: AgentEngine,
    exporter: Exporter | None,
    emit: OutputChannel,
) -> dict[str, Any]:
    """Write the built crate to disk and surface its absolute path (#233).

    Calls the injected (or real) ``export_crate`` over ``engine.state``; the
    destination is resolved by ``export_crate`` itself from
    ``state.metadata.output_path`` (the CLI-resolved path) with the session
    ``working_crate/`` fallback. On success the resolved ABSOLUTE crate path is
    emitted via *emit*. On failure the error is logged, emitted, and re-raised as
    :class:`CrateExportError` so the failure is never silently swallowed.
    """
    exporter = exporter or _default_exporter()
    state: CrateState = engine.state
    result = exporter(state)

    if not result.get("success"):
        error = result.get("error") or "unknown error"
        crate_path = result.get("crate_path")
        logger.error("Crate export failed (%s): %s", crate_path, error)
        emit(f"Crate export FAILED: {error}")
        raise CrateExportError(error)

    abs_path = Path(result["crate_path"]).resolve()
    logger.info("Crate written to %s", abs_path)
    emit(f"Crate written to: {abs_path}")
    return result


def format_guidance_summary(guidance_result: dict[str, Any] | None) -> str:
    """Render a concise, human-readable summary of a guidance run.

    Surfaces what the HITL tail did — gaps *resolved*, gaps *asked* about, gaps
    *remaining* per tier — and the final per-layer (``base`` / ``isa`` / ``tox``)
    conformance, so the user sees the build's state after guidance at a glance.
    A ``None`` result (guidance never ran, e.g. a non-interactive build) yields a
    short, non-crashing message.
    """
    if not guidance_result:
        return "No interactive guidance ran (headless build)."

    resolved = guidance_result.get("resolved") or []
    asked = guidance_result.get("asked") or []
    remaining = guidance_result.get("remaining_gaps") or {}
    conformance = guidance_result.get("conformance") or {}
    rounds = guidance_result.get("rounds", 0)

    must = remaining.get("must_open", 0)
    should = remaining.get("should_open", 0)
    may = remaining.get("may_open", 0)

    def _mark(layer: str) -> str:
        return "pass" if conformance.get(layer) else "fail"

    lines = [
        "Guidance complete:",
        f"  resolved: {len(resolved)} gap(s)",
        f"  asked:    {len(asked)} gap(s)",
        f"  remaining gaps: {must} MUST / {should} SHOULD / {may} MAY",
        (
            f"  conformance: base={_mark('base')} "
            f"isa={_mark('isa')} tox={_mark('tox')}"
        ),
        f"  rounds: {rounds}",
    ]
    return "\n".join(lines)


def _default_pipeline_runner() -> PipelineRunner:
    """The real automated spine, imported lazily so this module stays light.

    Importing :mod:`builder.agents.pipeline` is cheap and langchain-free (the
    leaf imports are themselves lazy), but deferring it keeps a test that injects
    its own runner fully independent of the spine.
    """
    from builder.agents.pipeline import run_pipeline

    return run_pipeline


def _default_guidance_runner() -> GuidanceRunner:
    """The real HITL guidance loop, imported lazily.

    Deferred so the import only happens on the interactive path — a headless
    build never imports guidance, and a test injecting its own runner is
    independent of the guidance module.
    """
    from builder.agents.guidance import run_guidance

    return run_guidance


def _default_exporter() -> Exporter:
    """The real on-disk crate writer, imported lazily.

    :func:`builder.tools.builder.export_crate` assembles the crate via
    ro-crate-py and writes ``ro-crate-metadata.json`` (never hand-rolled
    JSON-LD). Deferred so a test injecting its own exporter stays independent of
    the writer / ro-crate-py.
    """
    from builder.tools.builder import export_crate

    return export_crate
