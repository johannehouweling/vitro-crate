"""DOI / PubMedID PropertyValues carry their OBI propertyID as an @id node (#180).

The tox profile SHACL-duck-types a `schema:PropertyValue` named "DOI" or
"PubMedID" and REQUIRES `schema:propertyID` to *have value* a specific OBI IRI
(an `@id` node, not a string literal):

* DOI      -> http://purl.obolibrary.org/obo/OBI_0002110  (digital object identifier)
* PubMedID -> http://purl.obolibrary.org/obo/OBI_0001617  (PubMed identifier)

(IRIs verified against profiles/shapes/tox/10_doi_property_value.ttl and
11_pubmed_id_property_value.ttl — the `sh:hasValue` of each shape.)

`draft_property_value` previously emitted `propertyID` as a string literal (when
supplied) and never defaulted it, so a DOI/PubMedID PropertyValue silently failed
the tox pass. These tests assert the default + `@id`-wrap at draft time, that an
explicit IRI is still `@id`-wrapped, and (offline, with the bundled context) that
a DOI PropertyValue now passes tox validation.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState
from builder.tools.drafters import draft_property_value
from builder.tools.validation import build_and_validate

# build_and_validate runs the heavy SHACL passes; CI gates pytest at --timeout=30
# (see tests/test_tools_repair.py / test_e2e_agent_eval.py).
pytestmark = pytest.mark.timeout(120)

OBI_DOI = "http://purl.obolibrary.org/obo/OBI_0002110"
OBI_PUBMED = "http://purl.obolibrary.org/obo/OBI_0001617"


class TestDraftDefaultsPropertyID:
    def test_doi_defaults_obi_property_id_as_id_node(self):
        state = CrateState()
        entity = draft_property_value(state, "DOI", {"value": "10.1234/abcd"})
        assert entity.fields.get("propertyID") == {"@id": OBI_DOI}

    def test_pubmed_defaults_obi_property_id_as_id_node(self):
        state = CrateState()
        entity = draft_property_value(state, "PubMedID", {"value": "37123456"})
        assert entity.fields.get("propertyID") == {"@id": OBI_PUBMED}

    def test_explicit_doi_property_id_is_wrapped_as_id_node(self):
        """A model that passes the correct IRI as a bare string still gets a node."""
        state = CrateState()
        entity = draft_property_value(
            state, "DOI", {"value": "10.1234/abcd", "property_id": OBI_DOI}
        )
        assert entity.fields.get("propertyID") == {"@id": OBI_DOI}

    def test_non_identifier_name_unchanged(self):
        """A regular PropertyValue keeps its supplied propertyID string verbatim."""
        state = CrateState()
        entity = draft_property_value(
            state,
            "Passage Number",
            {"value": "5", "property_id": "http://purl.obolibrary.org/obo/EFO_0007061"},
        )
        assert entity.fields.get("propertyID") == "http://purl.obolibrary.org/obo/EFO_0007061"


class TestDoiPropertyValuePassesTox:
    def test_doi_property_value_validates(self):
        state = CrateState()
        state.metadata.title = "DOI crate"
        draft_property_value(state, "DOI", {"value": "10.1016/j.tiv.2023.105770"})
        result = build_and_validate(state, severity="required", profile="tox")
        doi_issues = [
            issue
            for issue in result.get("issues", [])
            if "propertyID" in str(issue.get("message", ""))
            or "DOI" in str(issue.get("message", ""))
        ]
        assert not doi_issues, f"DOI PropertyValue must not raise a tox Violation: {doi_issues}"
