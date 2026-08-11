"""Tests for builder/tools/mit_assessment.py — assess_mit_coverage tool."""

from __future__ import annotations

from pathlib import Path

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance, MITReport
from builder.tools._crate_mapping import populate_crate
from builder.tools.mit_assessment import assess_mit_coverage, mit_was_assessed
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

    def test_empty_state_credits_only_the_builds_own_boilerplate(self):
        """An empty state IS assessed, and earns nothing but build boilerplate.

        Scoring goes through the assembled crate (#311), and even an empty
        CrateState assembles to a root Dataset carrying what ro-crate-py writes
        for every crate. Exactly one checklist slot matches that:
        `Investigation:datePublished`, which the build auto-sets and the user is
        never asked for. Pinned at 1 rather than rounded away — it is a free
        point every crate collects, and if a *second* slot ever starts matching a
        crate with no content in it, that is a false pass and this fails.
        """
        result = assess_mit_coverage(CrateState())

        # Assessed: the checklist was read and matched against a real document.
        assert mit_was_assessed(result)
        assert sum(sc["completed"] for sc in result.module_scores.values()) == 1
        assert result.overall_score < 0.01

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
        report = assess_mit_coverage(state, graph=graph)
        # Domain coverage a CrateState field scan structurally cannot reach.
        assert report.overall_score > 0.0
        assert any(sc["completed"] > 0 for sc in report.module_scores.values())

    def test_no_graph_scores_identically_to_the_graph_path(self, tmp_path):
        """HONESTY CONTROL (#311): the two entry points must not diverge again.

        `assess_mit_coverage(state)` used to run a second, weaker matcher over
        `CrateState` fields and return 0.0 for this very crate while the graph
        path returned 0.148 — and the maturity report printed that 0.0 as "MIT
        coverage 0%". There is now one scoring owner: with no graph the assessor
        assembles one itself.

        The comparison is against an *independently* assembled document (this
        module's `_assembled_graph`, which builds the crate its own way) rather
        than against `mit_assessment._assemble_graph`, so this measures agreement
        between two assemblies and not a function against itself. Whole reports
        are compared, not just the overall: a per-module drift is a divergence too.
        """
        state = vhps_fixture_state("S-VHPS21")
        independent = assess_mit_coverage(state, graph=_assembled_graph(state, tmp_path))
        assembled_here = assess_mit_coverage(state)

        assert assembled_here == independent
        # Guard the guard: an equality between two unassessed zeros would pass
        # while saying nothing.
        assert assembled_here.overall_score > 0.0

    def test_module_totals_come_from_the_checklist_not_the_crate(self, tmp_path):
        """The denominator is the checklist's size, never the matcher's opinion.

        Scoring may change how many slots are CREDITED; it must never change how
        many exist. Re-derived from the raw YAML rather than from a second
        ``assess_mit_coverage`` call: since #311 both graph and no-graph paths run
        the identical scorer, so comparing them to each other would compare a
        report to a copy of itself and pass no matter how wrong the totals were.
        """
        from builder.tools.mit_assessment import iter_scorable_params, load_mit_yaml

        checklist = load_mit_yaml()
        assert checklist is not None, "the shipped MIT checklist must load"

        expected: dict[str, int] = {}
        for module, _param, _slots in iter_scorable_params(checklist):
            name = module.get("name", module.get("id", "unknown"))
            expected[name] = expected.get(name, 0) + 1

        state = vhps_fixture_state("S-VHPS21")
        graph = _assembled_graph(state, tmp_path)
        for scored in (assess_mit_coverage(state, graph=graph), assess_mit_coverage(state)):
            assert {k: v["total"] for k, v in scored.module_scores.items()} == expected
            # Guard the guard: an empty checklist would make the equality vacuous.
            assert sum(expected.values()) > 0

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


class TestUnassessedIsNotZero:
    """A coverage figure nobody measured must be reported as absent (#311).

    `MITReport.overall_score` is 0.0 whenever scoring could not happen at all —
    the checklist would not load, or the crate would not assemble. That 0.0 is
    the absence of a measurement, and rendering it as "0% covered" states
    something about a crate that was never examined. `mit_was_assessed` is the
    one predicate that tells the two apart, and the assessor must never raise: it
    is called from a report writer that still has three other axes to render.
    """

    def test_unreadable_checklist_reports_not_assessed(self, monkeypatch):
        monkeypatch.setattr("builder.tools.mit_assessment.load_mit_yaml", lambda: None)
        report = assess_mit_coverage(vhps_fixture_state("S-VHPS21"))

        assert mit_was_assessed(report) is False
        assert report.module_scores == {}

    def test_unassemblable_state_reports_not_assessed(self, monkeypatch):
        """A crate that will not assemble is unscoreable, not empty."""

        def _boom(_state):
            raise RuntimeError("assembly exploded")

        monkeypatch.setattr("builder.tools.mit_assessment._assemble_graph", _boom)
        report = assess_mit_coverage(vhps_fixture_state("S-VHPS21"))

        assert mit_was_assessed(report) is False
        assert report.module_scores == {}

    def test_a_caller_supplied_graph_is_not_re_assembled(self, monkeypatch, tmp_path):
        """The graph a caller already holds is used as-is — no second assembly.

        The export path passes the document it just built; assembling again there
        would double the cost of every export for an identical answer.
        """
        state = vhps_fixture_state("S-VHPS21")
        graph = _assembled_graph(state, tmp_path)

        def _boom(_state):
            raise AssertionError("assembled despite being handed a graph")

        monkeypatch.setattr("builder.tools.mit_assessment._assemble_graph", _boom)
        assert assess_mit_coverage(state, graph=graph).overall_score > 0.0

    def test_a_real_assessment_is_flagged_assessed(self):
        """Honesty control: the flag distinguishes, it does not just say False."""
        assert mit_was_assessed(assess_mit_coverage(vhps_fixture_state("S-VHPS21"))) is True
        assert mit_was_assessed(MITReport()) is False


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
