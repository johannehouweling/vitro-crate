"""Tests for the main entry point."""

from __future__ import annotations

from main import parse_args, main


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
        monkeypatch.setattr(d, "run_dashboard",
                            lambda *a, **kw: called.append(kw.get("session_id")))
        result = main(["--dashboard"])
        assert result == 0
        assert called == [None]  # no session specified

    def test_dashboard_flag_with_session(self, monkeypatch):
        """main() with --dashboard and --resume passes session id to dashboard."""
        called = []
        import builder.tools.dashboard as d
        monkeypatch.setattr(d, "run_dashboard",
                            lambda *a, **kw: called.append(kw.get("session_id")))
        result = main(["--dashboard", "--resume", "test_session"])
        assert result == 0
        assert called == ["test_session"]
