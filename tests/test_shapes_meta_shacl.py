"""The tox shapes are themselves valid SHACL (#823).

rocrate-validator runs pyshacl with ``meta_shacl=False``, so a malformed
constraint (a string where an integer belongs, a property shape with no
``sh:path``) is never reported: pyshacl skips what it cannot read and the crate
"conforms" to a check that silently stopped existing. Validating the shapes
graph against SHACL-SHACL once, here, closes that hole before the profile is
published.
"""

from __future__ import annotations

from pathlib import Path

import pyshacl
import pytest
from pyshacl.errors import ReportableRuntimeError
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import SH

_SHAPES_DIR = Path(__file__).resolve().parent.parent / "profiles" / "shapes" / "tox"


def _shapes_graph() -> Graph:
    shapes = Graph()
    for ttl in sorted(_SHAPES_DIR.glob("*.ttl")):
        shapes.parse(ttl, format="turtle")
    return shapes


def _meta_validate(shapes: Graph) -> None:
    """Raise if *shapes* does not conform to SHACL-SHACL."""
    pyshacl.validate(Graph(), shacl_graph=shapes, meta_shacl=True, advanced=True)


def test_the_tox_shapes_conform_to_shacl_shacl() -> None:
    _meta_validate(_shapes_graph())


def test_a_malformed_constraint_is_caught() -> None:
    """Negative control: the check is live, not a no-op."""
    shapes = _shapes_graph()
    ex = Namespace("https://example.org/")
    shapes.add((ex.Broken, SH.minCount, Literal("one")))
    shapes.add((ex.Broken, SH.path, ex.anything))
    with pytest.raises(ReportableRuntimeError):
        _meta_validate(shapes)
