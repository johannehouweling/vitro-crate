"""ISA-Tox RO-Crate Builder — Main entry point.

Usage:
    python -m main [--input <path>] [--output <path>] [--resume <session_id>]
    python -m main --interactive [--input <path>] [--provider openai|anthropic]
    python -m main --graph [--input <crate-or-metadata.json>] [--resume <session_id>]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from builder.engine import AgentEngine

logger = logging.getLogger(__name__)


def setup_logging(verbose: int = 0) -> None:
    """Configure logging for the builder.

    Levels:
        0 = WARNING (only warnings and errors)
        1 = INFO    (normal progress)
        2 = DEBUG   (verbose/tool internals)
    """
    level_map = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
    level = level_map.get(verbose, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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
        help="Path for the output ARC directory (RO-Crate)",
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
        "--legacy-react",
        action="store_true",
        help="With --interactive, use the legacy ReAct agent loop instead of the "
        "default deterministic pipeline + guidance build (retained pending the "
        "system-prompt strip; see AGENTS.md §14)",
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
        help="Render the LabProcess provenance DAG. Source is --input (a crate dir "
        "or ro-crate-metadata.json) or a session (--resume <id>, else the latest). "
        "Needs no LLM config.",
    )
    parser.add_argument(
        "--format",
        choices=["html", "mermaid"],
        default="html",
        help="--graph output: 'html' (rendered, opens in browser; default) or "
        "'mermaid' (raw source to stdout, for piping to mmdc/docs)",
    )
    parser.add_argument(
        "--view",
        choices=["crate", "provenance"],
        default="crate",
        help="--graph view: 'crate' (full entity graph, 3 layers; default) or "
        "'provenance' (just the LabProcess derivation chain)",
    )
    parser.add_argument(
        "--layer",
        choices=["1", "2", "3", "all", "crate", "isa", "isa-tox", "tox"],
        default="all",
        help="--graph --view crate: cumulative layer filter — 1/crate=packaging, "
        "2/isa=+structural, 3/isa-tox=all (default: all)",
    )
    parser.add_argument(
        "--all-edges",
        action="store_true",
        help="--graph --view crate: also draw secondary edges (CSVW internals, "
        "conformsTo, citation/funder)",
    )
    parser.add_argument(
        "--graph-out",
        type=str,
        default=None,
        help="Path for the rendered --graph HTML file (default: a temp file)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="With --graph --format html, write the file but do not open a browser",
    )
    return parser.parse_args(argv)


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
    """Render the provenance DAG. Returns 0 on success, 1 otherwise.

    ``--format mermaid`` prints the raw Mermaid source to stdout; the default
    ``html`` writes a self-contained, browser-renderable page (and opens it
    unless ``--no-browser``).
    """
    from builder.writers.provenance_dag import (
        render_crate_graph,
        render_mermaid_html,
        render_provenance_mermaid,
    )

    source = _resolve_graph_source(args)
    if source is None:
        print(
            "No crate found to graph. Provide --input <crate-or-metadata.json>, "
            "or --resume <session_id> (a built session), and try again.",
            file=sys.stderr,
        )
        return 1

    if args.view == "provenance":
        mermaid = render_provenance_mermaid(source)
    else:
        mermaid = render_crate_graph(source, layer=args.layer, all_edges=args.all_edges)

    if args.format == "mermaid":
        print(mermaid)
        return 0

    # html: write a rendered page and (optionally) open it.
    if args.graph_out:
        out_path = Path(args.graph_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        with tempfile.NamedTemporaryFile(
            prefix="provenance_", suffix=".html", delete=False
        ) as tmp:
            out_path = Path(tmp.name)
    title = (
        "RO-Crate provenance chain"
        if args.view == "provenance"
        else f"RO-Crate entity graph (layer ≤ {args.layer})"
    )
    out_path.write_text(render_mermaid_html(mermaid, title=title), encoding="utf-8")

    opened = False
    if not args.no_browser:
        try:
            opened = webbrowser.open(out_path.resolve().as_uri())
        except (webbrowser.Error, OSError) as exc:
            logger.warning("Could not open a browser for %s: %s", out_path, exc)
    suffix = " (opened in browser)" if opened else ""
    print(f"Provenance DAG written to {out_path}{suffix}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the builder CLI.

    Returns 0 on success, 1 on error.
    """
    args = parse_args(argv)
    setup_logging(args.verbose)

    logger.info("ISA-Tox RO-Crate Builder v0.1.0")

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

    # The DEFAULT interactive build is the deterministic pipeline + HITL guidance
    # tail (AGENTS.md §14.6.1), so it must run behind a REAL interactive
    # HumanInterface — else run_interactive_build would (correctly) skip guidance.
    # The legacy ReAct loop (--legacy-react) keeps the headless simulated default;
    # its own HITL routes through the agent loop, not the guidance tail.
    if args.interactive and not args.legacy_react:
        from builder.tools.hitl import ConsoleHumanInterface

        engine = AgentEngine(human_interface=ConsoleHumanInterface())
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
    elif args.input:
        logger.info("Initializing from input: %s", args.input)
        engine.initialize(args.input)
    else:
        logger.info("Starting with empty state (conversation mode)")
        engine.initialize()

    if args.output:
        engine.state.metadata.output_path = args.output

    entity_count = len(engine.state.list_entities())
    logger.info(
        "State initialized: %d files scanned, %d entities",
        len(engine.state.scanned_files),
        entity_count,
    )

    # Interactive build mode. Post-cutover (AGENTS.md §14, gated on the in-repo
    # A/B: pipeline reached 3/3 ISA-Tox conformance vs ReAct 1/3) the DEFAULT is
    # the deterministic pipeline + HITL guidance tail. The legacy ReAct loop is
    # retained behind --legacy-react (pending the task-7 prompt strip), not deleted.
    if args.interactive:
        if args.legacy_react:
            from builder.agents.agent_loop import run_interactive_agent

            run_interactive_agent(
                engine,
                provider=args.provider,
                model=args.model,
                base_url=args.api_base,
            )
            return 0

        # Default: automated pipeline, then the guidance tail for the real user
        # (run_interactive_build gates guidance on the interactive interface and
        # surfaces a concise summary via the output channel).
        from builder.agents.build import run_interactive_build

        run_interactive_build(engine, output=print)
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
