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

# Spec section title -> the class its rows constrain. Superclasses declared in
# ontology.ttl (tox:CellLineSample is a subclass of isa-ro-crate:Sample, ...) are
# followed, so the inherited ISA rows the tables repeat are matched against the
# ISA shapes that enforce them.
_SECTION_CLASSES: dict[str, URIRef] = {
    "MolecularEntity - Test and Control Chemicals": _BIOSCHEMAS.MolecularEntity,
    "Sample - Source Cells": _TOX.TestSystemSource,
    "Sample - Cell Line": _TOX.CellLineSample,
    "Sample - Primary Cells": _TOX.PrimaryCellSample,
    "Sample - Test System": _TOX.CulturedSample,
    "Sample - Co-Culture": _TOX.CoCultureSample,
    "LabProcess - Test System Preparation": _TOX.LabProcessTestSystemPreparation,
    "LabProcess - Exposure": _TOX.LabProcessExposure,
    "LabProcess - Endpoint Readout": _TOX.LabProcessEndpointReadout,
    "LabProcess - Data Analysis": _TOX.LabProcessDataAnalysis,
    "LabProtocol - Protocol record": _ISA.Protocol,
}

# A step's parameters are written under `parameterValue` (the ISA predicate) and
# `parameter` (schema:additionalProperty, the path the tox shapes check); the spec
# row is `parameterValue`. See profiles/context.py.
_LABPROCESS_ALIAS = {"additionalProperty": "parameterValue"}

_LEVEL_OF_SEVERITY = {SH.Violation: "MUST", SH.Warning: "SHOULD"}

_SECTION_RE = re.compile(r"^### (.+?)\n(.*?)(?=^#{2,3} |\Z)", re.M | re.S)
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
    shape targeting the section's class (or a superclass) places on the property."""
    rows: dict[tuple[str, str], str] = {}
    for title, cls in _SECTION_CLASSES.items():
        for target in _with_superclasses(g, cls):
            for shape in g.subjects(SH.targetClass, target):
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
    shapes = _shape_rows(_shapes_graph())
    drift = {
        key: (spec.get(key, "MAY"), shapes.get(key, "MAY"))
        for key in sorted(spec.keys() | shapes.keys())
        if spec.get(key, "MAY") != shapes.get(key, "MAY")
    }
    assert not drift, f"isa_tox.md (left) and the shapes (right) disagree: {drift}"
