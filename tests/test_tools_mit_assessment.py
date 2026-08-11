"""Tests for builder/tools/mit_assessment.py — assess_mit_coverage tool."""

from __future__ import annotations

from pathlib import Path

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance, MITReport
from builder.tools._crate_mapping import populate_crate
from builder.tools.mit_assessment import assess_mit_coverage
from profiles.context import ISA_TOX_CONTEXT
from tests.fixtures.vhps_golden_crates import vhps_fixture_state


def _assembled_graph(state: CrateState, tmp_path: Path) -> list[dict]:
    """Serialize *state* to an RO-Crate ``@graph`` (the assessment's real input)."""
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, tmp_path, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


class TestAssessMITCoverage:
    """Tests for assess_mit_coverage — compares entity fields against MIT YAML."""

    def test_returns_mit_report(self):
        """assess_mit_coverage returns an MITReport dataclass."""
        state = CrateState()
        result = assess_mit_coverage(state)

        assert isinstance(result, MITReport)

    def test_empty_state_returns_zero_score(self):
        """Empty state returns overall_score of 0.0 and empty module_scores."""
        state = CrateState()
        result = assess_mit_coverage(state)

        assert result.overall_score == 0.0
        assert isinstance(result.module_scores, dict)

    def test_populated_state_has_module_scores(self):
        """State with entities yields per-module scores."""
        state = CrateState()

        # Add a MolecularEntity with some fields filled
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={"name": "Test Compound", "smiles": "CCO"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.set_field_status("name", "filled", "llm")
        chem.set_field_status("smiles", "filled", "llm")
        state.add_entity(chem)

        # Add an Investigation with some fields
        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "Test Study", "description": "A test"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.set_field_status("name", "filled", "llm")
        inv.set_field_status("description", "filled", "llm")
        state.add_entity(inv)

        result = assess_mit_coverage(state)

        # Should have module scores
        assert len(result.module_scores) > 0
        # Overall score should be > 0 since we have some filled fields
        assert result.overall_score > 0.0

    def test_some_filled_fields_produces_partial_score(self):
        """State with some filled entities yields a partial overall_score < 1.0."""
        state = CrateState()

        # Add a MolecularEntity with some fields filled
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.fields["name"] = "Test Compound"
        chem.set_field_status("name", "filled", "llm")
        chem.fields["identifier"] = "CAS-123"
        chem.set_field_status("identifier", "filled", "llm")
        chem.fields["formula"] = "C2H6O"
        chem.set_field_status("formula", "filled", "llm")
        chem.fields["smiles"] = "CCO"
        chem.set_field_status("smiles", "filled", "llm")
        state.add_entity(chem)

        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.fields["name"] = "Test Study"
        inv.set_field_status("name", "filled", "llm")
        inv.fields["description"] = "A test"
        inv.set_field_status("description", "filled", "llm")
        state.add_entity(inv)

        result = assess_mit_coverage(state)

        # Score should be > 0 since we have some filled fields
        assert result.overall_score > 0.0
        # But less than 1.0 since many modules have zero coverage
        assert result.overall_score < 1.0

    def test_real_assembled_crate_has_nonzero_coverage(self, tmp_path):
        """A real golden crate scores non-zero MIT coverage when assessed against
        its assembled @graph — the crate_slot vocabulary describes the serialized
        crate (schema.org properties + additionalType), not the CrateState (#311)."""
        state = vhps_fixture_state("S-VHPS21")
        graph = _assembled_graph(state, tmp_path)
        result = assess_mit_coverage(state, graph=graph)
        assert result.overall_score > 0.0
        assert any(sc["completed"] > 0 for sc in result.module_scores.values())

    def test_graph_path_credits_domain_slots(self, tmp_path):
        """The graph matcher credits real ISA-Tox slots: the Exposure LabProcess's
        `parameter` (crate_slot `LabProcessExposure:param`) and the cell line's
        `sampleType` (`CellLineSample:sampleType`) are counted from the @graph."""
        state = vhps_fixture_state("S-VHPS21")
        graph = _assembled_graph(state, tmp_path)
        by_graph = assess_mit_coverage(state, graph=graph)
        by_state = assess_mit_coverage(state)  # legacy fallback, unchanged
        # The graph path finds domain coverage the state-field path cannot.
        assert by_graph.overall_score > by_state.overall_score

    def test_module_totals_stable_across_paths(self, tmp_path):
        """Switching to graph matching must not change the checklist size — only
        how many slots are credited. Per-module `total` stays identical."""
        state = vhps_fixture_state("S-VHPS21")
        graph = _assembled_graph(state, tmp_path)
        g = assess_mit_coverage(state, graph=graph).module_scores
        s = assess_mit_coverage(state).module_scores
        assert {k: v["total"] for k, v in g.items()} == {k: v["total"] for k, v in s.items()}

    def test_produces_correct_module_scores(self):
        """Verify module scores structure is correct."""
        state = CrateState()

        # Add a MolecularEntity with 2 fields (Chemical Information module)
        chem = Entity(
            entity_id="chem_001",
            type="MolecularEntity",
            fields={},
            _provenance=EntityProvenance(created_by="llm"),
        )
        chem.fields["name"] = "Test"
        chem.set_field_status("name", "filled", "llm")
        chem.fields["identifier"] = "CAS-123"
        chem.set_field_status("identifier", "filled", "llm")
        state.add_entity(chem)

        result = assess_mit_coverage(state)

        # Should have module scores with the expected structure
        for module_name, scores in result.module_scores.items():
            assert "completed" in scores
            assert "total" in scores
            assert isinstance(scores["completed"], int)
            assert isinstance(scores["total"], int)
            assert scores["completed"] <= scores["total"]


class TestUncuratedSlots:
    """An uncurated parameter is skipped, never a crash.

    The shipped checklist omits ``crate_slot`` on uncurated parameters, but a
    curator writing an explicit ``crate_slot: null`` (or a bare
    ``crate_slot:``) hands the parser ``None`` — both spellings must mean
    "not scorable", not an AttributeError that takes down every
    ``assess_mit_coverage`` call and with it the maturity-report embed.
    """

    def test_explicit_null_crate_slot_is_skipped_not_a_crash(self):
        from builder.tools.mit_assessment import iter_scorable_params

        mit_data = {
            "modules": [
                {
                    "id": "m",
                    "name": "M",
                    "sections": [
                        {
                            "parameters": [
                                {"id": "uncurated", "crate_slot": None},
                                {"id": "curated", "crate_slot": "Investigation:name"},
                            ]
                        }
                    ],
                }
            ]
        }
        scorable = [p["id"] for _m, p, _s in iter_scorable_params(mit_data)]
        assert scorable == ["curated"]

    def test_parse_crate_slots_tolerates_none(self):
        from builder.tools.mit_assessment import parse_crate_slots

        assert parse_crate_slots(None) == []


class TestGuidanceDocumentCoverage:
    """#491: coverage is also bucketed per guidance document.

    Every checklist parameter carries a ``standards`` map naming which of the
    seven guidance documents require it. The scorer buckets the same
    scorable-parameter walk per document so the maturity report can show which
    document a crate satisfies. Documents overlap — one parameter can be
    required by several — so buckets deliberately do not sum to the total.
    """

    def test_all_seven_documents_get_a_bucket(self):
        result = assess_mit_coverage(CrateState())
        assert set(result.standard_scores) == {
            "oecd_gd211",
            "lincs",
            "toxtemp",
            "nature",
            "oecd_gd34",
            "oecd_gd417",
            "oecd_oht201",
        }
        for key, bucket in result.standard_scores.items():
            assert bucket["completed"] == 0, key
            assert bucket["total"] > 0, key

    def test_totals_rederived_from_the_raw_checklist(self):
        """Drift guard: each document's denominator is exactly the scorable
        parameters that document marks true — re-derived here from the raw
        YAML (the ``TestMitSingleOwner`` traversal pattern)."""
        from builder.tools.mit_assessment import iter_scorable_params, load_mit_yaml

        mit_data = load_mit_yaml()
        assert mit_data is not None, "shipped MIT YAML must load"
        expected: dict[str, int] = {}
        for _module, param, _slots in iter_scorable_params(mit_data):
            for key, flagged in (param.get("standards") or {}).items():
                if flagged is True:
                    expected[key] = expected.get(key, 0) + 1

        result = assess_mit_coverage(CrateState())
        assert {k: b["total"] for k, b in result.standard_scores.items()} == expected

    def test_filled_slot_credits_exactly_its_documents(self):
        """Filling the Investigation title credits the documents that require
        ``experiment_name`` (OECD GD 211, ToxTemp, OECD GD 34) and cannot touch
        LINCS, whose parameters are all cell-line slots."""
        state = CrateState()
        inv = Entity(
            entity_id="inv_001",
            type="Investigation",
            fields={"name": "FRTL-5 perchlorate thyroid study"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        inv.set_field_status("name", "filled", "llm")
        state.add_entity(inv)

        buckets = assess_mit_coverage(state).standard_scores
        for key in ("oecd_gd211", "toxtemp", "oecd_gd34"):
            assert buckets[key]["completed"] >= 1, key
        assert buckets["lincs"]["completed"] == 0

    def test_standard_scores_survive_serialization(self):
        report = MITReport(
            module_scores={"m": {"completed": 1, "total": 2}},
            overall_score=0.5,
            standard_scores={"oecd_gd211": {"completed": 1, "total": 42}},
        )
        assert MITReport.from_dict(report.to_dict()) == report

    def test_old_serialized_reports_deserialize_without_the_field(self):
        """crate_state.json written before the split must still load."""
        report = MITReport.from_dict({"module_scores": {}, "overall_score": 0.0})
        assert report.standard_scores == {}


class TestPlaceholderValuesAreNotCredited:
    """#377: a build-time placeholder must not count as a filled MIT slot.

    The assembly synthesizes `name = "Untitled Investigation"` on the root when
    no title is set (`_crate_mapping.py`), so a graph-based match would credit
    `Investigation:name` on a crate that has no title at all — and, once the gap
    engine shares this matcher, would silently stop asking the user for it.

    This is the same class the module already guards against for
    `conditionsOfAccess` vs the always-present default `license`.
    """

    @staticmethod
    def _untitled_state():
        from builder.state import CrateState, Entity, EntityProvenance

        def ent(eid, t, **f):
            e = Entity(
                entity_id=eid, type=t, fields=dict(f),
                _provenance=EntityProvenance(created_by="llm"),
            )
            for k in f:
                e.set_field_status(k, "filled", "llm")
            return e

        state = CrateState()
        state.add_entity(ent("inv1", "Investigation", description="d", identifier="INV-1"))
        state.add_entity(ent("st1", "Study", description="d", investigation_id="inv1"))
        state.add_entity(ent("as1", "Assay", study_id="st1"))
        return state

    def test_placeholder_root_name_is_not_a_filled_slot(self):
        from builder.tools.mit_assessment import _assemble_graph, slot_matcher

        state = self._untitled_state()
        matcher = slot_matcher(state, graph={"@graph": _assemble_graph(state)})
        assert matcher("Investigation", "name") is False

    def test_a_real_title_is_still_credited(self):
        """Honesty control: the guard rejects the placeholder, not every name."""
        from builder.tools.mit_assessment import _assemble_graph, slot_matcher

        state = self._untitled_state()
        state.metadata.title = "FRTL-5 perchlorate thyroid study"
        matcher = slot_matcher(state, graph={"@graph": _assemble_graph(state)})
        assert matcher("Investigation", "name") is True


    def test_placeholder_set_is_derived_from_the_builders_own_constants(self):
        """Drift guard: the values come from the build, not a copied literal.

        Two different entry points synthesize two different root names
        (`_PLACEHOLDER_ROOT_NAME` via `_assemble_graph`, `_DEFAULT_ROOT_NAME` via
        `assemble_crate`), which is exactly how a hard-coded copy would go stale
        and start crediting a placeholder again.
        """
        from builder.tools.builder import (
            _DEFAULT_ROOT_NAME,
            _PLACEHOLDER_ROOT_DESCRIPTION,
            _PLACEHOLDER_ROOT_NAME,
        )
        from builder.tools.mit_assessment import _placeholder_values

        values = _placeholder_values()
        for const in (
            _PLACEHOLDER_ROOT_NAME,
            _DEFAULT_ROOT_NAME,
            _PLACEHOLDER_ROOT_DESCRIPTION,
        ):
            assert const.strip().lower() in values, const

    def test_the_assess_gaps_path_also_rejects_its_placeholder(self):
        """The two build paths use DIFFERENT defaults, so cover both.

        `assess_gaps` scores against `assemble_crate`'s document, whose root name
        falls back to `_DEFAULT_ROOT_NAME` — a different string from the one
        `_assemble_graph` produces.
        """
        from builder.tools.mit_assessment import slot_matcher
        from builder.tools.validation import _assemble_and_validate

        state = self._untitled_state()
        doc, _results = _assemble_and_validate(state, severity="required", profile="base")
        assert slot_matcher(state, graph=doc)("Investigation", "name") is False
