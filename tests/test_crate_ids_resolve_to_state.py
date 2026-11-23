"""The id a crate gives an entity leads back to the entity in the state.

A finding names an entity by the ``@id`` of its node, and that is the string the
agent has in hand when it goes to act on the finding. Four places turn it back
into a state entity: the repair loop, ``resolve_entity_id`` behind the tools, the reference
normaliser behind ``set_fields``, and the agent's test of whether a finding is
its own to fix. They share one function, so that an Assay identified by its
folder, and a file whose path needs escaping, are found by all of them.

The match is exact. A folder id shares no letters with the state id of its
Assay, and a guess by likeness would edit some other entity.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.drafters import draft_process
from builder.tools.management import resolve_entity_id, set_fields
from builder.tools.provenance import attach_files, find_or_create_file, link
from builder.tools.repair import _resolve_state_entity
from tests.fixtures.folder_deposits import backbone, deposit_state, graph_of, ids, place

ASSAY_ID = "assay%2001/"
PLATE_ID = "assay%2001/plate%201.csv"


def _deposit() -> tuple[CrateState, Entity, Entity]:
    """An Assay at ``assay 01/`` holding two files, and a second Assay with none.

    Two more files are described and placed nowhere. Their paths differ by a
    leading dot only.
    """
    state = deposit_state(
        "assay 01/plate 1.csv",
        "assay 01/raw/trace.csv",
        "assay 02/plate.csv",
        ".config/settings.csv",
        "config/settings.csv",
    )
    _study, (with_folder, without) = backbone(state, "Uptake", "Viability")
    place(state, with_folder, "assay 01")
    for scanned in state.scanned_files:
        if scanned.filename == "settings.csv":
            find_or_create_file(state, scanned)
    return state, with_folder, without


def _for_crate_id(state: CrateState, crate_id: str, **kwargs) -> Entity | None:
    from builder.tools._crate_mapping import entity_for_crate_id

    return entity_for_crate_id(state, crate_id, **kwargs)


def _file_at(state: CrateState, dest: str) -> Entity:
    (found,) = [f for f in state.list_entities("File") if f.fields.get("dest_path") == dest]
    return found


class TestTheFixtureIsWhatItSays:
    def test_the_crate_gives_the_assay_its_folder_and_the_files_their_paths(self) -> None:
        state, _with_folder, _without = _deposit()
        graph = graph_of(state)

        assert graph[ASSAY_ID]["additionalType"] == "Assay"
        assert graph["#Assay_assay_viability"]["additionalType"] == "Assay"
        assert {PLATE_ID, ".config/settings.csv", "config/settings.csv"} <= set(graph)


class TestOneFunctionFindsTheEntity:
    def test_a_folder_id_is_its_assay(self) -> None:
        state, with_folder, _without = _deposit()

        assert _for_crate_id(state, ASSAY_ID) is with_folder
        assert _for_crate_id(state, "./" + ASSAY_ID) is with_folder

    def test_a_folder_id_is_matched_exactly(self) -> None:
        state, _with_folder, _without = _deposit()

        assert _for_crate_id(state, "assay%2001") is None, "the slash is part of the id"
        assert _for_crate_id(state, "assay 01/") is None, "the id is percent-encoded"
        assert _for_crate_id(state, "assay%2001/raw/") is None, "a folder inside it is not it"
        assert _for_crate_id(state, "assay%2002/") is None, "no Assay holds a file of that folder"

    def test_a_local_id_is_still_its_entity(self) -> None:
        state, _with_folder, without = _deposit()

        assert _for_crate_id(state, "./#Assay_assay_viability") is without
        assert _for_crate_id(state, "#Study_study_study") is state.get_entity("study_study")

    def test_an_assay_at_its_folder_still_answers_to_the_local_id_it_had(self) -> None:
        """The id moves to the folder when the files are attached, and a caller
        may hold the one a finding gave it before."""
        state, with_folder, _without = _deposit()

        assert _for_crate_id(state, "./#Assay_assay_uptake") is with_folder

    def test_a_file_is_found_by_the_id_its_node_has(self) -> None:
        state, _with_folder, _without = _deposit()

        assert _for_crate_id(state, "./" + PLATE_ID) is _file_at(state, "assay 01/plate 1.csv")

    def test_the_dot_slash_is_removed_as_a_prefix(self) -> None:
        """Stripping the characters would take the dot of ``.config`` with it."""
        state, _with_folder, _without = _deposit()

        hidden = _file_at(state, ".config/settings.csv")
        assert _for_crate_id(state, "./.config/settings.csv") is hidden
        assert _for_crate_id(state, "./config/settings.csv") is _file_at(
            state, "config/settings.csv"
        )

    def test_a_caller_may_leave_files_out(self) -> None:
        state, with_folder, _without = _deposit()

        assert _for_crate_id(state, PLATE_ID, files=False) is None
        assert _for_crate_id(state, ASSAY_ID, files=False) is with_folder


class TestTheRepairLoopAndTheGapEngine:
    def test_a_finding_on_a_folder_assay_reaches_the_assay(self) -> None:
        state, with_folder, _without = _deposit()

        assert _resolve_state_entity(state, "./" + ASSAY_ID) is with_folder

    def test_a_finding_on_a_file_stays_without_an_entity(self) -> None:
        """A finding on a File is reported and never asked about. That is how it
        was, and resolving it here would turn each into a question."""
        state, _with_folder, _without = _deposit()

        assert _resolve_state_entity(state, "./" + PLATE_ID) is None

    def test_the_root_is_still_no_entity(self) -> None:
        state, _with_folder, _without = _deposit()

        assert _resolve_state_entity(state, "./") is None

class TestTheToolsTakeTheCrateId:
    def test_set_fields_writes_to_the_assay_named_by_its_folder(self) -> None:
        state, with_folder, _without = _deposit()

        set_fields(state, "./" + ASSAY_ID, {"description": "Uptake of T3."})

        assert with_folder.fields["description"] == "Uptake of T3."

    def test_a_file_is_resolved_from_its_encoded_id_and_from_its_plain_path(self) -> None:
        state, _with_folder, _without = _deposit()
        plate = _file_at(state, "assay 01/plate 1.csv")

        assert resolve_entity_id(state, "./" + PLATE_ID) == plate.entity_id
        assert resolve_entity_id(state, "assay 01/plate 1.csv") == plate.entity_id

    def test_two_paths_that_differ_by_a_leading_dot_stay_two_files(self) -> None:
        state, _with_folder, _without = _deposit()

        assert resolve_entity_id(state, "./.config/settings.csv") == (
            _file_at(state, ".config/settings.csv").entity_id
        )
        assert resolve_entity_id(state, "./config/settings.csv") == (
            _file_at(state, "config/settings.csv").entity_id
        )

    def test_what_resolves_to_nothing_is_returned_as_it_was_given(self) -> None:
        state, _with_folder, _without = _deposit()

        assert resolve_entity_id(state, "assay%2001") == "assay%2001"

    def test_attach_files_takes_the_crate_id_of_its_target(self) -> None:
        state, with_folder, without = _deposit()

        attached = attach_files(state, to="./#Assay_assay_viability", name_contains="assay 02/")
        assert attached["attached"] == 1
        assert attached["to"] == without.entity_id
        assert len(without.fields["hasPart"]) == 1

        again = attach_files(state, to=ASSAY_ID, name_contains="assay 01/raw/")
        assert again["attached"] == 1
        assert again["to"] == with_folder.entity_id
        assert len(with_folder.fields["hasPart"]) == 2, "the file was attached already"

    def test_attach_files_still_says_what_it_did_not_find(self) -> None:
        state, _with_folder, _without = _deposit()

        with pytest.raises(ValueError, match="attach_files target: Entity not found: assay%2003/"):
            attach_files(state, to="assay%2003/")

    def test_link_takes_the_crate_ids_of_both_ends(self) -> None:
        state, with_folder, _without = _deposit()
        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )

        link(state, "./#LabProcess_proc_readout", "result", PLATE_ID)

        assert readout.fields["result"] == _file_at(state, "assay 01/plate 1.csv").entity_id


class TestAReferenceWrittenAsACrateId:
    def test_a_step_given_its_assay_by_folder_belongs_to_that_assay(self) -> None:
        state, with_folder, without = _deposit()
        readout = draft_process(state, without.entity_id, "EndpointReadout", {"name": "Readout"})

        set_fields(state, readout.entity_id, {"assay_id": "./" + ASSAY_ID})

        assert readout.fields["assay_id"] == with_folder.entity_id
        assert ids(graph_of(state)[ASSAY_ID]["about"]) == ["#LabProcess_proc_readout"]

    def test_a_reference_is_no_longer_cut_at_its_first_underscore(self) -> None:
        """``sample_assay_uptake`` names nothing. Cut at the underscore it named
        the Assay ``assay_uptake``, and the step would have consumed an Assay."""
        state, with_folder, _without = _deposit()
        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )

        set_fields(state, readout.entity_id, {"object": "sample_assay_uptake"})

        assert readout.fields["object"] == ["sample_assay_uptake"]

    def test_a_type_qualified_id_names_its_entity_where_the_node_has_an_address(self) -> None:
        """The crate gives a compound with a PubChem CID that address, and a caller
        may still write the local form. Cut at its first underscore it named the
        compound, and matched whole it still does."""
        state, with_folder, _without = _deposit()
        state.add_entity(
            Entity(
                entity_id="chem_t3",
                type="MolecularEntity",
                fields={"name": "Triiodothyronine", "pubchem_cid": "5920"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        exposure = draft_process(state, with_folder.entity_id, "Exposure", {"name": "Exposure"})

        set_fields(state, exposure.entity_id, {"chemicals": ["./#MolecularEntity_chem_t3"]})

        assert exposure.fields["chemicals"] == ["chem_t3"]

    def test_a_local_crate_id_is_still_reduced_to_the_state_id(self) -> None:
        state, with_folder, without = _deposit()
        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )

        set_fields(state, readout.entity_id, {"assay_id": "./#Assay_assay_viability"})

        assert readout.fields["assay_id"] == without.entity_id

    def test_a_file_named_by_its_type_qualified_id_is_its_entity(self) -> None:
        """The model is told that the ``./#Type_id`` form is stored as the state id,
        and a File has that form as well as its path."""
        state, with_folder, _without = _deposit()
        plate = _file_at(state, "assay 01/plate 1.csv")
        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )

        set_fields(state, readout.entity_id, {"result": "#File_" + plate.entity_id})

        assert readout.fields["result"] == [plate.entity_id]
        assert resolve_entity_id(state, "./#File_" + plate.entity_id) == plate.entity_id
        assert resolve_entity_id(state, "File_" + plate.entity_id) == plate.entity_id

    def test_a_file_named_by_its_crate_id_is_its_entity(self) -> None:
        state, with_folder, _without = _deposit()
        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )

        set_fields(state, readout.entity_id, {"result": "./" + PLATE_ID})

        assert readout.fields["result"] == [_file_at(state, "assay 01/plate 1.csv").entity_id]


class TestTheIdsOfACrateWithoutItsFiles:
    """A metadata-only crate leaves the files and the folders out and describes
    each at ``#`` and the id it has in the crate with its files, and the report of
    such a crate names an entity by that id."""

    def test_a_folder_left_out_is_its_assay(self) -> None:
        state, with_folder, _without = _deposit()

        assert _for_crate_id(state, "./#" + ASSAY_ID) is with_folder
        assert _resolve_state_entity(state, "./#" + ASSAY_ID) is with_folder
        assert resolve_entity_id(state, "#" + ASSAY_ID) == with_folder.entity_id

    def test_a_file_left_out_is_its_file(self) -> None:
        state, _with_folder, _without = _deposit()
        plate = _file_at(state, "assay 01/plate 1.csv")

        assert _for_crate_id(state, "./#" + PLATE_ID) is plate
        assert _for_crate_id(state, "./#" + PLATE_ID, files=False) is None
        assert resolve_entity_id(state, "./#" + PLATE_ID) == plate.entity_id

    def test_the_tools_take_them(self) -> None:
        state, with_folder, _without = _deposit()
        plate = _file_at(state, "assay 01/plate 1.csv")

        attached = attach_files(state, to="#" + ASSAY_ID, name_contains="assay 01/raw/")
        assert attached["to"] == with_folder.entity_id

        readout = draft_process(
            state, with_folder.entity_id, "EndpointReadout", {"name": "Readout"}
        )
        link(state, "./#LabProcess_proc_readout", "result", "./#" + PLATE_ID)
        assert readout.fields["result"] == plate.entity_id

    def test_a_finding_on_one_is_the_agents_to_fix(self) -> None:
        from builder.agents.react.agent_loop import _issue_targets_own_entity

        state, _with_folder, _without = _deposit()

        for crate_id in ("./#" + ASSAY_ID, "./#" + PLATE_ID):
            line = f"[base] {crate_id}: Data Entities SHOULD have a `description` property"
            assert _issue_targets_own_entity(state, line) is True, crate_id

    def test_an_id_that_a_node_has_comes_first(self) -> None:
        """A file named like the local id of an Assay is left out at that id with an
        underscore more, so the local id still names the Assay."""
        state, _with_folder, without = _deposit()
        state.scanned_files.extend(deposit_state("Assay_assay_viability").scanned_files)
        named_alike = find_or_create_file(state, state.scanned_files[-1])

        assert _for_crate_id(state, "#Assay_assay_viability") is without
        assert _for_crate_id(state, "Assay_assay_viability") is named_alike


class TestWhoseFindingItIs:
    def _owns(self, state: CrateState, entity_id: str) -> bool:
        from builder.agents.react.agent_loop import _issue_targets_own_entity

        line = f"[base] {entity_id}: Data Entities SHOULD have a `description` property"
        return _issue_targets_own_entity(state, line)

    def test_a_finding_on_a_folder_assay_is_the_agents_to_fix(self) -> None:
        state, _with_folder, _without = _deposit()

        assert self._owns(state, "./" + ASSAY_ID) is True

    def test_one_on_a_file_whose_path_needs_escaping_is_the_agents_to_fix(self) -> None:
        state, _with_folder, _without = _deposit()

        assert self._owns(state, "./" + PLATE_ID) is True

    def test_one_on_a_node_the_build_makes_is_not(self) -> None:
        state, _with_folder, _without = _deposit()

        assert self._owns(state, "./#LabProcess_proc_exposure_col_well_id") is False
        assert self._owns(state, "./assay%2002/") is False


class TestAnUnnamedFolderIsCalledByItsName:
    def test_the_last_part_of_the_folder_is_what_a_reader_is_shown(self) -> None:
        """It is shown as the deposit spells it, so that a reader finds it there."""
        from builder.tools.remediation import _entity_label

        assert _entity_label("./assay_01_TH_uptake/", {}) == "assay_01_TH_uptake"
        assert _entity_label("./study_01/assay_02_deiodinase/", {}) == "assay_02_deiodinase"
        assert _entity_label("./Exposure%20scenarios/LDH%20analysis/", {}) == "LDH analysis"
        assert _entity_label("./#assay%2001/", {}) == "assay 01"

    def test_a_folder_named_like_a_type_keeps_its_whole_name(self) -> None:
        """S-VHPS21 deposits its assay in ``Assay_MCT8-MDCK1/``, a name the
        depositor gave and no type the crate put before an id."""
        from builder.tools.remediation import _entity_label

        assert _entity_label("./Assay_MCT8-MDCK1/", {}) == "Assay_MCT8-MDCK1"
        assert _entity_label("./#Assay_OATP1C1/", {}) == "Assay_OATP1C1"
        assert _entity_label("./Study_01/Assay_x/", {}) == "Assay_x"

    def test_an_address_that_ends_in_a_slash_is_shown_whole_as_before(self) -> None:
        from builder.tools.remediation import _entity_label

        licence = "https://creativecommons.org/licenses/by/4.0/"
        assert _entity_label(licence, {}) == licence
