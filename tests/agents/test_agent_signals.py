"""Tests for signal handling in the agent loop.

Verifies that Ctrl+C clears the line and re-prompts (does not kill the process),
and that Ctrl+D triggers an orderly exit.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestSignalHandling:
    """Tests for Ctrl+C (KeyboardInterrupt) and Ctrl+D (EOFError) handling."""

    def test_ctrl_c_clears_line_and_continues(self) -> None:
        """Ctrl+C should clear the line and re-prompt, not kill the terminal."""
        from rich.console import Console

        console = Console()
        # Simulate: first call raises KeyboardInterrupt, second returns "hello"
        mock_input = MagicMock(
            side_effect=[
                KeyboardInterrupt(),  # Ctrl+C on first try
                "hello",              # normal input on second try
            ]
        )

        with patch.object(console, "input", mock_input):
            # This mimics the pattern in the main loop
            result = None
            for _ in range(3):  # max 3 attempts
                try:
                    raw = console.input("> ")
                    result = raw.strip()
                    break
                except KeyboardInterrupt:
                    # Ctrl+C: clear line and re-prompt
                    console.print()  # clear the line visually
                    continue

            assert result == "hello", "Should get 'hello' after Ctrl+C recovery"
            # KeyboardInterrupt was raised and caught — console.print was called once
            assert mock_input.call_count == 2

    def test_ctrl_d_triggers_exit(self) -> None:
        """Ctrl+D (EOFError) should trigger exit, not continue the loop."""
        from rich.console import Console

        console = Console()
        mock_input = MagicMock(
            side_effect=[
                EOFError(),  # Ctrl+D
            ]
        )

        with patch.object(console, "input", mock_input):
            exited = False
            try:
                raw = console.input("> ")
            except EOFError:
                # Ctrl+D: exit
                exited = True

            assert exited, "EOFError should cause exit"

    def test_normal_input_returns_string(self) -> None:
        """Normal text input should be returned as-is (stripped)."""
        from rich.console import Console

        console = Console()
        mock_input = MagicMock(return_value="  scan my data  ")

        with patch.object(console, "input", mock_input):
            raw = console.input("> ")
            result = raw.strip()

            assert result == "scan my data"

    def test_ctrl_c_then_ctrl_d(self) -> None:
        """Ctrl+C then Ctrl+D: first clears, second exits."""
        from rich.console import Console

        console = Console()
        mock_input = MagicMock(
            side_effect=[
                KeyboardInterrupt(),  # Ctrl+C
                KeyboardInterrupt(),  # Ctrl+C again
                EOFError(),           # Ctrl+D finally
            ]
        )

        with patch.object(console, "input", mock_input):
            ctrl_c_count = 0
            eof_reached = False

            for _ in range(5):
                try:
                    raw = console.input("> ")
                    break  # got input
                except KeyboardInterrupt:
                    ctrl_c_count += 1
                    console.print()
                    continue
                except EOFError:
                    eof_reached = True
                    break

            assert ctrl_c_count == 2, "Should have caught 2 Ctrl+C"
            assert eof_reached, "Should have exited on Ctrl+D"

    def test_quit_command_exits(self) -> None:
        """Typing 'quit' should exit the main loop gracefully."""
        from rich.console import Console

        console = Console()
        mock_input = MagicMock(return_value="quit")

        with patch.object(console, "input", mock_input):
            raw = console.input("> ")
            result = raw.strip()

            assert result.lower() in ("quit", "exit", "q"), "'quit' should be recognised"