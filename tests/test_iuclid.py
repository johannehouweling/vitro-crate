"""Unit tests for the offline IUCLID / OHT 201 vocabulary resolver.

Unlike the HTTP lookup clients, ``lookups/iuclid.py`` resolves values against a
locally committed catalogue (``profiles/mit-data/oht201_value_sets.json``); it
performs no network I/O. These tests cover the catalogue path, the loader, the
exact/fuzzy/open/uncoded matching branches of ``resolve``, and entity emission
via ``iuclid_term``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rocrate.rocrate import ROCrate

from lookups import iuclid

# A small, self-contained phrase-group catalogue used to drive the resolver
# without depending on the (generated) committed value-set file.
_SAMPLE_TERMS = [
    {"code": "C1", "text": "Liver"},
    {"code": "C2", "text": "Kidney"},
    {"code": "OPEN", "text": "Other (specify)", "open": True},
]


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    """Reset the lru-cached catalogue between tests."""
    iuclid._catalog.cache_clear()
    yield
    iuclid._catalog.cache_clear()


# ---------------------------------------------------------------------------
# Catalogue path & loader
# ---------------------------------------------------------------------------


class TestCatalogPath:
    """The catalogue path must point at the committed file inside the repo."""

    def test_catalog_path_resolves_under_repo_root(self):
        """_CATALOG sits under the repo root, not an ancestor of the repo."""
        repo_root = Path(iuclid.__file__).resolve().parents[1]
        assert iuclid._CATALOG == (repo_root / "profiles" / "mit-data" / "oht201_value_sets.json")

    def test_catalog_loads_committed_file(self, tmp_path, monkeypatch):
        """_catalog() reads and parses the JSON catalogue at _CATALOG."""
        catalog_file = tmp_path / "oht201_value_sets.json"
        catalog_file.write_text(
            json.dumps({"phrase_groups": {"PG1": _SAMPLE_TERMS}, "bindings": {}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(iuclid, "_CATALOG", catalog_file)
        iuclid._catalog.cache_clear()

        assert iuclid.phrase_group_values("PG1") == _SAMPLE_TERMS

    def test_catalog_missing_file_degrades_to_empty(self, tmp_path, monkeypatch):
        """A missing catalogue file yields an empty value set, not an error."""
        monkeypatch.setattr(iuclid, "_CATALOG", tmp_path / "does-not-exist.json")
        iuclid._catalog.cache_clear()

        assert iuclid.phrase_group_values("PG1") == []


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


class TestResolve:
    """Matching precedence: exact -> fuzzy -> open term -> uncoded."""

    @pytest.fixture(autouse=True)
    def _patch_terms(self, monkeypatch):
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: list(_SAMPLE_TERMS))

    def test_unknown_phrase_group_returns_none(self, monkeypatch):
        """An unknown (empty) phrase group resolves to None."""
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: [])
        assert iuclid.resolve("Liver", "PG1") is None

    def test_exact_match_case_insensitive(self):
        """An exact, case-insensitive text match returns the coded term."""
        result = iuclid.resolve("liver", "PG1")
        assert result == {"code": "C1", "text": "Liver", "freetext": False}

    def test_fuzzy_match_above_threshold(self):
        """A close misspelling resolves to the nearest coded term."""
        result = iuclid.resolve("Kidney", "PG1")
        assert result == {"code": "C2", "text": "Kidney", "freetext": False}

    def test_no_match_uses_open_term_with_raw_value(self):
        """An unmatched value falls back to the group's open term, freetext."""
        result = iuclid.resolve("Spleen", "PG1")
        assert result == {"code": "OPEN", "text": "Spleen", "freetext": True}

    def test_no_match_no_open_term_returns_uncoded(self, monkeypatch):
        """Without an open term, an unmatched value is returned uncoded."""
        coded_only = [{"code": "C1", "text": "Liver"}]
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: coded_only)
        result = iuclid.resolve("Spleen", "PG1")
        assert result == {"code": "", "text": "Spleen", "freetext": True}


# ---------------------------------------------------------------------------
# iuclid_term() entity emission
# ---------------------------------------------------------------------------


class TestIuclidTerm:
    """iuclid_term emits a schema:DefinedTerm or None for unknown groups."""

    def test_unknown_group_returns_none(self, monkeypatch):
        """No DefinedTerm is emitted when the phrase group is unknown."""
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: [])
        crate = ROCrate()
        assert iuclid.iuclid_term(crate, "Liver", "PG1") is None

    def test_coded_match_emits_defined_term_with_termcode(self, monkeypatch):
        """A coded match emits a DefinedTerm carrying termCode + termset link."""
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: list(_SAMPLE_TERMS))
        crate = ROCrate()
        term = iuclid.iuclid_term(crate, "Liver", "PG1")

        assert term is not None
        assert term["@type"] == "DefinedTerm"
        assert term["name"] == "Liver"
        assert term["termCode"] == "C1"
        assert term.id.endswith("/PG1/C1")
        assert term["inDefinedTermSet"]["@type"] == "DefinedTermSet"

    def test_open_term_carries_open_code_and_slugged_id(self, monkeypatch):
        """An open-term match keeps the open code but slugs the @id by value."""
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: list(_SAMPLE_TERMS))
        crate = ROCrate()
        term = iuclid.iuclid_term(crate, "Spleen", "PG1")

        assert term is not None
        assert term["name"] == "Spleen"
        assert term["termCode"] == "OPEN"
        assert term.id.endswith("/PG1/spleen")

    def test_uncoded_match_omits_termcode_and_slugs_id(self, monkeypatch):
        """Without an open term, the freetext term has no termCode, slugged @id."""
        coded_only = [{"code": "C1", "text": "Liver"}]
        monkeypatch.setattr(iuclid, "phrase_group_values", lambda pg: coded_only)
        crate = ROCrate()
        term = iuclid.iuclid_term(crate, "Spleen", "PG1")

        assert term is not None
        assert term["name"] == "Spleen"
        assert "termCode" not in term or term.get("termCode") in (None, "")
        assert term.id.endswith("/PG1/spleen")
