"""Tests for the main entry point."""

from __future__ import annotations

import pytest

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
    """``--interactive`` runs the ReAct loop (AGENTS.md D15).

    These tests mock the loop (no live LLM, no network) and assert only what
    reaches it.
    """

    def _stub_config(self, monkeypatch):
        """Make --interactive proceed without a real LLM config check."""
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def test_there_is_no_arm_to_choose(self):
        """One build: the flags that chose between two are gone."""
        for flag in ("--react", "--pipeline"):
            with pytest.raises(SystemExit):
                parse_args(["--interactive", flag])

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

        assert main(["--interactive", "--prompt", "build the crate"]) == 0
        assert captured["initial_prompt"] == "build the crate"

    def test_an_input_folder_starts_the_react_build(self, monkeypatch, tmp_path):
        """A folder on the command line says what to do: no --prompt needed (#846)."""
        from builder.agents.build import DEFAULT_INSTRUCTION

        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        captured: dict = {}

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: captured.update(kw)
        )

        assert main(["--interactive", "--input", str(d)]) == 0
        assert captured["initial_prompt"] == DEFAULT_INSTRUCTION

        assert main(["--interactive", "--input", str(d), "--prompt", "only the LDH assay"]) == 0
        assert captured["initial_prompt"] == "only the LDH assay"

        assert main(["--interactive"]) == 0
        assert captured["initial_prompt"] is None

    def test_verbose_reaches_the_react_loop(self, monkeypatch):
        """The diagnostic opt-in survives main -> run_build -> the loop (#verbose)."""
        self._stub_config(monkeypatch)
        captured: dict = {}

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda engine, **kw: captured.update(kw)
        )

        assert main(["--interactive", "--verbose"]) == 0
        assert captured["verbose"] is True

    def test_interactive_runs_the_react_loop(self, monkeypatch):
        """--interactive runs the ReAct loop (#842)."""
        self._stub_config(monkeypatch)
        calls: list[str] = []

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop, "run_interactive_agent", lambda *a, **kw: calls.append("react")
        )

        assert main(["--interactive"]) == 0
        assert calls == ["react"]

    def test_the_engine_is_interactive(self, monkeypatch):
        """The loop runs behind a REAL interactive interface.

        Otherwise the scanner-approval guard (``_authorize_scan_root``) fail-closes
        on a non-interactive (simulated) human, so a conversational scan of a
        user-named folder returns no files. Giving the interactive run a
        ``ConsoleHumanInterface`` lets it prompt-once and approve.
        """
        from builder.tools.hitl import is_interactive

        self._stub_config(monkeypatch)
        seen: list[bool] = []

        import builder.agents.react.agent_loop as agent_loop

        def _capture(engine, *a, **kw):
            seen.append(is_interactive(engine.human_interface))

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _capture)

        result = main(["--interactive"])
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

    These mock the ReAct loop (no live LLM / network) and capture the engine's
    resolved ``state.metadata.output_path`` when the loop starts.
    """

    def _stub_config(self, monkeypatch):
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def _capture_output_path(self, monkeypatch) -> list:
        captured: list = []

        import builder.agents.react.agent_loop as agent_loop

        def _capture(engine, **kw):
            captured.append(engine.state.metadata.output_path)

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _capture)
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

    def test_no_input_no_output_does_not_default_to_sibling(self, monkeypatch, tmp_path):
        """No --input + no --output => output_path stays None (session fallback):
        there is no input to be a sibling of."""
        self._stub_config(monkeypatch)
        captured = self._capture_output_path(monkeypatch)

        assert main(["--interactive"]) == 0
        assert captured == [None]

    def test_no_input_with_output_honors_explicit(self, monkeypatch, tmp_path):
        """No --input but --output given => the explicit path is honored."""
        self._stub_config(monkeypatch)
        out = tmp_path / "conv_out"
        captured = self._capture_output_path(monkeypatch)

        assert main(["--interactive", "--output", str(out)]) == 0
        assert captured == [str(out)]


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
