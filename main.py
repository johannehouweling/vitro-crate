"""ISA-Tox RO-Crate Builder — Main entry point.

Usage:
    uv run python -m main [--input <path>] [--output <path>] [--resume <session_id>]
    uv run python -m main --interactive [--input <path>] [--provider openai|anthropic]
    uv run python -m main --graph [--input <crate-or-metadata.json>] [--resume <session_id>]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from builder.engine import AgentEngine

logger = logging.getLogger(__name__)


def _default_output_dir(input_path: str | Path, output_root: str | Path = "output") -> Path:
    """Resolve the default on-disk crate destination when no ``--output`` is given.

    Lands under ``output/<name>_crate`` and versions re-runs as ``_v2`` / ``_v3`` …
    (the first build has no suffix), matching the existing ``output/`` layout and
    never clobbering a previous build (#315). ``<name>`` is the input folder name
    with a trailing ``_extracted`` stripped, so an extracted archive
    ``S-VHPS26_extracted`` maps to ``S-VHPS26_crate``.

    This keeps builds out of a curated input tree (previously the crate was written
    to an ``<input>-ro-crate`` sibling, which polluted ``input/raw/``).
    """
    root = Path(output_root)
    name = Path(input_path).name
    suffix = "_extracted"
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    base = root / f"{name}_crate"
    if not base.exists():
        return base
    version = 2
    while (root / f"{name}_crate_v{version}").exists():
        version += 1
    return root / f"{name}_crate_v{version}"


def _next_output_version(path: str | Path) -> Path:
    """Return a sibling crate path that will not overwrite an earlier export."""
    target = Path(path)
    if not target.exists():
        return target
    stem = target.name
    match = re.match(r"^(.*)_v(\d+)$", stem)
    if match:
        base, version = match.group(1), int(match.group(2)) + 1
    else:
        base, version = stem, 2
    candidate = target.with_name(f"{base}_v{version}")
    while candidate.exists():
        version += 1
        candidate = target.with_name(f"{base}_v{version}")
    return candidate


def setup_logging(verbose: int = 0, interactive: bool = False) -> None:
    """Configure logging for the builder.

    Levels:
        0 = WARNING (only warnings and errors)
        1 = INFO    (normal progress)
        2 = DEBUG   (verbose/tool internals)

    The interactive build drives a multi-step deterministic pipeline whose
    progress is logged at INFO; with the default WARNING level the run looks
    dead. When *interactive* is set and the user has not requested any extra
    verbosity (``verbose == 0``), the effective level is raised to INFO so that
    pipeline progress is visible. A user-requested higher verbosity (``-v`` /
    ``-vv``) is never downgraded.

    The interactive INFO bump is for *our* progress lines only. Promoting the
    root logger to INFO would otherwise unleash the noisy third-party libraries
    (notably ``httpx``, which logs every request at INFO), drowning each guidance
    question under per-request spam. So when the bump -- and only the bump --
    raises the level to INFO, the noisy third-party loggers are pinned to WARNING
    while ``builder.*`` / ``__main__`` stay at INFO. A user who explicitly asked
    for ``-v`` / ``-vv`` opted into the verbosity and those loggers are left
    untouched.
    """
    level_map = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
    level = level_map.get(verbose, logging.WARNING)
    interactive_bump = interactive and verbose == 0
    if interactive_bump:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # basicConfig is a no-op once the root logger has handlers (e.g. a prior
    # call in the same process), so set the level explicitly to honour the
    # interactive bump and the requested verbosity deterministically.
    logging.getLogger().setLevel(level)
    # The interactive bump promotes the root logger to INFO; keep that for our
    # own loggers but silence the per-request chatter from third-party HTTP/SDK
    # libraries (httpx logs every "HTTP Request: POST .../chat/completions" at
    # INFO, burying each guidance question). Only do this for the bump -- never
    # when the user explicitly requested verbosity via -v / -vv.
    if interactive_bump:
        for noisy in ("httpx", "httpcore", "openai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # urllib3 goes further, to ERROR. It logs a WARNING per retry ATTEMPT,
        # and a lookup service having a bad afternoon then prints one line per
        # attempt per chemical — 22 compounds resolving in parallel produced
        # ~60 lines that tore through the prompt box and the pinned footer.
        # Those retries are an implementation detail we already handle: the
        # request is retried, and a genuine failure is reported by our own
        # lookup layer with the chemical's name attached, which is the message
        # actually worth reading. An explicit level here also OVERRIDES the
        # root-logger mute the ReAct loop applies during a turn, so without
        # this the noise arrives at exactly the wrong moment.
        logging.getLogger("urllib3").setLevel(logging.ERROR)

        # Records that DO get through are rendered as quiet, deduplicated
        # notices instead of raw stderr lines. The timestamp and dotted logger
        # path are noise to a user mid-session, plain white reads as the
        # assistant speaking, and an unchanging warning re-fired on every build
        # buries the conversation — one session printed the same four records
        # forty-four times. `-v` / `-vv` keeps the standard stream handler, on
        # the assumption that someone asking for verbosity wants the machinery.
        try:
            from builder.agents.ui import install_notice_handler

            # INFO, not WARNING: the interactive bump above already promoted the
            # root logger to INFO, so this shows exactly the records that print
            # today — just quietly, and once each — rather than hiding pipeline
            # progress that the bump exists to surface.
            install_notice_handler(level=logging.INFO)
        except Exception:  # noqa: BLE001 — never let chrome stop the session
            logger.debug("Could not install the notice handler", exc_info=True)


def _smoke_test_minutes(raw: str) -> float:
    """Parse ``--smoke-test MINUTES`` into a positive number of minutes.

    Rejects zero and negatives rather than accepting them as "stop immediately".
    Both would also make ``args.smoke_test`` FALSY, so the mode would silently
    not engage at all — a `--smoke-test 0` that quietly runs an ordinary
    interactive build, waiting for a person who is not there, is exactly the
    silent misfire this flag exists to make impossible.
    """
    try:
        minutes = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of minutes, got {raw!r}") from None
    if minutes <= 0:
        raise argparse.ArgumentTypeError(
            f"a smoke test needs a positive number of minutes, got {minutes:g}"
        )
    return minutes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="ISA-Tox RO-Crate Builder")
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default=None,
        help="Path to input directory containing research data",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Path for the output RO-Crate directory",
    )
    parser.add_argument(
        "--resume",
        "--session",
        "-r",
        type=str,
        default=None,
        help="Session ID to resume (e.g. 20260620_192039)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase verbosity (-v = INFO, -vv = DEBUG)",
    )
    parser.add_argument(
        "--interactive",
        "-I",
        action="store_true",
        help="Run in interactive build mode (deterministic pipeline + HITL "
        "guidance tail; requires LangChain extra + API key)",
    )
    parser.add_argument(
        "--react",
        action="store_true",
        help="With --interactive, use the ReAct agent loop (the LLM orchestrates "
        "the tool calls) instead of the default deterministic pipeline + guidance "
        "build (a supported alternative; see AGENTS.md §14)",
    )
    parser.add_argument(
        "--smoke-test",
        nargs="?",
        const=True,
        default=False,
        type=_smoke_test_minutes,
        metavar="MINUTES",
        help="Drive the interactive build with NOBODY at the keyboard: every "
        'choice prompt confirms its pre-selected option and every open field is '
        'answered "yes, continue". Implies --interactive; works on both arms. '
        "Takes an OPTIONAL wall-clock budget in minutes (--smoke-test 20): the "
        "run winds down at its next question once the time is spent and exports "
        "what it has. Without one it drives a few turns and stops. For TESTING "
        "the HITL path end to end — the crate it produces holds synthesised "
        "answers and is not curated metadata.",
    )
    parser.add_argument(
        "--prompt",
        "-P",
        help="With --interactive --react, an opening instruction (e.g. "
        "'build the crate') that starts the agent working immediately instead of "
        "waiting for you to type one. The session stays interactive afterwards. "
        "Ignored by the default pipeline build, which runs unprompted.",
    )
    parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default=None,
        choices=["openai", "anthropic"],
        help="LLM provider for interactive mode (auto-detected from env if omitted)",
    )
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default=None,
        help="Model name (e.g. gpt-4o-mini, llama3.2, claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--api-base",
        "-b",
        type=str,
        default=None,
        help="API base URL for OpenAI-compatible providers "
        "(e.g. http://localhost:11434/v1 for Ollama). "
        "Also read from VITRO_OPENAI_BASE_URL or OPENAI_BASE_URL env var.",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Run the interactive setup wizard to configure LLM provider",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Show current LLM configuration and exit",
    )
    parser.add_argument(
        "--dashboard",
        "-D",
        action="store_true",
        help="Show the profiler dashboard for the latest session (or --resume session)",
    )
    parser.add_argument(
        "--graph",
        "-g",
        action="store_true",
        help="Write the crate's interactive entity explorer and open it. Source "
        "is --input (a crate dir or ro-crate-metadata.json) or a session "
        "(--resume <id>, else the latest). Needs no LLM config.",
    )
    parser.add_argument(
        "--view",
        # The views are toggles inside the page now, so this picks the one it
        # opens on. "crate" and "provenance" are names this flag shipped under
        # and stay accepted: renaming a CLI value silently breaks every script
        # that passes it.
        choices=["researcher", "crate", "labprocesses", "provenance"],
        default="researcher",
        help="--graph: which view the explorer opens on — 'researcher' (the "
        "experiment, without the packaging; default), 'crate' (every entity), "
        "or 'labprocesses' (the derivation chain; 'provenance' is the same view "
        "under its old name). Every view is a toggle in the page itself.",
    )
    parser.add_argument(
        "--graph-out",
        type=str,
        default=None,
        help="Path for the --graph HTML file (default: a temp file)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="With --graph, write the file but do not open a browser",
    )
    args = parser.parse_args(argv)
    # --smoke-test IMPLIES --interactive rather than requiring it. Its whole job is
    # to drive the interactive build unattended, and a batch run never prompts — so
    # taking the flag without --interactive and doing nothing HITL-shaped would be
    # the silent misfire the mode exists to make visible. Normalised here, once, so
    # every downstream reader (the logging bump, the engine wiring, the build
    # dispatch) sees a single consistent `interactive` fact.
    if args.smoke_test:
        args.interactive = True
    return args


def _run_setup_wizard() -> bool:
    """Run the interactive config wizard.  Returns True if successful."""
    from builder.config import interactive_setup

    return interactive_setup()


def _show_config() -> None:
    """Print current configuration state."""
    from builder.config import (
        describe_config,
        is_configured,
        load_config,
        merge_with_env,
    )

    cfg = load_config()
    merge_with_env(cfg)
    print(describe_config())
    if not is_configured():
        print()
        print("No LLM provider is fully configured.")
        print("Run with --configure to set one up interactively,")
        print("or set VITRO_OPENAI_API_KEY / VITRO_ANTHROPIC_API_KEY env vars.")
    print()


def _ensure_configured() -> bool:
    """Check config; if missing, offer to run the wizard.

    Returns True if the user is ready to proceed (configured or skipped).
    """
    from builder.config import is_configured, load_config, merge_with_env

    # Load config file and merge into env (env vars take priority)
    cfg = load_config()
    merge_with_env(cfg)

    if is_configured():
        return True

    print()
    print("No LLM provider is configured yet.")
    print()
    try:
        answer = input("Would you like to set one up now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer in ("", "y", "yes"):
        return _run_setup_wizard()

    print("Skipping setup.  You can configure later with --configure.")
    return False


# The `--view` vocabulary, mapped onto the explorer's own view keys. "crate" and
# "provenance" are the names the flag shipped under; they still resolve.
_EXPLORER_VIEW_BY_FLAG = {
    "researcher": "researcher",
    "crate": "all",
    "labprocesses": "processes",
    "provenance": "processes",
}


def _resolve_graph_source(args: argparse.Namespace) -> dict[str, Any] | None:
    """Resolve the metadata ``@graph`` to render for ``--graph``.

    Priority:
        1. ``--input`` pointing at a ``ro-crate-metadata.json`` or a crate
           directory containing one — rendered straight from disk (no session,
           no LLM).
        2. A session: ``--resume <id>`` if given, otherwise the latest session;
           its CrateState is assembled in memory (no payload written) and its
           generated metadata document is returned.

    Returns the metadata document (with an ``@graph`` key), or ``None`` if no
    crate could be resolved.
    """
    if args.input:
        path = Path(args.input)
        meta = path / "ro-crate-metadata.json" if path.is_dir() else path
        if meta.is_file():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Could not read crate metadata from %s: %s", meta, exc)
                return None
        return None

    from builder.tools.session import list_sessions, load_session

    session_id = args.resume
    if session_id is None:
        sessions = list_sessions()
        if not sessions:
            return None
        # list_sessions() sorts ascending by id (timestamp) → last is latest.
        session_id = sessions[-1]["session_id"]

    state = load_session(session_id)
    if state is None:
        return None

    from builder.tools.builder import assemble_crate

    crate = assemble_crate(state, materialize_payload=False)
    return crate.metadata.generate()


def _run_graph(args: argparse.Namespace) -> int:
    """Write the crate's entity explorer and open it. 0 on success, 1 otherwise.

    The page is the section the maturity report embeds, in the report's own
    shell — one explorer rendered in two places. It is self-contained, so the
    file works from anywhere it is copied to; before #618 this mode emitted
    Mermaid and its HTML fetched mermaid.js from a CDN, which made the artifact
    meant for looking at the one that failed without a network.
    """
    from builder.writers.entity_explorer import render_explorer_page

    source = _resolve_graph_source(args)
    if source is None:
        print(
            "No crate found to graph. Provide --input <crate-or-metadata.json>, "
            "or --resume <session_id> (a built session), and try again.",
            file=sys.stderr,
        )
        return 1

    view = _EXPLORER_VIEW_BY_FLAG[args.view]
    if args.graph_out:
        out_path = Path(args.graph_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        with tempfile.NamedTemporaryFile(
            prefix="entity_graph_", suffix=".html", delete=False
        ) as tmp:
            out_path = Path(tmp.name)
    out_path.write_text(
        render_explorer_page(
            source, title="RO-Crate entity explorer", default_views=(view,)
        ),
        encoding="utf-8",
    )

    opened = False
    if not args.no_browser:
        try:
            opened = webbrowser.open(out_path.resolve().as_uri())
        except (webbrowser.Error, OSError) as exc:
            logger.warning("Could not open a browser for %s: %s", out_path, exc)
    suffix = " (opened in browser)" if opened else ""
    print(f"Entity explorer written to {out_path}{suffix}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the builder CLI.

    Returns 0 on success, 1 on error.
    """
    args = parse_args(argv)
    # The default interactive build (pipeline + guidance) logs progress at INFO;
    # bump the default level there so the run does not look dead. The ReAct
    # loop keeps its own output, so the bump applies to the whole interactive
    # path.
    setup_logging(args.verbose, interactive=args.interactive)

    logger.info("ISA-Tox RO-Crate Builder v0.1.0")

    if args.smoke_test:
        from builder.tools.hitl import SYNTHETIC_ANSWER_NOTICE

        # NOTE: this used to refuse the ReAct arm outright, because the ReAct
        # loop read its conversation straight off stdin
        # (builder.agents.ui.boxed_input) with no HumanInterface in the path, so a
        # synthetic interface had nothing to answer and the run sat on an empty
        # terminal. That read now goes through the interface when the answers are
        # synthetic (agent_loop, CONVERSATION_FIELD_TYPE), and a spent budget ends
        # the session the way Ctrl+D does — so the arm this mode most needs to
        # exercise unattended is reachable rather than refused.
        # Say it FIRST, before the build spends a token: if this mode was engaged
        # by accident, the very first thing on screen must be that nobody is
        # answering the prompts. It is repeated beside the exported crate path.
        print(SYNTHETIC_ANSWER_NOTICE)
        if isinstance(args.smoke_test, float):
            # Said in minutes because that is what was asked for, and said as
            # "winds down at its next question" because that is what happens: the
            # deadline is read between gaps / between turns, never mid-question,
            # so a long turn overruns it rather than being cut in half.
            print(
                f"Running for about {args.smoke_test:g} minute(s), then winding "
                "down at the next question and exporting."
            )

    # --show-config: print and exit
    if args.show_config:
        _show_config()
        return 0

    # --configure: run the wizard
    if args.configure:
        if _run_setup_wizard():
            return 0
        return 1

    # Dashboard mode — show live-updating profiler dashboard (before engine creation)
    if args.dashboard:
        from builder.tools.dashboard import run_dashboard

        run_dashboard(session_id=args.resume)
        return 0

    # Graph mode — render the provenance DAG as Mermaid (no LLM config needed)
    if args.graph:
        return _run_graph(args)

    # Ensure config is loaded before creating the engine
    if args.interactive:
        if not _ensure_configured():
            return 1

    # Any interactive run gets a REAL interactive HumanInterface. The DEFAULT
    # build needs it so run_interactive_build won't (correctly) skip guidance
    # (AGENTS.md §14.6.1); the ReAct loop needs it too so the scanner
    # approval guard (engine._authorize_scan_root) can prompt-once for a
    # user-named folder instead of fail-closing — without it a conversational
    # ReAct scan of an un-approved folder returns no files. Non-interactive
    # (batch) runs keep the headless simulated default.
    if args.smoke_test:
        # --smoke-test: an interface that answers ITSELF (confirm the pre-selected
        # choice, "yes, continue" into every open field). It reports
        # is_interactive = True, which is the point — the guidance tail is gated on
        # that single signal, so the headless SimulatedHumanInterface can never
        # exercise it. Scan roots stay fail-closed: confirming the pre-selection
        # denies a scan_root escalation (#197), pinned by
        # tests/test_smoke_test_mode.py.
        from builder.tools.hitl import SmokeTestHumanInterface

        # `--smoke-test` alone is True; `--smoke-test 20` is the float 20.0.
        # `isinstance(True, float)` is False, so the bare flag never reads as a
        # budget (unlike `isinstance(True, int)`, which would).
        minutes = args.smoke_test if isinstance(args.smoke_test, float) else None
        engine = AgentEngine(human_interface=SmokeTestHumanInterface(minutes=minutes))
    elif args.interactive:
        from builder.agents import ui
        from builder.tools.hitl import ConsoleHumanInterface

        # Inject the shared rounded ❯ box + green-● question styling (#344) so the
        # pipeline's guidance prompts render like the ReAct arm's: the question is
        # shown via ui.render_reply, the answer read via ui.boxed_input. Injection
        # (not an import inside hitl.py) keeps builder.tools free of a
        # builder.agents.ui dependency — no agents→tools→agents cycle.
        engine = AgentEngine(
            human_interface=ConsoleHumanInterface(
                prompt_func=lambda _field_type: ui.boxed_input(ui.get_console()),
                show_func=lambda text: ui.get_console().print(ui.render_reply(text)),
                # Decisions render as the same rounded box as free text, with the
                # expected answer pre-selected and arrow keys to change it.
                select_func=lambda choices, default: ui.select_option(
                    ui.get_console(), choices, default=default
                ),
                # Questions whose honest answer is several of the choices get a
                # checkbox box instead — space toggles, enter confirms the set.
                select_many_func=lambda hint, choices: ui.select_options(
                    ui.get_console(), choices, hint=hint
                ),
            )
        )
    else:
        engine = AgentEngine()

    if args.resume:
        logger.info("Resuming session: %s", args.resume)
        from builder.tools.session import load_session

        loaded = load_session(args.resume)
        if loaded is None:
            logger.error("Session not found: %s", args.resume)
            return 1
        engine.state = loaded
        # Resumed sessions bypass initialize(), so restore the profiler before
        # the ReAct/pipeline loop starts. Without this, run_tool and graph-node
        # instrumentation silently has no writer and profile.ndjson stops at the
        # previous session checkpoint.
        engine.ensure_profiler()
    elif args.input:
        logger.info("Initializing from input: %s", args.input)
        engine.initialize(args.input)
    else:
        logger.info("Starting with empty state (conversation mode)")
        engine.initialize()

    # Resolve the on-disk output destination (#233, #315). Precedence:
    #   1. --output / -o always wins.
    #   2. --output omitted AND --input given => output/<name>_crate, versioned
    #      _v2/_v3… (see _default_output_dir). Keeps builds out of a curated input
    #      tree (the old <input>-ro-crate sibling polluted input/raw/).
    #   3. No --input (conversation mode) => leave output_path unset so
    #      export_crate falls back to the session working_crate/ directory.
    if args.output:
        engine.state.metadata.output_path = args.output
    elif args.input:
        engine.state.metadata.output_path = str(_default_output_dir(args.input))
    elif args.resume and engine.state.metadata.output_path:
        # A continued session must never silently overwrite its previous crate.
        # Version the prior destination for each resumed export unless the user
        # explicitly supplied --output.
        engine.state.metadata.output_path = str(
            _next_output_version(engine.state.metadata.output_path)
        )

    entity_count = len(engine.state.list_entities())
    logger.info(
        "State initialized: %d files scanned, %d entities",
        len(engine.state.scanned_files),
        entity_count,
    )

    # Interactive build mode. Two first-class arms over one toolbox (AGENTS.md
    # §1, §14): the DEFAULT is the deterministic pipeline + HITL guidance tail
    # (the A/B winner on cost and termination); the ReAct agent loop is the
    # opt-in alternative behind --react.
    if args.interactive:
        from builder.agents.build import BuildMode, run_build

        mode = BuildMode.from_cli(react=args.react)

        # The default (pipeline) interactive build is folder-driven: with no
        # scanned files there is genuinely nothing to build, so tell the user how
        # to proceed instead of exiting near-silently. The ReAct loop is
        # conversational and runs without pre-scanned files, so this guard is
        # pipeline-only.
        if mode is BuildMode.PIPELINE and len(engine.state.scanned_files) == 0:
            print(
                "No input documents found. The interactive build is "
                "folder-driven: pass --input <folder> to build an ISA-Tox crate "
                "from your research documents, or use --react for the "
                "conversational agent."
            )
            return 0

        # One switch routes A/B (#309): PIPELINE -> deterministic spine + HITL
        # guidance tail (surfaced via the output channel); REACT -> the ReAct
        # loop (provider/model/base_url apply). run_build ignores the kwargs that
        # don't apply to the chosen mode.
        run_build(
            mode,
            engine,
            provider=args.provider,
            model=args.model,
            base_url=args.api_base,
            output=print,
            # This is the only place that knows whether the session was loaded or
            # freshly scanned; both arms are told rather than left to infer it
            # from state that --input has already populated (#410).
            resumed=bool(args.resume),
            # ReAct-only kickoff: without it the loop greets and blocks on stdin
            # having done no work, because the greeting invoke sits outside the
            # autonomous-continuation loop (#412).
            initial_prompt=args.prompt,
            verbose=args.verbose > 0,
        )
        return 0

    # Batch / info mode — print summary and exit
    status = engine.get_status()
    logger.debug("Status: %s", status)

    hint = engine.state.checkpoint.next_actions or ["Use draft_investigation to begin"]

    print("\n=== ISA-Tox RO-Crate Builder ===")
    print(f"Session:     {engine.state.session_id}")
    print(f"Input:       {args.input or '(conversation)'}")
    print(f"Files found: {len(engine.state.scanned_files)}")
    print(f"Entities:    {len(engine.state.list_entities())}")
    print(f"Next steps:  {hint[0] if hint else 'Ready'}")
    print("================================\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
