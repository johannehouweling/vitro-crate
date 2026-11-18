"""A Study or an Assay that has a folder is that folder in the crate (#852).

An identifier names something real where something real exists: a file is its
path, and so is the folder a study or an assay was deposited in. The node of
such a Study or Assay is the directory Dataset at that folder. Its ``@id`` is
the folder's path, percent-encoded as a file's is and ending in ``/``, so that
it is the beginning of the ids of the files inside it; its ``identifier`` is the
same path as a person writes it.

Where no folder can be derived nothing changes: the entity keeps its local
``#Study_…`` or ``#Assay_…`` id, and no directory is made for it.

The oracles are the ones a receiving lab has: ro-crate-py opens the written
crate, and the validator passes it on disk, where the base profile asks that a
directory the crate describes is there.
"""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from rocrate.model import Dataset
from rocrate.rocrate import ROCrate

from builder.state import CrateState
from builder.tools.builder import export_crate
from builder.tools.provenance import attach_files
from builder.tools.validation import build_and_validate, ensure_validated
from profiles.validator import validate_crate
from tests.fixtures.folder_deposits import (
    backbone,
    deposit_state,
    graph_of,
    ids,
    place,
    structure,
    written_deposit,
)
from tests.fixtures.vhps_golden_crates import vhps_fixture_state

pytestmark = pytest.mark.timeout(600)

LDH = "Exposure scenarios/Assays/LDH analysis"
LDH_ID = "Exposure%20scenarios/Assays/LDH%20analysis/"
BMD_ID = "Exposure%20scenarios/Assays/BMD%20analysis/"


def _two_assay_folders() -> tuple[CrateState, dict[str, dict]]:
    state = deposit_state(
        f"{LDH}/plate 1.csv",
        f"{LDH}/raw data + notes/plate 2.csv",
        "Exposure scenarios/Assays/BMD analysis/bmd.csv",
        "README.txt",
    )
    _study, (ldh, bmd) = backbone(state, "LDH", "BMD")
    place(state, ldh, LDH)
    place(state, bmd, "Exposure scenarios/Assays/BMD analysis")
    return state, graph_of(state)


def _study_folder_with_two_assays() -> tuple[CrateState, dict[str, dict]]:
    state = deposit_state("study_01/assay_01/a.csv", "study_01/assay_02/b.csv", "README.txt")
    _study, (first, _second, _third) = backbone(state, "First", "Second", "Third")
    place(state, first, "study_01/assay_01")
    state.deposit_structure = structure("study_01", "study_01/assay_01", "study_01/assay_02")
    return state, graph_of(state)


class TestTheNodeOfAnAssayThatHasAFolder:
    def test_its_id_is_the_folder_and_begins_the_ids_of_its_files(self) -> None:
        _state, graph = _two_assay_folders()

        assert sorted(ids(graph[LDH_ID]["hasPart"])) == [
            LDH_ID + "plate%201.csv",
            LDH_ID + "raw%20data%20%2B%20notes/plate%202.csv",
        ]

    def test_it_is_a_dataset_that_says_it_is_an_assay(self) -> None:
        """``@type`` stays the plain string: ro-crate-py follows the ``hasPart``
        of a directory only then."""
        _state, graph = _two_assay_folders()

        assert graph[LDH_ID]["@type"] == "Dataset"
        assert graph[LDH_ID]["additionalType"] == "Assay"
        assert graph[LDH_ID]["name"] == "LDH"

    def test_its_identifier_is_the_folder_as_a_person_writes_it(self) -> None:
        _state, graph = _two_assay_folders()

        assert graph[LDH_ID]["identifier"] == LDH
        assert graph[BMD_ID]["identifier"] == "Exposure scenarios/Assays/BMD analysis"

    def test_no_name_derived_node_stands_beside_it(self) -> None:
        _state, graph = _two_assay_folders()

        assert not [node_id for node_id in graph if node_id.startswith("#Assay_")]


class TestWhereAnAssayIsListed:
    def test_under_a_study_without_a_folder_the_root_lists_it_as_well(self) -> None:
        """ro-crate-py walks from the root through directories only, and refuses a
        crate whose directory it did not reach."""
        _state, graph = _two_assay_folders()

        assert {LDH_ID, BMD_ID} <= set(ids(graph["#Study_study_study"]["hasPart"]))
        assert {LDH_ID, BMD_ID} <= set(ids(graph["./"]["hasPart"]))

    def test_under_a_study_that_has_a_folder_the_study_alone_lists_it(self) -> None:
        _state, graph = _study_folder_with_two_assays()

        assert "study_01/assay_01/" in ids(graph["study_01/"]["hasPart"])
        assert "study_01/" in ids(graph["./"]["hasPart"])
        assert "study_01/assay_01/" not in ids(graph["./"]["hasPart"])

    def test_an_assay_without_a_folder_is_listed_by_its_study_alone(self) -> None:
        """Neither the second nor the third assay holds a file, and the labelling
        names two assay folders, so neither is paired with one."""
        _state, graph = _study_folder_with_two_assays()

        without = {"#Assay_assay_second", "#Assay_assay_third"}
        assert without <= set(ids(graph["study_01/"]["hasPart"]))
        assert not without & set(ids(graph["./"]["hasPart"]))

    def test_a_file_stays_listed_by_the_root(self) -> None:
        _state, graph = _two_assay_folders()

        assert set(ids(graph[LDH_ID]["hasPart"])) <= set(ids(graph["./"]["hasPart"]))


class TestAnEntityWithoutAFolderIsAsBefore:
    def test_a_flat_deposit_keeps_the_local_ids_and_their_identifiers(self) -> None:
        state = deposit_state("readout.csv", "ic50.csv")
        _study, (assay,) = backbone(state, "Uptake")
        assert attach_files(state, to=assay.entity_id)["attached"] == 2
        graph = graph_of(state)

        assert graph["#Study_study_study"]["identifier"] == "S-TEST1/study-study_study"
        assert graph["#Assay_assay_uptake"]["identifier"] == (
            "S-TEST1/study-study_study/assay-assay_uptake"
        )
        assert "#Assay_assay_uptake" in ids(graph["#Study_study_study"]["hasPart"])
        assert "#Assay_assay_uptake" not in ids(graph["./"]["hasPart"])

    def test_a_study_at_the_deposit_root_keeps_its_local_id(self) -> None:
        _state, graph = _two_assay_folders()

        assert graph["#Study_study_study"]["additionalType"] == "Study"
        assert graph["#Study_study_study"]["identifier"] == "S-TEST1/study-study_study"

    def test_an_assay_without_a_folder_keeps_its_identifier_under_a_study_with_one(self) -> None:
        """It nests under the study's descriptor, as before. Nested under the
        study's folder it would read as a folder of the deposit that is not there."""
        _state, graph = _study_folder_with_two_assays()

        assert graph["study_01/"]["identifier"] == "study_01"
        assert graph["#Assay_assay_third"]["identifier"] == (
            "S-TEST1/study-study_study/assay-assay_third"
        )


class TestIdentifiersStayDistinct:
    def test_a_folder_named_like_the_deposit_does_not_repeat_its_identifier(self) -> None:
        """The Investigation states the accession, and so would an Assay whose
        folder is named after it. The folder stays the Assay's ``@id``."""
        state = deposit_state("S-TEST1/plate.csv", "README.txt")
        _study, (assay,) = backbone(state, "Uptake")
        place(state, assay, "S-TEST1")
        graph = graph_of(state)

        stated = [
            graph["./"]["identifier"],
            graph["#Study_study_study"]["identifier"],
            graph["S-TEST1/"]["identifier"],
        ]
        assert graph["./"]["identifier"] == "S-TEST1"
        assert len(set(stated)) == 3, stated

    def test_every_level_of_a_deposit_with_folders_states_its_own(self) -> None:
        _state, graph = _study_folder_with_two_assays()

        stated = [
            node["identifier"]
            for node in graph.values()
            if node.get("additionalType") in ("Investigation", "Study", "Assay")
        ]
        assert len(stated) == 5
        assert len(set(stated)) == 5, stated


class TestTheVerdictFollowsTheStructure:
    """The build reads the labelling to decide a folder, so a verdict reached
    before the labelling changed describes another crate."""

    def test_a_recorded_verdict_is_stale_once_the_labelling_changes(self) -> None:
        state, _graph = _study_folder_with_two_assays()
        before = state.validation_fingerprint()
        ensure_validated(state, severity="required")
        assert not state.validation.is_stale_for(state)

        state.deposit_structure = structure("study_01", "study_01/assay_02")

        assert state.validation_fingerprint() != before
        assert state.validation.is_stale_for(state)


class TestTheGuidanceAsksNoArchive:
    """The base profile asks a directory whether it states a ``distribution``, an
    archive its repository serves once the crate is published."""

    def test_a_build_does_not_recommend_one_for_an_assay(self) -> None:
        state, _graph = _two_assay_folders()

        issues = build_and_validate(state, severity="recommended", profile="base")["issues"]

        assert [i for i in issues if str(i["entity_id"]).endswith(LDH_ID)], "nothing was found"
        assert not [i for i in issues if str(i["property"]).endswith("/distribution")]

    def test_the_folder_of_an_assay_is_asked_its_description_and_not_its_archive(self) -> None:
        from builder.tools.gap_analysis import REPORT_ONLY, assess_gaps

        state, _graph = _two_assay_folders()

        on_the_folder = [g for g in assess_gaps(state).gaps if g.entity_id == "./" + LDH_ID]
        archive = [g for g in on_the_folder if str(g.property).endswith("/distribution")]
        description = [g for g in on_the_folder if str(g.property).endswith("/description")]

        assert archive and {g.fix_hint for g in archive} == {REPORT_ONLY}
        assert description and {g.fix_hint for g in description} == {"ask-user"}
        assert {g.entity_type for g in on_the_folder} == {"Assay"}


# What an export writes itself, beside the deposit's files.
GENERATED = {"ro-crate-metadata.json", "ro-crate-preview.html", "ro-crate-metadata-maturity.html"}
ASSAY = "Assay MCT8-MDCK1"
ASSAY_ID = "Assay%20MCT8-MDCK1/"
FILES = (
    f"{ASSAY}/README.txt",
    f"{ASSAY}/Raw data + processed data/plate 1.csv",
    "notes.txt",
)


def _golden_deposit(folder: Path) -> CrateState:
    """The S-VHPS21 golden crate, which every profile passes, with a deposit whose
    one assay folder is attached to its Assay."""
    state = written_deposit(folder, *FILES, state=vhps_fixture_state("S-VHPS21"))
    assay = state.get_entity("assay")
    assert assay is not None
    place(state, assay, ASSAY)
    return state


def _written(crate_dir: Path) -> dict[str, dict]:
    document = json.loads((crate_dir / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    return {node["@id"]: node for node in document["@graph"]}


def _references(value):
    if isinstance(value, dict):
        if set(value) == {"@id"}:
            yield value["@id"]
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One deposit, exported once with its files and once without them."""
    root = tmp_path_factory.mktemp("folders")
    state = _golden_deposit(root / "deposit")
    validated = graph_of(state)
    public_state = _golden_deposit(root / "deposit_for_public")
    public_state.metadata.metadata_only = True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        full = export_crate(state, str(root / "full"))
        public = export_crate(public_state, str(root / "public"))
    assert full["success"] is True, full
    assert public["success"] is True, public
    return {
        "state": state,
        "validated": validated,
        "full_result": full,
        "full": root / "full",
        "public_result": public,
        "public": root / "public",
    }


class TestTheWrittenCrate:
    def test_the_assay_is_the_folder_under_a_study_that_has_none(self, exported) -> None:
        nodes = _written(exported["full"])

        assert nodes[ASSAY_ID]["additionalType"] == "Assay"
        assert nodes[ASSAY_ID]["identifier"] == ASSAY
        assert ASSAY_ID in ids(nodes["#Study_study"]["hasPart"])
        assert ASSAY_ID in ids(nodes["./"]["hasPart"])

    def test_the_folder_holds_the_files_the_deposit_had_in_it(self, exported) -> None:
        for relative in FILES:
            assert (exported["full"] / relative).is_file()

    def test_every_profile_still_passes_it(self, exported) -> None:
        """A directory has no bytes of its own, so the payload check must not
        report it as a file the crate does not carry."""
        validation = exported["full_result"]["validation"]

        assert validation["ok"] is True, exported["state"].validation.required_issues
        assert exported["state"].validation.required_issues == []
        assert exported["state"].validation.payload_checked is True

    def test_the_validator_passes_it_on_disk(self, exported) -> None:
        failed = [r.profile for r in validate_crate(exported["full"]) if not r.passed_required]

        assert failed == []

    def test_a_receiving_lab_can_open_it(self, exported) -> None:
        """Plain ro-crate-py, in a process that knows nothing of this repository."""
        script = (
            "from rocrate.rocrate import ROCrate;"
            f"c = ROCrate({str(exported['full'])!r});"
            f"a = c.dereference({ASSAY_ID!r});"
            "assert type(a).__name__ == 'Dataset', type(a);"
            f"assert {ASSAY_ID + 'README.txt'!r} in [p.id for p in a['hasPart']]"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )

        assert proc.returncode == 0, proc.stderr[-800:]

    def test_it_opens_in_the_process_that_wrote_it(self, exported) -> None:
        import profiles.models.isa  # noqa: F401
        import profiles.models.tox  # noqa: F401

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            crate = ROCrate(str(exported["full"]))

        assert isinstance(crate.dereference(ASSAY_ID), Dataset)

    def test_it_carries_the_ids_the_validation_read(self, exported) -> None:
        nodes = _written(exported["full"])

        assert set(exported["validated"]) <= set(nodes)
        assert ASSAY_ID in exported["validated"]


class TestAStudyThatHasAFolderIsReadBack:
    def test_ro_crate_py_reaches_the_assay_through_its_study(self, tmp_path: Path) -> None:
        """The Assay is listed by its Study alone, so the reader has to walk into
        the Study's directory to find it."""
        state = written_deposit(
            tmp_path / "deposit",
            "study 01/README.txt",
            "study 01/assay 01/plate.csv",
            state=vhps_fixture_state("S-VHPS21"),
        )
        study, assay = state.get_entity("study"), state.get_entity("assay")
        assert study is not None and assay is not None
        place(state, assay, "study 01/assay 01")
        state.deposit_structure = structure("study 01", "study 01/assay 01")
        out = tmp_path / "crate"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = export_crate(state, str(out))
            assert result["success"] is True, result
            crate = ROCrate(str(out))

        nodes = _written(out)
        assert "study%2001/assay%2001/" in ids(nodes["study%2001/"]["hasPart"])
        assert "study%2001/assay%2001/" not in ids(nodes["./"]["hasPart"])
        assert isinstance(crate.dereference("study%2001/assay%2001/"), Dataset)
        assert result["validation"]["ok"] is True, state.validation.required_issues


class TestWithoutItsFiles:
    """A metadata-only crate describes a folder as it describes a file: under a
    local identifier, with the path it belongs at."""

    def test_the_assay_is_described_as_a_folder_that_is_not_present(self, exported) -> None:
        nodes = _written(exported["public"])

        assert ASSAY_ID not in nodes, "a relative @id says the folder is in the crate"
        assert nodes["#" + ASSAY_ID]["localPath"] == ASSAY
        assert nodes["#" + ASSAY_ID]["@type"] == "Dataset"
        assert nodes["#" + ASSAY_ID]["additionalType"] == "Assay"
        assert nodes["#" + ASSAY_ID]["identifier"] == ASSAY

    def test_it_still_lists_its_files_and_is_still_listed(self, exported) -> None:
        nodes = _written(exported["public"])

        assert {
            "#" + ASSAY_ID + "README.txt",
            "#" + ASSAY_ID + "Raw%20data%20%2B%20processed%20data/plate%201.csv",
        } <= set(ids(nodes["#" + ASSAY_ID]["hasPart"]))
        assert "#" + ASSAY_ID in ids(nodes["#Study_study"]["hasPart"])
        assert "#" + ASSAY_ID in ids(nodes["./"]["hasPart"])

    def test_no_folder_and_no_data_file_is_written(self, exported) -> None:
        assert {path.name for path in exported["public"].iterdir()} == GENERATED

    def test_a_folder_is_not_counted_among_the_files_left_out(self, exported) -> None:
        assert exported["public_result"]["files_left_out"] == len(FILES)

    def test_nothing_points_at_an_entity_that_is_not_there(self, exported) -> None:
        nodes = _written(exported["public"])
        dangling = {
            ref
            for ref in _references(list(nodes.values()))
            if not ref.startswith(("http://", "https://")) and ref not in nodes
        }

        assert dangling == set()

    def test_every_profile_still_passes_it(self, exported) -> None:
        assert exported["public_result"]["validation"]["ok"] is True

    def test_the_validator_passes_it_on_disk(self, exported) -> None:
        """On disk the base profile asks that a directory the crate describes
        exists, which the document validated before the write cannot show."""
        failed = [r.profile for r in validate_crate(exported["public"]) if not r.passed_required]

        assert failed == []

    def test_ro_crate_py_opens_it(self, exported) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            crate = ROCrate(str(exported["public"]))

        assert crate.dereference("#" + ASSAY_ID)["localPath"] == ASSAY


class TestTheViewsStillDraw:
    def test_the_inventory_gives_the_assay_its_study_as_parent(self, exported) -> None:
        from builder.writers.provenance_dag import build_isa_inventory

        nodes = {
            node["id"]: node
            for node in build_isa_inventory(list(_written(exported["full"]).values()))["nodes"]
        }

        assert nodes[ASSAY_ID]["level"] == "Assay"
        assert nodes[ASSAY_ID]["parent"] == "#Study_study"
        assert nodes[ASSAY_ID]["state"] == "linked"

    def test_the_assay_has_its_lane_under_a_key_made_from_its_name(self, exported) -> None:
        from builder.writers.entity_explorer import build_assay_lanes

        (lane,) = build_assay_lanes(list(_written(exported["full"]).values()))

        assert lane["assay"] == ASSAY_ID
        assert lane["key"] == "assay-mct8-mdck1-cellular-uptake-assay"

    def test_a_folder_is_carried_and_a_local_record_is_not(self, exported) -> None:
        from builder.writers.provenance_dag import build_crate_graph

        nodes = {
            node["id"]: node
            for node in build_crate_graph(
                list(_written(exported["full"]).values()), all_edges=True
            )["nodes"]
        }

        assert nodes[ASSAY_ID]["residence"] == "carried"
        assert nodes["#Study_study"]["residence"] == "record"

    def test_the_report_inside_the_crate_was_rendered(self, exported) -> None:
        page = (exported["full"] / "ro-crate-metadata-maturity.html").read_text(encoding="utf-8")

        assert "assay-mct8-mdck1-cellular-uptake-assay" in page
