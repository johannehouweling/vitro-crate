#!/usr/bin/env python3
"""Write the Required column of ``profiles/docs/isa_tox.md`` from the shapes.

The shapes are the specification; the page explains it. So no one types a
requirement level: each property row's MUST / SHOULD / MAY is the strictest
presence constraint any shape for that section places on the property
(``sh:minCount`` / ``sh:qualifiedMinCount`` at ``sh:Violation`` = MUST, at
``sh:Warning`` = SHOULD, none = MAY), over the tox shapes and the bundled
``isa-ro-crate`` shapes a section inherits. A property a shape requires but the
table has no row for is refused, not added: a row needs a description, which only
a person can write.

Regenerate with::

    uv run python scripts/gen_spec_levels.py

``--check`` writes nothing and fails when the page differs from what the shapes
give, which is what ``tests/test_isa_tox_spec.py`` runs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDFS, SH

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from profiles.validator import DEFAULT_PROFILES_PATH  # noqa: E402  (patches the ISA ontology)

SPEC = _REPO / "profiles" / "docs" / "isa_tox.md"
TOX_SHAPES = _REPO / "profiles" / "shapes" / "tox"
ISA_SHAPES = Path(DEFAULT_PROFILES_PATH) / "isa-ro-crate"

TOX = Namespace("https://w3id.org/ro/crate/isa-tox/1.0/")
_ISA = Namespace("https://github.com/crs4/rocrate-validator/profiles/isa-ro-crate/")
_BIOSCHEMAS = Namespace("https://bioschemas.org/")
_RO_CRATE = Namespace("https://github.com/crs4/rocrate-validator/profiles/ro-crate/")

# Spec section title -> the ISA (or Bioschemas) class whose shapes the section
# inherits; for the Investigation, the root class the ISA shapes target.
SECTION_CLASSES: dict[str, URIRef] = {
    "Investigation": _RO_CRATE.RootDataEntity,
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

# The tox shapes select their focus node from the data (a sh:SPARQLTarget), so no
# class reaches them: each section names its own.
_EVERY_STEP = ("LabProcessExecutesAProtocol",)
SECTION_SHAPES: dict[str, set[str]] = {
    "Investigation": {"InvestigationMustHaveDescriptors"},
    "Study - Adverse Outcome Pathway": {"StudyShouldReferenceAOP"},
    "Assay - Key Event": {"AssayShouldReferenceKeyEvent"},
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
# Data-targeted shapes the spec states in an "Expected ... items" table instead.
NOT_IN_A_SECTION_TABLE = {
    "DOIPropertyValueMustHaveOBIPropertyID",
    "PubMedIDPropertyValueMustHaveOBIPropertyID",
}

# A step's parameters are checked on schema:additionalProperty; the spec row is
# `parameterValue`, the ISA predicate the build also writes (profiles/context.py).
_LABPROCESS_ALIAS = {"additionalProperty": "parameterValue"}
_LEVEL_OF_SEVERITY = {SH.Violation: "MUST", SH.Warning: "SHOULD"}

# Inherited requirements no shape can check, because the value is what selects the
# entity: a Dataset without `additionalType` "Study" is not a Study (#856).
SELECTOR_ENFORCED: dict[tuple[str, str], str] = {
    ("Study - Adverse Outcome Pathway", "additionalType"): "MUST",
    ("Assay - Key Event", "additionalType"): "MUST",
}

# `####` sub-sections carry their own tables and terminate their parent.
SECTION_RE = re.compile(r"^#{3,4} (.+?)\n(.*?)(?=^#{2,4} |\Z)", re.M | re.S)
ROW_RE = re.compile(r"^\|([A-Za-z]+)\|(MUST|SHOULD|MAY)\|", re.M)


def shapes_graph() -> Graph:
    g = Graph()
    for ttl in sorted(TOX_SHAPES.glob("*.ttl")) + sorted(ISA_SHAPES.glob("*.ttl")):
        g.parse(ttl, format="turtle")
    return g


def requires_presence(g: Graph, prop) -> bool:
    for key in (SH.minCount, SH.qualifiedMinCount):
        value = g.value(prop, key)
        if isinstance(value, Literal) and int(value) >= 1:
            return True
    return False


def _with_superclasses(g: Graph, cls: URIRef) -> set[URIRef]:
    out, frontier = {cls}, [cls]
    while frontier:
        for sup in g.objects(frontier.pop(), RDFS.subClassOf):
            if isinstance(sup, URIRef) and sup not in out:
                out.add(sup)
                frontier.append(sup)
    return out


def shape_levels(g: Graph) -> dict[tuple[str, str], str]:
    """``(section, property) -> MUST|SHOULD``: the strictest presence constraint."""
    rows: dict[tuple[str, str], str] = dict(SELECTOR_ENFORCED)
    for title, cls in SECTION_CLASSES.items():
        shapes = {s for t in _with_superclasses(g, cls) for s in g.subjects(SH.targetClass, t)}
        shapes |= {TOX[name] for name in SECTION_SHAPES.get(title, ())}
        for shape in shapes:
            for prop in g.objects(shape, SH.property):
                path = g.value(prop, SH.path)
                if not isinstance(path, URIRef) or not requires_presence(g, prop):
                    continue  # sequence/inverse paths are ISA structure, not table rows
                name = str(path).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                if title.startswith("LabProcess"):
                    name = _LABPROCESS_ALIAS.get(name, name)
                level = _LEVEL_OF_SEVERITY.get(g.value(prop, SH.severity) or SH.Violation)
                if level and (level == "MUST" or (title, name) not in rows):
                    rows[(title, name)] = level
    return rows


def render(text: str, levels: dict[tuple[str, str], str]) -> tuple[str, list[tuple[str, str]]]:
    """The page with every property row's level taken from *levels*, and the
    ``(section, property)`` pairs a shape requires that no row states."""
    out, last, missing = [], 0, []
    for m in SECTION_RE.finditer(text):
        title, body = m.group(1), m.group(2)
        if title not in SECTION_CLASSES:
            continue
        table, sep, rest = body.partition("**Expected")  # parameter tables follow it
        present = {p for p, _ in ROW_RE.findall(table)}
        table = ROW_RE.sub(
            lambda r: f"|{r.group(1)}|{levels.get((title, r.group(1)), 'MAY')}|", table
        )
        missing += sorted((t, p) for (t, p) in levels if t == title and p not in present)
        out += [text[last : m.start(2)], table, sep, rest]
        last = m.end(2)
    out.append(text[last:])
    return "".join(out), missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the page is stale")
    args = parser.parse_args()
    current = SPEC.read_text(encoding="utf-8")
    page, missing = render(current, shape_levels(shapes_graph()))
    if missing:
        sys.stderr.write(f"shapes require properties the spec has no row for: {missing}\n")
        return 1
    if args.check:
        if page != current:
            sys.stderr.write(
                "isa_tox.md levels differ from the shapes: run scripts/gen_spec_levels.py\n"
            )
            return 1
        return 0
    SPEC.write_text(page, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
