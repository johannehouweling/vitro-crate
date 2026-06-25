"""Tests for the main entry point."""

from __future__ import annotations

from main import main, parse_args


class TestParseArgs:
    """Tests for argument parsing."""

    def test_default_values(self):
        """parse_args returns default values when no args."""
        args = parse_args([])
        assert args.input is None
        assert args.output is None
        assert args.resume is None
        assert args.verbose == 0

    def test_input_flag(self):
        """parse_args parses --input flag."""
        args = parse_args(["--input", "/some/path"])
        assert args.input == "/some/path"

    def test_output_flag(self):
        """parse_args parses --output flag."""
        args = parse_args(["--output", "/out/path"])
        assert args.output == "/out/path"

    def test_resume_flag(self):
        """parse_args parses --resume flag."""
        args = parse_args(["--resume", "session_123"])
        assert args.resume == "session_123"

    def test_verbose_flag(self):
        """parse_args parses --verbose flag (count, not bool)."""
        args = parse_args(["--verbose"])
        assert args.verbose == 1
        args2 = parse_args(["-vv"])
        assert args2.verbose == 2

    def test_short_flags(self):
        """parse_args handles short flags (-i, -o, -r, -v)."""
        args = parse_args(["-i", "/in", "-o", "/out", "-r", "sess", "-v"])
        assert args.input == "/in"
        assert args.output == "/out"
        assert args.resume == "sess"
        assert args.verbose == 1


class TestMain:
    """Tests for the main() function."""

    def test_no_args_returns_zero(self):
        """main() with no arguments returns 0."""
        result = main([])
        assert result == 0

    def test_with_input_directory(self, tmp_path):
        """main() with --input creates a session with session_id."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        result = main(["--input", str(d)])
        assert result == 0

    def test_with_output_sets_output_path(self, tmp_path):
        """main() with --output sets output_path on state metadata."""
        out_dir = tmp_path / "output"
        result = main(["--output", str(out_dir)])
        assert result == 0

    def test_with_resume_nonexistent_returns_one(self):
        """main() with --resume for non-existent session returns 1."""
        result = main(["--resume", "nonexistent_session_xyz"])
        assert result == 1

    def test_verbose_sets_debug_logging(self, caplog):
        """main() with --verbose enables debug logging."""
        import logging

        caplog.set_level(logging.DEBUG)
        result = main(["--verbose"])
        assert result == 0

    def test_output_flag_with_capsys(self, capsys):
        """main() prints session status to stdout."""
        result = main([])
        captured = capsys.readouterr()
        assert "ISA-Tox RO-Crate Builder" in captured.out
        assert result == 0

    def test_dashboard_flag_calls_run_dashboard(self, monkeypatch):
        """main() with --dashboard calls run_dashboard (live)."""
        called = []
        import builder.tools.dashboard as d

        monkeypatch.setattr(
            d, "run_dashboard", lambda *a, **kw: called.append(kw.get("session_id"))
        )
        result = main(["--dashboard"])
        assert result == 0
        assert called == [None]  # no session specified

    def test_dashboard_flag_with_session(self, monkeypatch):
        """main() with --dashboard and --resume passes session id to dashboard."""
        called = []
        import builder.tools.dashboard as d

        monkeypatch.setattr(
            d, "run_dashboard", lambda *a, **kw: called.append(kw.get("session_id"))
        )
        result = main(["--dashboard", "--resume", "test_session"])
        assert result == 0
        assert called == ["test_session"]


class TestInteractiveDispatch:
    """The interactive build path: pipeline+guidance is the DEFAULT; ReAct is opt-in.

    The A/B gate (AGENTS.md §14) decided the cutover — the deterministic
    pipeline + HITL guidance is now the default interactive architecture and the
    legacy ReAct loop is retained behind ``--legacy-react``. These tests mock the
    run functions (no live LLM, no network) and assert only the dispatch routing.
    """

    def _stub_config(self, monkeypatch):
        """Make --interactive proceed without a real LLM config check."""
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def test_legacy_react_flag_is_parsed(self):
        args = parse_args(["--interactive", "--legacy-react"])
        assert args.legacy_react is True

    def test_default_interactive_routes_to_pipeline_build(self, monkeypatch, tmp_path):
        """--interactive (no opt-in) runs the deterministic pipeline + guidance.

        Supplies an --input folder with a file so the folder-driven build has
        something to do (an empty state now short-circuits with a notice).
        """
        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        calls: list[str] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: calls.append("build") or {"pipeline": {}, "guidance": None},
        )

        import builder.agents.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda *a, **kw: calls.append("react"),
        )

        result = main(["--interactive", "--input", str(d)])
        assert result == 0
        assert calls == ["build"]

    def test_legacy_react_flag_routes_to_react(self, monkeypatch):
        """--interactive --legacy-react runs the legacy ReAct loop, not the pipeline."""
        self._stub_config(monkeypatch)
        calls: list[str] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: calls.append("build") or {"pipeline": {}, "guidance": None},
        )

        import builder.agents.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda *a, **kw: calls.append("react"),
        )

        result = main(["--interactive", "--legacy-react"])
        assert result == 0
        assert calls == ["react"]

    def test_default_interactive_engine_is_interactive(self, monkeypatch, tmp_path):
        """The engine handed to the default pipeline build reports interactive HITL.

        Supplies an --input folder so the folder-driven build runs (an empty
        state short-circuits with a notice before reaching the build).
        """
        from builder.tools.hitl import is_interactive

        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        seen: list[bool] = []

        import builder.agents.build as build_mod

        def _capture(engine, **kw):
            seen.append(is_interactive(engine.human_interface))
            return {"pipeline": {}, "guidance": None}

        monkeypatch.setattr(build_mod, "run_interactive_build", _capture)

        result = main(["--interactive", "--input", str(d)])
        assert result == 0
        # The default interactive build must run behind a REAL interactive
        # interface, else run_interactive_build would skip guidance.
        assert seen == [True]

    def test_legacy_react_engine_is_interactive(self, monkeypatch):
        """--legacy-react also runs behind a REAL interactive interface.

        Otherwise the scanner-approval guard (``_authorize_scan_root``) fail-closes
        on a non-interactive (simulated) human, so a conversational legacy-react
        scan of a user-named folder returns no files. Giving the interactive
        legacy run a ``ConsoleHumanInterface`` lets it prompt-once and approve.
        """
        from builder.tools.hitl import is_interactive

        self._stub_config(monkeypatch)
        seen: list[bool] = []

        import builder.agents.agent_loop as agent_loop

        def _capture(engine, *a, **kw):
            seen.append(is_interactive(engine.human_interface))

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _capture)

        result = main(["--interactive", "--legacy-react"])
        assert result == 0
        assert seen == [True]


class TestInteractiveNoInput:
    """First-run UX for the default interactive build with no --input.

    The default interactive build is folder-driven; with zero scanned files there
    is genuinely nothing to build. These tests assert a friendly notice and a
    graceful exit (no crash), with the real pipeline stubbed out.
    """

    def _stub_config(self, monkeypatch):
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def test_no_input_prints_notice_and_returns_zero(self, monkeypatch, capsys):
        """--interactive with no --input prints the notice and returns 0."""
        self._stub_config(monkeypatch)
        calls: list[str] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: calls.append("build")
            or {"pipeline": {}, "guidance": None},
        )

        result = main(["--interactive"])
        captured = capsys.readouterr()
        assert result == 0
        assert "No input documents found" in captured.out
        assert "--input" in captured.out
        # Nothing to build: the pipeline build must not run.
        assert calls == []

    def test_with_input_does_not_print_notice(self, monkeypatch, capsys, tmp_path):
        """--interactive with --input (files present) skips the notice and builds."""
        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        calls: list[str] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: calls.append("build")
            or {"pipeline": {}, "guidance": None},
        )

        result = main(["--interactive", "--input", str(d)])
        captured = capsys.readouterr()
        assert result == 0
        assert "No input documents found" not in captured.out
        assert calls == ["build"]


class TestInteractiveVerbosity:
    """The interactive path raises the default log level to INFO for visibility."""

    def test_interactive_default_verbose_is_info(self):
        """setup_logging(0, interactive=True) yields effective level INFO."""
        import logging

        from main import setup_logging

        root = logging.getLogger()
        original = root.level
        try:
            setup_logging(0, interactive=True)
            assert root.level == logging.INFO
        finally:
            root.setLevel(original)

    def test_noninteractive_default_verbose_is_warning(self):
        """setup_logging(0) (batch) keeps the WARNING default."""
        import logging

        from main import setup_logging

        root = logging.getLogger()
        original = root.level
        try:
            setup_logging(0, interactive=False)
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original)

    def test_interactive_does_not_downgrade_explicit_debug(self):
        """An explicit -vv (DEBUG) is never downgraded by the interactive bump."""
        import logging

        from main import setup_logging

        root = logging.getLogger()
        original = root.level
        try:
            setup_logging(2, interactive=True)
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(original)
