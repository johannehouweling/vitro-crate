"""A person and an organisation take the ORCID and the ROR id the deposit states.

A BioStudies descriptor names each author and each organisation in a section of its
own, beside the funding:

    {"type": "author",
     "attributes": [{"name": "Name", "value": "Timo Hamers"},
                    {"name": "ORCID", "value": "0000-0002-5733-3290"}]}
    {"type": "organisation",
     "attributes": [{"name": "Name", "value": "VU - Vrije Universiteit Amsterdam"},
                    {"name": "RORID", "value": "https://ror.org/008xxew50"}]}

Nothing read them, so a drafted Person had an ORCID only when a name search found
exactly one, and an Organization a ROR only through an author's ORCID record: the crate
named people and organisations whose identifier the deposit itself states by a local
id made from their name. The reader keeps the name and the identifier and nothing
else, and it drafts nobody.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from builder.engine import AgentEngine, _read_declared_people
from builder.state import CrateState
from builder.tools.file_readers import extract_deposit_people

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HAMERS = {"name": "Timo Hamers", "orcid": "0000-0002-5733-3290"}
VU = {"name": "VU - Vrije Universiteit Amsterdam", "ror": "https://ror.org/008xxew50"}
DIERICHS = {"name": "Nathalie Dierichs", "orcid": "0009-0000-5074-6239"}
RIVM = {
    "name": (
        "National Institute for Public Health and the Environment (RIVM), "
        "Centre for Health Protection, Bilthoven"
    ),
    "ror": "https://ror.org/01cesdt21",
}
ERASMUS_MC = {
    "name": "Erasmus MC University Medical Centre, Rotterdam",
    "ror": "https://ror.org/018906e22",
}


def _descriptor(*sections: dict[str, Any]) -> str:
    study = {"type": "Study", "subsections": list(sections)}
    return json.dumps({"accno": "S-TEST1", "type": "submission", "section": study})


def _section(kind: str, **stated: str | None) -> dict[str, Any]:
    return {"type": kind, "attributes": [{"name": k, "value": v} for k, v in stated.items()]}


def _author(**stated: str | None) -> dict[str, Any]:
    return _section("author", **stated)


def _organisation(**stated: str | None) -> dict[str, Any]:
    return _section("organisation", **stated)


class TestExtractDepositPeople:
    def test_reads_the_sections_the_descriptor_declares(self) -> None:
        text = _descriptor(
            _author(Name="Timo Hamers", ORCID="0000-0002-5733-3290"),
            _organisation(Name=VU["name"], RORID=VU["ror"]),
        )
        assert extract_deposit_people(text) == ([HAMERS], [VU])

    def test_keeps_the_name_and_the_identifier_and_nothing_else(self) -> None:
        """An author section also holds an e-mail address, a role and an affiliation,
        and an organisation section a country: none of them identifies anyone."""
        author = _author(
            Name="Timo Hamers",
            **{"E-mail": "someone@example.org"},
            ORCID="0000-0002-5733-3290",
            Role="Corresponding author",
        )
        author["attributes"].append({"name": "affiliation", "value": "o2", "reference": True})
        organisation = _organisation(Name=VU["name"], RORID=VU["ror"], Country="Netherlands")
        found = extract_deposit_people(_descriptor(author, organisation))
        assert found == ([HAMERS], [VU])
        assert "someone@example.org" not in json.dumps(found)

    def test_matches_the_names_case_insensitively(self) -> None:
        author = {
            "type": "Author",
            "attributes": [
                {"name": "name", "value": "Timo Hamers"},
                {"name": "orcid", "value": "0000-0002-5733-3290"},
            ],
        }
        organisation = {
            "type": "Organization",
            "attributes": [
                {"name": "NAME", "value": VU["name"]},
                {"name": "RorId", "value": VU["ror"]},
            ],
        }
        assert extract_deposit_people(_descriptor(author, organisation)) == ([HAMERS], [VU])

    def test_reads_the_field_convention_too(self) -> None:
        """DataCite states a creator's identifiers with their scheme beside them, and an
        affiliation's ROR id the same way."""
        record = {
            "creators": [
                {
                    "name": "Timo Hamers",
                    "nameIdentifiers": [
                        {
                            "nameIdentifier": "https://orcid.org/0000-0002-5733-3290",
                            "nameIdentifierScheme": "ORCID",
                        }
                    ],
                    "affiliation": [
                        {
                            "name": VU["name"],
                            "affiliationIdentifier": VU["ror"],
                            "affiliationIdentifierScheme": "ROR",
                        }
                    ],
                }
            ],
            "contributors": [
                {
                    "name": "European Commission",
                    "nameType": "Organizational",
                    "nameIdentifiers": [
                        {
                            "nameIdentifier": "https://ror.org/00k4n6c32",
                            "nameIdentifierScheme": "ROR",
                        }
                    ],
                }
            ],
        }
        assert extract_deposit_people(json.dumps(record)) == (
            [{"name": "Timo Hamers", "orcid": "https://orcid.org/0000-0002-5733-3290"}],
            [VU, {"name": "European Commission", "ror": "https://ror.org/00k4n6c32"}],
        )

    def test_a_name_without_its_identifier_identifies_nobody(self) -> None:
        text = _descriptor(
            _author(Name="Martin Scholze", ORCID=""),
            _author(ORCID="0000-0002-9569-7562"),
            _organisation(Name="Brunel University London"),
            _organisation(Name="Universiteit Utrecht", RORID=None),
        )
        assert extract_deposit_people(text) is None

    def test_an_identifier_of_another_scheme_is_not_read(self) -> None:
        record = {
            "creators": [
                {
                    "name": "Timo Hamers",
                    "nameIdentifiers": [
                        {"nameIdentifier": "0000 0001 2345 6789", "nameIdentifierScheme": "ISNI"}
                    ],
                    "affiliation": [{"name": VU["name"]}, "Vrije Universiteit Amsterdam"],
                }
            ]
        }
        assert extract_deposit_people(json.dumps(record)) is None

    def test_a_declared_funder_is_not_read_as_an_organisation(self) -> None:
        """A funder is read as the funding, by its name, and stays as it is declared."""
        record = {
            "fundingReferences": [
                {
                    "funderName": "European Commission",
                    "funderIdentifier": "https://ror.org/00k4n6c32",
                    "funderIdentifierType": "ROR",
                    "awardNumber": "825161",
                }
            ]
        }
        funding = _section("Funding", Agency="EU Horizon 2020", Grant_id="825161")
        assert extract_deposit_people(json.dumps(record)) is None
        assert extract_deposit_people(_descriptor(funding)) is None

    def test_every_statement_is_kept_once_in_document_order(self) -> None:
        scholze = {"name": "Martin Scholze", "orcid": "0000-0002-9569-7562"}
        text = _descriptor(
            _author(Name="Timo Hamers", ORCID="0000-0002-5733-3290"),
            _author(Name="Martin Scholze", ORCID="0000-0002-9569-7562"),
            _author(Name="Timo Hamers", ORCID="0000-0002-5733-3290"),
        )
        assert extract_deposit_people(text) == ([HAMERS, scholze], [])

    def test_prose_is_not_a_declaration(self) -> None:
        assert extract_deposit_people("Timo Hamers (ORCID 0000-0002-5733-3290)") is None
        assert extract_deposit_people("- ORCID: 0000-0002-5733-3290") is None
        assert extract_deposit_people("not json at all") is None

    def test_every_deposit_on_hand_states_its_identifiers(self) -> None:
        found = {}
        for name in ("svhps21_real_input", "svhps22_real_input", "svhps26_real_input"):
            for path in (FIXTURES / name).glob("*.json"):
                if declared := extract_deposit_people(path.read_text()):
                    people, organisations = declared
                    found[name] = (
                        [p["name"] for p in people],
                        [o["ror"] for o in organisations],
                    )
        assert found == {
            "svhps21_real_input": (
                ["Fabian Wagenaars", "Timo Hamers", "Martin Scholze"],
                ["https://ror.org/008xxew50"],
            ),
            "svhps22_real_input": (
                [
                    "Nathalie Dierichs",
                    "W. Edward Visser",
                    "Max Tio",
                    "Daniëlle Westerbeek",
                    "Aldert Piersma",
                    "Ellen Hessel",
                    "Robin Peeters",
                    "Marcel Erwin Meima",
                ],
                ["https://ror.org/01cesdt21", "https://ror.org/018906e22"],
            ),
            "svhps26_real_input": (["Fabian Wagenaars"], []),
        }


def _initialised(tmp_path: Path) -> AgentEngine:
    deposit = tmp_path / "deposit"
    shutil.copytree(FIXTURES / "svhps22_real_input", deposit)
    engine = AgentEngine()
    engine.initialize(str(deposit))
    return engine


class TestTheSessionKeepsThem:
    def test_they_are_read_at_initialize_beside_the_funding(self, tmp_path: Path) -> None:
        state = _initialised(tmp_path).state
        assert len(state.metadata.people) == 8
        assert state.metadata.people[0] == DIERICHS
        assert state.metadata.organizations == [RIVM, ERASMUS_MC]
        assert state.metadata.funding, "the funding reader still runs beside it"

    def test_reading_them_asks_no_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identifiers are confirmed when someone drafts a person or an organisation
        of that name, never while the deposit is read."""
        import lookups._http as http

        def refuse() -> None:
            raise AssertionError("reading what a deposit states must not reach the network")

        monkeypatch.setattr(http, "get_session", refuse)
        state = _initialised(tmp_path).state
        assert state.metadata.people and state.metadata.organizations

    def test_the_reader_drafts_nobody(self, tmp_path: Path) -> None:
        state = _initialised(tmp_path).state
        assert state.list_entities("Person") == []
        assert state.list_entities("Organization") == []

    def test_what_a_session_already_holds_is_left_alone(self, tmp_path: Path) -> None:
        engine = _initialised(tmp_path)
        engine.state.metadata.people = [HAMERS]
        engine.state.metadata.organizations = []
        _read_declared_people(engine)
        assert engine.state.metadata.people == [HAMERS]
        assert engine.state.metadata.organizations == []

    def test_they_survive_a_session_round_trip(self, tmp_path: Path) -> None:
        state = _initialised(tmp_path).state
        kept = CrateState.from_json(state.to_json()).metadata
        assert kept.people == state.metadata.people
        assert kept.organizations == state.metadata.organizations

    def test_a_session_saved_before_the_fields_existed_still_loads(self) -> None:
        saved = json.loads(CrateState().to_json())
        saved["metadata"].pop("people", None)
        saved["metadata"].pop("organizations", None)
        loaded = CrateState.from_json(json.dumps(saved)).metadata
        assert (loaded.people, loaded.organizations) == ([], [])

    def test_a_session_that_holds_none_saves_none(self) -> None:
        assert not {"people", "organizations"} & set(CrateState().metadata.to_dict())
