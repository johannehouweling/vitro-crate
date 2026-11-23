"""An organisation takes the id of the ROR record that registers its name.

ROR registers an organisation under more than one name: the one it displays, its
names in other languages, and aliases. "Universiteit Utrecht" is one of the names
of Utrecht University, and "Charite-Universitätsmedizin Berlin" is the name of the
Charité spelled without its accent. Such a name is that record: not a guess from a
search, so the organisation takes ROR's id and the name ROR displays for it, and
two spellings of one organisation become one. A name no record registers, or
several do, is no answer and the organisation keeps its own name and no ROR. An
acronym is not a name: "VU" is several institutions.

ROR's answers here were recorded from its search on 2026-11-19. No test here
touches the network.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import responses
from requests.exceptions import Timeout
from rocrate.rocrate import ROCrate

from builder.state import CrateState
from lookups._http import TransientLookupError
from lookups.ror import find_registered, same_name

FIXTURES = Path(__file__).parent / "fixtures"
ROR_SEARCH = "https://api.ror.org/organizations"
UTRECHT = "https://ror.org/04pp8hn57"
CHARITE = "https://ror.org/001w7jn25"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _clear_cache():
    find_registered.cache_clear()
    yield
    find_registered.cache_clear()


def _answer(payload: dict) -> None:
    responses.add(responses.GET, ROR_SEARCH, json=payload, status=200)


class TestTheRecordThatRegistersTheName:
    @responses.activate
    def test_a_name_in_another_language_is_the_record_that_registers_it(self):
        _answer(_load("ror_search_universiteit_utrecht.json"))

        found = find_registered("Universiteit Utrecht")

        assert found["@id"] == UTRECHT
        assert found["name"] == "Utrecht University"
        assert found["addressCountry"] == "NL"

    @responses.activate
    def test_a_spelling_without_its_accents_and_dashes_is_the_same_name(self):
        _answer(_load("ror_search_charite.json"))

        found = find_registered("Charite-Universitätsmedizin Berlin")

        assert found["@id"] == CHARITE
        assert found["name"] == "Charité – Universitätsmedizin Berlin"

    @responses.activate
    def test_a_name_no_record_registers_is_no_answer(self):
        """ROR's first result for "EU Horizon 2020" is an Austrian research centre."""
        _answer(_load("ror_search_eu_horizon_2020.json"))

        assert find_registered("EU Horizon 2020") == {}

    @responses.activate
    def test_a_name_two_records_register_is_no_answer(self):
        payload = _load("ror_search_universiteit_utrecht.json")
        rival = copy.deepcopy(payload["items"][1])
        rival["names"].append({"value": "Universiteit Utrecht", "types": ["alias"], "lang": "nl"})
        payload["items"][1] = rival
        _answer(payload)

        assert find_registered("Universiteit Utrecht") == {}

    @responses.activate
    def test_an_acronym_is_not_a_name(self):
        """ "UMCU" is the acronym ROR lists for UMC Utrecht, not a name it registers."""
        _answer(_load("ror_search_universiteit_utrecht.json"))

        assert find_registered("UMCU") == {}

    @responses.activate
    def test_a_record_of_the_first_api_generation_is_read_the_same_way(self):
        _answer(
            {
                "items": [
                    {
                        "id": UTRECHT,
                        "name": "Utrecht University",
                        "labels": [{"label": "Universiteit Utrecht", "iso639": "nl"}],
                        "aliases": ["Rijksuniversiteit Utrecht"],
                        "acronyms": ["UU"],
                        "links": ["https://www.uu.nl"],
                    }
                ]
            }
        )

        assert find_registered("Rijksuniversiteit Utrecht")["@id"] == UTRECHT

    def test_an_empty_name_asks_nothing(self):
        assert find_registered("  ") == {}

    @responses.activate
    def test_an_unreachable_registry_is_reported_not_answered(self):
        responses.add(responses.GET, ROR_SEARCH, body=Timeout("timed out"))

        with pytest.raises(TransientLookupError):
            find_registered("Universiteit Utrecht")


class TestTwoNamesAreTheSameName:
    def test_a_connective_carries_no_weight(self):
        assert same_name("Brunel University of London", "Brunel University London")

    def test_accents_case_and_dashes_carry_no_weight(self):
        assert same_name(
            "Charité – Universitätsmedizin Berlin", "charite-universitatsmedizin berlin"
        )

    def test_a_real_but_different_institution_is_another_name(self):
        assert not same_name("University of London", "Brunel University London")


def _registry(monkeypatch, records: dict[str, dict]) -> None:
    """ROR, as the composites ask it: each name to the record that registers it."""
    import builder.tools.composites as composites

    monkeypatch.setattr(composites, "find_registered", lambda name: records.get(name, {}))


def _record(ror: str, name: str, country: str) -> dict:
    return {
        "@id": ror,
        "@type": "Organization",
        "name": name,
        "identifier": ror,
        "url": "",
        "addressCountry": country,
    }


CHARITE_RECORD = _record(CHARITE, "Charité – Universitätsmedizin Berlin", "DE")
UTRECHT_RECORD = _record(UTRECHT, "Utrecht University", "NL")


def _organization_of(state: CrateState, person_id: str):
    person = state.get_entity(person_id)
    assert person is not None
    org_id = str((person.fields.get("affiliation") or {}).get("@id") or "")
    org = state.get_entity(org_id)
    assert org is not None, f"{person_id} has no Organization"
    return org


def _crate_ids(state: CrateState) -> dict[str, str]:
    from builder.tools._crate_mapping import populate_crate
    from profiles.context import ISA_TOX_CONTEXT

    state.metadata.title = "Affiliations"
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    graph = crate.metadata.generate()["@graph"]
    return {node.get("name"): node["@id"] for node in graph if node.get("@type") == "Organization"}


class TestAnAuthorsAffiliation:
    """The ORCID record names an employer and gives no ROR id for it."""

    def _author(self, state, orcid, given, family, affiliation):
        from builder.tools.composites import _ensure_person_for_orcid

        data = {
            "givenName": given,
            "familyName": family,
            "name": f"{given} {family}",
            "affiliation_name": affiliation,
            "affiliation_ror": "",
        }
        return _ensure_person_for_orcid(state, orcid, data)

    def test_the_employer_takes_the_record_that_registers_its_name(self, monkeypatch):
        _registry(monkeypatch, {"Charité – Universitätsmedizin Berlin": CHARITE_RECORD})
        state = CrateState()

        person = self._author(
            state,
            "0000-0001-7298-844X",
            "Caroline",
            "Frädrich",
            "Charité – Universitätsmedizin Berlin",
        )

        org = _organization_of(state, person.entity_id)
        assert org.fields.get("ror") == CHARITE
        assert _crate_ids(state) == {"Charité – Universitätsmedizin Berlin": CHARITE}

    def test_two_spellings_of_one_employer_are_one_organization(self, monkeypatch):
        _registry(
            monkeypatch,
            {
                "Charité – Universitätsmedizin Berlin": CHARITE_RECORD,
                "Charite-Universitätsmedizin Berlin": CHARITE_RECORD,
            },
        )
        state = CrateState()

        first = self._author(
            state,
            "0000-0001-7298-844X",
            "Caroline",
            "Frädrich",
            "Charité – Universitätsmedizin Berlin",
        )
        second = self._author(
            state, "0000-0002-9187-9078", "Josef", "Köhrle", "Charite-Universitätsmedizin Berlin"
        )

        assert _organization_of(state, first.entity_id) is _organization_of(state, second.entity_id)
        assert len(state.list_entities("Organization")) == 1

    def test_a_name_no_record_registers_keeps_its_name_and_no_ror(self, monkeypatch):
        _registry(monkeypatch, {})
        state = CrateState()

        person = self._author(
            state,
            "0000-0003-2050-0961",
            "Kostja",
            "Renko",
            "Institut für Experimentelle Endokrinologie",
        )

        org = _organization_of(state, person.entity_id)
        assert org.fields.get("name") == "Institut für Experimentelle Endokrinologie"
        assert not org.fields.get("ror")

    def test_an_unreachable_registry_keeps_the_name(self, monkeypatch):
        import builder.tools.composites as composites

        def down(name):
            raise TransientLookupError("ROR is down")

        monkeypatch.setattr(composites, "find_registered", down)
        state = CrateState()

        person = self._author(
            state,
            "0000-0001-7298-844X",
            "Caroline",
            "Frädrich",
            "Charité – Universitätsmedizin Berlin",
        )

        org = _organization_of(state, person.entity_id)
        assert org.fields.get("name") == "Charité – Universitätsmedizin Berlin"
        assert not org.fields.get("ror")

    def test_a_ror_the_orcid_record_gives_is_not_asked_again(self, monkeypatch):
        import builder.tools.composites as composites

        def unexpected(name):
            raise AssertionError("ROR asked for a name whose id ORCID gave")

        monkeypatch.setattr(composites, "find_registered", unexpected)
        monkeypatch.setattr(composites, "_ror_facts", lambda ror: {})
        from builder.tools.composites import _ensure_person_for_orcid

        state = CrateState()
        data = {
            "givenName": "Peter",
            "familyName": "Cenijn",
            "affiliation_name": "Vrije Universiteit Amsterdam",
            "affiliation_ror": "https://ror.org/008xxew50",
        }
        person = _ensure_person_for_orcid(state, "0009-0004-3506-9400", data)

        assert (
            _organization_of(state, person.entity_id).fields.get("ror")
            == "https://ror.org/008xxew50"
        )


class TestAnAffiliationOnAPapersAuthorLine:
    BRUNEL_A = "Centre for Pollution Research and Policy, Brunel University London, Uxbridge, UK."

    def test_the_institution_takes_the_record_that_registers_its_name(self, monkeypatch):
        import builder.tools.composites as composites

        brunel = _record("https://ror.org/00dn4t376", "Brunel University of London", "GB")
        monkeypatch.setattr(
            composites,
            "find_registered",
            lambda name: brunel if name == "Brunel University London" else {},
        )

        assert composites._registered_institution(self.BRUNEL_A) == (
            "Brunel University of London",
            "https://ror.org/00dn4t376",
        )


