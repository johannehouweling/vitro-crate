"""Findings about vocabulary a crate CITES are separated from findings about it.

An RO-Crate describes what it asserts and links what it cites. The validator
agrees — its base shapes exclude schema.org, w3.org, purl.org, bioschemas.org and
urn: from these very checks — but that exclusion list is the one a workflow crate
needs, so any other vocabulary is reported as an entity with no name, no
schema.org type and no description. Nothing can be done about those: the terms
belong to, and are versioned by, someone else.

The tempting fix is to add the missing namespaces. It is wrong twice over: it
fits whichever crate prompted it and needs an edit per ontology (the reason
00ca43b deleted exactly such a list), and it cannot express the case that decides
the question —

    https://orcid.org                      a scheme the crate cites
    https://orcid.org/0009-0000-5074-6239  an author the crate describes

Same prefix, opposite answers. So the crate is asked instead of a list.
"""

from __future__ import annotations

import pytest

from builder.tools.validation import (
    _cited_iris,
    _context_vocabulary,
    _partition_citations,
)

OBO = "http://purl.obolibrary.org/obo/PATO_0000033"
ORCID_SCHEME = "https://orcid.org"
ORCID_PERSON = "https://orcid.org/0009-0000-5074-6239"


def _doc(graph, context=None):
    return {"@context": context or ["https://w3id.org/ro/crate/1.2/context"], "@graph": graph}


class TestWhatCountsAsACitation:
    def test_a_term_referenced_only_as_vocabulary_is_a_citation(self):
        doc = _doc([{"@id": "#col", "@type": "csvw:Column", "propertyUrl": {"@id": OBO}}])
        assert OBO in _cited_iris(doc)

    def test_an_identifier_scheme_is_a_citation(self):
        """`propertyID` says which scheme an id belongs to. That is a citation."""
        doc = _doc([{"@id": "#pv", "@type": "PropertyValue", "propertyID": ORCID_SCHEME}])
        assert ORCID_SCHEME in _cited_iris(doc)

    def test_a_described_entity_is_never_a_citation(self):
        doc = _doc(
            [
                {"@id": "./", "author": {"@id": ORCID_PERSON}},
                {"@id": ORCID_PERSON, "@type": "Person", "name": "Nathalie Dierichs"},
            ]
        )
        assert ORCID_PERSON not in _cited_iris(doc)

    def test_the_scheme_and_a_person_under_it_are_told_apart(self):
        """The case no namespace list can express — same prefix, opposite answers."""
        doc = _doc(
            [
                {"@id": "./", "author": {"@id": ORCID_PERSON}},
                {"@id": ORCID_PERSON, "@type": "Person", "name": "Someone"},
                {"@id": "#pv", "@type": "PropertyValue", "propertyID": ORCID_SCHEME},
            ]
        )
        cited = _cited_iris(doc)
        assert ORCID_SCHEME in cited
        assert ORCID_PERSON not in cited

    def test_a_crate_local_id_is_never_a_citation(self):
        doc = _doc([{"@id": "./", "hasPart": {"@id": "#thing"}}])
        assert _cited_iris(doc) == set()


class TestTheSafetyProperty:
    """Nothing the crate ASSERTS may be silenced by this."""

    def test_an_undescribed_entity_referenced_by_a_real_property_stays_reported(self):
        """An author we linked but never drafted is a genuine gap, not a citation."""
        doc = _doc([{"@id": "./", "author": {"@id": "https://example.org/person/1"}}])
        assert "https://example.org/person/1" not in _cited_iris(doc)

    def test_one_asserting_reference_is_enough(self):
        """Cited in one place and asserted in another, it is still an entity."""
        target = "https://example.org/thing"
        doc = _doc(
            [
                {"@id": "#col", "propertyUrl": {"@id": target}},
                {"@id": "./", "hasPart": {"@id": target}},
            ]
        )
        assert target not in _cited_iris(doc)

    def test_a_type_only_stub_is_not_treated_as_described(self):
        """`{"@id", "@type"}` with nothing else is the shape the findings are about."""
        doc = _doc(
            [
                {"@id": "#col", "propertyUrl": {"@id": OBO}},
                {"@id": OBO, "@type": "DefinedTerm"},
            ]
        )
        assert OBO in _cited_iris(doc)


class TestContextVocabulary:
    def test_classes_and_properties_from_the_context_are_citations(self):
        """These reach the validator as types and predicates, never as {"@id"}.

        A real crate produced 47 findings asking a *property* IRI for a
        human-readable name.
        """
        ctx = {
            "KeyEvent": "https://aopwiki.org/ontology/KeyEvent",
            "has_key_event": "https://aopwiki.org/ontology/hasKeyEvent",
            "@vocab": "http://schema.org/",
        }
        doc = _doc([{"@id": "#e", "@type": "KeyEvent"}], context=[ctx])
        cited = _cited_iris(doc)
        assert "https://aopwiki.org/ontology/KeyEvent" in cited
        assert "https://aopwiki.org/ontology/hasKeyEvent" in cited

    def test_namespace_keys_are_not_terms(self):
        assert _context_vocabulary({"@context": [{"@vocab": "http://schema.org/"}]}) == set()

    def test_a_remote_context_reference_is_left_alone(self):
        assert _context_vocabulary({"@context": "https://w3id.org/ro/crate/1.2/context"}) == set()

    def test_expanded_term_definitions_are_read(self):
        ctx = {"thing": {"@id": "https://example.org/vocab/thing", "@type": "@id"}}
        assert "https://example.org/vocab/thing" in _context_vocabulary({"@context": [ctx]})


class TestPartitioning:
    def _issue(self, entity_id):
        return {"entity_id": entity_id, "severity": "recommended", "profile": "base"}

    def test_citations_are_separated_not_dropped(self):
        issues = [self._issue(OBO), self._issue("./#Study_x")]
        gaps, citations = _partition_citations(issues, {OBO})
        assert [i["entity_id"] for i in gaps] == ["./#Study_x"]
        assert [i["entity_id"] for i in citations] == [OBO]
        assert len(gaps) + len(citations) == len(issues), "a finding must never vanish"

    def test_nothing_cited_leaves_the_list_untouched(self):
        """Equality, not identity: the guarantee is that no finding is lost.

        This asserted `gaps is issues` when the function short-circuited on an
        empty cited set. It no longer can — every finding is now also checked
        against `_is_unanswerable` — and identity was never the promise.
        """
        issues = [self._issue("./#Study_x")]
        gaps, citations = _partition_citations(issues, set())
        assert gaps == issues
        assert citations == []


class TestTheToolResult:
    @pytest.fixture
    def state(self):
        from builder.state import CrateState
        from builder.tools.drafters import draft_investigation

        st = CrateState()
        draft_investigation(st, {"name": "I", "description": "D"})
        return st

    def test_the_result_carries_both(self, state):
        from builder.tools import validation as V

        V.clear_sweep_memo()
        out = V.build_and_validate(state, severity="recommended", profile="all")
        assert "issues" in out
        assert "citations" in out
        V.clear_sweep_memo()

    def test_a_memo_hit_still_carries_citations(self, state, monkeypatch):
        """A cached answer must be indistinguishable from a fresh one."""
        from builder.tools import validation as V

        V.clear_sweep_memo()
        first = V.build_and_validate(state, severity="optional", profile="all")
        second = V.build_and_validate(state, severity="optional", profile="all")
        assert second["citations"] == first["citations"]
        V.clear_sweep_memo()
