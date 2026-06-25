"""Tests for the ``resolve_publication`` composite tool (Issue #179).

``resolve_publication`` closes the gap PR #217 deliberately deferred: a plan
carries a publication *title* only (D5 — no DOI), but ISA REQUIRES a
``ScholarlyArticle`` with an identifier. The composite resolves the title to a
DOI via a Crossref title-search, gated by a strict confidence rule (D5 — never
fabricate a DOI), then builds the publication via the existing
:func:`draft_publication_with_authors` (DOI -> ScholarlyArticle + authors).

The chain is::

    title -> search_works_by_title (Crossref) -> confidence gate ->
        draft_publication_with_authors(doi=<resolved>)

Tests never hit the network: the Crossref title-search and the DOI-resolution
drafter are monkeypatched on the ``composites`` module (where
``resolve_publication`` resolves the symbols), so the chain runs entirely
offline.
"""

from __future__ import annotations

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools import composites
from builder.tools.composites import resolve_publication

pytestmark = pytest.mark.timeout(120)

_TITLE = "Adverse outcome pathway-based assessment of TPO inhibition in vitro"
_DOI = "10.1016/j.tox.2021.152898"
_DOI_URL = f"https://doi.org/{_DOI}"


def _by_type(state: CrateState, type_name: str) -> list:
    return [e for e in state.list_entities() if e.type == type_name]


def _candidate(title: str, doi: str, score: float) -> dict:
    """A single Crossref title-search candidate (the shape the helper returns)."""
    return {"title": title, "doi": doi, "score": score}


@pytest.fixture
def offline(monkeypatch):
    """Serve a confident title-search hit + a fake DOI-resolution drafter.

    Both are patched on the ``composites`` module namespace, which is where
    ``resolve_publication`` resolves the symbols.
    """
    calls: dict[str, list] = {"search": [], "draft": []}

    def fake_search(title, rows=5):  # noqa: ANN001
        calls["search"].append((title, rows))
        # Top candidate is an exact-normalized-title match with a high score.
        return [
            _candidate(_TITLE, _DOI, 92.5),
            _candidate("Some unrelated paper", "10.9999/other", 12.0),
        ]

    def fake_draft(state, doi, human_interface=None):  # noqa: ANN001
        calls["draft"].append(doi)
        # Mimic draft_publication_with_authors: ensure a Publication entity in
        # state keyed off the DOI, returning its standard result shape.
        pub = composites._ensure_publication(
            state,
            doi,
            {"identifier": _DOI_URL, "name": _TITLE, "headline": _TITLE},
        )
        return {
            "publication_id": pub.entity_id,
            "doi": _DOI_URL,
            "authors": [],
            "hitl": 0,
        }

    monkeypatch.setattr(composites, "search_works_by_title", fake_search)
    monkeypatch.setattr(
        composites, "draft_publication_with_authors", fake_draft
    )
    return calls


class TestResolvePublicationHappyPath:
    def test_confident_match_builds_publication(self, offline):
        state = CrateState()
        result = resolve_publication(state, title=_TITLE)

        assert result["ok"] is True
        # The resolved DOI is reported and a single Publication exists.
        assert _DOI in result["doi"]
        pubs = _by_type(state, "Publication")
        assert len(pubs) == 1
        assert result["entity_id"] == pubs[0].entity_id
        assert state.get_entity(result["entity_id"]) is pubs[0]

    def test_returns_title_and_score(self, offline):
        state = CrateState()
        result = resolve_publication(state, title=_TITLE)
        assert result["title"] == _TITLE
        assert result["score"] == 92.5

    def test_delegates_to_draft_publication_with_authors(self, offline):
        state = CrateState()
        resolve_publication(state, title=_TITLE)
        # The DOI-resolution drafter was called exactly once, with the resolved DOI.
        assert len(offline["draft"]) == 1
        assert _DOI in offline["draft"][0]


class TestResolvePublicationConfidenceGate:
    def test_low_score_no_match_creates_no_entity(self, monkeypatch):
        """A weak Crossref score is rejected (D5) — no entity, reported."""

        def fake_search(title, rows=5):  # noqa: ANN001
            # Exact title text but a low score: not confident.
            return [_candidate(_TITLE, _DOI, 3.0)]

        def fake_draft(state, doi, human_interface=None):  # noqa: ANN001
            raise AssertionError("draft must not be called on a low-confidence match")

        monkeypatch.setattr(composites, "search_works_by_title", fake_search)
        monkeypatch.setattr(
            composites, "draft_publication_with_authors", fake_draft
        )

        state = CrateState()
        result = resolve_publication(state, title=_TITLE)

        assert result["ok"] is False
        assert result["title"] == _TITLE
        assert "no confident" in result["reason"].lower()
        assert _by_type(state, "Publication") == []

    def test_title_mismatch_creates_no_entity(self, monkeypatch):
        """A high score but a non-matching title is rejected (D5)."""

        def fake_search(title, rows=5):  # noqa: ANN001
            # High score, but the candidate is a different paper.
            return [_candidate("A completely different study", _DOI, 99.0)]

        def fake_draft(state, doi, human_interface=None):  # noqa: ANN001
            raise AssertionError("draft must not be called on a title mismatch")

        monkeypatch.setattr(composites, "search_works_by_title", fake_search)
        monkeypatch.setattr(
            composites, "draft_publication_with_authors", fake_draft
        )

        state = CrateState()
        result = resolve_publication(state, title=_TITLE)

        assert result["ok"] is False
        assert _by_type(state, "Publication") == []

    def test_no_candidates_creates_no_entity(self, monkeypatch):
        def fake_search(title, rows=5):  # noqa: ANN001
            return []

        monkeypatch.setattr(composites, "search_works_by_title", fake_search)

        state = CrateState()
        result = resolve_publication(state, title="Nonexistent paper title")

        assert result["ok"] is False
        assert _by_type(state, "Publication") == []


class TestResolvePublicationIdempotent:
    def test_second_call_no_duplicate(self, offline):
        state = CrateState()
        first = resolve_publication(state, title=_TITLE)
        second = resolve_publication(state, title=_TITLE)
        assert len(_by_type(state, "Publication")) == 1
        assert first["entity_id"] == second["entity_id"]
        assert first["ok"] is True and second["ok"] is True


class TestResolvePublicationViaEngine:
    def test_callable_through_run_tool(self, offline):
        engine = AgentEngine()
        engine.initialize()
        result = engine.run_tool("resolve_publication", title=_TITLE)
        assert result["ok"] is True
        assert engine.state.get_entity(result["entity_id"]) is not None
