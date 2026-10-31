"""The requirement tables in ``profiles/docs/isa_tox.md`` agree with the shapes
that enforce them (#783, #823).

Every ``###`` section of the spec carries a property table whose ``Required``
column reads MUST, SHOULD or MAY. The validator enforces presence through
``sh:minCount`` / ``sh:qualifiedMinCount`` at ``sh:Violation`` or ``sh:Warning``,
in the tox shapes and in the bundled ``isa-ro-crate`` shapes the tox classes
inherit from. The two must say the same thing, in both directions:

* MUST   <-> a presence constraint at Violation on that property
* SHOULD <-> a presence constraint at Warning, and none at Violation
* MAY    <-> no presence constraint at either severity

so the page cannot tell a reader a slot is optional while the validator fails
the crate without it, nor promise a check no shape performs.

Scope: the property tables. The "Expected ``parameterValue`` /
``additionalProperty`` items" tables that follow them list recommended
parameter names, which the spec states are not shape-checked. Shapes with no
spec table (AOP and Key Event references, CSVW tables, DOI/PubMed identifiers,
culture lineage, root descriptors) are outside this test.
"""

from __future__ import annotations

import re
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDFS, SH

from profiles.validator import DEFAULT_PROFILES_PATH  # importing patches the bundled ISA ontology

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = _ROOT / "profiles" / "docs" / "isa_tox.md"
_TOX_SHAPES = _ROOT / "profiles" / "shapes" / "tox"
_ISA_SHAPES = Path(DEFAULT_PROFILES_PATH) / "isa-ro-crate"

_TOX = Namespace("https://w3id.org/ro/crate/isa-tox/1.0/")
_ISA = Namespace("https://github.com/crs4/rocrate-validator/profiles/isa-ro-crate/")
_BIOSCHEMAS = Namespace("https://bioschemas.org/")

# Spec section title -> the ISA (or Bioschemas) class whose rows the section
# inherits. The profile mints no classes of its own, so the rows a section adds
# come from the shapes listed for it in _SECTION_SHAPES.
_SECTION_CLASSES: dict[str, URIRef] = {
    "MolecularEntity - Test and Control Chemicals": _BIOSCHEMAS.MolecularEntity,
    "Sample - Source Cells": _ISA.Sample,
    "Source Cells - Cell Line": _ISA.Sample,
    "Source Cells - Primary Cells": _ISA.Sample,
    "Sample - Test System": _ISA.Sample,
    "Test System - Co-Culture": _ISA.Sample,
    "Study - Adverse Outcome Pathway": _ISA.Study,
    "Assay - Key Event": _ISA.Assay,
    "LabProcess - Test System Preparation": _ISA.Process,
    "LabProcess - Exposure": _ISA.Process,
    "LabProcess - Endpoint Readout": _ISA.Process,
    "LabProcess - Data Analysis": _ISA.Process,
    "LabProtocol - Protocol record": _ISA.Protocol,
}

# The tox shapes select their focus node from the data (a sh:SPARQLTarget on a
# sampleType or a protocol record's intendedUse), so no class reaches them: each
# section names its own. test_every_data_targeted_shape_belongs_to_a_section
# keeps this list complete.
_EVERY_STEP = ("LabProcessExecutesAProtocol", "LabProcessExecutesOneProtocol")
_SECTION_SHAPES: dict[str, set[str]] = {
    "Sample - Source Cells": {"CellLineSampleMustHaveSampleType"},
    "Source Cells - Cell Line": {
        "CellLineSampleMustHaveSampleType",
        "CellLineSampleShouldHaveIdentifier",
    },
    "Source Cells - Primary Cells": {
        "CellLineSampleMustHaveSampleType",
        "PrimaryCellSampleShouldDescribeSource",
    },
    "Sample - Test System": {
        "CulturedSampleDerivesFromOneLine",
        "CulturedSampleShouldStateItsSource",
    },
    "Test System - Co-Culture": {"CoCultureDerivesFromSeveralLines"},
    "LabProcess - Test System Preparation": {"TestSystemPreparationRequirements", *_EVERY_STEP},
    "LabProcess - Exposure": {
        "ExposureRequirements",
        "ExposureShouldHaveCsvwConditionTable",
        *_EVERY_STEP,
    },
    "LabProcess - Endpoint Readout": {
        "EndpointReadoutConsumesExposedMaterial",
        "EndpointReadoutRequirements",
        *_EVERY_STEP,
    },
    "LabProcess - Data Analysis": {
        "DataAnalysisRequirements",
        "DataAnalysisShouldFollowAnalysisScript",
        *_EVERY_STEP,
    },
}
# Data-targeted shapes whose requirements the spec states in an "Expected ...
# items" table rather than a section's property table.
_NOT_IN_A_SECTION_TABLE = {
    "DOIPropertyValueMustHaveOBIPropertyID",
    "PubMedIDPropertyValueMustHaveOBIPropertyID",
}

# A step's parameters are written under `parameterValue` (the ISA predicate) and
# `parameter` (schema:additionalProperty, the path the tox shapes check); the spec
# row is `parameterValue`. See profiles/context.py.
_LABPROCESS_ALIAS = {"additionalProperty": "parameterValue"}

_LEVEL_OF_SEVERITY = {SH.Violation: "MUST", SH.Warning: "SHOULD"}

# Requirements the profile inherits but no shape can check, because the value is
# what selects the entity in the first place: a Dataset without `additionalType`
# "Study" is not a Study, so there is no Study for a Study shape to validate. The
# ISA profile states them as MUST, so the tables state them too; they are listed
# here so the parity check reads a missing shape as the tautology it is rather
# than as drift (#856).
_SELECTOR_ENFORCED: dict[tuple[str, str], str] = {
    ("Study - Adverse Outcome Pathway", "additionalType"): "MUST",
    ("Assay - Key Event", "additionalType"): "MUST",
}

# `####` sub-sections carry their own tables (the source-cell kinds, co-culture),
# so they are parsed as sections in their own right and terminate their parent.
_SECTION_RE = re.compile(r"^#{3,4} (.+?)\n(.*?)(?=^#{2,4} |\Z)", re.M | re.S)
_ROW_RE = re.compile(r"^\|([A-Za-z]+)\|(MUST|SHOULD|MAY)\|", re.M)


def _spec_rows() -> dict[tuple[str, str], str]:
    """``(section, property) -> MUST|SHOULD|MAY`` from each section's property table."""
    rows: dict[tuple[str, str], str] = {}
    for title, body in _SECTION_RE.findall(_SPEC.read_text(encoding="utf-8")):
        if title not in _SECTION_CLASSES:
            continue
        table = body.split("**Expected", 1)[0]  # the parameter tables follow it
        for prop, level in _ROW_RE.findall(table):
            rows[(title, prop)] = level
    return rows


def _shapes_graph() -> Graph:
    g = Graph()
    for ttl in sorted(_TOX_SHAPES.glob("*.ttl")) + sorted(_ISA_SHAPES.glob("*.ttl")):
        g.parse(ttl, format="turtle")
    return g


def _with_superclasses(g: Graph, cls: URIRef) -> set[URIRef]:
    out, frontier = {cls}, [cls]
    while frontier:
        for sup in g.objects(frontier.pop(), RDFS.subClassOf):
            if isinstance(sup, URIRef) and sup not in out:
                out.add(sup)
                frontier.append(sup)
    return out


def _requires_presence(g: Graph, prop) -> bool:
    for key in (SH.minCount, SH.qualifiedMinCount):
        value = g.value(prop, key)
        if isinstance(value, Literal) and int(value) >= 1:
            return True
    return False


def _shape_rows(g: Graph) -> dict[tuple[str, str], str]:
    """``(section, property) -> MUST|SHOULD``: the strictest presence constraint any
    shape for the section places on the property: the shapes of the class the
    section inherits from (or a superclass), and the section's own data-targeted
    shapes."""
    rows: dict[tuple[str, str], str] = {}
    for title, cls in _SECTION_CLASSES.items():
        shapes = {
            shape
            for target in _with_superclasses(g, cls)
            for shape in g.subjects(SH.targetClass, target)
        }
        shapes |= {_TOX[name] for name in _SECTION_SHAPES.get(title, ())}
        for shape in shapes:
            for prop in g.objects(shape, SH.property):
                path = g.value(prop, SH.path)
                if not isinstance(path, URIRef) or not _requires_presence(g, prop):
                    continue  # sequence/inverse paths are ISA structure, not table rows
                name = str(path).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                if title.startswith("LabProcess"):
                    name = _LABPROCESS_ALIAS.get(name, name)
                level = _LEVEL_OF_SEVERITY.get(g.value(prop, SH.severity) or SH.Violation)
                if level and (level == "MUST" or (title, name) not in rows):
                    rows[(title, name)] = level
    return rows


def test_every_section_has_rows_to_check() -> None:
    rows = _spec_rows()
    assert {title for title, _ in rows} == set(_SECTION_CLASSES)
    assert len(rows) >= 40, rows


def test_requirement_levels_match_the_shapes() -> None:
    spec = _spec_rows()
    shapes = _shape_rows(_shapes_graph()) | _SELECTOR_ENFORCED
    drift = {
        key: (spec.get(key, "MAY"), shapes.get(key, "MAY"))
        for key in sorted(spec.keys() | shapes.keys())
        if spec.get(key, "MAY") != shapes.get(key, "MAY")
    }
    assert not drift, f"isa_tox.md (left) and the shapes (right) disagree: {drift}"


def test_every_data_targeted_shape_belongs_to_a_section() -> None:
    """A shape with a sh:SPARQLTarget is reached only through _SECTION_SHAPES.

    One missing from it would have its requirements checked against no table,
    and a name left behind after a shape is renamed would check nothing.
    """
    g = _shapes_graph()
    targeted = {s for s in set(g.subjects(SH.target)) if str(s).startswith(str(_TOX))}
    names = {str(s).rsplit("/", 1)[-1] for s in targeted}
    requiring = {
        str(s).rsplit("/", 1)[-1]
        for s in targeted
        if any(_requires_presence(g, p) for p in g.objects(s, SH.property))
    }
    listed = set().union(*_SECTION_SHAPES.values()) | _NOT_IN_A_SECTION_TABLE
    assert requiring - listed == set(), f"in no section: {sorted(requiring - listed)}"
    assert listed - names == set(), f"no such shape: {sorted(listed - names)}"
