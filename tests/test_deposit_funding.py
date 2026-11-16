"""The funding a deposit declares is read, and the crate states it.

A BioStudies descriptor names its funder and its grant number in a section of
their own, beside the authors:

    {"type": "Funding", "attributes": [{"name": "Agency", "value": "EU Horizon 2020"},
                                       {"name": "Grant_id", "value": "825161"}]}

Nothing read it, so a crate named a funder only when a model drafted one, and no
crate carried a grant number at all. The funder is the root's ``funder``, an
Organization. A grant number is no property of an organization, so it is a
``Grant`` the root's ``funding`` references, and the grant names its funder.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from builder.engine import AgentEngine, _read_declared_funding
from builder.state import CrateState
from builder.tools.builder import assemble_crate
from builder.tools.drafters import draft_organization
from builder.tools.file_readers import extract_deposit_funding

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NWA = {"funder": "Dutch Research Agenda (NWA)", "identifier": "1292.19.272"}


def _descriptor(*sections: dict[str, Any]) -> str:
    study = {"type": "Study", "subsections": list(sections)}
    return json.dumps({"accno": "S-TEST1", "type": "submission", "section": study})


def _funding(**stated: str) -> dict[str, Any]:
    return {"type": "Funding", "attributes": [{"name": k, "value": v} for k, v in stated.items()]}


class TestExtractDepositFunding:
    def test_reads_the_section_the_descriptor_declares(self) -> None:
        text = _descriptor(_funding(Agency="EU Horizon 2020", Grant_id="825161"))
        assert extract_deposit_funding(text) == [
            {"funder": "EU Horizon 2020", "identifier": "825161"}
        ]

    def test_matches_the_names_case_insensitively(self) -> None:
        section = {
            "type": "funding",
            "attributes": [
                {"name": "agency", "value": "NWO"},
                {"name": "grant_id", "value": "1292.19.272"},
            ],
        }
        assert extract_deposit_funding(_descriptor(section)) == [
            {"funder": "NWO", "identifier": "1292.19.272"}
        ]

    def test_reads_the_field_convention_too(self) -> None:
        """DataCite states the same two facts as fields of a funding reference."""
        record = {
            "fundingReferences": [
                {"funderName": "European Commission", "awardNumber": "825161"},
                {"funderName": "Dutch Research Council", "awardTitle": "A project"},
            ]
        }
        assert extract_deposit_funding(json.dumps(record)) == [
            {"funder": "European Commission", "identifier": "825161"},
            {"funder": "Dutch Research Council"},
        ]

    def test_a_funder_with_no_grant_number_is_still_a_funder(self) -> None:
        unnumbered = [{"name": "Agency", "value": "NWO"}, {"name": "Grant_id"}]
        text = _descriptor({"type": "Funding", "attributes": unnumbered})
        assert extract_deposit_funding(text) == [{"funder": "NWO"}]

    def test_every_statement_is_kept_and_a_repeat_is_stated_once(self) -> None:
        first = _funding(Agency="NWO", Grant_id="1")
        second = _funding(Agency="ZonMw", Grant_id="2")
        assert extract_deposit_funding(_descriptor(first, second, first)) == [
            {"funder": "NWO", "identifier": "1"},
            {"funder": "ZonMw", "identifier": "2"},
        ]

    def test_a_document_declaring_no_funding_yields_nothing(self) -> None:
        assert extract_deposit_funding(_descriptor({"type": "Funding", "attributes": []})) == []
        assert extract_deposit_funding(_descriptor({"type": "author", "attributes": []})) == []
        assert extract_deposit_funding("not json at all") == []

    def test_prose_is_not_a_declaration(self) -> None:
        """A README line is a sentence about funding, not a named funder and grant."""
        assert extract_deposit_funding("- Funding: NWO, grant 1292.19.272") == []
        assert extract_deposit_funding("Funding: this work was funded by NWO") == []

    def test_every_deposit_on_hand_declares_its_funding(self) -> None:
        found = {}
        for name in ("svhps21_real_input", "svhps22_real_input", "svhps26_real_input"):
            for path in (FIXTURES / name).glob("*.json"):
                if funding := extract_deposit_funding(path.read_text()):
                    found[name] = funding
        horizon = [{"funder": "EU Horizon 2020", "identifier": "825161"}]
        assert found == {
            "svhps21_real_input": horizon,
            "svhps22_real_input": [NWA],
            "svhps26_real_input": horizon,
        }


def _initialised(tmp_path: Path) -> AgentEngine:
    deposit = tmp_path / "deposit"
    shutil.copytree(FIXTURES / "svhps22_real_input", deposit)
    engine = AgentEngine()
    engine.initialize(str(deposit))
    return engine


def _graph(state: CrateState) -> list[dict[str, Any]]:
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _ids(node: dict[str, Any], key: str) -> list[str]:
    value = node.get(key) or []
    return [ref["@id"] for ref in (value if isinstance(value, list) else [value])]


def _typed(graph: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [n for n in graph if n.get("@type") == kind]


class TestTheFundingReachesTheCrate:
    def test_it_is_read_at_initialize_beside_the_doi(self, tmp_path: Path) -> None:
        state = _initialised(tmp_path).state
        assert state.metadata.funding == [NWA]
        assert state.metadata.doi, "the DOI reader still runs beside it"

    def test_the_root_names_its_funder_and_its_grant(self, tmp_path: Path) -> None:
        graph = _graph(_initialised(tmp_path).state)
        root = next(n for n in graph if n.get("@id") == "./")
        (funder,) = (n for n in graph if n.get("@id") in _ids(root, "funder"))
        (grant,) = (n for n in graph if n.get("@id") in _ids(root, "funding"))
        assert (funder["@type"], funder["name"]) == ("Organization", NWA["funder"])
        assert (grant["@type"], grant["identifier"]) == ("Grant", NWA["identifier"])
        assert grant["name"] == "Dutch Research Agenda (NWA) grant 1292.19.272"
        assert _ids(grant, "funder") == [funder["@id"]]

    def test_a_funder_the_crate_already_describes_is_not_written_twice(self) -> None:
        state = CrateState()
        state.metadata.funding = [{"funder": "Dutch Research Council", "identifier": "NWA 1"}]
        draft_organization(state, "dutch research council", {"ror": "https://ror.org/04jsz6e67"})
        graph = _graph(state)
        root = next(n for n in graph if n.get("@id") == "./")
        assert _ids(root, "funder") == ["https://ror.org/04jsz6e67"]
        assert len(_typed(graph, "Organization")) == 1
        (grant,) = _typed(graph, "Grant")
        assert _ids(grant, "funder") == ["https://ror.org/04jsz6e67"]

    def test_a_funder_with_no_grant_number_gets_no_grant(self) -> None:
        state = CrateState()
        state.metadata.funding = [{"funder": "NWO"}]
        graph = _graph(state)
        root = next(n for n in graph if n.get("@id") == "./")
        assert [n["name"] for n in graph if n.get("@id") in _ids(root, "funder")] == ["NWO"]
        assert "funding" not in root
        assert _typed(graph, "Grant") == []

    def test_a_grant_number_with_no_funder_names_none(self) -> None:
        state = CrateState()
        state.metadata.funding = [{"identifier": "825161"}]
        graph = _graph(state)
        root = next(n for n in graph if n.get("@id") == "./")
        (grant,) = _typed(graph, "Grant")
        assert _ids(root, "funding") == [grant["@id"]]
        assert (grant["name"], grant["identifier"]) == ("Grant 825161", "825161")
        assert "funder" not in grant and "funder" not in root

    def test_two_grants_of_one_funder_share_the_funder(self) -> None:
        state = CrateState()
        state.metadata.funding = [
            {"funder": "NWO", "identifier": "1"},
            {"funder": "NWO", "identifier": "2"},
        ]
        graph = _graph(state)
        root = next(n for n in graph if n.get("@id") == "./")
        assert len(_ids(root, "funder")) == 1
        assert sorted(g["identifier"] for g in _typed(graph, "Grant")) == ["1", "2"]

    def test_the_checklist_credits_the_funding(self, tmp_path: Path) -> None:
        from builder.tools.mit_assessment import slot_matcher

        state = _initialised(tmp_path).state
        assert slot_matcher(state, graph=_graph(state))("Investigation", "funder")
        assert not slot_matcher(CrateState(), graph=_graph(CrateState()))("Investigation", "funder")

    def test_the_entity_graph_draws_the_grant_from_the_root(self) -> None:
        """A predicate the graph model does not know draws no edge, and the grant would
        hang off its funder alone."""
        from builder.writers.provenance_dag import build_crate_graph, relation_terms

        state = CrateState()
        state.metadata.funding = [NWA]
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        edges = build_crate_graph(crate.metadata.generate(), all_edges=True)["edges"]
        (grant,) = [e for e in edges if e["label"] == "funding"]
        assert (grant["src"], grant["dst"]) == ("./", "#grant_1292.19.272")
        assert relation_terms()["funding"] == "schema:funding"

    def test_no_profile_requires_more_of_it(self, tmp_path: Path) -> None:
        """What is written is complete as far as any MUST goes; the SHOULDs name what a
        funder known only by its name still lacks."""
        from builder.tools.validation import build_and_validate

        state = CrateState()
        state.metadata.funding = [NWA]
        found = build_and_validate(state, severity="recommended", profile="base")["issues"]
        about = [i for i in found if re.search("funder|grant", str(i["entity_id"]))]
        assert about, "a funder with no ROR id is reported"
        assert {i["severity"] for i in about} == {"recommended"}

    def test_funding_already_set_is_left_alone(self, tmp_path: Path) -> None:
        engine = _initialised(tmp_path)
        engine.state.metadata.funding = [{"funder": "Mine"}]
        _read_declared_funding(engine)
        assert engine.state.metadata.funding == [{"funder": "Mine"}]

    def test_it_survives_a_session_round_trip(self, tmp_path: Path) -> None:
        state = _initialised(tmp_path).state
        assert CrateState.from_json(state.to_json()).metadata.funding == [NWA]

    def test_a_session_saved_before_the_field_existed_still_loads(self) -> None:
        saved = json.loads(CrateState().to_json())
        saved["metadata"].pop("funding", None)
        assert CrateState.from_json(json.dumps(saved)).metadata.funding == []
