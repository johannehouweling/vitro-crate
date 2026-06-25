"""Tests for the AgentEngine orchestrator."""

from __future__ import annotations

from pathlib import Path

from builder.engine import AgentEngine
from builder.state import CrateState


class TestAgentEngine:
    """Test suite for the AgentEngine class."""

    def test_creates_instance_with_default_state(self):
        """Engine creates instance with a default CrateState."""
        engine = AgentEngine()
        assert isinstance(engine.state, CrateState)
        assert engine.state.session_id == ""

    def test_initialize_sets_session_id(self):
        """initialize sets session_id and timestamps."""
        engine = AgentEngine()
        engine.initialize()
        assert engine.state.session_id != ""
        assert engine.state.created_at != ""
        assert engine.state.updated_at != ""

    def test_initialize_with_input_scans_files(self, tmp_path):
        """initialize with input_path scans files and populates scanned_files."""
        d = tmp_path / "test_data"
        d.mkdir()
        (d / "data.csv").write_text("a,b,c\n1,2,3\n")

        engine = AgentEngine()
        engine.initialize(str(d))
        assert len(engine.state.scanned_files) == 1
        assert engine.state.metadata.input_path == str(d)

    def test_initialize_missing_directory_does_not_crash(self):
        """initialize with non-existent dir returns empty scanned_files."""
        engine = AgentEngine()
        engine.initialize("/tmp/nonexistent_dir_xyz_123")
        assert len(engine.state.scanned_files) == 0

    def test_run_tool_calls_draft_investigation(self):
        """run_tool calls draft_investigation and returns an Entity."""
        engine = AgentEngine()
        engine.initialize()
        result = engine.run_tool("draft_investigation", hints={"name": "Test Inv"})
        assert result is not None
        assert result.type == "Investigation"
        assert result.fields.get("name") == "Test Inv"
        # Entity should be in state
        assert engine.state.get_entity(result.entity_id) is not None

    def test_run_tool_raises_for_unknown_tool(self):
        """run_tool raises ValueError for unknown tool name."""
        engine = AgentEngine()
        engine.initialize()
        import pytest

        with pytest.raises(ValueError, match="Unknown tool"):
            engine.run_tool("nonexistent_tool_xyz")

    def test_is_stuck_returns_true_when_stuck_flag_set(self):
        """is_stuck property reflects the stuck flag."""
        engine = AgentEngine()
        assert not engine.is_stuck
        engine.mark_stuck("Test stuck reason")
        assert engine.is_stuck

    def test_mark_stuck_updates_state(self):
        """mark_stuck sets stuck flag and logs reasoning."""
        engine = AgentEngine()
        engine.initialize()
        engine.mark_stuck("Cannot resolve identifier")
        assert engine.state.stuck is True
        assert len(engine.state.checkpoint.reasoning_log) >= 1
        last_step = engine.state.checkpoint.reasoning_log[-1]
        assert "Cannot resolve identifier" in last_step.result

    def test_get_status_returns_correct_structure(self):
        """get_status returns dict with expected keys."""
        engine = AgentEngine()
        engine.initialize()
        status = engine.get_status()
        assert "session_id" in status
        assert "phase" in status
        assert "entity_counts" in status
        assert "total_entities" in status
        assert "iteration_count" in status

    def test_records_tool_calls_in_reasoning_log(self):
        """Each run_tool call increments iteration_count and logs the action."""
        engine = AgentEngine()
        engine.initialize()
        assert engine.state.iteration_count == 0
        engine.run_tool("draft_investigation", hints={"name": "Test"})
        assert engine.state.iteration_count == 1
        engine.run_tool("draft_investigation", hints={"name": "Test 2"})
        assert engine.state.iteration_count == 2

    def test_get_available_tools_returns_list(self):
        """get_available_tools returns a non-empty list of tool names."""
        engine = AgentEngine()
        tools = engine.get_available_tools()
        assert len(tools) > 0
        assert "draft_investigation" in tools
        assert "scan_files" in tools

    def test_run_tool_read_multiple_files_registered(self, tmp_path):
        """run_tool can call read_multiple_files via the engine.

        The read tools are sandboxed to ``approved_scan_roots`` (#167), so the
        files must live inside an approved root for the read to succeed.
        """
        engine = AgentEngine()
        engine.initialize()
        # Approve a directory and read files that live inside it.
        data = tmp_path / "data"
        data.mkdir()
        engine.state.approved_scan_roots.add(str(data.resolve()))
        a = data / "a.txt"
        a.write_text("hello\nworld\n")
        b = data / "b.txt"
        b.write_text("foo\nbar\n")
        result = engine.run_tool("read_multiple_files", paths=[str(a), str(b)])
        assert result is not None
        assert isinstance(result, dict)
        assert result["count"] == 2
        assert "hello" in result["files"][str(a)]
        assert "foo" in result["files"][str(b)]

    def test_scan_files_non_list_result_does_not_overwrite_state(self, monkeypatch):
        """run_tool preserves scanned_files if scan_files returns a non-list."""
        engine = AgentEngine()
        engine.state.scanned_files = ["existing"]  # ty: ignore

        def fake_scan_files(**kwargs):
            return None

        monkeypatch.setattr("builder.tools.scanner.scan_files", fake_scan_files)

        result = engine.run_tool("scan_files", path="/tmp/does-not-matter")

        assert result is None
        assert engine.state.scanned_files == ["existing"]


def _make_zip(tmp_path, name="study.zip", with_sibling=False):
    """Helper: build a small zip under tmp_path; return its Path."""
    import zipfile

    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "x.txt").write_text("hello world")
    (src / "y.csv").write_text("a,b\n1,2\n")
    if with_sibling:
        (tmp_path / "secret.txt").write_text("do not read me")
    zpath = tmp_path / name
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(src / "x.txt", "x.txt")
        zf.write(src / "y.csv", "y.csv")
    return zpath


class TestScanApprovedRoots:
    """Fail-closed approved-roots guard (#197). The agent's own scan_files
    call must NEVER auto-approve a new root: with no approved roots the scan
    is refused and the set stays empty. Roots are added only by initialize()
    (a user-provided input path) or an explicit real approval. The guard must
    (a) not be wiped by an empty scan, (b) allow follow-up scans of extracted
    contents, and (c) NOT over-broaden to expose unrelated sibling files (D9)."""

    def test_empty_scan_result_does_not_overwrite_state(self, monkeypatch):
        """A denied/empty scan must NOT wipe a populated inventory with zero."""
        engine = AgentEngine()
        engine.state.scanned_files = ["existing1", "existing2"]  # ty: ignore

        monkeypatch.setattr("builder.tools.scanner.scan_files", lambda **kw: [])

        result = engine.run_tool("scan_files", path="/tmp/denied")

        assert result == []
        assert engine.state.scanned_files == ["existing1", "existing2"]

    def test_agent_scan_with_no_roots_refuses_and_does_not_autoapprove(self, tmp_path):
        """Fail-closed: with no approved roots, the agent's scan is refused and
        no root is added (the set stays empty)."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_text("a")

        engine = AgentEngine()
        assert engine.state.approved_scan_roots == set()

        r = engine.run_tool("scan_files", path=str(d))

        assert r == []  # refused
        assert engine.state.approved_scan_roots == set(), "agent scan must not auto-approve"
        assert engine.state.scanned_files == []  # nothing stored

    def test_directory_scan_after_initialize_allows_subdir(self, tmp_path):
        """initialize() approves the input dir; a subdir is then scannable."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_text("a")
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")

        engine = AgentEngine()
        engine.initialize(str(d))
        assert str(d.resolve()) in engine.state.approved_scan_roots

        r = engine.run_tool("scan_files", path=str(sub))
        assert len(r) == 1  # subdir of an approved root is allowed

    def test_initialize_zip_approves_extracted_dir_not_parent(self, tmp_path):
        """initialize(zip) approves the extraction dir, NOT the archive's parent
        — so a sibling file next to the zip stays out of reach (security)."""
        zpath = _make_zip(tmp_path, with_sibling=True)

        engine = AgentEngine()
        engine.initialize(str(zpath))

        extracted = str((tmp_path / "study_extracted").resolve())
        assert extracted in engine.state.approved_scan_roots
        assert str(tmp_path.resolve()) not in engine.state.approved_scan_roots

        # Scanning the parent (which holds secret.txt) must be DENIED.
        r = engine.run_tool("scan_files", path=str(tmp_path))
        assert r == []
        # ...and must not have clobbered the real inventory.
        assert len(engine.state.scanned_files) >= 2

    def test_followup_scan_of_extracted_dir_is_not_denied(self, tmp_path):
        """initialize a zip, then the agent scans its extracted dir — allowed."""
        zpath = _make_zip(tmp_path, name="d.zip")

        engine = AgentEngine()
        engine.initialize(str(zpath))
        assert len(engine.state.scanned_files) >= 2

        extracted = zpath.parent / "d_extracted"
        r2 = engine.run_tool("scan_files", path=str(extracted))
        assert len(r2) >= 2, "follow-up scan of extracted dir was denied"
        assert len(engine.state.scanned_files) >= 2

    def test_initialize_zip_then_scan_extracted_not_denied(self, tmp_path):
        """Production CLI path: initialize('archive.zip') then scan extracted."""
        zpath = _make_zip(tmp_path, name="study.zip")

        engine = AgentEngine()
        engine.initialize(str(zpath))
        assert len(engine.state.scanned_files) >= 2

        extracted = tmp_path / "study_extracted"
        r = engine.run_tool("scan_files", path=str(extracted))
        assert len(r) >= 2, "extracted dir denied on the initialize() entry path"

    def test_empty_first_scan_does_not_open_guard(self, tmp_path):
        """An agent scan of an unapproved (even empty) dir leaves the guard
        closed: nothing is approved and a later scan is still denied."""
        empty = tmp_path / "empty"
        empty.mkdir()

        engine = AgentEngine()
        r1 = engine.run_tool("scan_files", path=str(empty))
        assert r1 == []
        assert engine.state.approved_scan_roots == set(), "guard must stay closed"

        # An unrelated path must remain denied (guard never opened).
        other = tmp_path / "other"
        other.mkdir()
        (other / "f.txt").write_text("x")
        r2 = engine.run_tool("scan_files", path=str(other))
        assert r2 == []

    def test_unrelated_dir_scan_denied_after_initialize(self, tmp_path):
        """The agent cannot wander into an unrelated directory unprompted."""
        d1 = tmp_path / "d1"
        d1.mkdir()
        (d1 / "a.txt").write_text("a")
        d2 = tmp_path / "d2"
        d2.mkdir()
        (d2 / "b.txt").write_text("b")

        engine = AgentEngine()
        engine.initialize(str(d1))
        assert len(engine.state.scanned_files) == 1

        r2 = engine.run_tool("scan_files", path=str(d2))
        assert r2 == []  # d2 is not under the approved root (d1)
        assert len(engine.state.scanned_files) == 1  # inventory preserved

    def test_initialize_does_not_approve_forbidden_root(self, tmp_path, monkeypatch):
        """A forbidden dir (e.g. the user's home) can never become an approved
        root, even when handed to initialize()."""
        engine = AgentEngine()
        engine.initialize(str(Path.home()))
        assert str(Path.home()) not in engine.state.approved_scan_roots
        assert engine.state.approved_scan_roots == set()
