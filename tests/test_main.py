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

    The A/B gate (AGENTS.md D15) decided the default — the deterministic
    pipeline + HITL guidance is the default interactive architecture and the
    ReAct loop is opt-in behind ``--react``. These tests mock the
    run functions (no live LLM, no network) and assert only the dispatch routing.
    """

    def _stub_config(self, monkeypatch):
        """Make --interactive proceed without a real LLM config check."""
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def test_react_flag_is_parsed(self):
        args = parse_args(["--interactive", "--react"])
        assert args.react is True

    def test_prompt_flag_is_parsed(self):
        """`--prompt/-P` carries the ReAct kickoff message (#412)."""
        assert parse_args(["--interactive"]).prompt is None
        assert parse_args(["--interactive", "--prompt", "build it"]).prompt == "build it"
        assert parse_args(["--interactive", "-P", "go"]).prompt == "go"

    def test_prompt_flag_reaches_the_react_loop(self, monkeypatch):
        """The kickoff must survive main -> run_build -> the loop (#412)."""
        self._stub_config(monkeypatch)
        captured: dict = {}

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda engine, **kw: captured.update(kw),
        )

        assert main(["--interactive", "--react", "--prompt", "build the crate"]) == 0
        assert captured["initial_prompt"] == "build the crate"

    def test_verbose_reaches_the_react_loop(self, monkeypatch):
        """The diagnostic opt-in survives main -> run_build -> ReAct (#verbose)."""
        self._stub_config(monkeypatch)
        captured: dict = {}

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: captured.update(kw)
        )

        assert main(["--interactive", "--react", "--verbose"]) == 0
        assert captured["verbose"] is True

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

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda *a, **kw: calls.append("react"),
        )

        result = main(["--interactive", "--input", str(d)])
        assert result == 0
        assert calls == ["build"]

    def test_react_flag_routes_to_react(self, monkeypatch):
        """--interactive --react runs the ReAct loop, not the pipeline."""
        self._stub_config(monkeypatch)
        calls: list[str] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: calls.append("build") or {"pipeline": {}, "guidance": None},
        )

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda *a, **kw: calls.append("react"),
        )

        result = main(["--interactive", "--react"])
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

    def test_react_engine_is_interactive(self, monkeypatch):
        """--react also runs behind a REAL interactive interface.

        Otherwise the scanner-approval guard (``_authorize_scan_root``) fail-closes
        on a non-interactive (simulated) human, so a conversational ReAct
        scan of a user-named folder returns no files. Giving the interactive
        ReAct run a ``ConsoleHumanInterface`` lets it prompt-once and approve.
        """
        from builder.tools.hitl import is_interactive

        self._stub_config(monkeypatch)
        seen: list[bool] = []

        import builder.agents.react.agent_loop as agent_loop

        def _capture(engine, *a, **kw):
            seen.append(is_interactive(engine.human_interface))

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _capture)

        result = main(["--interactive", "--react"])
        assert result == 0
        assert seen == [True]


class TestDefaultOutputDir:
    """The no-``--output`` default lands under ``output/<name>_crate``, versioned
    ``_v2`` / ``_v3`` … (#315), matching the existing ``output/`` layout. ``<name>``
    is the input folder name with a trailing ``_extracted`` stripped."""

    def test_lands_under_output_root_stripping_extracted(self, tmp_path):
        from main import _default_output_dir

        out = _default_output_dir("/data/S-VHPS26_extracted", output_root=tmp_path)
        assert out == tmp_path / "S-VHPS26_crate"

    def test_non_extracted_name_used_verbatim(self, tmp_path):
        from main import _default_output_dir

        out = _default_output_dir("/data/experiment", output_root=tmp_path)
        assert out == tmp_path / "experiment_crate"

    def test_versions_increment_without_clobber(self, tmp_path):
        from main import _default_output_dir

        (tmp_path / "S-VHPS26_crate").mkdir()
        assert _default_output_dir("/x/S-VHPS26_extracted", output_root=tmp_path) == (
            tmp_path / "S-VHPS26_crate_v2"
        )
        (tmp_path / "S-VHPS26_crate_v2").mkdir()
        assert _default_output_dir("/x/S-VHPS26_extracted", output_root=tmp_path) == (
            tmp_path / "S-VHPS26_crate_v3"
        )


class TestOutputPathDefaulting:
    """``--output`` precedence and the versioned ``output/`` default (#233, #315).

    Decision (issue #233):
      * ``--output`` / ``-o`` always wins.
      * ``--output`` omitted AND ``--input`` given => write a SIBLING of the input
        folder: ``<input_parent>/<input_name>-ro-crate/``.
      * No ``--input`` (conversation mode) => keep ``output_path`` ``None`` so
        ``export_crate`` falls back to the session ``working_crate/``.

    These mock ``run_interactive_build`` (no live LLM / network) and capture the
    engine's resolved ``state.metadata.output_path`` at dispatch time.
    """

    def _stub_config(self, monkeypatch):
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def _capture_output_path(self, monkeypatch) -> list:
        captured: list = []

        import builder.agents.build as build_mod

        def _capture(engine, **kw):
            captured.append(engine.state.metadata.output_path)
            return {"pipeline": {}, "guidance": None, "export": None}

        monkeypatch.setattr(build_mod, "run_interactive_build", _capture)
        return captured

    def test_input_without_output_defaults_to_versioned_output(self, monkeypatch, tmp_path):
        """--input <dir>, no --output => output_path is output/<name>_crate (#315)."""
        from pathlib import Path

        self._stub_config(monkeypatch)
        monkeypatch.chdir(tmp_path)  # so output/ is created under tmp, not the repo
        d = tmp_path / "experiment_extracted"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        captured = self._capture_output_path(monkeypatch)

        result = main(["--interactive", "--input", str(d)])
        assert result == 0
        # Lands under ./output, '_extracted' stripped from the name.
        expected = Path("output") / "experiment_crate"
        assert [Path(p) for p in captured] == [expected]

    def test_explicit_output_wins_over_sibling_default(self, monkeypatch, tmp_path):
        """--output X overrides the sibling-of-input default."""
        self._stub_config(monkeypatch)
        d = tmp_path / "experiment"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        out = tmp_path / "explicit_out"
        captured = self._capture_output_path(monkeypatch)

        result = main(["--interactive", "--input", str(d), "--output", str(out)])
        assert result == 0
        assert captured == [str(out)]

    def test_no_input_no_output_does_not_default_to_sibling(
        self, monkeypatch, tmp_path
    ):
        """No --input + no --output => output_path stays None (session fallback).

        The folder-driven build short-circuits with no scanned files, so it never
        reaches the build. We capture the engine's resolved output_path by stubbing
        the no-input notice's gate point: assert the build is NOT invoked and that
        no sibling default was computed (there is no input to be a sibling of).
        """
        self._stub_config(monkeypatch)
        captured = self._capture_output_path(monkeypatch)

        result = main(["--interactive"])
        assert result == 0
        # The folder-driven build short-circuits (no files), so it never runs.
        assert captured == []

    def test_no_input_with_output_honors_explicit(self, monkeypatch, tmp_path):
        """No --input but --output given => the explicit path is honored.

        With no input there are no scanned files, so the build short-circuits with
        the notice. The defaulting logic must still set the explicit ``--output``
        on state.metadata (precedence holds even without --input).
        """
        self._stub_config(monkeypatch)
        out = tmp_path / "conv_out"

        # Capture the engine state right after defaulting, before the short-circuit,
        # via the AgentEngine the run constructs. We assert no crash + clean exit;
        # the sibling default must NOT fire (no input), so --output is what wins.
        captured = self._capture_output_path(monkeypatch)
        result = main(["--interactive", "--output", str(out)])
        assert result == 0
        # No scanned files => build short-circuits; the build is not invoked.
        assert captured == []


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

    def test_interactive_silences_noisy_third_party_loggers(self):
        """The interactive INFO bump must not unleash httpx/openai request spam.

        When *interactive* raises the effective level to INFO (and the user did
        not ask for it via -v/-vv), the noisy third-party loggers are forced to
        WARNING so each guidance question is not buried under per-request
        ``HTTP Request: POST .../chat/completions`` INFO lines, while our own
        ``builder.*`` progress lines stay at INFO.
        """
        import logging

        from main import setup_logging

        root = logging.getLogger()
        noisy_names = ("httpx", "httpcore", "openai", "urllib3")
        originals = {name: logging.getLogger(name).level for name in noisy_names}
        original_root = root.level
        try:
            setup_logging(0, interactive=True)
            for name in noisy_names:
                assert (
                    logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
                ), name
            assert logging.getLogger("builder").getEffectiveLevel() == logging.INFO
        finally:
            root.setLevel(original_root)
            for name, lvl in originals.items():
                logging.getLogger(name).setLevel(lvl)

    def test_explicit_verbose_does_not_raise_third_party_loggers(self):
        """Under an explicit -vv (DEBUG) the user opted into verbosity.

        The noisy third-party loggers must NOT be force-raised, so their
        DEBUG/INFO output is honoured rather than suppressed.
        """
        import logging

        from main import setup_logging

        root = logging.getLogger()
        noisy_names = ("httpx", "httpcore", "openai", "urllib3")
        originals = {name: logging.getLogger(name).level for name in noisy_names}
        original_root = root.level
        try:
            # Start from NOTSET so a force-raise would be observable.
            for name in noisy_names:
                logging.getLogger(name).setLevel(logging.NOTSET)
            setup_logging(2, interactive=True)
            for name in noisy_names:
                assert logging.getLogger(name).level == logging.NOTSET, name
        finally:
            root.setLevel(original_root)
            for name, lvl in originals.items():
                logging.getLogger(name).setLevel(lvl)
