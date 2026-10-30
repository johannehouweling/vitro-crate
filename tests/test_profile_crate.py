"""The Profile Crate must pass what the RO-Crate Profile Portal actually checks (#856).

The portal's CI runs three gates (`scripts/validate_profiles.py` and
`scripts/upload_profiles.py` in eScienceLab/RO-Crate-Profile-Portal):

1. the URL answers with a JSON content type — nothing to test here, GitHub
   Pages serves `.json` as `application/json`;
2. `detect_profiles` finds a profile and `validate` in `metadata_only` mode
   reports no issues;
3. the parsed graph holds a ``prof:Profile``-typed subject, or the upload
   raises.

Gate 2 is :func:`validate_crate_dict` with ``profile="base"`` here — the same
rocrate-validator, offline. Gate 3 is asserted structurally: ``Profile`` in the
root's ``@type`` plus the context mapping that turns it into ``prof:Profile``,
which is what rdflib derives. Parsing it with rdflib instead would fetch the
remote context and make the suite network-dependent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from profiles.validator import validate_crate_dict
from scripts.gen_profile_crate import (
    CC_BY,
    DATE_PUBLISHED,
    ORCID,
    PARENT_IRI,
    PROFILE_IRI,
    VERSION,
    build,
)

REPO = Path(__file__).resolve().parents[1]
PROF_PROFILE = "http://www.w3.org/ns/dx/prof/Profile"


@pytest.fixture(scope="module")
def doc(tmp_path_factory) -> dict:
    return build(tmp_path_factory.mktemp("profile_crate"))


def _node(doc: dict, entity_id: str) -> dict:
    return next(n for n in doc["@graph"] if n["@id"] == entity_id)


class TestPortalGates:
    def test_the_descriptor_conforms_to_ro_crate_1_2(self, doc):
        """`detect_profiles` finds nothing without this, and the PR fails."""
        descriptor = _node(doc, "ro-crate-metadata.json")
        assert descriptor["conformsTo"] == {"@id": "https://w3id.org/ro/crate/1.2"}
        assert descriptor["about"] == {"@id": PROFILE_IRI}

    def test_it_validates_clean_against_the_base_profile(self, doc):
        (result,) = validate_crate_dict(doc, severity="required", profile="base")
        assert result.issues == [], [(i.entity_id, i.message) for i in result.issues]

    def test_the_root_is_a_prof_profile(self, doc):
        root = _node(doc, PROFILE_IRI)
        assert "Profile" in root["@type"]
        assert "Dataset" in root["@type"]

    def test_the_context_maps_profile_to_the_prof_class(self, doc):
        """What rdflib resolves ``Profile`` to in the portal's upload step."""
        local = REPO / "profiles" / "contexts" / "ro-crate-1.2-context.jsonld"
        terms = json.loads(local.read_text(encoding="utf-8"))["@context"]
        assert terms["Profile"] == PROF_PROFILE


class TestProfileCrateRequirements:
    """RO-Crate 1.2 §Profile Crate."""

    def test_the_root_declares_its_lineage_and_version(self, doc):
        root = _node(doc, PROFILE_IRI)
        assert root["isProfileOf"] == {"@id": PARENT_IRI}
        assert root["version"] == VERSION
        assert root["datePublished"] == DATE_PUBLISHED
        assert root["name"]
        assert root["description"]

    def test_the_spec_text_is_cc_by_and_the_root_follows_it(self, doc):
        assert _node(doc, PROFILE_IRI)["license"] == {"@id": CC_BY}

    def test_the_author_is_identified_by_orcid(self, doc):
        assert _node(doc, PROFILE_IRI)["author"] == {"@id": ORCID}
        assert _node(doc, ORCID)["@type"] == "Person"


def test_nothing_generated_is_committed():
    """The crate is generated on demand; a committed copy could go stale."""
    assert not (REPO / "profiles" / "profile_crate").exists()
    assert "dist/" in (REPO / ".gitignore").read_text(encoding="utf-8")
