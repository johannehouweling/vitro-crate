"""A LabProcess says nothing about what it was not told — one spelling of absent.

Every subtype merges its own properties through ``LabProcess.__init__``. A key
whose value is ``None`` or ``[]`` is omitted there, once, so a node never
carries ``"parameter": []`` (the shape's minCount fires on the missing key just
the same) and a reader meets one spelling of "absent", not two (#740, #650).
"""

from __future__ import annotations

import pytest
from rocrate.rocrate import ROCrate

from profiles.models.isa import LabProcess, Sample
from profiles.models.tox import (
    LabProcessDataAnalysis,
    LabProcessEndpointReadout,
    LabProcessExposure,
    LabProcessTestSystemPreparation,
)


def _nothing_stated(crate):
    return {
        "TestSystemPreparation": lambda: LabProcessTestSystemPreparation(
            crate,
            "#cc",
            "Culture",
            cell_line=[],
            culture_medium=None,
            result=Sample(crate, "#cultured", "cultured cells"),
            labprotocol=None,
        ),
        "Exposure": lambda: LabProcessExposure(
            crate, "#exp", None, None, None, samples=[], labprotocol=None
        ),
        "EndpointReadout": lambda: LabProcessEndpointReadout(
            crate, "#er", None, None, [], None, None, None, None, None
        ),
        "DataAnalysis": lambda: LabProcessDataAnalysis(
            crate, "#da", object=[], result=[], labprotocol=None
        ),
    }


@pytest.mark.parametrize(
    "subtype", ["TestSystemPreparation", "Exposure", "EndpointReadout", "DataAnalysis"]
)
def test_a_subtype_told_nothing_states_nothing(subtype):
    node = _nothing_stated(ROCrate())[subtype]()
    absent = {k for k, v in node.properties().items() if v is None or v == []}
    assert not absent, f"{subtype} carries {absent} as an empty spelling of absent"
    assert "parameter" not in node.properties()


def test_the_base_class_omits_an_empty_or_null_override():
    node = LabProcess(
        ROCrate(), "#p", labprotocol=None, properties={"parameter": [], "agent": None}
    )
    assert "parameter" not in node.properties()
    assert "agent" not in node.properties()
