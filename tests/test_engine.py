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

    def test_reasoning_log_entry_includes_call_args(self):
        """Issue #240: each reasoning_log entry records the tool ARGUMENTS, not
        just the result, so you can see which path read_file was called with.
        """
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("draft_investigation", hints={"name": "Hepatotox screen"})
        step = engine.state.checkpoint.reasoning_log[-1]
        assert step.tool == "draft_investigation"
        # The recorded action/args names the argument that was passed.
        assert "Hepatotox screen" in step.action

    def test_reasoning_log_args_are_bounded(self):
        """A huge argument is truncated so reasoning_log entries stay small."""
        engine = AgentEngine()
        engine.initialize()
        huge = "z" * 5000
        engine.run_tool("draft_investigation", hints={"name": huge})
        step = engine.state.checkpoint.reasoning_log[-1]
        # The action (which embeds the args repr) is bounded.
        assert len(step.action) < 1000

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
        """run_tool preserves scanned_files when a scan of an unapproved path is
        refused (no interactive human, nothing approved).

        The default engine is non-interactive and ``/tmp/does-not-matter`` is not
        an approved root, so the scan is refused before scan_files is ever called.
        The refusal surfaces a reason dict (not a silent ``[]``/``None``) and the
        existing inventory is left untouched.
        """
        engine = AgentEngine()
        engine.state.scanned_files = ["existing"]  # ty: ignore

        def fake_scan_files(**kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("scan_files must not run for an unapproved path")

        monkeypatch.setattr("builder.tools.scanner.scan_files", fake_scan_files)

        result = engine.run_tool("scan_files", path="/tmp/does-not-matter")

        assert isinstance(result, dict)
        assert "error" in result
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
        """A denied scan must NOT wipe a populated inventory with zero.

        The default engine is non-interactive and ``/tmp/denied`` is unapproved,
        so the scan is refused with a reason dict before scan_files runs; the
        existing inventory survives untouched.
        """
        engine = AgentEngine()
        engine.state.scanned_files = ["existing1", "existing2"]  # ty: ignore

        monkeypatch.setattr(
            "builder.tools.scanner.scan_files",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not scan")),
        )

        result = engine.run_tool("scan_files", path="/tmp/denied")

        assert isinstance(result, dict) and "error" in result  # refused, not silent
        assert engine.state.scanned_files == ["existing1", "existing2"]

    def test_agent_scan_with_no_roots_refuses_and_does_not_autoapprove(self, tmp_path):
        """Fail-closed: with no approved roots and no interactive human, the
        agent's scan is refused (with a reason) and no root is added."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_text("a")

        engine = AgentEngine()
        assert engine.state.approved_scan_roots == set()

        r = engine.run_tool("scan_files", path=str(d))

        assert isinstance(r, dict) and "error" in r  # refused with a reason
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
        assert isinstance(r, dict) and "error" in r  # refused (parent unapproved)
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
        assert isinstance(r1, dict) and "error" in r1  # refused (non-interactive)
        assert engine.state.approved_scan_roots == set(), "guard must stay closed"

        # An unrelated path must remain denied (guard never opened).
        other = tmp_path / "other"
        other.mkdir()
        (other / "f.txt").write_text("x")
        r2 = engine.run_tool("scan_files", path=str(other))
        assert isinstance(r2, dict) and "error" in r2

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
        assert isinstance(r2, dict) and "error" in r2  # d2 not under approved d1
        assert len(engine.state.scanned_files) == 1  # inventory preserved

    def test_initialize_does_not_approve_forbidden_root(self, tmp_path, monkeypatch):
        """A forbidden dir (e.g. the user's home) can never become an approved
        root, even when handed to initialize()."""
        engine = AgentEngine()
        engine.initialize(str(Path.home()))
        assert str(Path.home()) not in engine.state.approved_scan_roots
        assert engine.state.approved_scan_roots == set()


class _RecordingHuman:
    """Interactive HITL test double with a scripted present() decision.

    ``is_interactive = True`` so the engine treats it as a REAL user able to
    approve a new scan root. Records every ``present`` call so tests can assert
    the engine prompted (or, when already approved, did NOT prompt).
    """

    is_interactive: bool = True

    def __init__(self, action: str = "approved") -> None:
        self._action = action
        self.present_calls: list[dict[str, object]] = []

    def present(self, context, options=None, purpose=None):
        self.present_calls.append({"context": context, "purpose": purpose})
        return {"action": self._action, "comments": None, "edits": None}

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


class TestScanRootApproval:
    """Prompt-once, children-only approval for a user-submitted scan folder.

    The gap: a folder the user points the agent at — one NOT passed via
    ``--input`` — was refused with a SILENT empty ``[]``. The fix prompts a REAL
    interactive human once; on approval the SUBMITTED directory (children only,
    never the parent) is added to ``approved_scan_roots`` and the scan proceeds.
    Non-interactive runs (SimulatedHumanInterface / None) keep failing closed,
    and a refusal always surfaces a human-readable reason (never a silent empty).
    """

    def test_interactive_approve_adds_dir_and_returns_files(self, tmp_path):
        """A real user approving a submitted dir -> dir (not parent) approved and
        the scan returns the files."""
        d = tmp_path / "submitted"
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "b.csv").write_text("x,y\n1,2\n")

        human = _RecordingHuman(action="approved")
        engine = AgentEngine(human_interface=human)
        assert engine.state.approved_scan_roots == set()

        result = engine.run_tool("scan_files", path=str(d))

        # Prompted exactly once, as a scan_root escalation.
        from builder.tools.hitl import SCAN_ROOT_PURPOSE

        assert len(human.present_calls) == 1
        assert human.present_calls[0]["purpose"] == SCAN_ROOT_PURPOSE
        assert str(d) in str(human.present_calls[0]["context"])  # names the path

        # The submitted dir is now approved and the scan returned the files.
        assert str(d.resolve()) in engine.state.approved_scan_roots
        assert isinstance(result, list)
        assert len(result) == 2
        assert len(engine.state.scanned_files) == 2

    def test_interactive_approve_does_not_approve_parent(self, tmp_path):
        """Approving /a/b/c approves c only: the parent /a/b is NOT scannable,
        but a child /a/b/c/child IS (children-only invariant)."""
        parent = tmp_path / "parent"
        child = parent / "child"
        grand = child / "grand"
        grand.mkdir(parents=True)
        (parent / "secret.txt").write_text("do not read me")
        (child / "ok.txt").write_text("ok")
        (grand / "deep.txt").write_text("deep")

        human = _RecordingHuman(action="approved")
        engine = AgentEngine(human_interface=human)

        # User submits the child dir.
        engine.run_tool("scan_files", path=str(child))
        assert str(child.resolve()) in engine.state.approved_scan_roots
        assert str(parent.resolve()) not in engine.state.approved_scan_roots

        # A descendant of the approved child is scannable WITHOUT a new prompt.
        prompts_before = len(human.present_calls)
        r_grand = engine.run_tool("scan_files", path=str(grand))
        assert isinstance(r_grand, list) and len(r_grand) == 1
        assert len(human.present_calls) == prompts_before, "descendant must not re-prompt"

        # The PARENT is NOT covered by the child approval — scanning it prompts
        # again; reject it so secret.txt stays out of reach.
        human._action = "rejected"
        r_parent = engine.run_tool("scan_files", path=str(parent))
        assert isinstance(r_parent, dict) and "error" in r_parent
        assert str(parent.resolve()) not in engine.state.approved_scan_roots

    def test_interactive_reject_does_not_approve_and_surfaces_reason(self, tmp_path):
        """A real user declining -> not approved, refusal surfaced (not a silent
        empty success)."""
        d = tmp_path / "submitted"
        d.mkdir()
        (d / "a.txt").write_text("a")

        human = _RecordingHuman(action="rejected")
        engine = AgentEngine(human_interface=human)

        result = engine.run_tool("scan_files", path=str(d))

        assert len(human.present_calls) == 1
        assert str(d.resolve()) not in engine.state.approved_scan_roots
        assert isinstance(result, dict) and "error" in result
        # The reason is human-readable and names what happened.
        reason = result["error"].lower()
        assert "refused" in reason and "approval" in reason
        assert engine.state.scanned_files == []

    def test_non_interactive_simulated_never_auto_approves(self, tmp_path):
        """SimulatedHumanInterface (eval/batch) NEVER auto-approves: the scan is
        refused (fail-closed for #197/#198) and a reason is surfaced."""
        from builder.tools.hitl import SimulatedHumanInterface

        d = tmp_path / "submitted"
        d.mkdir()
        (d / "a.txt").write_text("a")

        engine = AgentEngine(human_interface=SimulatedHumanInterface())
        result = engine.run_tool("scan_files", path=str(d))

        assert str(d.resolve()) not in engine.state.approved_scan_roots
        assert isinstance(result, dict) and "error" in result
        assert engine.state.scanned_files == []

    def test_interactive_approve_of_forbidden_root_is_refused(self, tmp_path):
        """Even with an affirmative answer, a bare/forbidden root (home dir) can
        NEVER be approved — the denylist stands."""
        human = _RecordingHuman(action="approved")
        engine = AgentEngine(human_interface=human)

        result = engine.run_tool("scan_files", path=str(Path.home()))

        assert str(Path.home()) not in engine.state.approved_scan_roots
        assert engine.state.approved_scan_roots == set()
        assert isinstance(result, dict) and "error" in result

    def test_already_approved_path_does_not_prompt(self, tmp_path):
        """A path already inside an approved root (via --input) scans directly,
        with NO prompt."""
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_text("a")
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")

        human = _RecordingHuman(action="approved")
        engine = AgentEngine(human_interface=human)
        engine.initialize(str(d))  # --input path: approves d, no prompt
        assert str(d.resolve()) in engine.state.approved_scan_roots
        assert human.present_calls == []  # initialize must not prompt

        # A subdir of the approved root scans directly, still no prompt.
        r = engine.run_tool("scan_files", path=str(sub))
        assert isinstance(r, list) and len(r) == 1
        assert human.present_calls == [], "already-approved path must not prompt"


class TestOnToolEvent:
    """The optional ``on_tool_event`` callback lets the pipeline spinner show the
    currently-running tool (#266). The deterministic pipeline runs tools via
    ``engine.run_tool`` (not LangChain), so without this hook there is no
    per-tool signal. It must default to ``None`` (no behavior change when unset)
    and must never let a callback exception break ``run_tool``.
    """

    def test_default_on_tool_event_is_none(self) -> None:
        """A fresh engine has no tool-event callback (strict no-op by default)."""
        engine = AgentEngine()
        assert engine.on_tool_event is None

    def test_callback_fires_start_then_end_when_set(self) -> None:
        """The callback receives (tool_name, 'start', args) then (tool_name, 'end', '')."""
        engine = AgentEngine()
        engine.initialize()
        events: list[tuple[str, str, str]] = []
        engine.on_tool_event = lambda name, phase, args_str: events.append((name, phase, args_str))

        engine.run_tool("draft_investigation", hints={"name": "Inv"})

        assert events == [
            ("draft_investigation", "start", "Inv"),
            ("draft_investigation", "end", ""),
        ]

    def test_end_fires_even_when_tool_raises(self) -> None:
        """The 'end' event still fires when the tool body raises (finally-guarded)."""
        import pytest

        engine = AgentEngine()
        engine.initialize()
        events: list[tuple[str, str, str]] = []
        engine.on_tool_event = lambda name, phase, args_str: events.append((name, phase, args_str))

        with pytest.raises(ValueError):
            engine.run_tool("nonexistent_tool_xyz")

        # start fired before the lookup; end still fired despite the raise.
        assert ("nonexistent_tool_xyz", "start", "") in events
        assert ("nonexistent_tool_xyz", "end", "") in events

    def test_unset_callback_no_calls_no_error(self) -> None:
        """With no callback set, run_tool behaves exactly as before (no error)."""
        engine = AgentEngine()
        engine.initialize()
        # No callback set; must run cleanly and return the entity.
        result = engine.run_tool("draft_investigation", hints={"name": "X"})
        assert result is not None
        assert result.type == "Investigation"

    def test_raising_callback_does_not_break_run_tool(self) -> None:
        """A callback that raises must not abort the tool call (#266)."""
        engine = AgentEngine()
        engine.initialize()

        def _boom(_name: str, _phase: str, _args_str: str = "") -> None:
            raise RuntimeError("spinner blew up")

        engine.on_tool_event = _boom
        # The tool still runs and returns its result despite the bad callback.
        result = engine.run_tool("draft_investigation", hints={"name": "Y"})
        assert result is not None
        assert result.fields.get("name") == "Y"
