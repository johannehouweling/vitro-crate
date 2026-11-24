"""A declared funder takes the organisation a registry confirms, or stays a recommendation.

ISA-Tox asks every Organization to be identified by a ROR ID, and a deposit names
its funders in words, often a programme. `resolve_declared_funders` answers in two
ways, and neither guesses (D5): an EU grant number CORDIS holds under the programme
the deposit names makes the grant that CORDIS project, funded by the European
Commission; otherwise the one ROR record that registers exactly the declared name.
A funder neither answers is written as declared, and the profile recommends a ROR
ID for it rather than requiring one. The build looks it up before its loop, and
running it again adds nothing.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools import composites
from builder.tools._crate_mapping import organizations_named
from builder.tools.composites import resolve_declared_funders
from builder.tools.validation import build_and_validate, clear_sweep_memo

UTRECHT = "https://ror.org/04pp8hn57"
COMMISSION = "https://ror.org/00k4n6c32"
REGISTERED = {
    "Utrecht University": {"@id": UTRECHT, "name": "Utrecht University"},
    "European Commission": {"@id": COMMISSION, "name": "European Commission"},
}

# What CORDIS returns for grant agreement 825161, as `lookups.cordis` reads it.
ATHENA = {
    "id": "825161",
    "acronym": "ATHENA",
    "title": "Assays for the identification of Thyroid Hormone axis-disrupting chemicals",
    "url": "https://cordis.europa.eu/project/id/825161",
    "grant_doi": "10.3030/825161",
    "framework_programmes": ["H2020"],
    "ec_contribution": "6565463.75",
}


@pytest.fixture(autouse=True)
def _ror(monkeypatch):
    monkeypatch.setattr(composites, "registered_organization", lambda n: REGISTERED.get(n, {}))
    monkeypatch.setattr(composites, "_ror_facts", lambda ror: {})


def _state(*funders: str, grant: str | None = None) -> CrateState:
    state = CrateState()
    state.metadata.title = "Funded study"
    state.metadata.funding = [
        {"funder": f, "identifier": grant or f"{i}"} for i, f in enumerate(funders)
    ]
    return state


class TestAFunderTakesItsRegisteredId:
    def test_a_registered_name_becomes_that_organisation(self):
        state = _state("Utrecht University", "EU Horizon 2020")
        assert resolve_declared_funders(state) == ["EU Horizon 2020"]
        (org,) = state.list_entities("Organization")
        assert org.fields["name"] == "Utrecht University"
        assert org.fields["ror"] == UTRECHT
        status = org.get_field_status("ror")
        assert status is not None and (status.status, status.source) == ("verified", "lookup")

    def test_the_crates_own_holder_of_the_id_is_the_funder(self):
        state = _state("Utrecht University")
        state.add_entity(
            Entity(
                entity_id="org_uu",
                type="Organization",
                fields={"name": "Universiteit Utrecht", "ror": UTRECHT},
                _provenance=EntityProvenance(created_by="lookup"),
            )
        )
        assert resolve_declared_funders(state) == []
        (org,) = state.list_entities("Organization")
        assert org.fields["name"] == "Universiteit Utrecht"
        assert "Utrecht University" in org.fields["alternateName"]

    def test_running_twice_changes_nothing(self):
        state = _state("Utrecht University")
        resolve_declared_funders(state)
        before = [dict(o.fields) for o in state.list_entities("Organization")]
        resolve_declared_funders(state)
        assert [dict(o.fields) for o in state.list_entities("Organization")] == before

    def test_an_alias_is_added_once(self):
        state = _state("Utrecht University")
        state.add_entity(_org("org_uu", "Universiteit Utrecht", ror=UTRECHT))
        for _ in range(3):
            resolve_declared_funders(state)
        (org,) = state.list_entities("Organization")
        assert org.fields["alternateName"] == ["Utrecht University"]

    def test_a_namesake_without_the_id_takes_it_too(self):
        # The crate holds the organisation twice: by the declared name alone, and
        # under its ROR ID by another. Both are given the ID, so the build writes one
        # node, and the funder is the identified one.
        state = _state("Utrecht University")
        state.add_entity(_org("org_name", "Utrecht University"))
        state.add_entity(_org("org_uu", "Universiteit Utrecht", ror=UTRECHT))
        resolve_declared_funders(state)
        assert {o.fields.get("ror") for o in state.list_entities("Organization")} == {UTRECHT}
        assert organizations_named(state, "Utrecht University")[0].entity_id in {"org_name", "org_uu"}
        assert resolve_declared_funders(state) == []


def _org(entity_id: str, name: str, **fields) -> Entity:
    return Entity(
        entity_id=entity_id,
        type="Organization",
        fields={"name": name, **fields},
        _provenance=EntityProvenance(created_by="lookup"),
    )


class TestTheIdentifiedOrganisationIsMeant:
    def test_one_holding_a_ror_id_comes_first(self):
        state = CrateState()
        state.add_entity(_org("a", "Utrecht University"))
        state.add_entity(_org("b", "Universiteit Utrecht", ror=UTRECHT, alternateName=["Utrecht University"]))
        assert [o.entity_id for o in organizations_named(state, "UTRECHT UNIVERSITY")] == ["b", "a"]


def _ror_findings(state: CrateState, severity: str) -> set[tuple[str, str]]:
    clear_sweep_memo()
    result = build_and_validate(state, severity=severity, profile="tox")
    clear_sweep_memo()
    return {
        (str(i.get("entity_id") or "").removeprefix("./"), str(i.get("severity")))
        for i in result["issues"]
        if "identified by a ROR ID" in str(i.get("message") or "")
    }


class TestTheCrateNamesTheFunderItFound:
    def test_an_aliased_funder_is_the_registered_node_and_a_programme_is_recommended(self):
        state = _state("Utrecht University", "EU Horizon 2020")
        state.add_entity(_org("org_uu", "Universiteit Utrecht", ror=UTRECHT))
        resolve_declared_funders(state)
        assert _ror_findings(state, "required") == set()
        assert _ror_findings(state, "recommended") == {("#funder_EU_Horizon_2020", "recommended")}

    def test_a_funder_that_is_also_an_affiliation_is_still_required(self):
        state = _state("A lab")
        lab = _org("org_lab", "A lab")
        state.add_entity(lab)
        state.add_entity(
            Entity(
                entity_id="p",
                type="Person",
                fields={"name": "Ada Lovelace", "orcid": "0000-0002-1825-0097", "affiliation": "org_lab"},
                _provenance=EntityProvenance(created_by="lookup"),
            )
        )
        assert ("#Organization_org_lab", "required") in _ror_findings(state, "required")


class TestAnEuGrantIsTheCordisProject:
    @pytest.fixture(autouse=True)
    def _cordis(self, monkeypatch):
        asked = []

        def fetch(grant_id):
            asked.append(grant_id)
            return dict(ATHENA) if grant_id == "825161" else {}

        monkeypatch.setattr(composites, "fetch_cordis_project", fetch)
        return asked

    def test_the_grant_is_recorded_and_the_commission_funds_it(self):
        state = _state("EU Horizon 2020", grant="825161")
        assert resolve_declared_funders(state) == []
        assert state.metadata.grants["825161"] == {
            "acronym": "ATHENA",
            "title": ATHENA["title"],
            "url": ATHENA["url"],
            "doi": "10.3030/825161",
            "programme": "H2020",
            "funder_ror": COMMISSION,
        }
        (org,) = state.list_entities("Organization")
        assert (org.fields["name"], org.fields["ror"]) == ("European Commission", COMMISSION)

    def test_the_crate_writes_the_grant_at_its_doi(self):
        from builder.tools.builder import assemble_crate

        state = _state("EU Horizon 2020", grant="825161")
        resolve_declared_funders(state)
        crate = assemble_crate(state, materialize_payload=False, include_all_scanned=False)
        nodes = {n["@id"]: n for n in crate.metadata.generate()["@graph"]}
        assert nodes["./"]["funder"] == {"@id": COMMISSION}
        assert nodes["./"]["funding"] == {"@id": "https://doi.org/10.3030/825161"}
        grant = nodes["https://doi.org/10.3030/825161"]
        assert grant["funder"] == {"@id": COMMISSION}
        assert (grant["identifier"], grant["alternateName"]) == ("825161", "ATHENA")
        assert grant["url"] == "https://cordis.europa.eu/project/id/825161"
        assert not any("Horizon" in str(n.get("name")) for n in nodes.values() if n.get("@type") == "Organization")
        assert _ror_findings(state, "recommended") == set()

    @pytest.mark.parametrize(
        ("declared", "confirmed"),
        [
            ("EU Horizon 2020", True),
            ("Horizon 2020", True),
            ("H2020", True),
            ("European Commission", True),
            ("the European Union (Horizon-2020)", True),
            ("Horizon Europe", False),
            ("Dutch Research Council", False),
            ("Horizon", False),
        ],
    )
    def test_the_declared_funder_must_name_the_programme(self, declared, confirmed):
        assert bool(composites.cordis_grant(declared, "825161")) is confirmed

    def test_another_programme_is_another_grant(self):
        state = _state("Dutch Research Agenda (NWA)", grant="825161")
        resolve_declared_funders(state)
        assert state.metadata.grants == {}

    def test_a_number_cordis_does_not_hold_is_no_grant(self, _cordis):
        state = _state("EU Horizon 2020", grant="1292.19.272")
        assert resolve_declared_funders(state) == ["EU Horizon 2020"]
        assert state.metadata.grants == {}

    def test_cordis_out_of_reach_is_no_answer(self, monkeypatch):
        from lookups._http import TransientLookupError

        def down(grant_id):
            raise TransientLookupError("cordis.europa.eu timed out")

        monkeypatch.setattr(composites, "fetch_cordis_project", down)
        state = _state("EU Horizon 2020", grant="825161")
        assert resolve_declared_funders(state) == ["EU Horizon 2020"]
        assert state.metadata.grants == {}

    def test_running_again_asks_nothing(self, _cordis):
        state = _state("EU Horizon 2020", grant="825161")
        resolve_declared_funders(state)
        resolve_declared_funders(state)
        assert _cordis == ["825161"]
        assert len(state.list_entities("Organization")) == 1

    def test_the_grant_survives_a_saved_session(self):
        state = _state("EU Horizon 2020", grant="825161")
        resolve_declared_funders(state)
        restored = CrateState.from_dict(state.to_dict())
        assert restored.metadata.grants == state.metadata.grants


class TestTheBuildLooksItUp:
    def test_the_build_looks_up_the_funders_before_its_loop(self, monkeypatch):
        from builder.agents import build
        from builder.agents.react import agent_loop
        from builder.engine import AgentEngine

        seen = []
        monkeypatch.setattr(build, "get_provider", lambda: "openai")
        monkeypatch.setattr(build, "read_deposit_structure", lambda *a, **k: None)
        monkeypatch.setattr(composites, "look_up_declared_funders", lambda state: seen.append("funders"))
        monkeypatch.setattr(agent_loop, "run_interactive_agent", lambda engine, **k: seen.append("loop") or {})
        build.run_build(AgentEngine(state=_state("Utrecht University")), interactive=False)
        assert seen == ["funders", "loop"]

    def test_without_a_provider_nothing_is_looked_up(self, monkeypatch):
        from builder.agents import build
        from builder.agents.react import agent_loop
        from builder.engine import AgentEngine

        seen = []
        monkeypatch.setattr(build, "get_provider", lambda: None)
        monkeypatch.setattr(build, "read_deposit_structure", lambda *a, **k: None)
        monkeypatch.setattr(composites, "look_up_declared_funders", lambda state: seen.append("funders"))
        monkeypatch.setattr(agent_loop, "run_interactive_agent", lambda engine, **k: seen.append("loop") or {})
        build.run_build(AgentEngine(state=_state("Utrecht University")), interactive=False)
        assert seen == ["loop"]

    def test_a_failed_lookup_costs_only_the_ids(self, monkeypatch):
        def broken(state):
            raise RuntimeError("ROR is down")

        monkeypatch.setattr(composites, "resolve_declared_funders", broken)
        composites.look_up_declared_funders(_state("Utrecht University"))  # does not raise
