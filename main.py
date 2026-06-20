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


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the builder."""
    level = logging.DEBUG if verbose else logging.INFO
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
        "--resume", "-r",
        type=str, default=None,
        help="Session ID to resume",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the builder CLI.

    Returns 0 on success, 1 on error.
    """
    args = parse_args(argv)
    setup_logging(args.verbose)

    logger.info("ISA-Tox RO-Crate Builder v0.1.0")

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

        run_interactive_agent(engine, provider=args.provider)
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
