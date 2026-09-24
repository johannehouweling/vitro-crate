"""Guard test for the LabProcess requirement tables in ``profiles/docs/isa_tox.md``.

The spec states a requirement level for each subtype's ``object`` / ``result``;
the tox SHACL shapes enforce one. A row reads MUST exactly when a tox shape
targeting that subtype sets ``sh:minCount`` of at least 1 at ``sh:Violation`` on
that path, so the page cannot tell a reader a slot is optional while the
validator fails the crate without it (#783).
"""

from __future__ import annotations

import re
from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import SH

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = Namespace("http://schema.org/")
_TOX = Namespace("https://w3id.org/ro/crate/isa-tox/1.0/")

# `### LabProcess - Test System Preparation` up to the next heading, and its object/result rows.
_SECTION_RE = re.compile(r"^### LabProcess - (.+?)\n(.*?)(?=^#{2,3} )", re.M | re.S)
_ROW_RE = re.compile(r"^\|(object|result)\|(MUST|SHOULD|MAY)\|", re.M)


def _required_at_violation(shapes: Graph, subtype: str, path: str) -> bool:
    return any(
        (prop, SH.path, _SCHEMA[path]) in shapes
        and shapes.value(prop, SH.severity, default=SH.Violation) == SH.Violation
        and int(str(shapes.value(prop, SH.minCount) or 0)) >= 1
        for shape in shapes.subjects(SH.targetClass, _TOX[f"LabProcess{subtype}"])
        for prop in shapes.objects(shape, SH.property)
    )


def test_labprocess_object_and_result_levels_match_the_tox_shapes() -> None:
    shapes = Graph()
    for ttl in sorted((_ROOT / "profiles" / "shapes" / "tox").glob("*.ttl")):
        shapes.parse(ttl, format="turtle")
    spec = (_ROOT / "profiles" / "docs" / "isa_tox.md").read_text(encoding="utf-8")
    rows = {
        (title.replace(" ", ""), path): level
        for title, body in _SECTION_RE.findall(spec)
        for path, level in _ROW_RE.findall(body)
    }
    assert len(rows) == 8, f"expected object + result rows for four subtypes: {rows}"

    drift = {
        key: level
        for key, level in rows.items()
        if (level == "MUST") != _required_at_violation(shapes, *key)
    }
    assert not drift, f"isa_tox.md disagrees with the tox shapes on these rows: {drift}"
