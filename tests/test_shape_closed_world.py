"""The tox shapes are open-world, as ``isa_tox.md`` states (#823).

A property the profile does not name is neither checked nor forbidden. A shape
that declared ``sh:closed`` would make every unlisted property a violation and
turn the spec's promise into a lie, so the policy is pinned on both sides: the
sentence in the spec and the absence of the constraint in the shapes.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph
from rdflib.namespace import SH

_ROOT = Path(__file__).resolve().parent.parent


def test_the_spec_states_the_open_world_policy() -> None:
    spec = (_ROOT / "profiles" / "docs" / "isa_tox.md").read_text(encoding="utf-8")
    assert "The shapes are open-world" in spec


def test_no_tox_shape_is_closed() -> None:
    g = Graph()
    for ttl in sorted((_ROOT / "profiles" / "shapes" / "tox").glob("*.ttl")):
        g.parse(ttl, format="turtle")
    closed = [str(s) for s in g.subjects(SH.closed, None)]
    assert closed == [], f"these shapes declare sh:closed against the stated policy: {closed}"
