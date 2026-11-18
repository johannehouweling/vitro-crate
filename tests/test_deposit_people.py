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
from pathlib import Path
from typing import Any

from builder.tools.file_readers import extract_deposit_people

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HAMERS = {"name": "Timo Hamers", "orcid": "0000-0002-5733-3290"}
VU = {"name": "VU - Vrije Universiteit Amsterdam", "ror": "https://ror.org/008xxew50"}


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
