"""A Person's job title comes from ORCID, not from asking someone.

The ISA profile asks every Person for a `schema:jobTitle`, and one crate carried
five of those findings on authors we had already resolved. ORCID publishes the
role on the same employment record the affiliation is read from — so the answer
was on the wire and being discarded.

It is written only when ORCID states it. Plenty of researchers leave the role
blank, and there the finding stands: an invented title is worse than a missing
one, and this is a SHOULD.
"""

from __future__ import annotations

import pytest
import responses

from builder.state import CrateState
from builder.tools.composites import _ensure_person_for_orcid


def _record(role_title=None, org="Utrecht University"):
    summary = {"organization": {"name": org}}
    if role_title is not None:
        summary["role-title"] = role_title
    return {
        "person": {"name": {"given-names": {"value": "F."}, "family-name": {"value": "Wagenaars"}}},
        "activities-summary": {
            "employments": {"affiliation-group": [{"summaries": [{"employment-summary": summary}]}]}
        },
    }


class TestTheLookupReadsTheRole:
    @responses.activate
    def test_role_title_is_returned(self):
        from lookups.orcid import lookup_orcid

        lookup_orcid.cache_clear()
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0003-4766-7358/record",
            json=_record("Postdoc"),
            status=200,
        )
        assert lookup_orcid("0000-0003-4766-7358")["job_title"] == "Postdoc"

    @responses.activate
    def test_a_missing_role_is_empty_not_invented(self):
        from lookups.orcid import lookup_orcid

        lookup_orcid.cache_clear()
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0002-5392-0519/record",
            json=_record(None),
            status=200,
        )
        assert lookup_orcid("0000-0002-5392-0519")["job_title"] == ""

    @responses.activate
    def test_the_affiliation_still_comes_through(self):
        """The role is read from the same summary; it must not displace the org."""
        from lookups.orcid import lookup_orcid

        lookup_orcid.cache_clear()
        responses.add(
            responses.GET,
            "https://pub.orcid.org/v3.0/0000-0003-4766-7358/record",
            json=_record("Postdoc", org="Erasmus MC"),
            status=200,
        )
        out = lookup_orcid("0000-0003-4766-7358")
        assert out["affiliation_name"] == "Erasmus MC"
        assert out["job_title"] == "Postdoc"


class TestThePersonCarriesIt:
    def test_job_title_is_written(self):
        state = CrateState()
        person = _ensure_person_for_orcid(
            state,
            "0000-0003-4766-7358",
            {"givenName": "F.", "familyName": "Wagenaars", "job_title": "Postdoc"},
        )
        assert person.fields["jobTitle"] == "Postdoc"

    @pytest.mark.parametrize("absent", ["", "   ", None])
    def test_nothing_is_written_when_orcid_has_none(self, absent):
        state = CrateState()
        person = _ensure_person_for_orcid(
            state,
            "0000-0002-5392-0519",
            {"givenName": "M.", "familyName": "Meima", "job_title": absent},
        )
        assert "jobTitle" not in person.fields

    def test_the_field_name_survives_the_context_filter(self):
        """The Person is written under the context's term `jobTitle`, not the lookup's key."""
        from builder.tools._crate_mapping import _context_terms

        state = CrateState()
        person = _ensure_person_for_orcid(
            state, "0000-0003-4766-7358", {"familyName": "W", "job_title": "Postdoc"}
        )
        assert "jobTitle" in _context_terms()
        assert "job_title" not in person.fields


class TestTheCrateWritesItAsATerm:
    """The ISA shape says a job title MUST be a DefinedTerm, so the crate never
    carries the string ORCID returned: assembly mints one term per title (#837).
    This is the single chokepoint every Person passes through, so an ORCID role,
    a typed-in answer and a ReAct ``set_fields`` all come out the same way."""

    @staticmethod
    def _graph(state):
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.context import ISA_TOX_CONTEXT

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
        return {n["@id"]: n for n in crate.metadata.generate()["@graph"]}

    def test_a_string_title_becomes_a_defined_term_reference(self):
        state = CrateState()
        person = _ensure_person_for_orcid(
            state,
            "0000-0003-4766-7358",
            {"givenName": "F.", "familyName": "Wagenaars", "job_title": "Postdoc"},
        )
        graph = self._graph(state)
        node = graph[
            f"#{person.entity_id}" if not person.entity_id.startswith("http") else person.entity_id
        ]
        ref = node["jobTitle"]
        assert isinstance(ref, dict) and set(ref) == {"@id"}, ref
        term = graph[ref["@id"]]
        assert term["@type"] == "DefinedTerm"
        assert term["name"] == "Postdoc"
        assert "termCode" not in term, "no code exists for a free-text role; never invent one"

    def test_two_people_with_the_same_title_share_one_term(self):
        state = CrateState()
        for orcid, family in (
            ("0000-0003-4766-7358", "Wagenaars"),
            ("0000-0002-5392-0519", "Meima"),
        ):
            _ensure_person_for_orcid(
                state, orcid, {"givenName": "X", "familyName": family, "job_title": "Postdoc"}
            )
        graph = self._graph(state)
        refs = {graph[p]["jobTitle"]["@id"] for p in graph if graph[p].get("@type") == "Person"}
        assert len(refs) == 1
        assert sum(1 for n in graph.values() if n.get("@type") == "DefinedTerm") == 1

    def test_a_title_that_already_references_a_term_is_kept(self):
        """A term the drafter minted (or a reloaded crate) is wired, not re-minted."""
        from builder.state import Entity, EntityProvenance

        state = CrateState()
        state.add_entity(
            Entity(
                entity_id="dt_senior_scientist",
                type="DefinedTerm",
                fields={"name": "Senior scientist", "termCode": "ROLE:1"},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        state.add_entity(
            Entity(
                entity_id="p1",
                type="Person",
                fields={
                    "name": "Ada",
                    "givenName": "Ada",
                    "jobTitle": {"@id": "dt_senior_scientist"},
                },
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        graph = self._graph(state)
        ref = graph["#Person_p1"]["jobTitle"]
        assert ref == {"@id": "#DefinedTerm_dt_senior_scientist"}
        assert graph[ref["@id"]]["termCode"] == "ROLE:1"
        assert sum(1 for n in graph.values() if n.get("@type") == "DefinedTerm") == 1
