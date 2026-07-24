"""Golden-crate regression tests over the VHP4Safety study fixtures (Issue #97).

A real study dataset that the toolbox must keep producing as a valid crate. These
run in CI with no network / no on-disk EBI data — the fixtures are self-contained.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from builder.tools.validation import build_and_validate
from tests.fixtures.vhps_golden_crates import VHPS_STUDIES, vhps_fixture_state


class TestSVhps21Golden:
    def test_builds_and_validates_clean_at_required(self):
        state = vhps_fixture_state("S-VHPS21")
        result = build_and_validate(state, severity="required")
        assert result["ok"] is True, result["issues"]
        assert result["conformance"] == {"base": True, "isa": True, "tox": True}
        assert result["issues"] == []


class TestFixtureFramework:
    def test_registry_is_non_empty_and_keyed_by_accession(self):
        assert VHPS_STUDIES
        for code, spec in VHPS_STUDIES.items():
            assert spec.accession == code

    @pytest.mark.parametrize("study_code", sorted(VHPS_STUDIES))
    def test_every_registered_study_builds_with_matching_accession(self, study_code):
        state = vhps_fixture_state(study_code)
        assert isinstance(state, CrateState)
        assert state.metadata.accession == study_code

    def test_unknown_study_code_raises(self):
        with pytest.raises(KeyError, match="S-VHPS99"):
            vhps_fixture_state("S-VHPS99")
