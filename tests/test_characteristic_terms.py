"""A stated organ, tissue or cell type carries its ontology term when one is called that.

A record states `Organ: Brain` with the UBERON term for brain beside it. A crate
stated the word alone, so a record written from it lost the term. The word the
documents use stays the value. A lookup adds the term only when the ontology has
exactly one term of its own whose label is that word: UBERON for an organ or a
tissue, CL for a cell type. Any other outcome leaves the word alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import responses
from requests.exceptions import ConnectionError

import builder.tools.composites as composites
from builder.state import CrateState, Entity
from builder.tools._crate_mapping import (
    _CELL_LINE_CHARACTERISTICS,
    CHARACTERISTIC_ONTOLOGIES,
    TERM_FIELDS,
    term_field,
)
from builder.tools.builder import assemble_crate
from builder.tools.composites import resolve_cell_line, resolve_characteristic_terms
from builder.tools.drafters import draft_cell_line_sample, draft_study
from builder.tools.field_kinds import drafter_visible_fields, is_identifier_field
from builder.tools.hitl import HumanResponse, InputResponse
from builder.tools.management import set_fields
from lookups._http import TransientLookupError, reset_circuit_breaker, reset_host_throttle
from lookups.bao import lookup_exact_term

FIXTURES = Path(__file__).parent / "fixtures"
OLS = "https://www.ebi.ac.uk/ols4/api/search"
OBO = "http://purl.obolibrary.org/obo/"
KIDNEY = OBO + "UBERON_0002113"
BRAIN = {
    "@id": OBO + "UBERON_0000955",
    "@type": "DefinedTerm",
    "name": "brain",
    "termCode": "UBERON:0000955",
}
KIDNEY_CELL = {
    "@id": OBO + "CL_0002518",
    "@type": "DefinedTerm",
    "name": "kidney epithelial cell",
    "termCode": "CL:0002518",
}


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"ols_exact_{name}.json").read_text(encoding="utf-8"))


def _answering(*docs: dict[str, Any]) -> dict[str, Any]:
    return {"response": {"numFound": len(docs), "docs": list(docs)}}


@pytest.fixture(autouse=True)
def _fresh_lookup():
    lookup_exact_term.cache_clear()
    reset_host_throttle()
    reset_circuit_breaker()
    yield
    lookup_exact_term.cache_clear()
    reset_circuit_breaker()


class TestLookupExactTerm:
    @responses.activate
    def test_the_term_called_exactly_that_is_returned(self) -> None:
        responses.add(responses.GET, OLS, json=_recorded("uberon_kidney"))
        assert lookup_exact_term("Kidney ", "uberon") == {
            "@id": KIDNEY,
            "@type": "DefinedTerm",
            "name": "kidney",
            "termCode": "UBERON:0002113",
        }

    @responses.activate
    def test_the_service_is_asked_for_an_exact_label_of_the_ontology_s_own_terms(self) -> None:
        responses.add(responses.GET, OLS, json=_recorded("cl_kidney_epithelial_cell"))
        assert lookup_exact_term("kidney epithelial cell", "cl")["termCode"] == "CL:0002518"
        query = urlsplit(str(responses.calls[0].request.url)).query
        asked = {key: values[0] for key, values in parse_qs(query).items()}
        assert (asked["q"], asked["ontology"]) == ("kidney epithelial cell", "cl")
        assert (asked["exact"], asked["queryFields"]) == ("true", "label")
        assert (asked["local"], asked["obsoletes"]) == ("true", "false")

    @responses.activate
    def test_a_word_no_term_is_called_finds_nothing(self) -> None:
        responses.add(responses.GET, OLS, json=_recorded("uberon_none"))
        assert lookup_exact_term("Epithelial ovary cell", "uberon") == {}

    @responses.activate
    def test_a_longer_label_is_not_the_word(self) -> None:
        (kidney,) = _recorded("uberon_kidney")["response"]["docs"]
        responses.add(responses.GET, OLS, json=_answering({**kidney, "label": "kidney capillary"}))
        assert lookup_exact_term("kidney", "uberon") == {}

    @responses.activate
    def test_a_term_another_ontology_owns_is_not_this_ontology_s(self) -> None:
        (cell,) = _recorded("cl_kidney_epithelial_cell")["response"]["docs"]
        responses.add(responses.GET, OLS, json=_answering(cell))
        assert lookup_exact_term("kidney epithelial cell", "uberon") == {}

    @responses.activate
    def test_two_terms_under_one_label_name_neither(self) -> None:
        (kidney,) = _recorded("uberon_kidney")["response"]["docs"]
        other = {**kidney, "iri": KIDNEY[:-1] + "4", "short_form": "UBERON_0002114"}
        responses.add(responses.GET, OLS, json=_answering(kidney, other))
        assert lookup_exact_term("kidney", "uberon") == {}

    @responses.activate
    def test_an_unreachable_service_is_not_an_answer(self) -> None:
        responses.add(responses.GET, OLS, body=ConnectionError("no route"))
        with pytest.raises(TransientLookupError):
            lookup_exact_term("kidney", "uberon")

    def test_nothing_is_asked_about_nothing(self) -> None:
        assert lookup_exact_term("  ", "uberon") == {}


def _known(
    monkeypatch: pytest.MonkeyPatch,
    terms: dict[tuple[str, str], dict[str, Any]],
    *,
    down: bool = False,
) -> list[tuple[str, str]]:
    """Answer the composite from *terms*, keyed by (word, ontology); return what it asked."""
    asked: list[tuple[str, str]] = []

    def answer(label: str, ontology: str) -> dict[str, Any]:
        asked.append((label, ontology))
        if down:
            return {"found": False, "data": {}, "error": "unreachable", "transient": True}
        if hit := terms.get((label.casefold(), ontology)):
            return {"found": True, "data": dict(hit), "error": None}
        return {"found": False, "data": {}, "error": "no such term", "transient": False}

    monkeypatch.setattr(composites, "lookup_exact_term", answer)
    return asked


def _source(state: CrateState, name: str = "donor cells", **stated: Any) -> Entity:
    return draft_cell_line_sample(state, name, {"source_kind": "primary cells", **stated})


def _stating(field: str, value: str, *, entity: Entity) -> dict[str, str]:
    return {"entity_id": entity.entity_id, "field": field, "value": value}


class TestResolveCharacteristicTerms:
    def test_a_stated_word_gets_the_term_called_that(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        cells = _source(state, organ="Brain")

        outcome = resolve_characteristic_terms(state)

        assert asked == [("Brain", "uberon")]
        assert cells.fields["organ"] == "Brain", "the word the documents use stays the value"
        assert cells.fields["organ_term"] == BRAIN
        status = cells.get_field_status("organ_term")
        assert status is not None and (status.status, status.source) == ("verified", "lookup")
        assert "ols" in cells._provenance.lookups_used
        assert outcome == {
            "found": [{**_stating("organ", "Brain", entity=cells), "term": BRAIN["@id"]}],
            "not_found": [],
            "unavailable": [],
        }

    def test_each_characteristic_is_asked_of_its_own_ontology(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {})
        state = CrateState()
        _source(state, organ="Kidney", tissue="cortex", cell_type="epithelial cell")
        resolve_characteristic_terms(state)
        assert sorted(asked) == [
            ("Kidney", "uberon"),
            ("cortex", "uberon"),
            ("epithelial cell", "cl"),
        ]

    def test_a_word_no_term_is_called_stays_alone(self, monkeypatch) -> None:
        _known(monkeypatch, {})
        state = CrateState()
        cells = _source(state, cell_type="Epithelial ovary cell")
        outcome = resolve_characteristic_terms(state)
        assert cells.fields["cell_type"] == "Epithelial ovary cell"
        assert "cell_type_term" not in cells.fields
        assert outcome["not_found"] == [
            _stating("cell_type", "Epithelial ovary cell", entity=cells)
        ]

    def test_an_outage_is_not_an_answer(self, monkeypatch) -> None:
        _known(monkeypatch, {("brain", "uberon"): BRAIN}, down=True)
        state = CrateState()
        cells = _source(state, organ="Brain")
        outcome = resolve_characteristic_terms(state)
        assert "organ_term" not in cells.fields
        assert outcome["unavailable"] == [_stating("organ", "Brain", entity=cells)]
        assert outcome["not_found"] == []

    def test_a_confirmed_term_is_not_asked_about_again(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        cells = _source(state, organ="Brain")
        resolve_characteristic_terms(state)
        again = resolve_characteristic_terms(state)
        assert asked == [("Brain", "uberon")]
        assert cells.fields["organ_term"] == BRAIN
        assert again["found"] == [
            {**_stating("organ", "Brain", entity=cells), "term": BRAIN["@id"]}
        ]

    def test_a_term_for_a_word_no_longer_stated_goes(self, monkeypatch) -> None:
        _known(monkeypatch, {("brain", "uberon"): BRAIN}, down=False)
        state = CrateState()
        cells = _source(state, organ="Brain")
        resolve_characteristic_terms(state)
        set_fields(state, cells.entity_id, {"organ": "Liver lobe 3"})
        _known(monkeypatch, {}, down=True)
        resolve_characteristic_terms(state)
        assert "organ_term" not in cells.fields, "brain is not the term of another word"

    def test_a_term_nobody_looked_up_is_not_kept(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {})
        state = CrateState()
        cells = _source(state, organ="Brain", organ_term=dict(BRAIN))
        resolve_characteristic_terms(state)
        assert asked == [("Brain", "uberon")], "a term in the state is no proof it was looked up"
        assert "organ_term" not in cells.fields

    def test_a_term_without_its_word_goes(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {})
        state = CrateState()
        cells = _source(state, organ_term=dict(BRAIN))
        resolve_characteristic_terms(state)
        assert asked == []
        assert "organ_term" not in cells.fields

    def test_a_word_is_asked_about_once_for_all_who_state_it(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        first = _source(state, "donor A cells", organ="Brain")
        second = _source(state, "donor B cells", organ=" brain ")
        resolve_characteristic_terms(state)
        assert asked == [("Brain", "uberon")]
        assert first.fields["organ_term"] == second.fields["organ_term"] == BRAIN

    def test_one_source_can_be_named(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        first = _source(state, "donor A cells", organ="Brain")
        second = _source(state, "donor B cells", tissue="cortex")
        resolve_characteristic_terms(state, entity_id=first.entity_id)
        assert asked == [("Brain", "uberon")]
        assert "tissue_term" not in second.fields

    @pytest.mark.parametrize("unnamed", [None, ""])
    def test_no_name_means_every_source(self, monkeypatch, unnamed: str | None) -> None:
        """A model that fills an optional argument with nothing still asks about all of them."""
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        _source(state, "donor A cells", organ="Brain")
        _source(state, "donor B cells", tissue="cortex")
        resolve_characteristic_terms(state, entity_id=unnamed)
        assert sorted(asked) == [("Brain", "uberon"), ("cortex", "uberon")]

    def test_a_source_that_is_not_there_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="cell_nowhere"):
            resolve_characteristic_terms(CrateState(), entity_id="cell_nowhere")

    def test_only_a_source_of_cells_is_asked_about(self, monkeypatch) -> None:
        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        study = draft_study(state, "Brain study", {"organ": "Brain"})
        assert resolve_characteristic_terms(state)["found"] == []
        assert asked == []
        assert "organ_term" not in study.fields

    @pytest.mark.parametrize("stated", ["", "   ", None, 7, ["Brain", "Liver"]])
    def test_only_a_stated_word_is_asked_about(self, monkeypatch, stated: Any) -> None:
        asked = _known(monkeypatch, {})
        state = CrateState()
        _source(state, organ=stated)
        resolve_characteristic_terms(state)
        assert asked == []


class TestResolveCellLine:
    def test_what_it_is_told_about_the_cells_gets_its_terms(self, monkeypatch) -> None:
        _known(monkeypatch, {("kidney epithelial cell", "cl"): KIDNEY_CELL})
        state = CrateState()
        told = {"source_kind": "primary cells", "cell_type": "kidney epithelial cell"}
        outcome = resolve_cell_line(state, "kidney tubuloids, donor T19", hints=told)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields["cell_type_term"] == KIDNEY_CELL

    def test_a_term_handed_in_is_refused(self, monkeypatch) -> None:
        _known(monkeypatch, {})
        state = CrateState()
        told = {"source_kind": "primary cells", "organ": "Brain", "organ_term": dict(BRAIN)}
        outcome = resolve_cell_line(state, "donor cells", hints=told)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields["organ"] == "Brain"
        assert "organ_term" not in cells.fields

    def test_a_failing_term_lookup_does_not_cost_the_cells(self, monkeypatch) -> None:
        def broken(label: str, ontology: str) -> dict[str, Any]:
            raise RuntimeError("the term service fell over")

        monkeypatch.setattr(composites, "lookup_exact_term", broken)
        state = CrateState()
        told = {"source_kind": "primary cells", "organ": "Brain"}
        outcome = resolve_cell_line(state, "donor cells", hints=told)
        cells = state.get_entity(outcome["entity_id"])
        assert cells is not None and cells.fields["organ"] == "Brain"


def _graph(state: CrateState) -> list[dict[str, Any]]:
    crate = assemble_crate(state, output_dir=None, materialize_payload=False)
    return crate.metadata.generate()["@graph"]


def _characteristics(graph: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [
        node
        for node in graph
        if node.get("additionalType") == "CharacteristicValue" and node.get("name") == name
    ]


class TestTheCrate:
    def test_a_confirmed_term_is_the_reference_of_the_value(self, monkeypatch) -> None:
        _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        _source(state, organ="Brain")
        resolve_characteristic_terms(state)

        graph = _graph(state)

        (organ,) = _characteristics(graph, "Organ")
        assert organ["value"] == "Brain"
        assert organ["valueReference"] == {"@id": BRAIN["@id"]}
        assert [node for node in graph if node["@id"] == BRAIN["@id"]] == [BRAIN]
        (sample,) = [node for node in graph if "Sample" in node["@type"]]
        assert not set(sample) & TERM_FIELDS
        assert not [node for node in graph if "term" in str(node.get("name", "")).lower().split()]

    def test_a_word_without_a_term_is_written_as_before(self, monkeypatch) -> None:
        _known(monkeypatch, {})
        state = CrateState()
        _source(state, organ="Brain")
        resolve_characteristic_terms(state)
        (organ,) = _characteristics(_graph(state), "Organ")
        assert organ["value"] == "Brain"
        assert "valueReference" not in organ

    def test_a_term_nobody_looked_up_is_not_written(self) -> None:
        state = CrateState()
        _source(state, organ="Brain", organ_term=dict(BRAIN))
        graph = _graph(state)
        (organ,) = _characteristics(graph, "Organ")
        assert "valueReference" not in organ
        assert not [node for node in graph if node["@id"] == BRAIN["@id"]]
        assert not [node for node in graph if node.get("value") == str(BRAIN)]

    def test_a_term_for_another_word_is_not_written(self, monkeypatch) -> None:
        _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        cells = _source(state, organ="Brain")
        resolve_characteristic_terms(state)
        set_fields(state, cells.entity_id, {"organ": "Liver lobe 3"})
        graph = _graph(state)
        (organ,) = _characteristics(graph, "Organ")
        assert organ["value"] == "Liver lobe 3"
        assert "valueReference" not in organ
        assert not [node for node in graph if node["@id"] == BRAIN["@id"]]

    @pytest.mark.parametrize("confirmed", ["donor A cells", "donor B cells"])
    def test_a_word_two_sources_state_keeps_its_term(self, monkeypatch, confirmed: str) -> None:
        """Both state one characteristic node, whichever of them is written last."""
        _known(monkeypatch, {("brain", "uberon"): BRAIN})
        state = CrateState()
        sources = {
            name: _source(state, name, organ="Brain") for name in ("donor A cells", "donor B cells")
        }
        resolve_characteristic_terms(state, entity_id=sources[confirmed].entity_id)
        (organ,) = _characteristics(_graph(state), "Organ")
        assert organ["valueReference"] == {"@id": BRAIN["@id"]}

    @pytest.mark.timeout(600)
    def test_the_term_earns_the_crate_no_finding(self, monkeypatch) -> None:
        from builder.tools.validation import build_and_validate

        def findings(*, with_term: bool) -> set[tuple[str, str]]:
            _known(monkeypatch, {("brain", "uberon"): BRAIN} if with_term else {})
            state = CrateState()
            state.metadata.title = "Thyroid hormone transport in brain cells"
            _source(state, organ="Brain")
            resolve_characteristic_terms(state)
            issues = build_and_validate(state, severity="optional", profile="all")["issues"]
            return {(str(issue.get("profile")), str(issue.get("message"))) for issue in issues}

        assert findings(with_term=True) == findings(with_term=False)


class TestATermIsNeverTakenFromProse:
    def test_each_characteristic_with_an_ontology_has_a_field_for_its_term(self) -> None:
        assert TERM_FIELDS == {"organ_term", "tissue_term", "cell_type_term"}
        assert TERM_FIELDS == {term_field(field) for field in CHARACTERISTIC_ONTOLOGIES}
        written = {characteristic.aliases[0] for characteristic in _CELL_LINE_CHARACTERISTICS}
        assert set(CHARACTERISTIC_ONTOLOGIES) <= written

    @pytest.mark.parametrize("field", sorted({"organ_term", "tissue_term", "cell_type_term"}))
    def test_it_is_an_identifier_no_model_is_asked_for(self, field: str) -> None:
        assert is_identifier_field(field)
        assert field not in drafter_visible_fields("CellLineSample")


class TestThePipeline:
    def _engine(self, monkeypatch, provider: str | None):
        import builder.agents.pipeline.pipeline as pipeline_mod
        from builder.engine import AgentEngine
        from builder.tools.hitl import SimulatedHumanInterface

        monkeypatch.setattr(pipeline_mod, "get_provider", lambda: provider)
        monkeypatch.setattr(pipeline_mod, "extract_plan", lambda *a, **k: None)
        monkeypatch.setattr(pipeline_mod, "draft_entity_fields", lambda *a, **k: {})
        state = CrateState()
        state.metadata.title = "Thyroid hormone transport in brain cells"
        _source(state, organ="Brain")
        engine = AgentEngine(state=state, human_interface=SimulatedHumanInterface())
        engine.initialize()
        return engine

    @pytest.mark.timeout(600)
    def test_the_spine_looks_the_terms_up_before_it_validates(self, monkeypatch) -> None:
        from builder.agents.pipeline.pipeline import run_pipeline

        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        engine = self._engine(monkeypatch, "openai")
        run_pipeline(engine)
        assert asked == [("Brain", "uberon")]
        (cells,) = engine.state.list_entities("CellLineSample")
        assert cells.fields["organ_term"] == BRAIN

    @pytest.mark.timeout(600)
    def test_the_spine_without_a_provider_asks_nothing(self, monkeypatch) -> None:
        from builder.agents.pipeline.pipeline import run_pipeline

        asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
        engine = self._engine(monkeypatch, None)
        run_pipeline(engine)
        assert asked == []

    @pytest.mark.timeout(600)
    def test_a_failing_term_lookup_does_not_break_the_spine(self, monkeypatch) -> None:
        from builder.agents.pipeline.pipeline import run_pipeline

        def broken(label: str, ontology: str) -> dict[str, Any]:
            raise RuntimeError("the term service fell over")

        engine = self._engine(monkeypatch, "openai")
        monkeypatch.setattr(composites, "lookup_exact_term", broken)
        assert "ok" in run_pipeline(engine)


class _SilentHuman:
    """Answers nothing: every question is skipped."""

    def present(
        self, context: str, options: list[str] | None = None, purpose: str | None = None
    ) -> HumanResponse:
        return {"action": "rejected", "comments": None, "edits": None}

    def request_input(self, prompt: str, field_type: str = "text") -> InputResponse:
        return {"value": None, "skipped": True}


@pytest.mark.timeout(600)
def test_the_question_round_ends_by_looking_the_terms_up(monkeypatch) -> None:
    """What a person answers there is stated after the spine asked, so the round asks again."""
    from builder.agents.pipeline.guidance import run_guidance
    from builder.engine import AgentEngine

    asked = _known(monkeypatch, {("brain", "uberon"): BRAIN})
    state = CrateState()
    state.metadata.title = "Thyroid hormone transport in brain cells"
    cells = _source(state, organ="Brain")
    human = _SilentHuman()
    run_guidance(AgentEngine(state=state, human_interface=human), human, max_rounds=2)
    assert asked == [("Brain", "uberon")]
    assert cells.fields["organ_term"] == BRAIN


def test_the_agent_can_ask_for_the_terms_itself() -> None:
    """A value the agent states after the cells were resolved has no other way to its term."""
    from builder.agents.react.tools_spec import TOOL_SPECS
    from builder.tools.registry import TOOL_REGISTRY

    assert TOOL_REGISTRY.all()["resolve_characteristic_terms"].fn is resolve_characteristic_terms
    (spec,) = [spec for spec in TOOL_SPECS if spec["name"] == "resolve_characteristic_terms"]
    parameters: Any = spec["parameters"]
    assert set(parameters["properties"]) == {"entity_id"}
    assert not parameters.get("required")
