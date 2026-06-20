"""Tests for builder/tools/session.py and builder/tools/hitl.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, EntityProvenance, FileClassification


# =========================================================================
# save_session
# =========================================================================


class TestSaveSession:
    """Tests for the save_session function."""

    def test_creates_directory_and_files(self, tmp_path):
        """save_session creates sessions/<session_id>/ with crate_state.json."""
        from builder.tools import session as sess_mod

        sess_mod.SESSION_DIR = tmp_path / "sessions"
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