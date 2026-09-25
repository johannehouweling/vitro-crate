"""A key the model invents does not ship as a bare JSON-LD key (#737).

What keeps an undefined key out of the crate is membership in the ``@context``
the crate declares — RO-Crate 1.2 plus ISA-Tox — and nothing about the key's
shape. The rule used to be applied only to keys containing an underscore, so
every single-word invention (``bioassay``, ``endpoint``, ``replicates``,
``contact``) shipped uncompacted and failed BASE at REQUIRED: twelve of the
twenty-five REQUIRED findings across a 29-build corpus of one deposit.

``contact`` on the sole Investigation is the attribution slot
``_wire_root_attribution`` already emits as ``contactPoint`` ``{"@id"}``; a bare
ORCID string beside it, under a key no context defines, is the same fact stated
once correctly and once wrongly, and a PropertyValue beside it states it twice,
whichever source (the metadata slot or the Investigation) wired the contactPoint.
A ``contact`` the contactPoint does not state (on another entity, or one that
names no entity) is an invented key like the rest and is kept as a PropertyValue.
"""

from __future__ import annotations

import json

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import field_would_be_dropped
from builder.tools.builder import build_crate

pytestmark = pytest.mark.timeout(120)

ORCID = "https://orcid.org/0000-0002-1825-0097"
OTHER_ORCID = "https://orcid.org/0000-0001-5109-3700"
TECHNIQUE = "http://purl.obolibrary.org/obo/OBI_0000070"
INVENTED = ("bioassay", "endpoint", "replicates", "contact")


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _state() -> CrateState:
    state = CrateState()
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d", contact=ORCID))
    state.add_entity(
        _ent(
            "st1", "Study", name="St", description="d", investigation_id="inv1", contact=OTHER_ORCID
        )
    )
    state.add_entity(
        _ent(
            "as1",
            "Assay",
            name="As",
            description="d",
            study_id="st1",
            bioassay="Deiodinase",
            endpoint="T4 uptake",
            replicates=None,
            measurement_technique=TECHNIQUE,
        )
    )
    return state


@pytest.fixture(scope="module")
def doc(tmp_path_factory) -> dict:
    return _build(_state(), tmp_path_factory.mktemp("crate"))


def _kept_contacts(doc: dict, node: dict) -> list[str]:
    """The values of the PropertyValues named ``contact`` that *node* carries."""
    graph = {n["@id"]: n for n in doc["@graph"]}
    refs = node.get("additionalProperty", [])
    refs = refs if isinstance(refs, list) else [refs]
    return [graph[r["@id"]]["value"] for r in refs if graph[r["@id"]].get("name") == "contact"]


class TestTheBuiltCrate:
    def test_no_node_carries_an_invented_key(self, doc):
        leaked = {(node["@id"], key) for node in doc["@graph"] for key in node if key in INVENTED}
        assert leaked == set()

    def test_the_value_is_kept_as_a_property_value(self, doc):
        kept = [
            node
            for node in doc["@graph"]
            if node.get("@type") == "PropertyValue" and node.get("name") == "bioassay"
        ]
        assert [node["value"] for node in kept] == ["Deiodinase"]

    def test_the_contact_is_the_root_contact_point(self, doc):
        assert _root(doc).get("contactPoint") == {"@id": ORCID}
        assert _kept_contacts(doc, _root(doc)) == [], "stated once, as the contactPoint"

    def test_a_contact_the_root_does_not_read_is_kept(self, doc):
        study = next(node for node in doc["@graph"] if node.get("additionalType") == "Study")
        assert _kept_contacts(doc, study) == [OTHER_ORCID]

    @pytest.mark.parametrize("profile", ["base", "isa"])
    def test_the_profile_passes_at_required(self, doc, profile):
        from profiles.validator import validate_crate_dict

        result = validate_crate_dict(doc, profile=profile, severity="required")[0]
        assert result.passed_required, [issue.message for issue in result.issues]

    def test_a_renamed_literal_term_keeps_its_iri_a_string(self, doc):
        """ISA types ``measurementTechnique`` string-or-DefinedTerm, never a bare node."""
        assay = next(node for node in doc["@graph"] if node.get("additionalType") == "Assay")
        assert assay.get("measurementTechnique") == TECHNIQUE


def _build(state: CrateState, tmp_path) -> dict:
    result = build_crate(state, str(tmp_path))
    assert result["success"] is True, result
    return json.loads((tmp_path / "ro-crate-metadata.json").read_text(encoding="utf-8"))


def _root(doc: dict) -> dict:
    return next(node for node in doc["@graph"] if node["@id"] == "./")


def test_the_contact_the_metadata_slot_already_states_is_not_restated(tmp_path):
    state = CrateState()
    state.metadata.contact = ORCID
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d", contact=ORCID))
    doc = _build(state, tmp_path)
    assert _root(doc).get("contactPoint") == {"@id": ORCID}
    assert "contact" not in _root(doc)
    assert _kept_contacts(doc, _root(doc)) == [], "stated once, as the contactPoint"


@pytest.mark.parametrize(
    "contact", ["jane.doe@uni.nl", "Jane Doe", "0000-0002-1825-0097", [ORCID, OTHER_ORCID]]
)
def test_a_contact_that_names_no_entity_is_kept(tmp_path, contact):
    state = CrateState()
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d", contact=contact))
    doc = _build(state, tmp_path)
    assert _root(doc).get("contactPoint") is None
    assert _kept_contacts(doc, _root(doc)) == [str(contact)]


def test_a_contact_list_keeps_the_contacts_the_root_does_not_name(tmp_path):
    state = CrateState()
    state.add_entity(_ent("p1", "Person", name="Jane Doe", orcid=ORCID))
    state.add_entity(_ent("p2", "Person", name="Joe Bloggs", orcid=OTHER_ORCID))
    state.add_entity(
        _ent("inv1", "Investigation", name="Inv", description="d", contact=["p1", "p2"])
    )
    doc = _build(state, tmp_path)
    assert _root(doc).get("contactPoint") == {"@id": ORCID}
    assert _kept_contacts(doc, _root(doc)) == [str(["p1", "p2"])]


@pytest.mark.parametrize("key", ["contactPoint", "contact_point"])
def test_a_contact_point_is_a_reference_under_either_spelling(tmp_path, key):
    """An ORCID the crate describes is a node; as a string the base pass fails it."""
    from profiles.validator import validate_crate_dict

    state = CrateState()
    state.add_entity(_ent("p1", "Person", name="Jane Doe", orcid=ORCID))
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d", **{key: ORCID}))
    doc = _build(state, tmp_path)
    assert _root(doc).get("contactPoint") == {"@id": ORCID}
    result = validate_crate_dict(doc, profile="base", severity="required")[0]
    assert result.passed_required, [issue.message for issue in result.issues]


def test_each_of_two_investigations_keeps_its_contact(tmp_path):
    state = CrateState()
    for inv_id, orcid in (("inv1", ORCID), ("inv2", OTHER_ORCID)):
        state.add_entity(_ent(inv_id, "Investigation", name=inv_id, description="d", contact=orcid))
    doc = _build(state, tmp_path)
    investigations = [n for n in doc["@graph"] if n.get("additionalType") == "Investigation"]
    assert sorted(v for n in investigations for v in _kept_contacts(doc, n)) == sorted(
        (ORCID, OTHER_ORCID)
    )


class TestTheDropRuleIsContextMembership:
    def test_a_single_word_key_outside_the_context_is_dropped(self):
        assert field_would_be_dropped("bioassay") is True

    @pytest.mark.parametrize(
        "field", ["contactPoint", "license", "has_key_event", "measurement_method"]
    )
    def test_a_context_term_is_kept_whatever_its_shape(self, field):
        assert field_would_be_dropped(field) is False
