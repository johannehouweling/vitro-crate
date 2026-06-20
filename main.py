"""ISA-Tox RO-Crate Builder — Main entry point.

Usage:
    python -m main [--input <path>] [--output <path>] [--resume <session_id>]
    python -m main --interactive [--input <path>] [--provider openai|anthropic]
"""

from __future__ import annotations

import argparse
import logging
import sys

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
    parser = argparse.ArgumentParser(
        description="ISA-Tox RO-Crate Builder"
    )
    parser.add_argument(
        "--input", "-i",
        type=str, default=None,
        help="Path to input directory containing research data",
    )
    parser.add_argument(
        "--output", "-o",
        type=str, default=None,
        help="Path for the output ARC directory (RO-Crate)",
    )
    parser.add_argument(
        "--resume", "--session", "-r",
        type=str, default=None,
        help="Session ID to resume (e.g. 20260620_192039)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="count",
        default=0,
        help="Increase verbosity (-v = INFO, -vv = DEBUG)",
    )
    parser.add_argument(
        "--interactive", "-I",
        action="store_true",
        help="Run in interactive agent mode (requires LangChain extra + API key)",
    )
    parser.add_argument(
        "--provider", "-p",
        type=str, default=None,
        choices=["openai", "anthropic"],
        help="LLM provider for interactive mode (auto-detected from env if omitted)",
    )
    parser.add_argument(
        "--model", "-m",
        type=str, default=None,
        help="Model name (e.g. gpt-4o-mini, llama3.2, claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--api-base", "-b",
        type=str, default=None,
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
    return parser.parse_args(argv)


def _run_setup_wizard() -> bool:
    """Run the interactive config wizard.  Returns True if successful."""
    from builder.config import interactive_setup
    return interactive_setup()


def _show_config() -> None:
    """Print current configuration state."""
    from builder.config import describe_config, is_configured
    from builder.config import load_config, merge_with_env

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
    from builder.config import is_configured, merge_with_env, load_config

    # Load config file and merge into env (env vars take priority)
    cfg = load_config()
    merge_with_env(cfg)

    if is_configured():
        return True

    print()
    print("No LLM provider is configured yet.")
    print()
    try:
        answer = input(
            "Would you like to set one up now? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if answer in ("", "y", "yes"):
        return _run_setup_wizard()

    print("Skipping setup.  You can configure later with --configure.")
    return False


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

    # Ensure config is loaded before creating the engine
    if args.interactive:
        if not _ensure_configured():
            return 1

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

    # Interactive agent mode — enter the LangChain REPL
    if args.interactive:
        from builder.agents.agent_loop import run_interactive_agent

        run_interactive_agent(
            engine,
            provider=args.provider,
            model=args.model,
            base_url=args.api_base,
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
