"""A study without an exposure is an ISA crate, not an ISA-Tox crate.

ISA-Tox requires an Investigation to include an Exposure. A study that treats nothing
(culture conditions, a characterisation of a cell model) cannot meet that and should
not claim to. ``--profile isa`` is the depositor saying so: the crate states the ISA
profile alone, its verdict and its report cover RO-Crate and ISA, the agent is told
there is no exposure step, and nothing shows a third layer the crate does not claim.
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import pytest

import main
from builder.agents.ui import (
    UiSnapshot,
    render_goodbye,
    render_resume_summary,
    render_status_markup,
)
from builder.state import (
    CrateMetadata,
    CrateState,
    Entity,
    EntityProvenance,
    conformance_by_layer,
)
from builder.tools._crate_mapping import PROFILE_ISA, PROFILE_ISATOX
from builder.tools.builder import assemble_crate, export_crate
from builder.tools.session import get_status
from builder.tools.validation import apply_validation_result, build_and_validate, clear_sweep_memo

pytestmark = pytest.mark.timeout(300)


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state(profile: str | None = None) -> CrateState:
    """An investigation, a study and an assay: base and ISA pass, ISA-Tox wants an Exposure."""
    s = CrateState()
    s.metadata.title = "Culture conditions of a kidney model"
    s.metadata.description = "Transcriptomics of mono- and co-cultures; nothing is exposed."
    if profile:
        s.metadata.profile = profile
    s.add_entity(_ent("inv_1", "Investigation", name=s.metadata.title, description="d"))
    s.add_entity(_ent("study_1", "Study", name="Culture study", investigation_id="inv_1"))
    s.add_entity(_ent("assay_1", "Assay", name="Transcriptomics", study_id="study_1"))
    return s


def _root(state: CrateState) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    graph = crate.metadata.generate()["@graph"]
    return {"root": next(n for n in graph if n["@id"] == "./"), "ids": {n["@id"] for n in graph}}


def _snap(**over: Any) -> UiSnapshot:
    base: dict[str, Any] = {
        "session_id": "s1",
        "entity_count": 3,
        "file_count": 0,
        "base_passed": True,
        "isa_passed": True,
        "tox_passed": False,
        "required_issue_count": 0,
    }
    return UiSnapshot(**{**base, **over})


class TestTheSetting:
    def test_a_crate_claims_isa_tox_unless_told_otherwise(self):
        assert CrateMetadata().profile == "isa-tox"
        assert "profile" not in CrateMetadata().to_dict()
        assert CrateState().layers == ("base", "isa", "tox")

    def test_it_is_kept_with_the_session(self):
        kept = CrateMetadata.from_dict(CrateMetadata(profile="isa").to_dict())
        assert kept.profile == "isa"
        assert _state("isa").layers == ("base", "isa")

    def test_the_command_line_names_the_profile(self):
        assert main.parse_args(["--profile", "isa"]).profile == "isa"
        assert main.parse_args(["--profile", "isa-tox"]).profile == "isa-tox"
        assert main.parse_args([]).profile is None
        with pytest.raises(SystemExit):
            main.parse_args(["--profile", "tox"])


class TestTheCrate:
    def test_an_isa_crate_states_the_isa_profile_alone(self):
        found = _root(_state("isa"))
        assert found["root"]["conformsTo"] == {"@id": PROFILE_ISA}
        assert PROFILE_ISATOX not in found["ids"]

    def test_a_crate_states_both_by_default(self):
        found = _root(_state())
        assert found["root"]["conformsTo"] == [{"@id": PROFILE_ISA}, {"@id": PROFILE_ISATOX}]
        assert PROFILE_ISATOX in found["ids"]


class TestTheVerdict:
    def setup_method(self):
        clear_sweep_memo()

    def test_an_isa_crate_is_judged_on_the_layers_it_claims(self):
        result = build_and_validate(_state("isa"))
        assert result["conformance"] == {"base": True, "isa": True}
        assert result["ok"] is True
        assert not [i for i in result["issues"] if i["profile"] == "tox"]

    def test_the_same_study_fails_isa_tox_when_it_claims_it(self):
        result = build_and_validate(_state())
        assert result["conformance"] == {"base": True, "isa": True, "tox": False}
        assert result["ok"] is False

    def test_a_pass_asked_for_by_name_still_runs(self):
        assert build_and_validate(_state("isa"), profile="tox")["conformance"] == {"tox": False}

    def test_a_remembered_verdict_does_not_answer_for_a_layer_it_never_judged(self):
        state = _state("isa")
        build_and_validate(state)  # base and ISA, remembered for this state
        assert build_and_validate(state, profile="tox")["conformance"] == {"tox": False}
        assert build_and_validate(state)["conformance"] == {"base": True, "isa": True}

    def test_the_session_reports_the_layers_it_claims(self):
        state = _state("isa")
        apply_validation_result(state, "build_and_validate", build_and_validate(state))
        assert state.conformance() == {"base": True, "isa": True}
        assert get_status(state)["validation_status"] == {"base": True, "isa": True}

    def test_layers_stack_whatever_is_claimed(self):
        assert conformance_by_layer(base=True, isa=False, tox=True) == {"base": True, "isa": False, "tox": False}
        assert conformance_by_layer(base=True, isa=True, tox=False, layers=("base", "isa")) == {"base": True, "isa": True}


class TestTheWrittenCrate:
    @pytest.fixture(scope="class")
    def exported(self, tmp_path_factory):
        clear_sweep_memo()
        out = tmp_path_factory.mktemp("isa") / "crate"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = export_crate(_state("isa"), str(out))
        assert result["success"] is True, result
        return result, out

    def test_the_export_is_conformant(self, exported):
        result, _ = exported
        assert result["validation"]["ok"] is True

    def test_the_report_shows_the_two_layers(self, exported):
        _, out = exported
        page = (out / "ro-crate-metadata-maturity.html").read_text(encoding="utf-8")
        assert 'data-cell="isa-required"' in page
        assert 'data-cell="tox-required"' not in page
        assert "2 / 2 profiles" in page

    def test_the_root_on_disk_states_isa_alone(self, exported):
        _, out = exported
        graph = json.loads((out / "ro-crate-metadata.json").read_text(encoding="utf-8"))["@graph"]
        assert next(n for n in graph if n["@id"] == "./")["conformsTo"] == {"@id": PROFILE_ISA}


class TestNothingShowsALayerTheCrateDoesNotClaim:
    def test_the_status_line(self):
        assert "Tox" not in render_status_markup(_snap(layers=("base", "isa")))
        assert "Tox" in render_status_markup(_snap())

    def test_the_resume_and_goodbye_panels(self):
        from rich.console import Console

        def text(renderable) -> str:
            console = Console(record=True, width=200)
            console.print(renderable)
            return console.export_text()

        isa = _snap(layers=("base", "isa"), crate_path="/tmp/x")
        assert "Tox" not in text(render_resume_summary(isa, resumed=True))
        assert "Tox" not in text(render_goodbye("s1", {}, resumable=False, snap=isa))
        assert "Tox" in text(render_resume_summary(_snap(), resumed=True))

    def test_the_dashboard(self):
        from rich.console import Console

        from builder.tools.dashboard import _build_cratestate_panel

        def text(state: dict) -> str:
            console = Console(record=True, width=200)
            console.print(_build_cratestate_panel(state))
            return console.export_text()

        saved = _state("isa").to_dict()
        assert "Tox" not in text(saved)
        assert "Tox" in text(_state().to_dict())


class TestTheBuild:
    def test_the_agent_is_told_there_is_no_exposure(self):
        from builder.agents.react.agent_loop import _completeness_nudge

        assert "no Exposure" in _completeness_nudge(_state("isa"))
        assert "no Exposure" not in _completeness_nudge(_state())
