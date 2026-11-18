"""The analysis-script rule accepts a script the analysis executes itself.

A step that follows deposited documents executes the files themselves, so the
rule that a data analysis SHOULD follow a deposited script holds when the
analysis executes a ``File`` typed ``SoftwareSourceCode`` and ``LabProtocol``, as
it does when the analysis executes a protocol record based on one. A wet-lab
document in the script's place still falls short of it.

Each test changes a built document by hand, so the shape is pinned whatever the
builder routes to the step: the analysis here has no document of its own and is
built with a record based on nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.state import CrateState, Entity, EntityProvenance, EntityType
from builder.tools.builder import assemble_crate
from builder.tools.validation import validate_document

pytestmark = pytest.mark.timeout(300)

SCRIPT = "scripts/fit_curves.R"
SOP = "docs/SOP.docx"
RULE = "deposited analysis script"


def _ent(entity_id: str, type_: EntityType, **fields: Any) -> Entity:
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _document() -> dict[str, Any]:
    """An analysis with no document, beside a script and an SOP no step follows."""
    state = CrateState()
    state.metadata.title = "An analysis and its script"
    state.add_entity(_ent("study_1", "Study", name="S"))
    state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
    state.add_entity(_ent("raw", "File", name="raw.csv", path="assay/raw.csv"))
    state.add_entity(_ent("out", "File", name="ec50.csv", path="assay/ec50.csv"))
    state.add_entity(_ent("script", "File", name="fit_curves.R", path=SCRIPT))
    state.add_entity(
        _ent("sop", "File", name="SOP.docx", path=SOP, additional_types=["LabProtocol"])
    )
    state.add_entity(
        _ent(
            "ana",
            "LabProcess",
            name="Curve fitting",
            process_type="DataAnalysis",
            assay_id="assay_1",
            object=["raw"],
            result=["out"],
            software="R drc",
        )
    )
    crate = assemble_crate(
        state, output_dir=None, materialize_payload=False, include_all_scanned=False
    )
    return crate.metadata.generate()


def _nodes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["@id"]: node for node in document["@graph"]}


def _findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    report = validate_document(document, severity="recommended", profile="tox")
    return [issue for issue in report["issues"] if RULE in issue["message"]]


def _execute(document: dict[str, Any], protocol_id: str) -> None:
    """Let the analysis execute *protocol_id* in place of its record, which carried
    the step's protocol type."""
    nodes = _nodes(document)
    analysis = nodes["#LabProcess_ana"]
    record = nodes[analysis["executesLabProtocol"]["@id"]]
    nodes[protocol_id]["intendedUse"] = record["intendedUse"]
    analysis["executesLabProtocol"] = {"@id": protocol_id}
    document["@graph"].remove(record)


class TestAnAnalysisFollowsADepositedScript:
    def test_a_record_based_on_nothing_falls_short(self):
        (finding,) = _findings(_document())
        assert finding["entity_id"].endswith("#LabProcess_ana"), finding

    def test_a_record_based_on_the_script_follows_it(self):
        document = _document()
        nodes = _nodes(document)
        record = nodes[nodes["#LabProcess_ana"]["executesLabProtocol"]["@id"]]
        record["isBasedOn"] = {"@id": SCRIPT}
        assert _findings(document) == []

    def test_an_analysis_that_executes_the_script_follows_it(self):
        document = _document()
        assert "SoftwareSourceCode" in _nodes(document)[SCRIPT]["@type"]
        _execute(document, SCRIPT)
        assert _findings(document) == []

    def test_an_analysis_that_executes_a_wet_lab_document_falls_short(self):
        document = _document()
        assert "SoftwareSourceCode" not in _nodes(document)[SOP]["@type"]
        _execute(document, SOP)
        (finding,) = _findings(document)
        assert finding["entity_id"].endswith("#LabProcess_ana"), finding
