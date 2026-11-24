"""The tools that read an identifier-namespace finding say the right thing about it.

A finding that a person has no ORCID iD, or that a CAS number is malformed, can be
cleared only by an identifier from a lookup or from the user. So the fix hint tells
the agent never to guess one and never to delete the entity, and names the
namespace the shape's one message cannot; the report's instruction names what is
wrong rather than the generic "Add an identifier"; a funder named only as one is
asked for a ROR ID as a recommendation, and pointed at CORDIS; the ReAct loop hands
the person and organisation findings back to the user without calling the crate
complete, and names who lacks an identifier when the session ends; and
`verify_identifier` clears an ORCID iD that is somebody else's.
"""

from __future__ import annotations

from builder.agents.react import agent_loop
from builder.agents.react.agent_loop import format_unidentified_agents
from builder.engine import AgentEngine
from builder.state import CrateState, Entity, EntityProvenance
from builder.tools import verification
from builder.tools.remediation import describe, group_findings
from builder.tools.validation import _synthesize_fix
from profiles.identifier_namespaces import (
    FUNDER_SHOULD_HAVE_ROR,
    MALFORMED,
    ORGANIZATION_NEEDS_ROR,
    PERSON_NEEDS_ORCID,
)
from profiles.validator import RoutableIssue

PERSON = f"A Person {PERSON_NEEDS_ORCID}: its @id is https://orcid.org/ followed by the iD"
ORGANISATION = f"An Organization {ORGANIZATION_NEEDS_ROR}: its @id is https://ror.org/ followed by the ID"
FUNDER = (
    f"An Organization named only as a funder {FUNDER_SHOULD_HAVE_ROR}: its @id is "
    "https://ror.org/ followed by the ID"
)
NODE = f"An @id in a registered identifier namespace {MALFORMED}"
VALUE = f"An identifier PropertyValue's schema:value {MALFORMED}"


def _f(entity_id, message, prop=None, severity="required"):
    return {"entity_id": entity_id, "message": message, "property": prop, "severity": severity}


def _issue(message, entity_id="./#Person_ada", prop=None) -> RoutableIssue:
    return RoutableIssue(
        entity_id=entity_id,
        property=prop,
        property_value=None,
        message=message,
        severity="required",
        check_id="tox-ro-crate-1.0_20.0",
        profile="tox",
    )


class TestTheReportSaysWhatIsWrong:
    def test_a_person_without_an_orcid_is_asked_for_one(self):
        action = group_findings([_f("#a", PERSON)], labels={"#a": "Ada Lovelace"})[0]
        assert describe(action) == "Add an ORCID for Ada Lovelace."

    def test_an_organisation_without_a_ror_id_is_asked_for_one(self):
        action = group_findings([_f("#o", ORGANISATION)], labels={"#o": "A lab"})[0]
        assert describe(action) == "Add a ROR ID for A lab."

    def test_a_funder_without_one_is_asked_for_one_as_a_recommendation(self):
        finding = _f("#f", FUNDER, severity="recommended")
        action = group_findings([finding], labels={"#f": "EU Horizon 2020"})[0]
        assert describe(action) == "Add a ROR ID for EU Horizon 2020."
        assert action.tier == "RECOMMENDED"

    def test_a_malformed_id_names_its_namespace(self):
        cases = {
            "https://pubchem.ncbi.nlm.nih.gov/compound/aspirin": "PubChem compound address",
            "http://purl.obolibrary.org/obo/NCIT_16403": "NCIT term",
            "https://orcid.org/0000-0002-1825-009": "ORCID iD",
        }
        for node_id, words in cases.items():
            action = group_findings([_f(node_id, NODE)], labels={node_id: "Iopanoic acid"})[0]
            assert describe(action) == f"Correct the {words} for Iopanoic acid.", node_id

    def test_a_malformed_value_is_corrected_not_added(self):
        action = group_findings([_f("#pv", VALUE)], labels={"#pv": "CAS 96833"})[0]
        assert describe(action) == "Correct the identifier for CAS 96833."


class TestTheFixHintNeverGuesses:
    def test_a_person_hint_asks_and_verifies(self):
        hint = _synthesize_fix(_issue(PERSON))
        assert "request_input" in hint and "verify_identifier" in hint
        assert "Never guess" in hint and "do not remove" in hint

    def test_a_funder_hint_points_at_the_grant(self):
        hint = _synthesize_fix(_issue(FUNDER, "./#funder_EU_Horizon_2020"))
        assert "CORDIS" in hint and "European Commission" in hint and "Never guess" in hint

    def test_an_organisation_hint_takes_only_an_exact_name(self):
        hint = _synthesize_fix(_issue(ORGANISATION, "./#funder_eu"))
        assert "lookup_ror" in hint and "registers exactly" in hint
        assert "depositor" in hint and "Never guess" in hint

    def test_a_malformed_id_hint_names_the_namespace_and_its_lookup(self):
        cases = {
            "https://pubchem.ncbi.nlm.nih.gov/compound/aspirin": ("PubChem compound address", "resolve_compound"),
            "https://www.cellosaurus.org/HeLa": ("Cellosaurus address", "resolve_cell_line"),
            "http://purl.obolibrary.org/obo/NCIT_16403": ("NCIT term", "lookup_ontology_term"),
        }
        for node_id, (namespace, tool) in cases.items():
            hint = _synthesize_fix(_issue(NODE, node_id))
            assert namespace in hint and tool in hint, hint
            assert "Never type or correct an identifier by hand" in hint

    def test_a_hint_quotes_the_pattern_and_no_example_identifier(self):
        hint = _synthesize_fix(_issue(NODE, "https://orcid.org/0000"))
        assert r"\d{4}-\d{4}-\d{4}-\d{3}(\d|X)" in hint
        assert "0000-0003-4423-4370" not in hint  # the registry's example iD

    def test_a_malformed_value_hint_names_the_entity_it_is_written_from(self):
        graph = [
            {"@id": "https://pubchem.ncbi.nlm.nih.gov/compound/3735", "identifier": [{"@id": "#param_cas_x"}]},
            {"@id": "#param_cas_x", "@type": "PropertyValue", "name": "CAS", "value": "96833"},
        ]
        hint = _synthesize_fix(_issue(VALUE, "./#param_cas_x"), graph)
        assert "`cas` on `https://pubchem.ncbi.nlm.nih.gov/compound/3735`" in hint, hint

    def test_a_value_with_no_owner_still_says_not_to_type_it(self):
        graph = [{"@id": "#pv_doi", "@type": "PropertyValue", "name": "DOI", "value": "x"}]
        hint = _synthesize_fix(_issue(VALUE, "./#pv_doi"), graph)
        assert "DOI" in hint and "Never type" in hint


class TestTheLoopHandsTheseBack:
    def _state(self, required: list[str]) -> CrateState:
        state = CrateState()
        state.add_entity(
            Entity(entity_id="p", type="Person", fields={"name": "Ada"}, _provenance=EntityProvenance(created_by="llm"))
        )
        state.validation.base_passed = True
        state.validation.isa_passed = True
        state.validation.tox_passed = not required
        state.validation.required_issues = list(required)
        return state

    def test_only_identifier_findings_are_for_the_user(self):
        assert agent_loop._only_people_can_answer(self._state([PERSON, ORGANISATION]))

    def test_any_other_finding_keeps_the_loop_working(self):
        assert not agent_loop._only_people_can_answer(self._state([PERSON, NODE]))
        assert not agent_loop._only_people_can_answer(self._state([]))

    def test_a_failing_lower_layer_keeps_the_loop_working(self):
        state = self._state([PERSON])
        state.validation.isa_passed = False
        assert not agent_loop._only_people_can_answer(state)

    def test_the_loop_stops_without_calling_the_crate_complete(self, monkeypatch):
        monkeypatch.setattr(agent_loop, "open_items", lambda *a, **k: [])
        engine = AgentEngine(state=self._state([PERSON, PERSON, ORGANISATION]))
        assert agent_loop._agent_is_done(engine)
        assert not agent_loop._crate_is_complete(engine)

    def test_the_hint_says_who_needs_what(self):
        state = self._state([PERSON, PERSON, ORGANISATION])
        hint = agent_loop._identifiers_wanted(state)
        assert hint == (
            "2 people need an ORCID iD and 1 organisation needs a ROR ID to conform to "
            "ISA-Tox — give them here, or 'quit'."
        )
        assert agent_loop._prompt_hint("Done.", open_work=False, waiting_on=hint) == hint

    def test_no_hint_while_the_agent_has_work(self):
        assert agent_loop._identifiers_wanted(self._state([PERSON, NODE])) is None
        assert agent_loop._prompt_hint("Done.", open_work=False, waiting_on=None) == (
            agent_loop._HINT_DONE
        )

    def test_the_exit_names_who_lacks_one(self):
        state = self._state([PERSON])
        state.validation.issue_records = [
            {"profile": "tox", "severity": "required", "entity_id": "./#Person_ada", "message": PERSON}
        ]
        said = []
        agent_loop._say_who_lacks_an_identifier(AgentEngine(state=state), said.append)
        assert len(said) == 1 and "#Person_ada" in said[0]


class TestTheSessionEndNamesWho:
    RESULT = {
        "issues": [
            _f("./#Person_ada", PERSON),
            _f("./#Person_ada", PERSON),
            _f("./#CitationAuthor_Charles_Babbage", PERSON),
            _f("./#lab", ORGANISATION),
            _f("./#funder_eu_horizon_2020", FUNDER, severity="recommended"),
            _f("./#x", VALUE),
        ],
    }

    def test_it_names_each_once(self):
        line = format_unidentified_agents(self.RESULT)
        assert line is not None
        assert "2 people with no ORCID iD (#CitationAuthor_Charles_Babbage, #Person_ada)" in line
        assert "1 organisation with no ROR ID (#lab)" in line

    def test_a_funder_is_a_recommendation_and_not_listed(self):
        line = format_unidentified_agents(self.RESULT) or ""
        assert "funder" not in line

    def test_nothing_to_say_says_nothing(self):
        assert format_unidentified_agents({"issues": [_f("./#x", VALUE)]}) is None


class TestAnOrcidOfSomebodyElseIsCleared:
    def _person(self) -> tuple[CrateState, Entity]:
        state = CrateState()
        person = Entity(
            entity_id="p",
            type="Person",
            fields={"name": "Ada Lovelace", "familyName": "Lovelace", "orcid": "0000-0002-1825-009X"},
            _provenance=EntityProvenance(created_by="llm"),
        )
        state.add_entity(person)
        return state, person

    def test_another_family_name_clears_the_id(self, monkeypatch):
        record = {"found": True, "data": {"familyName": "Babbage"}}
        monkeypatch.setitem(verification._VERIFIERS, ("Person", "orcid"), (lambda q: record, "orcid"))
        state, person = self._person()
        result = verification.verify_identifier(state, "p", "orcid")
        assert result["verified"] is False and result["mismatch"] is True
        assert "orcid" not in person.fields

    def test_the_same_family_name_verifies(self, monkeypatch):
        record = {"found": True, "data": {"familyName": "Lovelace"}}
        monkeypatch.setitem(verification._VERIFIERS, ("Person", "orcid"), (lambda q: record, "orcid"))
        state, person = self._person()
        assert verification.verify_identifier(state, "p", "orcid")["verified"] is True
        assert person.fields["orcid"] == "0000-0002-1825-009X"
