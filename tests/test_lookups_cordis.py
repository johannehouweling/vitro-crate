"""The CORDIS client reads an EU grant's project record, and nothing more.

The record is replayed from ``tests/fixtures/cordis_project_825161.json``, a trimmed
copy of what ``https://cordis.europa.eu/project/id/825161?format=json`` returned
(the H2020 project ATHENA). ``tests/test_lookups_live.py`` asks CORDIS itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookups import cordis
from lookups._http import NOT_FOUND, TransientLookupError

_RECORD = json.loads(
    (Path(__file__).parent / "fixtures" / "cordis_project_825161.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    cordis.fetch_cordis_project.cache_clear()
    yield
    cordis.fetch_cordis_project.cache_clear()


def _serve(monkeypatch, answer):
    asked: list[tuple[str, dict | None]] = []

    def get(url, *, params=None, **_):
        asked.append((url, params))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cordis, "http_get_json", get)
    return asked


def test_a_project_is_read_from_its_record(monkeypatch):
    asked = _serve(monkeypatch, _RECORD)
    assert cordis.fetch_cordis_project("825161") == {
        "id": "825161",
        "acronym": "ATHENA",
        "title": (
            "Assays for the identification of Thyroid Hormone axis-disrupting chemicals: "
            "Elaborating Novel Assessment strategies"
        ),
        "url": "https://cordis.europa.eu/project/id/825161",
        "grant_doi": "10.3030/825161",
        "framework_programmes": ["H2020"],
        "ec_contribution": "6565463.75",
    }
    assert asked == [("https://cordis.europa.eu/project/id/825161", {"format": "json"})]


def test_a_number_cordis_does_not_hold_is_nothing(monkeypatch):
    _serve(monkeypatch, NOT_FOUND)
    assert cordis.fetch_cordis_project("999999999") == {}


def test_a_record_of_another_project_is_nothing(monkeypatch):
    _serve(monkeypatch, {**_RECORD, "id": "123"})
    assert cordis.fetch_cordis_project("825161") == {}


@pytest.mark.parametrize("grant", ["1292.19.272", "NWA.1292.19.272", "", "  "])
def test_what_is_no_cordis_project_id_is_not_asked(monkeypatch, grant):
    asked = _serve(monkeypatch, _RECORD)
    assert cordis.fetch_cordis_project(grant) == {}
    assert asked == []


def test_an_older_project_without_a_doi_has_none(monkeypatch):
    _serve(monkeypatch, {**_RECORD, "identifiers": None, "ecMaxContribution": None})
    record = cordis.fetch_cordis_project("825161")
    assert (record["grant_doi"], record["ec_contribution"]) == ("", "")


def test_cordis_out_of_reach_is_raised_and_not_remembered(monkeypatch):
    _serve(monkeypatch, TransientLookupError("cordis.europa.eu timed out"))
    with pytest.raises(TransientLookupError):
        cordis.fetch_cordis_project("825161")
    _serve(monkeypatch, _RECORD)
    assert cordis.fetch_cordis_project("825161")["acronym"] == "ATHENA"
