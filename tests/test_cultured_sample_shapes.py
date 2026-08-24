"""The co-culture claim is unrepresentable, not merely un-made (#678).

The builder now splits a culture per cell line, so the crates this repo produces
no longer assert a mixture. That fixes today's output; it does not stop a crate
built by the ReAct arm, hand-authored, or produced by a future refactor from
saying it again. These pin the shapes that do.

A co-culture is a real experimental design and is accommodated rather than
forbidden: the material says which it is, and both directions are checked — a
mixture posing as a pure culture fails, and a "co-culture" of one line fails too.
"""

from __future__ import annotations

import copy
import json
import warnings

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate
from profiles.validator import validate_crate_dict

pytestmark = pytest.mark.timeout(180)

CELL_CULTURE_TERM = "http://purl.obolibrary.org/obo/OBI_0001876"
CO_CULTURE_TERM = "http://purl.obolibrary.org/obo/NCIT_C93168"


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


@pytest.fixture(scope="module")
def valid_doc(tmp_path_factory):
    """A crate that passes, to be perturbed one property at a time."""
    state = CrateState()
    state.add_entity(_ent("assay_1", "Assay", name="Deiodinase Assay"))
    state.add_entity(
        _ent("cell_a", "CellLineSample", name="SK-N-AS", accession="CVCL_1700")
    )
    state.add_entity(
        _ent("cell_b", "CellLineSample", name="MO3.13", accession="CVCL_D357")
    )
    state.add_entity(
        _ent(
            "proc_cult",
            "LabProcess",
            name="Culture neural cells",
            process_type="CellCulture",
            assay_id="assay_1",
            cell_line=["cell_a", "cell_b"],
            culture_medium="CT medium",
        )
    )
    state.add_entity(
        _ent(
            "proc_exp",
            "LabProcess",
            name="2-hour D3 activity exposure",
            process_type="Exposure",
            assay_id="assay_1",
            duration="2 hours",
        )
    )
    out = tmp_path_factory.mktemp("shapes") / "crate"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert build_crate(state, str(out))["success"] is True
    with open(out / "ro-crate-metadata.json") as f:
        return json.load(f)


def _tox(doc):
    """The ISA-Tox pass over an in-memory document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = validate_crate_dict(doc, profile="tox")
    assert results, "no ISA-Tox pass ran"
    return results[0]


def _cultured(doc):
    """A Sample the crate types as a cell culture."""
    for node in doc["@graph"]:
        st = node.get("sampleType")
        if isinstance(st, dict) and st.get("@id") == CELL_CULTURE_TERM:
            return node
    raise AssertionError("no cultured Sample in the fixture")


def test_the_fixture_itself_passes(valid_doc):
    """Guards every other test here: a failure below must be the perturbation."""
    assert _tox(valid_doc).passed_required, _tox(valid_doc).required_issues


class TestACulturedSampleComesFromOneLine:
    def test_two_lines_on_a_cultured_sample_is_a_violation(self, valid_doc):
        doc = copy.deepcopy(valid_doc)
        _cultured(doc)["derivesFrom"] = [
            {"@id": "https://www.cellosaurus.org/CVCL_1700"},
            {"@id": "https://www.cellosaurus.org/CVCL_D357"},
        ]
        result = _tox(doc)
        assert not result.passed_required, (
            "a cultured Sample deriving from two cell lines asserts a co-culture "
            "and must not validate"
        )

    def test_the_same_sample_typed_as_a_co_culture_passes(self, valid_doc):
        """Co-culture is accommodated, not forbidden.

        The only difference from the test above is what the material says it is.
        """
        doc = copy.deepcopy(valid_doc)
        sample = _cultured(doc)
        sample["derivesFrom"] = [
            {"@id": "https://www.cellosaurus.org/CVCL_1700"},
            {"@id": "https://www.cellosaurus.org/CVCL_D357"},
        ]
        sample["sampleType"] = {"@id": CO_CULTURE_TERM}
        doc["@graph"].append(
            {
                "@id": CO_CULTURE_TERM,
                "@type": "DefinedTerm",
                "name": "Co-Culture",
                "termCode": "NCIT:C93168",
            }
        )
        result = _tox(doc)
        assert result.passed_required, (
            f"a declared co-culture of two lines is legitimate: {result.required_issues}"
        )


class TestACoCultureIsAMixture:
    def test_a_co_culture_of_one_line_is_a_violation(self, valid_doc):
        """The other direction: the label must not be free.

        A material typed Co-Culture that derives from a single line is as wrong
        as a mixture claiming to be pure — it would let the type be used to
        escape the constraint above rather than to describe an experiment.
        """
        doc = copy.deepcopy(valid_doc)
        sample = _cultured(doc)
        sample["sampleType"] = {"@id": CO_CULTURE_TERM}
        sample["derivesFrom"] = [{"@id": "https://www.cellosaurus.org/CVCL_1700"}]
        doc["@graph"].append(
            {
                "@id": CO_CULTURE_TERM,
                "@type": "DefinedTerm",
                "name": "Co-Culture",
                "termCode": "NCIT:C93168",
            }
        )
        assert not _tox(doc).passed_required, (
            "a co-culture of one cell line is a contradiction"
        )


def _with_readout(doc, *, consumes):
    """Append a well-formed EndpointReadout that consumes *consumes*.

    Hand-authored rather than built, because the builder will not produce the
    shapes under test — that is the point of having them.
    """
    doc = copy.deepcopy(doc)
    doc["@graph"].append(
        {
            "@id": "data/raw_measurements.csv",
            "@type": "File",
            "name": "raw measurements",
            "encodingFormat": "text/csv",
        }
    )
    doc["@graph"].append(
        {
            "@id": "#param_Endpoint_test",
            "@type": "PropertyValue",
            "name": "Endpoint",
            "value": "T3 conversion",
        }
    )
    readout = {
        "@id": "#LabProcess_proc_read",
        "@type": ["LabProcess", "schema:Action"],
        "additionalType": "EndpointReadout",
        "name": "D3 deiodinase activity readout",
        "output": [{"@id": "data/raw_measurements.csv"}],
        "additionalProperty": [{"@id": "#param_Endpoint_test"}],
        "parameterValue": [{"@id": "#param_Endpoint_test"}],
    }
    if consumes is not None:
        readout["input"] = consumes
    doc["@graph"].append(readout)
    for node in doc["@graph"]:
        if node.get("@id") == "./":
            node.setdefault("hasPart", [])
            if isinstance(node["hasPart"], list):
                node["hasPart"].append({"@id": "data/raw_measurements.csv"})
    return doc


class TestAReadoutSaysWhatItMeasured:
    """The readout shape had no input rule at all, which is why a readout
    consuming the wrong material — or nothing — validated clean (#650, #678)."""

    def test_a_readout_consuming_nothing_is_a_violation(self, valid_doc):
        doc = _with_readout(valid_doc, consumes=None)
        assert not _tox(doc).passed_required, (
            "a readout that names no input measured nothing; the crate cannot "
            "say what was measured"
        )

    def test_a_readout_consuming_a_sample_passes(self, valid_doc):
        sample_id = _cultured(valid_doc)["@id"]
        doc = _with_readout(valid_doc, consumes=[{"@id": sample_id}])
        result = _tox(doc)
        assert result.passed_required, (
            f"a readout measuring a Sample is well-formed: {result.required_issues}"
        )

    def test_a_readout_consuming_a_file_passes(self, valid_doc):
        """A File is a legitimate input — a re-analysis measures deposited data."""
        doc = _with_readout(valid_doc, consumes=[{"@id": "data/raw_measurements.csv"}])
        result = _tox(doc)
        assert result.passed_required, (
            f"a readout measuring a File is well-formed: {result.required_issues}"
        )
