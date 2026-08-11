"""Tests for builder/tools/session.py and builder/tools/hitl.py."""

from __future__ import annotations

import json
import os

from builder.state import CrateState, Entity, EntityProvenance

# =========================================================================
# save_session
# =========================================================================


class TestSaveSession:
    """Tests for the save_session function."""

    def test_creates_directory_and_files(self, tmp_path):
        """save_session creates sessions/<session_id>/ with crate_state.json."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None
        state = CrateState()
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        result = sess_mod.save_session(state)

        assert result["success"] is True
        assert "session_id" in result
        session_id = result["session_id"]
        session_path = tmp_path / "sessions" / session_id
        assert session_path.is_dir(), "Session directory was not created"
        assert (session_path / "crate_state.json").is_file(), "crate_state.json was not created"
        assert (session_path / "session.log").is_file(), "session.log was not created"

    def test_writes_valid_json_roundtrip(self, tmp_path):
        """save_session writes valid JSON that can be round-tripped."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None
        state = CrateState()
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Test Investigation", "description": "A test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        result = sess_mod.save_session(state)
        session_path = tmp_path / "sessions" / result["session_id"]

        with open(session_path / "crate_state.json") as f:
            data = json.load(f)

        assert data["session_id"] == state.session_id
        assert "entities" in data
        assert "investigations" in data["entities"]
        assert len(data["entities"]["investigations"]) == 1
        assert data["entities"]["investigations"][0]["entity_id"] == "inv_001"
        assert data["entities"]["investigations"][0]["fields"]["title"] == "Test Investigation"

    # ------------------------------------------------------------------
    # Atomic write tests
    # ------------------------------------------------------------------

    def test_atomic_write_uses_temp_file_and_rename(self, tmp_path, monkeypatch):
        """save_session writes to a temp file then renames atomically."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        replace_calls: list[tuple[str, str]] = []
        original_replace = os.replace

        def _tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", _tracking_replace)

        state = CrateState()
        state.session_id = "atomic_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Atomic Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        sess_mod.save_session(state)
        session_path = tmp_path / "sessions" / "atomic_test"
        state_path = str(session_path / "crate_state.json")

        assert len(replace_calls) >= 1
        src, dst = replace_calls[0]
        assert dst == state_path
        assert "tmp" in src.lower() or src.endswith(".tmp")

    def test_crash_during_write_does_not_truncate_existing(self, tmp_path, monkeypatch):
        """If writing the temp file fails, the original crate_state.json stays intact."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        # First save — write initial state
        state = CrateState()
        state.session_id = "crash_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Initial"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)
        sess_mod.save_session(state)

        session_path = tmp_path / "sessions" / "crash_test"
        state_path = session_path / "crate_state.json"

        with open(state_path) as f:
            f.read()

        # Modify state so change-detection does not skip
        entity.fields["title"] = "Updated"

        # Make os.replace() fail after the temp file has been written
        def _failing_replace(src, dst):
            raise OSError("Disk full! Cannot rename temp file.")

        monkeypatch.setattr(os, "replace", _failing_replace)

        result = sess_mod.save_session(state)
        assert result["success"] is False
        assert "error" in result
        assert "Disk full" in result["error"]

        # Original file must still be intact
        with open(state_path) as f:
            after_content = f.read()
        # Should still contain the old content (not truncated)
        assert "Initial" in after_content
        assert "Updated" not in after_content

    # ------------------------------------------------------------------
    # fsync test
    # ------------------------------------------------------------------

    def test_fsync_is_called_on_temp_file(self, tmp_path, monkeypatch):
        """save_session calls os.fsync() on the temp file descriptor before rename."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        fsync_called: list[int] = []
        original_fsync = os.fsync

        def _tracking_fsync(fd):
            fsync_called.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", _tracking_fsync)

        state = CrateState()
        state.session_id = "fsync_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Fsync Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        sess_mod.save_session(state)
        assert len(fsync_called) >= 1, "os.fsync() was not called"

    # ------------------------------------------------------------------
    # Change-aware saving tests
    # ------------------------------------------------------------------

    def test_skips_write_when_state_unchanged(self, tmp_path):
        """save_session skips the file write when the state has not changed."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        state = CrateState()
        state.session_id = "dedup_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        # First save — actually writes
        result1 = sess_mod.save_session(state)
        assert result1["success"] is True

        session_path = tmp_path / "sessions" / "dedup_test"
        state_path = session_path / "crate_state.json"
        mtime_before = state_path.stat().st_mtime_ns

        # Second save with same state — should skip
        result2 = sess_mod.save_session(state)
        assert result2["success"] is True
        assert result2.get("skipped") is True

        mtime_after = state_path.stat().st_mtime_ns
        assert mtime_after == mtime_before, "File was modified even though state unchanged"

    def test_writes_when_state_changes(self, tmp_path):
        """save_session writes when the state content has changed."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        state = CrateState()
        state.session_id = "change_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "First"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        # First save
        sess_mod.save_session(state)
        session_path = tmp_path / "sessions" / "change_test"
        state_path = session_path / "crate_state.json"

        with open(state_path) as f:
            first_content = json.load(f)

        # Modify state
        entity.fields["title"] = "Updated"

        # Second save — should write
        result = sess_mod.save_session(state)
        assert result["success"] is True
        assert result.get("skipped") is False

        with open(state_path) as f:
            data = json.load(f)

        # Content must reflect the change
        assert data["entities"]["investigations"][0]["fields"]["title"] == "Updated"
        # The file must have been re-written (different content than first save)
        assert data != first_content

    # ------------------------------------------------------------------
    # Error handling tests
    # ------------------------------------------------------------------

    def test_returns_error_on_write_failure(self, tmp_path, monkeypatch):
        """save_session returns error dict when write fails."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        def _failing_replace(*args, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(os, "replace", _failing_replace)

        state = CrateState()
        state.session_id = "fail_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Fail Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        result = sess_mod.save_session(state)
        assert result["success"] is False
        assert "error" in result
        assert "Permission denied" in result["error"]

    def test_first_save_works_with_no_previous_state(self, tmp_path):
        """save_session succeeds on first call (no previous state to compare)."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        state = CrateState()
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "First"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        result = sess_mod.save_session(state)
        assert result["success"] is True
        assert "session_id" in result


# =========================================================================
# save_session force (always_write parameter)
# =========================================================================


class TestSaveSessionForce:
    """Tests for forced save (always_write parameter)."""

    def test_force_always_writes_even_if_unchanged(self, tmp_path, monkeypatch):
        """When always_write=True, the save happens regardless of state changes."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        state = CrateState()
        state.session_id = "force_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        sess_mod.save_session(state)
        session_path = tmp_path / "sessions" / "force_test"
        state_path = session_path / "crate_state.json"

        # Count the atomic commit (os.replace of the temp file over
        # crate_state.json) rather than comparing st_mtime_ns before/after.
        # File timestamps come from the kernel's *coarse* clock, which only
        # advances once per tick -- measured at ~4.1ms on the CI filesystem --
        # so two saves microseconds apart stamp an IDENTICAL mtime and a
        # strict `>` fails at random (~57% of back-to-back writes collide).
        # The claim under test is "the write happened", so observe the write.
        replaced_paths: list[str] = []
        real_replace = os.replace

        def _counting_replace(src, dst):
            replaced_paths.append(str(dst))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _counting_replace)

        # Control: identical content without the flag must skip the write
        # entirely, otherwise the forced write below would prove nothing.
        unforced = sess_mod.save_session(state)
        assert unforced["skipped"] is True
        assert str(state_path) not in replaced_paths, (
            "Unchanged state should not be rewritten without always_write"
        )

        result = sess_mod.save_session(state, always_write=True)

        assert result["success"] is True
        assert result["skipped"] is False
        assert replaced_paths.count(str(state_path)) == 1, (
            "always_write=True must rewrite crate_state.json even though the "
            "content hash is unchanged"
        )


# =========================================================================
# agent_loop save failure handling — logged, not swallowed
# =========================================================================


class TestSessionSaveFailures:
    """Tests that save failures are surfaced in the result dict."""

    def test_save_failure_returns_error_dict(self, tmp_path, monkeypatch):
        """When save_session fails, the error info is in the returned dict."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
        sess_mod._last_saved_state_hash = None

        def _failing_replace(src, dst):
            raise OSError("Disk quota exceeded")

        monkeypatch.setattr(os, "replace", _failing_replace)

        state = CrateState()
        state.session_id = "log_fail_test"
        entity = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"title": "Test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(entity)

        result = sess_mod.save_session(state)
        assert result["success"] is False
        assert "error" in result
        assert "Disk quota exceeded" in result["error"]
