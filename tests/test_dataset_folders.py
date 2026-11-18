"""A Study and an Assay are identified by their folder in the deposit (#852).

"The folder tree is the depositor's own statement of what the studies and assays
are." A Study or an Assay whose files lie in one folder of the deposit that is
its alone is therefore identified by that folder, and one function decides which
folder that is: ``dataset_folders``. It reads the accepted labelling of the
deposit first and the files attached to the entity otherwise, and it gives no
folder where none can be derived: the entity then keeps its local identifier.

Every state here is built with the tools a build uses. A deposit is a list of
scanned files, and a file is placed with ``attach_files``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, Entity
from builder.tools.deposit_structure import fallback_structure
from builder.tools.drafters import draft_assay, draft_process, draft_study
from builder.tools.management import set_fields
from builder.tools.provenance import attach_files, draft_file, find_or_create_file
from tests.fixtures.folder_deposits import ROOT, backbone, deposit_state, place, structure

FIXTURES = Path(__file__).parent / "fixtures"


def _folders(state: CrateState) -> dict[tuple[str, str], str]:
    from builder.tools._crate_mapping import dataset_folders

    return dataset_folders(state)


def _key(entity: Entity) -> tuple[str, str]:
    return (entity.type, entity.entity_id)


def _place_files(state: CrateState, entity: Entity, *files: str) -> None:
    """Attach exactly *files* to *entity*."""
    result = attach_files(state, to=entity.entity_id, paths=[f"{ROOT}/{f}" for f in files])
    assert result["attached"] == len(files)


def _place_everything(state: CrateState, entity: Entity) -> None:
    """Attach every scanned file to *entity*, as the pipeline does."""
    attach_files(state, to=entity.entity_id, paths=[fc.path for fc in state.scanned_files])


class TestAnAssayTakesTheFolderOfItsFiles:
    """Without an accepted labelling the attached files are the evidence."""

    def test_each_assay_takes_the_folder_that_holds_its_files(self) -> None:
        state = deposit_state(
            "assay_01_TH_uptake/README.txt",
            "assay_01_TH_uptake/EDCs/raw data/004668.csv",
            "assay_02_deiodinase/README.txt",
        )
        _study, (uptake, deiodinase) = backbone(state, "Uptake", "Deiodinase")
        place(state, uptake, "assay_01_TH_uptake")
        place(state, deiodinase, "assay_02_deiodinase")

        assert _folders(state) == {
            _key(uptake): "assay_01_TH_uptake",
            _key(deiodinase): "assay_02_deiodinase",
        }

    def test_it_is_the_deepest_folder_common_to_all_of_them(self) -> None:
        state = deposit_state(
            "Exposure scenarios/Assays/LDH analysis/plate 1.csv",
            "Exposure scenarios/Assays/LDH analysis/raw/plate 2.csv",
            "Exposure scenarios/Assays/BMD analysis/bmd.csv",
        )
        _study, (ldh,) = backbone(state, "LDH")
        place(state, ldh, "Exposure scenarios/Assays/LDH analysis")

        assert _folders(state) == {_key(ldh): "Exposure scenarios/Assays/LDH analysis"}

    def test_a_file_at_the_deposit_root_leaves_it_without_one(self) -> None:
        state = deposit_state("Assay_MCT8-MDCK1/README.txt", "S-VHPS21.json")
        _study, (assay,) = backbone(state, "Uptake")
        _place_everything(state, assay)

        assert _folders(state) == {}

    def test_files_of_two_folders_leave_it_without_one(self) -> None:
        state = deposit_state("raw/readout.csv", "processed/ic50.csv")
        _study, (assay,) = backbone(state, "Uptake")
        _place_everything(state, assay)

        assert _folders(state) == {}

    def test_the_deposit_root_is_never_a_folder(self) -> None:
        state = deposit_state("readout.csv", "ic50.csv")
        _study, (assay,) = backbone(state, "Uptake")
        _place_everything(state, assay)

        assert _folders(state) == {}

    def test_an_assay_holding_every_file_of_the_deposit_takes_none(self) -> None:
        """The pipeline places every scanned file on its one Assay. That says what
        the deposit holds, not which folder is the assay's, and the folder that
        holds every file is here the study's."""
        state = deposit_state(
            "Exposure scenarios/README.txt",
            "Exposure scenarios/Assays/LDH analysis/plate 1.csv",
            "Exposure scenarios/Assays/BMD analysis/bmd.csv",
        )
        _study, (assay,) = backbone(state, "Everything")
        _place_everything(state, assay)

        assert _folders(state) == {}

        state.deposit_structure = fallback_structure()

        assert _folders(state) == {}, "a labelling that was not accepted is no evidence"

    def test_a_state_that_scanned_nothing_has_no_folder(self) -> None:
        """A folder counts only if the scan saw it, so the same entities without
        their scan keep their local identifiers."""
        state = deposit_state("assay_01/plate.csv", "README.txt")
        _study, (assay,) = backbone(state, "Uptake")
        place(state, assay, "assay_01")
        assert _folders(state) == {_key(assay): "assay_01"}

        state.scanned_files = []

        assert _folders(state) == {}


class TestTheAcceptedLabellingIsReadFirst:
    """With a labelling the checks accepted, the labelling says which folder is an
    assay's, and the attached files say which Assay it is."""

    def test_a_record_at_the_root_does_not_stop_the_assay_folder(self) -> None:
        state = deposit_state("Assay_MCT8-MDCK1/README.txt", "S-VHPS21.json")
        study, (assay,) = backbone(state, "Uptake")
        _place_everything(state, assay)
        state.deposit_structure = structure(".", "Assay_MCT8-MDCK1")

        assert _folders(state) == {_key(assay): "Assay_MCT8-MDCK1"}
        assert _key(study) not in _folders(state), "the study is the deposit root"

    def test_an_assay_holding_several_assay_folders_takes_none_of_them(self) -> None:
        """The pipeline places every file on one Assay. That Assay is none of the
        deposit's assays, and it does not take the study's folder either."""
        state = deposit_state("study_01/assay_01/a.csv", "study_01/assay_02/b.csv")
        study, (assay,) = backbone(state, "Everything")
        _place_everything(state, assay)
        state.deposit_structure = structure("study_01", "study_01/assay_01", "study_01/assay_02")

        assert _folders(state) == {_key(study): "study_01"}

    def test_without_a_file_the_only_assay_takes_the_only_assay_folder(self) -> None:
        state = deposit_state("Assay_OATP1C1/workbook.csv", "S-VHPS26.json")
        _study, (assay,) = backbone(state, "Uptake")
        state.deposit_structure = structure(".", "Assay_OATP1C1")

        assert _folders(state) == {_key(assay): "Assay_OATP1C1"}

    def test_without_a_file_none_of_several_assays_takes_a_folder(self) -> None:
        state = deposit_state("a1/x.csv", "a2/y.csv")
        backbone(state, "First", "Second")
        state.deposit_structure = structure(".", "a1", "a2")

        assert _folders(state) == {}

    def test_without_a_file_one_assay_takes_none_of_several_folders(self) -> None:
        state = deposit_state("a1/x.csv", "a2/y.csv")
        backbone(state, "Only")
        state.deposit_structure = structure(".", "a1", "a2")

        assert _folders(state) == {}

    def test_a_file_outside_every_assay_folder_is_no_file_against_the_only_one(self) -> None:
        """The record at the root confirms no assay folder and refutes none, so the
        only Assay still takes the only assay folder."""
        state = deposit_state("Assay_OATP1C1/workbook.csv", "S-VHPS26.json")
        _study, (assay,) = backbone(state, "Uptake")
        _place_files(state, assay, "S-VHPS26.json")
        state.deposit_structure = structure(".", "Assay_OATP1C1")

        assert _folders(state) == {_key(assay): "Assay_OATP1C1"}

    def test_each_assay_takes_the_assay_folder_its_files_lie_in(self) -> None:
        state = deposit_state("s/README.txt", "s/a1/x.csv", "s/a2/y.csv")
        study, (first, second) = backbone(state, "First", "Second")
        place(state, first, "s/a1")
        place(state, second, "s/a2")
        state.deposit_structure = structure("s", "s/a1", "s/a2")

        assert _folders(state) == {
            _key(study): "s",
            _key(first): "s/a1",
            _key(second): "s/a2",
        }

    def test_two_assays_holding_files_of_one_assay_folder_take_none(self) -> None:
        """The labelling says which folders are assays'. A folder that holds files of
        two Assays is still neither one's alone."""
        state = deposit_state("a1/x.csv", "a1/y.csv", "a2/z.csv")
        _study, (first, second) = backbone(state, "First", "Second")
        _place_files(state, first, "a1/x.csv")
        _place_files(state, second, "a1/y.csv", "a2/z.csv")
        state.deposit_structure = structure(".", "a1", "a2")

        assert _folders(state) == {}

    def test_a_folder_does_not_pass_from_one_assay_to_another(self) -> None:
        """An id a finding gave for one Assay must not come to name another one as
        files are attached."""
        state = deposit_state("x/1.csv", "x/2.csv", "y/5.csv")
        _study, (first, second) = backbone(state, "First", "Second")
        state.deposit_structure = structure(".", "x", "y")
        _place_files(state, first, "x/1.csv")
        assert _folders(state) == {_key(first): "x"}

        _place_files(state, second, "x/2.csv")
        assert _folders(state) == {}

        _place_files(state, first, "y/5.csv")
        assert _folders(state) == {}, "x still holds a file of each"

    def test_a_study_folder_that_holds_a_file_of_another_study_is_taken_by_none(self) -> None:
        state = deposit_state("s/README.txt", "s/a1/x.csv", "s/stray.csv")
        study, (own,) = backbone(state, "Own")
        investigation = state.list_entities("Investigation")[0]
        other = draft_study(state, investigation.entity_id, {"name": "Other"})
        foreign = draft_assay(state, other.entity_id, {"name": "Foreign"})
        _place_files(state, study, "s/README.txt")
        place(state, own, "s/a1")
        _place_files(state, foreign, "s/stray.csv")
        state.deposit_structure = structure("s", "s/a1")

        assert _folders(state) == {_key(own): "s/a1"}

    def test_a_labelling_that_was_not_accepted_is_no_evidence(self) -> None:
        """The fallback names the root for everything. It is what a build keeps
        when it could not read the deposit, so the files decide."""
        state = deposit_state("assay_01/plate.csv", "README.txt")
        _study, (assay,) = backbone(state, "Uptake")
        place(state, assay, "assay_01")
        state.deposit_structure = fallback_structure()

        assert _folders(state) == {_key(assay): "assay_01"}

    def test_a_labelling_of_another_root_is_not_used(self) -> None:
        """A labelling is relative to the folder that was scanned, and a crate path
        to the input path. Here ``plates`` names a folder beside the input path,
        and the input path holds a folder of the same name."""
        state = deposit_state("plates/x.csv", root=f"{ROOT}/study")
        state.approved_scan_roots = {ROOT}
        backbone(state, "Uptake")
        state.deposit_structure = structure(".", "plates")

        assert _folders(state) == {}


class TestAFolderIsOneEntitysAlone:
    def test_two_assays_holding_files_of_one_folder_take_none(self) -> None:
        state = deposit_state("cell_line_protocols/h4.docx", "cell_line_protocols/sk.docx")
        _study, (first, second) = backbone(state, "First", "Second")
        _place_files(state, first, "cell_line_protocols/h4.docx")
        _place_files(state, second, "cell_line_protocols/sk.docx")

        assert _folders(state) == {}

    def test_an_assay_inside_the_folder_of_another_leaves_both_without_one(self) -> None:
        state = deposit_state("x/a.csv", "x/sub/b.csv")
        _study, (outer, inner) = backbone(state, "Outer", "Inner")
        _place_files(state, outer, "x/a.csv")
        _place_files(state, inner, "x/sub/b.csv")

        assert _folders(state) == {}

    def test_where_a_study_and_an_assay_share_a_folder_the_assay_takes_it(self) -> None:
        state = deposit_state("s/README.txt", "s/plate.csv")
        study, (assay,) = backbone(state, "Uptake")
        _place_files(state, study, "s/README.txt")
        _place_files(state, assay, "s/plate.csv")

        assert _folders(state) == {_key(assay): "s"}

    def test_a_study_takes_the_folder_that_holds_its_assays(self) -> None:
        state = deposit_state("s/README.txt", "s/a1/x.csv", "s/a2/y.csv")
        study, (first, second) = backbone(state, "First", "Second")
        _place_files(state, study, "s/README.txt")
        place(state, first, "s/a1")
        place(state, second, "s/a2")

        assert _folders(state) == {
            _key(study): "s",
            _key(first): "s/a1",
            _key(second): "s/a2",
        }

    def test_a_study_whose_assay_lies_outside_its_folder_takes_none(self) -> None:
        state = deposit_state("s/README.txt", "elsewhere/x.csv")
        study, (assay,) = backbone(state, "Uptake")
        _place_files(state, study, "s/README.txt")
        place(state, assay, "elsewhere")

        assert _folders(state) == {_key(assay): "elsewhere"}

    def test_two_studies_in_one_folder_take_none(self) -> None:
        state = deposit_state("s/one.txt", "s/two.txt")
        first, _assays = backbone(state)
        investigation = state.list_entities("Investigation")[0]
        second = draft_study(state, investigation.entity_id, {"name": "Second"})
        _place_files(state, first, "s/one.txt")
        _place_files(state, second, "s/two.txt")

        assert _folders(state) == {}

    def test_each_study_takes_its_own_folder(self) -> None:
        state = deposit_state("s1/README.txt", "s1/a/x.csv", "s2/README.txt", "s2/b/y.csv")
        first, (assay_a,) = backbone(state, "A", study="First")
        investigation = state.list_entities("Investigation")[0]
        second = draft_study(state, investigation.entity_id, {"name": "Second"})
        assay_b = draft_assay(state, second.entity_id, {"name": "B"})
        _place_files(state, first, "s1/README.txt")
        _place_files(state, second, "s2/README.txt")
        place(state, assay_a, "s1/a")
        place(state, assay_b, "s2/b")

        assert _folders(state) == {
            _key(first): "s1",
            _key(second): "s2",
            _key(assay_a): "s1/a",
            _key(assay_b): "s2/b",
        }

    def test_a_study_whose_folder_holds_the_assay_of_another_takes_none(self) -> None:
        state = deposit_state("s1/README.txt", "s1/b/y.csv", "s1/a/x.csv")
        first, (assay_a,) = backbone(state, "A", study="First")
        investigation = state.list_entities("Investigation")[0]
        second = draft_study(state, investigation.entity_id, {"name": "Second"})
        assay_b = draft_assay(state, second.entity_id, {"name": "B"})
        _place_files(state, first, "s1/README.txt")
        place(state, assay_a, "s1/a")
        place(state, assay_b, "s1/b")

        assert _folders(state) == {_key(assay_a): "s1/a", _key(assay_b): "s1/b"}


class TestWhatIsNoEvidence:
    """Only a deposited file that was placed on the entity says where it lives."""

    def test_a_file_a_step_produced_is_no_evidence(self) -> None:
        state = deposit_state("raw/readout.csv")
        _study, (assay,) = backbone(state, "Uptake")
        result = find_or_create_file(state, state.scanned_files[0])
        draft_process(
            state,
            assay.entity_id,
            "EndpointReadout",
            {"name": "Readout", "result": result.entity_id},
        )

        assert _folders(state) == {}

    def test_the_folder_the_tool_puts_an_unplaced_file_in_is_no_folder(self) -> None:
        """A File without a path is written to ``data/``, which is this tool's
        folder and not one of the deposit."""
        state = deposit_state("README.txt")
        _study, (assay,) = backbone(state, "Uptake")
        notes = draft_file(state, name="notes.txt")
        set_fields(state, assay.entity_id, {"hasPart": [notes.entity_id]})

        assert _folders(state) == {}

    def test_a_data_folder_the_deposit_has_is_a_folder(self) -> None:
        state = deposit_state("data/plate.csv", "README.txt")
        _study, (assay,) = backbone(state, "Uptake")
        place(state, assay, "data")

        assert _folders(state) == {_key(assay): "data"}

    def test_files_listed_under_a_pagetab_alias_are_not_placed(self) -> None:
        """``dataFiles`` and ``resources`` are fields of their own, which the build
        nests beside the attached files; only what ``attach_files`` writes places a
        file."""
        state = deposit_state("assay_01/plate.csv", "assay_01/notes.txt")
        _study, (assay,) = backbone(state, "Uptake")
        plate, notes = (find_or_create_file(state, scanned) for scanned in state.scanned_files)
        set_fields(
            state,
            assay.entity_id,
            {"dataFiles": [plate.entity_id], "resources": [notes.entity_id]},
        )

        assert _folders(state) == {}


def _scanned(fixture: str) -> AgentEngine:
    engine = AgentEngine()
    engine.initialize(str(FIXTURES / fixture))
    assert engine.state.scanned_files, f"the fixture {fixture} was not scanned"
    return engine


@pytest.mark.timeout(120)
class TestTheRealDeposits:
    """Three deposits as they were submitted, scanned from disk."""

    @pytest.mark.parametrize(
        ("fixture", "folder"),
        [("svhps21_real_input", "Assay_MCT8-MDCK1"), ("svhps26_real_input", "Assay_OATP1C1")],
    )
    def test_one_assay_folder_and_the_record_at_the_root(self, fixture: str, folder: str) -> None:
        state = _scanned(fixture).state
        study, (assay,) = backbone(state, "Uptake")
        _place_everything(state, assay)

        assert _folders(state) == {}, "the record at the root is attached as well"

        state.deposit_structure = structure(".", folder)

        assert _folders(state) == {_key(assay): folder}
        assert _key(study) not in _folders(state)

    def test_four_assay_folders_under_a_study_at_the_root(self) -> None:
        state = _scanned("svhps22_real_input").state
        names = {
            "assay_01_TH_uptake": "Thyroid hormone uptake",
            "assay_02_deiodinase": "Deiodinase activity",
            "assay_03_metabolism": "Thyroid hormone metabolism",
            "assay_04_TRactivation": "T3-responsive gene expression",
        }
        study, assays = backbone(state, *names.values())
        for folder, assay in zip(names, assays, strict=True):
            place(state, assay, folder)
        expected = {_key(assay): folder for folder, assay in zip(names, assays, strict=True)}

        assert _folders(state) == expected

        state.deposit_structure = structure(".", *names)

        assert _folders(state) == expected, "the study is the deposit root either way"
        assert _key(study) not in _folders(state)

    def test_one_assay_holding_all_four_folders_takes_none(self) -> None:
        state = _scanned("svhps22_real_input").state
        _study, (assay,) = backbone(state, "Everything")
        _place_everything(state, assay)
        state.deposit_structure = structure(
            ".",
            "assay_01_TH_uptake",
            "assay_02_deiodinase",
            "assay_03_metabolism",
            "assay_04_TRactivation",
        )

        assert _folders(state) == {}
