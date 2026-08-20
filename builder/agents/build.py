"""The interactive hybrid build path (Issue #179 — completes the §14 loop).

This is the **interactive entrypoint** that joins the two halves of the §14.6
hybrid build loop into the end-to-end sequence a real user runs:

    Extract → Materialize → Assess → Auto-resolve   ──►   Guidance
    └──────────── run_pipeline (AUTOMATED) ─────────┘     run_guidance (HITL)

:func:`run_interactive_build` runs the **automated** deterministic pipeline
(:func:`builder.agents.pipeline.pipeline.run_pipeline`) and then — *only* when a REAL
interactive :class:`~builder.tools.hitl.HumanInterface` is present — runs the
HITL gap-resolution tail (:func:`builder.agents.pipeline.guidance.run_guidance`) so the
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

import enum
import inspect
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from builder.agents.llm import ModelOverrides
from builder.agents.progress_spinner import ProgressSpinner
from builder.tools.hitl import SYNTHETIC_ANSWER_NOTICE, answers_are_synthetic, is_interactive
from builder.tools.session import save_session

if TYPE_CHECKING:
    from builder.engine import AgentEngine
    from builder.state import CrateState
    from builder.tools.hitl import HumanInterface, InputResponse

logger = logging.getLogger(__name__)

__all__ = [
    "BuildMode",
    "run_build",
    "run_interactive_build",
    "format_guidance_summary",
    "format_gap_summary",
    "format_unresolved_compounds",
    "CrateExportError",
]

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


class BuildMode(enum.Enum):
    """Which build variant runs — the single switch the CLI and eval both flip.

    Both modes drive the same engine + toolbox via ``engine.run_tool``; only the
    orchestration differs (Issue #309). Both are first-class, permanently
    co-maintained variants (AGENTS.md §1, D15) — this is a selector, not a step
    toward removing either.

    The values are the eval harness's ``--arch`` strings, so it can map its CLI
    choice straight onto the enum with ``BuildMode(arch)``.
    """

    PIPELINE = "pipeline"  # deterministic, code-orchestrated (--interactive default)
    REACT = "react"  # LLM-orchestrated ReAct loop (--react)

    @classmethod
    def from_cli(cls, *, react: bool) -> BuildMode:
        """Map the CLI mode flags to a :class:`BuildMode`.

        ``--react`` selects :attr:`REACT`; the default is :attr:`PIPELINE`.
        """
        return cls.REACT if react else cls.PIPELINE


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
    overrides: ModelOverrides | None = None,
    resumed: bool = False,
) -> dict[str, Any]:
    """Run the automated pipeline, the HITL guidance tail, then export to disk.

    The engine MUST already be :meth:`~builder.engine.AgentEngine.initialize`-d.
    The automated pipeline always runs. The HITL guidance loop runs **iff** the
    engine's :class:`~builder.tools.hitl.HumanInterface` is interactive
    (:func:`builder.tools.hitl.is_interactive`) — a real user's frontend — so the
    non-interactive / simulated path (the A/B eval, batch, tests) runs the
    automated pipeline alone and ``run_guidance`` is never invoked. When guidance
    runs, a concise summary of its results is surfaced via *output*.

    **Headless gap summary (#179, Lane 5; #296).** On the non-interactive path
    there is no human to answer, so ``run_guidance`` cannot run — but the user is
    still shown the build's posture. After the pipeline + export, the build emits a
    single, non-blocking summary line (the open MUST count plus base/isa/tox
    conformance) via *output*. It is derived from the validation result the
    pipeline ALREADY computed (``pipeline_result``) — it does **not** re-run a
    fresh ``assess_gaps`` (whose ``severity="optional"`` SHACL+MIT+FAIR sweep is
    the #115 tox-pass bottleneck), so the summary adds negligible time to a build
    that already validated. Pure observability — it never prompts and never mutates
    state (D5).

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
            :func:`builder.agents.pipeline.pipeline.run_pipeline` (kept guidance-free).
        guidance_runner: Injected HITL runner; defaults to the real
            :func:`builder.agents.pipeline.guidance.run_guidance`.
        exporter: Injected on-disk writer; defaults to the real
            :func:`builder.tools.builder.export_crate` (crate assembly via
            ro-crate-py — never hand-rolled JSON-LD).
        output: Sink for the staged progress lines, the human-readable guidance
            summary, and the final crate path (e.g. ``print`` or a console
            writer). Defaults to a no-op.
        overrides: Caller-pinned provider/model/base URL for the bounded leaves
            (#399). ``None`` or an empty set means "resolve from the environment",
            the pre-existing behaviour. Threaded to an injected runner only when
            that runner accepts it, so narrower test doubles keep working.

    Returns:
        ``{"pipeline": <run_pipeline result>, "guidance": <run_guidance result or
        None>, "export": <export_crate result>}`` — ``guidance`` is ``None``
        exactly when the path was non-interactive and the tail was skipped;
        ``export`` is always the (successful) export result dict.

    Raises:
        CrateExportError: If the final on-disk export fails (surfaced first).
    """
    base_emit: OutputChannel = output or (lambda _msg: None)
    human: HumanInterface | None = getattr(engine, "human_interface", None)
    interactive = is_interactive(human)

    # Live progress spinner (#266): only on the REAL interactive path (an
    # interactive HumanInterface — the CLI's ConsoleHumanInterface), so the
    # headless / simulated path (the A/B eval, batch, tests) stays completely
    # silent — no spinner, no daemon thread, no stdout noise — and the built
    # @graph hash is unperturbed (the spinner is pure UI). When active it:
    #   * subscribes to engine.on_tool_event so the live region shows the
    #     currently-running deterministic tool (the pipeline runs tools via
    #     engine.run_tool, not LangChain, so this is the only per-tool signal), and
    #   * receives the existing #253 phase-progress strings via set_current.
    # The prior engine.on_tool_event hook is restored afterwards.
    # The pinned status footer is started BEFORE the spinner so the spinner can
    # delegate its line to the footer's activity row (one painter owns the bottom
    # of the terminal). Bound here so the finally can always tear it down.
    from builder.agents import ui

    footer: Any | None = None
    if interactive:
        footer = ui.make_status_footer(engine)
        footer.start()
    delegated = bool(footer is not None and footer.active)

    spinner: ProgressSpinner | None = (
        ProgressSpinner(
            tick_interval=0.12 if delegated else 0.5,
            activity_sink=footer.set_activity if delegated else None,
        )
        if interactive
        else None
    )
    emit = _spinner_emit(base_emit, spinner)
    prior_tool_event = engine.on_tool_event
    if spinner is not None:
        engine.on_tool_event = lambda tool, phase, args_str: (
            spinner.set_current(f"{tool}({args_str})" if args_str else tool)
            if phase == "start" else None
        )

    spinner_ctx = spinner if spinner is not None else nullcontext()
    try:
        # Shared UI chrome (#344), interactive-only so the headless / eval path
        # stays byte-identical: the resume summary before the spinner starts, then
        # the status bar + goodbye after it tears down (so neither is clobbered).
        # These are the SAME builder.agents.ui.print_* helpers the ReAct arm calls
        # — one implementation, no per-arm copy. The per-prompt status bar the
        # ReAct loop shows before every input is added to the guidance tail by
        # wrapping the human (see _run_build_body / _StatusBarHuman).
        if interactive:
            ui.print_resume_summary(engine, resumed=resumed)
        with spinner_ctx:
            result = _run_build_body(
                engine,
                human=human,
                interactive=interactive,
                emit=emit,
                pipeline_runner=pipeline_runner,
                guidance_runner=guidance_runner,
                exporter=exporter,
                overrides=overrides,
            )
        if interactive:
            ui.print_status_bar(engine, footer)
            ui.print_goodbye(engine)
        return result
    finally:
        # Restore the prior tool-event hook even if the build raised (#266).
        engine.on_tool_event = prior_tool_event
        if footer is not None:
            footer.stop()


def run_build(
    mode: BuildMode,
    engine: AgentEngine,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    output: OutputChannel | None = None,
    resumed: bool = False,
    initial_prompt: str | None = None,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """Dispatch a build to *mode*'s entrypoint — the single A/B switch (#309).

    :attr:`BuildMode.PIPELINE` runs the deterministic spine + HITL guidance tail
    (:func:`run_interactive_build`) and returns its result dict.
    :attr:`BuildMode.REACT` runs the LLM-orchestrated ReAct loop
    (:func:`builder.agents.react.agent_loop.run_interactive_agent`), which mutates
    ``engine.state`` in place and has no structured return, so this returns
    ``None``.

    ``provider`` / ``model`` / ``base_url`` reach **both** modes — the ReAct loop
    as explicit arguments, the pipeline as a :class:`~builder.agents.llm.ModelOverrides`
    threaded to its bounded leaves. They used to be documented and implemented as
    ReAct-only while the pipeline resolved its model from the environment, so a
    ``--model X`` A/B ran the two arms on two different models (#399). ``output``
    remains pipeline-only; the ReAct loop has no progress sink.

    ``resumed`` likewise reaches **both** modes. It is the caller's fact — in the
    CLI, ``bool(args.resume)`` — and must never be re-derived downstream from how
    populated ``engine.state`` looks: ``initialize(--input)`` scans files before
    either arm starts, so content-based inference reports every fresh run as a
    resume (#410).

    Args:
        mode: Which variant to run.
        engine: An initialized :class:`~builder.engine.AgentEngine`.
        provider: LLM provider override (auto-detected when ``None``).
        model: Model-name override.
        base_url: Custom OpenAI-compatible base URL.
        output: Progress/summary sink for the pipeline path (e.g. ``print``).
        resumed: True iff this run was started from a saved session
            (``--resume``), as opposed to a fresh scan.
        initial_prompt: ReAct-only opening message, so the loop starts working
            instead of blocking on stdin (#412). Ignored for ``PIPELINE``, which
            has no stdin gate — the spine runs unprompted.
        verbose: Show bounded diagnostics for ReAct model errors. Ignored
            by the pipeline mode.

    Returns:
        The pipeline result dict for :attr:`BuildMode.PIPELINE`; ``None`` for
        :attr:`BuildMode.REACT`.
    """
    if mode is BuildMode.REACT:
        from builder.agents.react.agent_loop import run_interactive_agent

        react_kwargs: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "resumed": resumed,
            "initial_prompt": initial_prompt,
        }
        # Preserve the original call shape for callers that omit the verbose
        # flag; the ReAct runner's default remains False.
        if verbose:
            react_kwargs["verbose"] = True
        run_interactive_agent(engine, **react_kwargs)
        return None

    from builder.agents.llm import ModelOverrides

    return run_interactive_build(
        engine,
        output=output,
        overrides=ModelOverrides(provider=provider, model=model, base_url=base_url),
        resumed=resumed,
    )


def _spinner_emit(base_emit: OutputChannel, spinner: ProgressSpinner | None) -> OutputChannel:
    """Wrap *base_emit* so each progress line also drives the spinner (#266).

    With no spinner this is exactly *base_emit* (a strict pass-through — the
    headless path is unchanged). With a spinner, every emitted line is fed into
    ``spinner.set_current`` (the live region shows the latest phase, e.g. the
    #253 ``Scaffolding ISA backbone…`` strings) **and** still forwarded to
    *base_emit* so persistent lines (``Scanning ✓``, the guidance summary, the
    final ``Crate written to <path>``) print above the spinner as before.
    """
    if spinner is None:
        return base_emit

    _spinner_only_prefixes = (
        "Scanning ",
        "Scaffolding ",
        "Extracting ",
        "Materializing ",
        "Validating ",
        "Resolving ",
    )

    def emit(msg: str) -> Any:
        spinner.set_current(msg)
        # The live spinner is the single display for active phases. Do not also
        # print the same phase as a permanent line immediately above it.
        if isinstance(msg, str) and msg.startswith(_spinner_only_prefixes):
            return None
        return base_emit(msg)

    return emit


class _StatusBarHuman:
    """A ``HumanInterface`` proxy that prints the shared status bar before each
    free-text prompt — the per-turn status line the ReAct loop shows before every
    input (#344). It calls the SAME ``ui.print_status_bar`` the ReAct loop calls
    (no per-arm copy); every other attribute delegates to the wrapped interface.

    Wrapped around the real interface for the interactive guidance tail only, so a
    fresh status bar (entities climbing, validation dots flipping green) heads each
    gap question as the user answers.
    """

    def __init__(self, inner: HumanInterface, engine: AgentEngine) -> None:
        self._inner = inner
        self._engine = engine

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        # The active spinner already carries the current phase/tool. Avoid
        # reprinting the full status bar before every guidance question; doing so
        # duplicates the live progress UI and makes a long guidance session noisy.
        return self._inner.request_input(prompt, field_type)

    def __getattr__(self, name: str) -> Any:
        # Everything not overridden above (present, is_interactive, is_done, …)
        # delegates to the wrapped interface. __getattr__ only fires for attributes
        # missing on the proxy, so _inner/_engine/request_input are unaffected.
        return getattr(self._inner, name)


def _run_build_body(
    engine: AgentEngine,
    *,
    human: HumanInterface | None,
    interactive: bool,
    emit: OutputChannel,
    pipeline_runner: PipelineRunner | None,
    guidance_runner: GuidanceRunner | None,
    exporter: Exporter | None,
    overrides: ModelOverrides | None = None,
) -> dict[str, Any]:
    """Run the pipeline → (guidance) → export → save sequence (#266 spinner body).

    Split out of :func:`run_interactive_build` so the spinner context manager can
    wrap the whole build (pipeline + guidance) with the wiring decisions made once
    by the caller. Behaviour is identical to the pre-#266 inline body.
    """
    # Progress (#241): the input is already scanned by engine.initialize(); lead
    # with a concise inventory line so the user sees the build picking up.
    scanned = len(getattr(engine.state, "scanned_files", []) or [])
    if scanned:
        emit(f"Scanning ✓ ({scanned} files)")

    pipeline_runner = pipeline_runner or _default_pipeline_runner()
    pipeline_result = _run_pipeline_with_progress(pipeline_runner, engine, emit, overrides)

    if not interactive:
        # Headless / simulated: run the automated pipeline ALONE so the A/B stays
        # a clean automated-vs-automated comparison. ``run_guidance`` is NEVER
        # invoked here — there is no human to answer — so the build is still
        # completed and written to disk. But the user is shown the build's posture:
        # a ONE-SHOT, non-blocking summary (open MUST count + base/isa/tox
        # conformance) derived from the validation the pipeline ALREADY computed
        # (#179, Lane 5; #296 — no second full SHACL sweep). Pure observability —
        # it never prompts and never mutates state (D5).
        logger.debug("Non-interactive build: skipping the HITL guidance tail")
        export_result = _export_crate_to_disk(engine, exporter, emit, human)
        emit(format_gap_summary(pipeline_result))
        _final_save(engine)
        return {
            "pipeline": pipeline_result,
            "guidance": None,
            "export": export_result,
        }

    guidance_runner = guidance_runner or _default_guidance_runner()
    emit("Resolving gaps…")
    # Wrap the human so a fresh status bar heads each guidance question — the same
    # per-prompt status line the ReAct loop shows, via the shared ui helper (#344).
    # human is non-None on this interactive path (is_interactive(None) is False).
    prompt_human = _StatusBarHuman(human, engine) if human is not None else human
    guidance_kwargs: dict[str, Any] = {}
    if overrides is not None and _accepts_kwarg(guidance_runner, "overrides"):
        guidance_kwargs["overrides"] = overrides
    guidance_result = guidance_runner(engine, prompt_human, **guidance_kwargs)

    emit(format_guidance_summary(guidance_result))
    # (#338) The interactive tail prints the GUIDANCE summary, which knows nothing
    # about the spine's compound retry — and the identifier gap behind it is
    # report-only, so the loop never surfaced it either. Re-state it here or the
    # interactive user is the one person who never learns the CAS is missing.
    unresolved = format_unresolved_compounds(pipeline_result)
    if unresolved:
        emit(f"  {unresolved}")

    # Export LAST so the guidance-enriched crate is what lands on disk (#233).
    export_result = _export_crate_to_disk(engine, exporter, emit, human)
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
    overrides: ModelOverrides | None = None,
) -> dict[str, Any]:
    """Call *pipeline_runner*, threading *emit* in as the spine's progress sink.

    The real :func:`builder.agents.pipeline.pipeline.run_pipeline` accepts keyword-only
    ``progress`` (#241) and ``overrides`` (#399) arguments. To stay
    backward-compatible with injected test runners whose signature is ``(engine)``
    only, each is introspected and passed **only** when accepted — otherwise the
    runner is called the legacy way. This keeps the spine's per-phase lines and the
    caller's model selection flowing through the real path while never breaking a
    narrower injected double.
    """
    kwargs: dict[str, Any] = {}
    if _accepts_kwarg(pipeline_runner, "progress"):
        kwargs["progress"] = emit
    if overrides is not None and _accepts_kwarg(pipeline_runner, "overrides"):
        kwargs["overrides"] = overrides
    return pipeline_runner(engine, **kwargs)


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
            logger.warning("Final session save failed: %s", result.get("error", "unknown error"))
    except Exception:  # noqa: BLE001 - persistence is best-effort; never break the build
        logger.exception("Unexpected error during final session save")


def _export_crate_to_disk(
    engine: AgentEngine,
    exporter: Exporter | None,
    emit: OutputChannel,
    human: HumanInterface | None = None,
) -> dict[str, Any]:
    """Write the built crate to disk and surface its absolute path (#233).

    Calls the injected (or real) ``export_crate`` over ``engine.state``; the
    destination is resolved by ``export_crate`` itself from
    ``state.metadata.output_path`` (the CLI-resolved path) with the session
    ``working_crate/`` fallback. On success the resolved ABSOLUTE crate path is
    emitted via *emit*. On failure the error is logged, emitted, and re-raised as
    :class:`CrateExportError` so the failure is never silently swallowed.

    When the answers came from a frontend that makes them up rather than asking a
    person (``--smoke-test``, :func:`builder.tools.hitl.answers_are_synthetic`), the
    notice is emitted immediately after the path. It is said HERE, at the one call
    site that prints the path, so the two lines cannot drift apart: a crate whose
    prose fields are placeholders must never be read from scrollback as a real one.
    Nothing is written INTO the crate — that would be fabricating metadata (D5).
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
    if answers_are_synthetic(human):
        emit(SYNTHETIC_ANSWER_NOTICE)
    return result


def format_unresolved_compounds(pipeline_result: dict[str, Any] | None) -> str | None:
    """One line naming the compounds that still have no identifier, or ``None`` (#338).

    ``run_pipeline`` re-attempts ``resolve_compound`` for every MolecularEntity
    that carries no CAS / PubChem CID / DTXSID and returns whatever is still
    missing under ``unresolved_compounds``. That list has to be *said*: the gap
    engine marks identifier gaps ``report-only``, and the guidance loop never
    draws a report-only gap, so without this line the crate ships CAS-less and the
    user is never told — the quiet failure #338 was reopened for.

    Returns ``None`` when nothing is unresolved, so both call sites (the headless
    :func:`format_gap_summary` and the interactive tail) can simply skip it rather
    than print a reassuring "0 unresolved".
    """
    names = (pipeline_result or {}).get("unresolved_compounds") or []
    if not names:
        return None
    return (
        f"unresolved identifiers: {len(names)} compound(s) with no authoritative "
        f"CAS/PubChem id ({', '.join(str(n) for n in names)}) — re-queried after "
        "the first miss and still not found; the crate ships without them"
    )


def format_guidance_summary(guidance_result: dict[str, Any] | None) -> str:
    """Render a concise, human-readable summary of a guidance run.

    Surfaces what the HITL tail did — gaps *resolved*, gaps *asked* about, gaps
    *remaining* per tier — and the final per-layer (``base`` / ``isa`` / ``tox``)
    conformance, so the user sees the build's state after guidance at a glance.
    A ``None`` result (guidance never ran, e.g. a non-interactive build) yields a
    short, non-crashing message.

    The tail's own token spend (#384) is appended when it is non-zero, so the
    figure the status bar has been climbing through is also stated once at the
    end. An offline / no-provider tail spends nothing and gets no line.
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
        (f"  conformance: base={_mark('base')} isa={_mark('isa')} tox={_mark('tox')}"),
        f"  rounds: {rounds}",
    ]

    usage = guidance_result.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    if tokens_in or tokens_out:
        total = int(usage.get("total_tokens") or (tokens_in + tokens_out))
        lines.append(f"  tokens: {tokens_in}→{tokens_out} ({total})")

    return "\n".join(lines)


def format_gap_summary(pipeline_result: dict[str, Any] | None) -> str:
    """Render a concise headless build-posture summary from the pipeline result.

    Used on the headless path (#179, Lane 5) — where ``run_guidance`` never runs —
    to report the build's posture in one line: the count of open **MUST** issues
    plus the final per-layer (``base`` / ``isa`` / ``tox``) conformance.

    **Reuse, don't re-validate (#296).** The values come straight from the
    validation result the deterministic pipeline ALREADY computed
    (``run_pipeline`` returns ``{"conformance", "issues", ...}`` from its
    required-severity fix loop), so this adds negligible time. It deliberately does
    NOT call ``assess_gaps`` — that sweeps the heaviest ``severity="optional"``
    SHACL pass plus MIT/FAIR (the #115 tox-pass bottleneck), which on the headless
    path is both a real per-build UX regression and a CI timeout. The pipeline only
    validates at REQUIRED severity, so SHOULD/MAY gaps are not computed on this fast
    path; the line reports that they were not assessed rather than fabricating a
    count (D5 — read-only reporting of real state). Wording is deliberately distinct
    from :func:`format_guidance_summary` (no "resolved"/"asked" verbs) since no
    interactive guidance ran.
    """
    result = pipeline_result or {}
    issues = result.get("issues") or []
    conformance = result.get("conformance") or {}

    # REQUIRED issues from the pipeline's required-severity fix loop -> open MUST.
    must_open = sum(
        1 for issue in issues if isinstance(issue, dict) and issue.get("severity") == "required"
    )

    def _mark(layer: str) -> str:
        return "pass" if conformance.get(layer) else "fail"

    lines = [
        "Headless build complete (no interactive guidance):",
        f"  open gaps: {must_open} MUST (SHOULD/MAY not assessed on the fast build)",
        (f"  conformance: base={_mark('base')} isa={_mark('isa')} tox={_mark('tox')}"),
    ]

    # Condition-table posture (#422): the central domain table staying empty used
    # to live only in a discarded reason string while this summary read clean.
    table = (result.get("materialized") or {}).get("condition_table")
    if isinstance(table, dict) and table:
        if not table.get("populated"):
            reason = str(table.get("reason") or "no reason recorded")
            lines.append(f"  condition table: not populated — {reason}")
        elif table.get("proposed"):
            lines.append(
                f"  condition table: proposed from crate entities "
                f"({table.get('rows')} row(s), awaiting confirmation)"
            )
        else:
            lines.append(f"  condition table: {table.get('rows')} row(s)")

    # (#338) An unresolved compound identifier is a reporting fact, not a
    # conformance failure, so it gets its own line rather than being folded into
    # the MUST count — but it is never omitted.
    unresolved = format_unresolved_compounds(result)
    if unresolved:
        lines.append(f"  {unresolved}")
    return "\n".join(lines)


def _default_pipeline_runner() -> PipelineRunner:
    """The real automated spine, imported lazily so this module stays light.

    Importing :mod:`builder.agents.pipeline.pipeline` is cheap and langchain-free (the
    leaf imports are themselves lazy), but deferring it keeps a test that injects
    its own runner fully independent of the spine.
    """
    from builder.agents.pipeline.pipeline import run_pipeline

    return run_pipeline


def _default_guidance_runner() -> GuidanceRunner:
    """The real HITL guidance loop, imported lazily.

    Deferred so the import only happens on the interactive path — a headless
    build never imports guidance, and a test injecting its own runner is
    independent of the guidance module.
    """
    from builder.agents.pipeline.guidance import run_guidance

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
