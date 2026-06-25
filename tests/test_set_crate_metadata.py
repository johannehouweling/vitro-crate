"""Tests for set_crate_metadata + CrateMetadata date fields (#180).

The gold S-VHPS21 root carries ``releaseDate`` / ``dateModified``. These live in
``CrateState.metadata`` as ``release_date`` / ``date_modified`` and are set via
the ``set_crate_metadata`` state-mutating tool. The dataclass defaults them to
``None`` so old saved sessions (which lack the keys) still load, and only the
fields actually passed are mutated (D5: never fabricate dates).
"""

from __future__ import annotations

from builder.state import CrateMetadata, CrateState
from builder.tools.management import set_crate_metadata


class TestCrateMetadataDateFields:
    def test_new_date_fields_default_to_none(self):
        m = CrateMetadata()
        assert m.release_date is None
        assert m.date_modified is None

    def test_to_dict_omits_unset_date_fields(self):
        m = CrateMetadata(title="t")
        d = m.to_dict()
        assert "release_date" not in d
        assert "date_modified" not in d

    def test_to_dict_includes_set_date_fields(self):
        m = CrateMetadata(release_date="2025-11-10", date_modified="2026-06-14T19:37:30Z")
        d = m.to_dict()
        assert d["release_date"] == "2025-11-10"
        assert d["date_modified"] == "2026-06-14T19:37:30Z"

    def test_from_dict_round_trips_date_fields(self):
        m = CrateMetadata(
            title="t", release_date="2025-11-10", date_modified="2026-06-14T19:37:30Z"
        )
        restored = CrateMetadata.from_dict(m.to_dict())
        assert restored.release_date == "2025-11-10"
        assert restored.date_modified == "2026-06-14T19:37:30Z"

    def test_from_dict_back_compat_old_session_without_date_keys(self):
        # An old saved session's metadata blob has no release_date/date_modified
        # keys at all; it must still load with the fields defaulting to None.
        legacy = {"title": "Old", "description": "d", "input_type": "directory"}
        restored = CrateMetadata.from_dict(legacy)
        assert restored.title == "Old"
        assert restored.release_date is None
        assert restored.date_modified is None


class TestCrateStateRoundTrip:
    def test_round_trip_with_date_fields(self):
        state = CrateState(session_id="s1")
        state.metadata.title = "Dated"
        state.metadata.release_date = "2025-11-10"
        state.metadata.date_modified = "2026-06-14T19:37:30Z"

        restored = CrateState.from_json(state.to_json())
        assert restored.metadata.release_date == "2025-11-10"
        assert restored.metadata.date_modified == "2026-06-14T19:37:30Z"

    def test_round_trip_without_date_fields(self):
        # Back-compat: a state with no dates set must round-trip with None dates.
        state = CrateState(session_id="s2")
        state.metadata.title = "Undated"

        restored = CrateState.from_json(state.to_json())
        assert restored.metadata.release_date is None
        assert restored.metadata.date_modified is None

    def test_load_legacy_state_json_without_metadata_date_keys(self):
        # Simulate a session saved before the date fields existed.
        import json

        state = CrateState(session_id="s3")
        state.metadata.title = "Legacy"
        blob = json.loads(state.to_json())
        blob["metadata"].pop("release_date", None)
        blob["metadata"].pop("date_modified", None)

        restored = CrateState.from_dict(blob)
        assert restored.metadata.title == "Legacy"
        assert restored.metadata.release_date is None
        assert restored.metadata.date_modified is None


class TestSetCrateMetadataTool:
    def test_sets_release_and_modified_dates(self):
        state = CrateState()
        result = set_crate_metadata(
            state,
            release_date="2025-11-10",
            date_modified="2026-06-14T19:37:30Z",
        )
        assert state.metadata.release_date == "2025-11-10"
        assert state.metadata.date_modified == "2026-06-14T19:37:30Z"
        # The tool returns a token-bounded summary of what it set.
        assert result["release_date"] == "2025-11-10"
        assert result["date_modified"] == "2026-06-14T19:37:30Z"

    def test_only_passed_fields_are_mutated(self):
        # D5: omitting a field leaves it untouched — never fabricated/cleared.
        state = CrateState()
        state.metadata.release_date = "2025-01-01"
        set_crate_metadata(state, date_modified="2026-06-14T19:37:30Z")
        assert state.metadata.release_date == "2025-01-01"
        assert state.metadata.date_modified == "2026-06-14T19:37:30Z"

    def test_can_also_set_title_description_accession(self):
        state = CrateState()
        set_crate_metadata(
            state, title="My crate", description="A desc", accession="S-VHPS21"
        )
        assert state.metadata.title == "My crate"
        assert state.metadata.description == "A desc"
        assert state.metadata.accession == "S-VHPS21"

    def test_no_args_is_noop_and_returns_current(self):
        state = CrateState()
        state.metadata.title = "Existing"
        result = set_crate_metadata(state)
        assert state.metadata.title == "Existing"
        # Nothing set → returned summary carries no fabricated values.
        assert result.get("release_date") is None
        assert result.get("date_modified") is None

    def test_empty_string_is_ignored(self):
        # An empty string is not a date; treat it like an omitted field (D5).
        state = CrateState()
        state.metadata.release_date = "2025-01-01"
        set_crate_metadata(state, release_date="")
        assert state.metadata.release_date == "2025-01-01"


class TestToolRegistration:
    def test_tool_is_registered(self):
        import builder.tools.management  # noqa: F401  (triggers registration)
        from builder.tools.registry import TOOL_REGISTRY

        assert "set_crate_metadata" in TOOL_REGISTRY
        spec = TOOL_REGISTRY.get_spec("set_crate_metadata")
        assert spec is not None
        assert spec.takes_state is True

    def test_tool_in_specs(self):
        from builder.agents.tools_spec import TOOL_SPECS

        assert any(s["name"] == "set_crate_metadata" for s in TOOL_SPECS)
