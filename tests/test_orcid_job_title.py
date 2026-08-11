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
        """`_scalar_props` drops snake_case fields that are not context terms.

        `jobTitle` is camelCase so it is never dropped — but the lookup's key is
        `job_title`, and writing THAT onto the entity would have been silently
        discarded at build time.
        """
        state = CrateState()
        person = _ensure_person_for_orcid(
            state, "0000-0003-4766-7358", {"familyName": "W", "job_title": "Postdoc"}
        )
        assert "_" not in "jobTitle"
        assert "job_title" not in person.fields
