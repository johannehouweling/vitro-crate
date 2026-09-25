"""A structural entity nothing references is a defect the ISA profile cannot report (#537).

Same class as #530 — a check that cannot fire being read as a pass — but
structural, and it spans the whole profile rather than one rule.

The ISA shapes infer their target class from the very edge whose absence is the
defect. ``FindISAProcesses`` mints ``isa-ro-crate:Process`` only for a
``bioschemas:LabProcess`` that some Dataset already points at, and
``ProcessMustBeReferencedFromDataset`` then targets that inferred class. A
process no Dataset references never earns the label, so the rule written to
catch exactly this defect has no target and stays silent — along with every
other rule keyed to the class. 11 of the profile's 12 shape files are built this
way, so the blind spot is general: when a structural edge is missing, the whole
rule-set for that layer switches off, and the crate reports conformant precisely
when its structure is most broken.

The upstream shapes are not ours to restructure, so the invariant is asserted on
our side instead: a structural entity this crate mints must be reachable from the
root by a directed walk — "referenced by something" is not enough, because an
island references its own members (#738). An entity named by an absolute URI is
described here but lives elsewhere, the same line #530 draws for the payload, so
it is excluded — except the AOP head, which stands for the subgraph under it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.state import CrateState, Entity, ValidationReport
from builder.tools.builder import export_crate
from builder.tools.validation import (
    apply_validation_result,
    build_and_validate,
    ensure_validated,
    verify_isa_reachability,
)
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(120)

REGRESSION_CRATE = Path("output/svhps26_real_input_crate/ro-crate-metadata.json")


def _doc(*entities: dict) -> dict:
    """A serialized crate document with the descriptor and root already wired.

    Every Dataset passed is a part of the root, as an Assay or Study is in an
    assembled crate, so the walk from ``./`` reaches it.
    """
    parts = [{"@id": e["@id"]} for e in entities if e.get("@type") == "Dataset"]
    return {
        "@context": "https://w3id.org/ro/crate/1.2/context",
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
            },
            {"@id": "./", "@type": "Dataset", "name": "Investigation", "hasPart": parts},
            *entities,
        ],
    }


def _process(pid: str, **props) -> dict:
    return {"@id": pid, "@type": "LabProcess", "name": pid, **props}


def _state_with_a_detached_process(tmp_path: Path) -> CrateState:
    """A crate carrying a step no Assay is ``about``.

    The generic shape of the defect, not one deposit's: any process that loses
    its container — created and then cascade-deleted mid-run, as in the session
    behind #537 — assembles into exactly this.
    """
    state = vhps_fixture_state("S-VHPS21")
    state.metadata.output_path = str(tmp_path / "crate")
    state.add_entity(
        Entity(
            entity_id="proc_orphan",
            type="LabProcess",
            fields={"name": "Orphaned step", "description": "nothing points here"},
        )
    )
    return state


class TestVerifyIsaReachability:
    """The invariant, stated over the assembled document rather than a rule id."""

    def test_a_process_no_dataset_references_is_reported(self) -> None:
        issues = verify_isa_reachability(_doc(_process("#proc_exposure")))

        assert [i["entity_id"] for i in issues] == ["#proc_exposure"]
        assert issues[0]["severity"] == "required"
        assert issues[0]["profile"] == "isa"
        assert "#proc_exposure" in issues[0]["message"]

    def test_a_process_an_assay_is_about_is_not_reported(self) -> None:
        doc = _doc(
            {
                "@id": "#assay_1",
                "@type": "Dataset",
                "name": "Assay",
                "about": [{"@id": "#proc_exposure"}],
            },
            _process("#proc_exposure"),
        )

        assert verify_isa_reachability(doc) == []

    def test_a_detached_protocol_is_reported(self) -> None:
        """Measured on `output/svhps22_real_input_crate_v2`, which carries four."""
        doc = _doc({"@id": "#proto_uptake", "@type": "LabProtocol", "name": "Uptake SOP"})

        assert [i["entity_id"] for i in verify_isa_reachability(doc)] == ["#proto_uptake"]

    def test_a_protocol_a_process_uses_is_not_reported(self) -> None:
        doc = _doc(
            {
                "@id": "#assay_1",
                "@type": "Dataset",
                "about": [{"@id": "#proc_exposure"}],
            },
            _process("#proc_exposure", agent={"@id": "#proto_uptake"}),
            {"@id": "#proto_uptake", "@type": "LabProtocol", "name": "Uptake SOP"},
        )

        assert verify_isa_reachability(doc) == []

    def test_a_sample_a_process_consumed_is_not_reported(self) -> None:
        doc = _doc(
            {"@id": "#assay_1", "@type": "Dataset", "about": [{"@id": "#proc_exposure"}]},
            _process("#proc_exposure", object={"@id": "#sample_h4"}),
            {"@id": "#sample_h4", "@type": "Sample", "name": "H4"},
        )

        assert verify_isa_reachability(doc) == []

    def test_an_entity_named_by_an_absolute_uri_is_not_reported(self) -> None:
        """A Cellosaurus cell line is described here and lives elsewhere.

        Fifteen of the S-VHPS22 builds in `output/` carry such a Sample with
        nothing pointing at it. That is a record of an external thing, not a
        hole in this crate's backbone — the same line #530 draws for the
        payload — and a characteristic it carries (a passage, a cell type) is
        its own description, reached through it alone. Only the AOP head, which
        stands for the subgraph the crate mints under it, is the exception (#738).
        """
        term = "http://purl.obolibrary.org/obo/NCIT_C16403"
        doc = _doc(
            {"@id": "#assay_1", "@type": "Dataset", "about": [{"@id": "#proc_exposure"}]},
            _process("#proc_exposure", object={"@id": "#sample_h4"}),
            {"@id": "#sample_h4", "@type": "Sample", "name": "H4", "sampleType": {"@id": term}},
            {"@id": term, "@type": "DefinedTerm", "name": "cell line"},
            {
                "@id": "https://www.cellosaurus.org/CVCL_D357",
                "@type": "Sample",
                "name": "MO3.13",
                "sampleType": {"@id": term},
                "additionalProperty": {"@id": "#param_passage"},
            },
            {"@id": "#param_passage", "@type": "PropertyValue", "name": "passage", "value": "12"},
        )

        assert verify_isa_reachability(doc) == []

    def test_a_file_is_not_this_checks_business(self) -> None:
        """Files are #530's question. This one is about the ISA backbone."""
        doc = _doc({"@id": "data/orphan.csv", "@type": "File", "name": "orphan"})

        assert verify_isa_reachability(doc) == []

    def test_every_orphan_is_reported_not_just_the_first(self) -> None:
        doc = _doc(_process("#proc_a"), _process("#proc_b"), _process("#proc_c"))

        assert [i["entity_id"] for i in verify_isa_reachability(doc)] == [
            "#proc_a",
            "#proc_b",
            "#proc_c",
        ]

    def test_the_fix_says_what_to_wire(self) -> None:
        issues = verify_isa_reachability(_doc(_process("#proc_exposure")))

        assert "#proc_exposure" in issues[0]["fix"]

    def test_reachability_is_directed_so_an_island_is_reported_whole(self) -> None:
        """A protocol only a detached process uses IS referenced — by the island."""
        doc = _doc(
            _process("#proc_a", agent={"@id": "#proto_x"}),
            {"@id": "#proto_x", "@type": "LabProtocol", "name": "x"},
        )

        assert [i["entity_id"] for i in verify_isa_reachability(doc)] == ["#proc_a", "#proto_x"]


class TestAnAopIslandIsDetached:
    """`materialize_aop_subgraph("610")` with no Study: 36 nodes attached to nothing (#738).

    Every KeyEvent is referenced — by the head, inside the island — and every id
    is an AOP-Wiki IRI, so "referenced by anything, local ids only" saw nothing.
    """

    _HEAD = "https://aopwiki.org/aops/610"
    _EVENTS = [f"https://aopwiki.org/events/{n}" for n in (888, 177, 1234)]

    def _study(self, **props: object) -> dict:
        return {"@id": "#study_1", "@type": "Dataset", "additionalType": "Study", **props}

    def _island(self) -> list[dict]:
        return [
            {
                "@id": self._HEAD,
                "@type": ["AdverseOutcomePathway", "DefinedTerm"],
                "name": "AOP 610",
                "has_key_event": [{"@id": e} for e in self._EVENTS],
            },
            *[{"@id": e, "@type": ["KeyEvent", "DefinedTerm"], "name": e} for e in self._EVENTS],
        ]

    def test_an_aop_no_study_mentions_is_reported_once_by_its_head(self) -> None:
        issues = verify_isa_reachability(_doc(self._study(), *self._island()))

        assert [i["entity_id"] for i in issues] == [self._HEAD]
        assert issues[0]["profile"] == "isa"
        assert issues[0]["severity"] == "required"

    def test_an_aop_the_study_mentions_is_not_reported(self) -> None:
        doc = _doc(self._study(mentions=[{"@id": self._HEAD}]), *self._island())

        assert verify_isa_reachability(doc) == []


class TestTheCrateThatPassedClean:
    """`output/svhps26_real_input_crate` — three orphaned processes, ISA green."""

    def test_its_three_orphaned_processes_are_reported(self) -> None:
        if not REGRESSION_CRATE.exists():  # pragma: no cover - crate not built
            pytest.skip(f"{REGRESSION_CRATE} not available")

        issues = verify_isa_reachability(json.loads(REGRESSION_CRATE.read_text()))

        # A superset: the directed walk also names whatever those three processes
        # were the only way to — their protocols and samples are islands too.
        assert {
            "#LabProcess_proc_culture_and_seed_cho_k1_oatp1c1_cells",
            "#LabProcess_proc_30_minute_co_exposure_for_oatp1c1_t4_uptake_assay",
            "#LabProcess_proc_normalize_uptake_and_viability_and_fit_"
            "concentration_response_curves",
        } <= {i["entity_id"] for i in issues}
        assert all(i["severity"] == "required" for i in issues)


class TestVerdictRecordsWhetherReachabilityWasSeen:
    """A verdict that never asked must not be read as one that asked and found nothing."""

    def test_defaults_to_unchecked_and_round_trips(self) -> None:
        assert ValidationReport().isa_reachability_checked is False
        report = ValidationReport(isa_reachability_checked=True)
        restored = ValidationReport.from_dict(report.to_dict())
        assert restored.isa_reachability_checked is True
        # A checkpoint written before the field existed still loads.
        legacy = report.to_dict()
        del legacy["isa_reachability_checked"]
        assert ValidationReport.from_dict(legacy).isa_reachability_checked is False

    def test_a_verdict_that_never_looked_says_so(self) -> None:
        """The green pill over-claims hardest on a verdict clean at REQUIRED."""
        from builder.writers.maturity_report import build_maturity_html

        state = vhps_fixture_state("S-VHPS21")
        val = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            assessed_tiers={"required", "recommended", "optional"},
        )

        assert "not checked for detached entities" in build_maturity_html(state, validation=val)

        val.isa_reachability_checked = True
        page = build_maturity_html(state, validation=val)
        assert "not checked for detached entities" not in page


class TestTheInLoopVerdictCarriesReachability:
    """The check runs inside `build_and_validate`, so no verdict predates it (#738).

    v21 of S-VHPS22 exported with ``ok: True`` while its own embedded report
    said "detached from the ISA backbone", and the next in-loop verdict then
    replaced ``required_issues`` wholesale and recorded zero.
    """

    def test_the_in_loop_verdict_is_not_ok_and_its_write_back_keeps_the_finding(
        self, tmp_path: Path
    ) -> None:
        state = _state_with_a_detached_process(tmp_path)

        result = build_and_validate(state, profile="all")

        assert result["ok"] is False
        assert any(
            i["profile"] == "isa" and "proc_orphan" in i["entity_id"] for i in result["issues"]
        ), result["issues"]
        apply_validation_result(state, "build_and_validate", result)

        assert any("proc_orphan" in issue for issue in state.validation.required_issues), (
            state.validation.required_issues
        )
        assert state.validation.isa_reachability_checked is True

    def test_the_gap_engine_carries_it_as_a_must_gap(self, tmp_path: Path) -> None:
        """The guidance summary reads `assess_gaps`, which must agree with the loop.

        Report-only: the edge is missing on whatever should point AT the entity,
        so no value the guidance loop could set on the entity itself clears it.
        """
        from builder.tools.gap_analysis import REPORT_ONLY, assess_gaps

        report = assess_gaps(_state_with_a_detached_process(tmp_path))

        assert report.conformance["isa"] is False
        assert [
            (g.tier, g.fix_hint)
            for g in report.gaps
            if g.source == "shacl" and "proc_orphan" in (g.entity_id or "") and g.tier == "MUST"
        ] == [("MUST", REPORT_ONLY)]

    def test_a_current_verdict_that_never_asked_is_re_run(self, tmp_path: Path) -> None:
        """A disk `validate` report covers every tier and never asked (#738)."""
        state = _state_with_a_detached_process(tmp_path)
        state.validation = ValidationReport(
            base_passed=True,
            isa_passed=True,
            tox_passed=True,
            assessed_tiers={"required", "recommended", "optional"},
            input_fingerprint=state.validation_fingerprint(),
        )

        info = ensure_validated(state)

        assert (info["ran"], info["reason"]) == (True, "reachability-unasked")
        assert any("proc_orphan" in issue for issue in state.validation.required_issues)


class TestExportRefusesToCallADetachedBackboneClean:
    """The end-to-end regression: the shipped report must not say Conformant."""

    def test_export_records_the_detached_process_as_a_required_issue(
        self, tmp_path: Path
    ) -> None:
        state = _state_with_a_detached_process(tmp_path)

        result = export_crate(state, str(tmp_path / "crate"))

        assert result["success"], result["error"]
        assert result["validation"]["ok"] is False
        assert any("proc_orphan" in issue for issue in state.validation.required_issues), (
            state.validation.required_issues
        )
        assert state.validation.isa_passed is False

    def test_a_sample_the_export_itself_wires_on_is_not_reported(self, tmp_path: Path) -> None:
        """The verdict describes the crate written, after export's own wiring step."""
        state = vhps_fixture_state("S-VHPS21")
        state.add_entity(
            Entity(
                entity_id="cell_primary",
                type="CellLineSample",
                fields={"name": "Primary hepatocytes", "source_kind": "primary cells"},
            )
        )

        result = export_crate(state, str(tmp_path / "crate"))

        assert result["wiring"]["wired"] == {"cell_lines": ["cell_primary"]}
        assert result["validation"]["ok"] is True
        assert not any("cell_primary" in i for i in state.validation.required_issues)

    def test_the_embedded_report_does_not_headline_conformant(self, tmp_path: Path) -> None:
        state = _state_with_a_detached_process(tmp_path)

        export_crate(state, str(tmp_path / "crate"))

        from tests.fixtures.report import profile_verdict

        page = (tmp_path / "crate" / "ro-crate-metadata-maturity.html").read_text(encoding="utf-8")
        assert profile_verdict(page) == "no"

    def test_a_crate_whose_backbone_is_whole_still_passes(self, tmp_path: Path) -> None:
        """Paired with the test above.

        Without it, "the verdict reads as a fail" would pass just as well on a
        fixture that was never conformant, and the assertion would pin nothing.
        """
        state = vhps_fixture_state("S-VHPS21")
        state.metadata.output_path = str(tmp_path / "crate")

        export_crate(state, str(tmp_path / "crate"))

        from tests.fixtures.report import profile_verdict

        assert state.validation.isa_reachability_checked is True
        assert state.validation.required_issues == []
        page = (tmp_path / "crate" / "ro-crate-metadata-maturity.html").read_text(encoding="utf-8")
        assert profile_verdict(page) == "ok"
