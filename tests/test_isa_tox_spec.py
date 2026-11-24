"""The requirement levels in ``profiles/docs/isa_tox.md`` are the shapes' (#783, #823).

The page's Required column is written by ``scripts/gen_spec_levels.py`` from the
shapes, so a level is decided in one place. These tests fail when the page was not
regenerated after a shape changed, when a shape requires a property the page has
no row for, and when a data-targeted shape is reached by no section.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from rdflib.namespace import SH

_GENERATOR = Path(__file__).resolve().parent.parent / "scripts" / "gen_spec_levels.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_spec_levels", _GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def graph(gen):
    return gen.shapes_graph()


def test_every_section_has_rows(gen) -> None:
    text = gen.SPEC.read_text(encoding="utf-8")
    titles = {t for t, body in gen.SECTION_RE.findall(text) if gen.ROW_RE.search(body)}
    assert set(gen.SECTION_CLASSES) <= titles


def test_the_page_states_the_shapes_levels(gen, graph) -> None:
    current = gen.SPEC.read_text(encoding="utf-8")
    page, missing = gen.render(current, gen.shape_levels(graph))
    assert not missing, f"shapes require properties the page has no row for: {missing}"
    assert page == current, "isa_tox.md is stale: run `uv run python scripts/gen_spec_levels.py`"


def test_every_data_targeted_shape_belongs_to_a_section(gen, graph) -> None:
    """A shape with a sh:SPARQLTarget reaches the page only through SECTION_SHAPES."""
    targeted = {s for s in set(graph.subjects(SH.target)) if str(s).startswith(str(gen.TOX))}
    names = {str(s).rsplit("/", 1)[-1] for s in targeted}
    requiring = {
        str(s).rsplit("/", 1)[-1]
        for s in targeted
        if any(gen.requires_presence(graph, p) for p in graph.objects(s, SH.property))
    }
    listed = set().union(*gen.SECTION_SHAPES.values()) | gen.NOT_IN_A_SECTION_TABLE
    assert requiring - listed == set(), f"in no section: {sorted(requiring - listed)}"
    assert listed - names == set(), f"no such shape: {sorted(listed - names)}"
