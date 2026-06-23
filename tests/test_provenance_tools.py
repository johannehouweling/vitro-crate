"""Tests for the LabProcess derivation-chain tools (Issue #88).

draft_file / link / check_provenance let the agent build and lint the
Sample →[CellCulture]→ Sample →[Exposure]→ table →[EndpointReadout]→ raw
→[DataAnalysis]→ figures provenance chain explicitly, rather than relying on
build-time synthesis that a weak model never triggers.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import PROVENANCE_RELATIONS, _REF_FIELDS
from builder.tools.provenance import check_provenance, draft_file, link


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


class TestProvenanceVocabulary:
    def test_provenance_relations_subset_of_ref_fields(self):
        # The edge verbs `link` accepts must be a strict subset of the reference
        # fields the crate mapping resolves, so the two can never drift.
        assert set(PROVENANCE_RELATIONS) <= set(_REF_FIELDS)


class TestDraftFile:
    def test_draft_file_creates_file_entity(self):
        state = CrateState()
        fe = draft_file(
            state,
            name="results.csv",
            path="data/results.csv",
            role="raw_data",
            encoding_format="text/csv",
        )
        assert fe.type == "File"
        assert fe.fields["name"] == "results.csv"
        assert fe.fields["dest_path"] == "data/results.csv"
        assert fe.fields["role"] == "raw_data"
        assert fe.fields["encodingFormat"] == "text/csv"
        # Added to state, retrievable as a File
        assert any(f.entity_id == fe.entity_id for f in state.list_entities("File"))

    def test_draft_file_name_only(self):
        state = CrateState()
        fe = draft_file(state, name="loose.csv")
        assert fe.type == "File"
        assert fe.fields["name"] == "loose.csv"
        assert "role" not in fe.fields


class TestLink:
    def _state(self):
        state = CrateState()
        state.add_entity(_ent("p", "LabProcess", process_type="DataAnalysis"))
        state.add_entity(_ent("f1", "File", name="a.csv"))
        state.add_entity(_ent("f2", "File", name="b.csv"))
        return state

    def test_link_sets_relation_field(self):
        state = self._state()
        link(state, "p", "object", "f1")
        assert state.get_entity("p").fields["object"] == "f1"

    def test_link_appends_second_value_as_list(self):
        state = self._state()
        link(state, "p", "object", "f1")
        link(state, "p", "object", "f2")
        assert set(state.get_entity("p").fields["object"]) == {"f1", "f2"}

    def test_link_unknown_relation_rejects(self):
        state = self._state()
        with pytest.raises(ValueError, match="relation"):
            link(state, "p", "frobnicate", "f1")

    def test_link_missing_target_rejects(self):
        state = self._state()
        with pytest.raises(ValueError, match="not found"):
            link(state, "p", "result", "does_not_exist")

    def test_link_missing_source_rejects(self):
        state = self._state()
        with pytest.raises(ValueError, match="not found"):
            link(state, "ghost", "result", "f1")


class TestCheckProvenance:
    def test_flags_dangling_readout(self):
        # An EndpointReadout consumes samples but produces no output (result):
        # the build mapping has no fallback for this type, so the chain dangles.
        state = CrateState()
        state.add_entity(_ent("er", "LabProcess", process_type="EndpointReadout"))
        state.add_entity(_ent("s", "Sample", name="exposed"))
        link(state, "er", "samples", "s")

        report = check_provenance(state)
        assert report["ok"] is False
        assert len(report["issues"]) == 1
        issue = report["issues"][0]
        assert issue["entity_id"] == "er"
        assert issue["property"] in ("result", "output")
        assert issue["severity"] == "required"
        assert "er" in issue["message"]
        assert issue["fix"]

    def test_flags_orphan_file(self):
        # A File referenced by no process and not in any hasPart is an orphan.
        state = CrateState()
        state.add_entity(_ent("orphan", "File", name="loose.csv"))

        report = check_provenance(state)
        assert report["ok"] is False
        assert len(report["issues"]) == 1
        issue = report["issues"][0]
        assert issue["entity_id"] == "orphan"
        assert issue["fix"]

    def test_orphan_file_cleared_when_referenced(self):
        state = CrateState()
        state.add_entity(_ent("da", "LabProcess", process_type="DataAnalysis"))
        state.add_entity(_ent("raw", "File", name="raw.csv"))
        state.add_entity(_ent("fig", "File", name="fig.png"))
        link(state, "da", "object", "raw")
        link(state, "da", "result", "fig")

        report = check_provenance(state)
        assert report["ok"] is True, report

    def test_passes_connected_chain(self):
        # Full Sample→CellCulture→Exposure→EndpointReadout→DataAnalysis chain,
        # every process output wired to the next input — zero issues.
        state = CrateState()
        state.add_entity(_ent("cc", "LabProcess", process_type="CellCulture"))
        state.add_entity(_ent("exp", "LabProcess", process_type="Exposure"))
        state.add_entity(_ent("er", "LabProcess", process_type="EndpointReadout"))
        state.add_entity(_ent("da", "LabProcess", process_type="DataAnalysis"))
        state.add_entity(_ent("cells", "CellLineSample", name="HepG2"))
        state.add_entity(_ent("cultured", "Sample", name="cultured"))
        state.add_entity(_ent("table", "File", name="conditions.csv"))
        state.add_entity(_ent("raw", "File", name="raw.csv"))
        state.add_entity(_ent("fig", "File", name="fig.png"))

        link(state, "cc", "object", "cells")
        link(state, "cc", "result", "cultured")
        link(state, "exp", "samples", "cultured")
        link(state, "exp", "result", "table")
        link(state, "er", "samples", "cultured")
        link(state, "er", "result", "raw")
        link(state, "da", "object", "raw")
        link(state, "da", "result", "fig")

        report = check_provenance(state)
        assert report["ok"] is True, report
        assert report["issues"] == []

    def test_removing_a_link_reintroduces_an_issue(self):
        # Guards that the connected-chain pass is not vacuous: drop the readout's
        # output and the dangling-readout issue returns.
        state = CrateState()
        state.add_entity(_ent("er", "LabProcess", process_type="EndpointReadout"))
        state.add_entity(_ent("cultured", "Sample", name="cultured"))
        state.add_entity(_ent("raw", "File", name="raw.csv"))
        link(state, "er", "samples", "cultured")
        link(state, "er", "result", "raw")
        assert check_provenance(state)["ok"] is True

        del state.get_entity("er").fields["result"]
        assert check_provenance(state)["ok"] is False
