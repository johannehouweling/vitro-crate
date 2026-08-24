"""One CellCulture per cell line, and one exposed Sample per cultured one (#678).

The crate used to assert a co-culture that never happened: a single CellCulture
consumed every named line and emitted ONE Sample whose ``derivesFrom`` listed all
of them. The deposit says otherwise — S-VHPS22 ships one culture protocol
document per line. These pin the split, and pin it at both hops, because
splitting the culture alone just relocates the merge to the Exposure.
"""

from __future__ import annotations

import json

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.builder import build_crate

pytestmark = pytest.mark.timeout(120)


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _build(state, tmp_path):
    out = tmp_path / "crate"
    result = build_crate(state, str(out))
    assert result["success"] is True, result
    with open(out / "ro-crate-metadata.json") as f:
        graph = json.load(f)["@graph"]
    return graph, {e["@id"]: e for e in graph}


def _ids(value):
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [v.get("@id") if isinstance(v, dict) else v for v in items]


def _typed(node, wanted):
    types = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
    return wanted in types


def _processes(graph, flavour=None):
    out = [n for n in graph if _typed(n, "LabProcess")]
    if flavour is not None:
        out = [n for n in out if n.get("additionalType") == flavour]
    return out


def _two_line_culture(**extra):
    """An assay whose culture names two lines — the shape that produced the merge."""
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
            name="Culture SK-N-AS and MO3.13 neural cells",
            process_type="CellCulture",
            assay_id="assay_1",
            cell_line=["cell_a", "cell_b"],
            culture_medium="CT medium",
            **extra,
        )
    )
    return state


class TestCultureSplitsPerCellLine:
    def test_two_lines_yield_two_culture_processes(self, tmp_path):
        graph, _ = _build(_two_line_culture(), tmp_path)
        cultures = _processes(graph, "CellCulture")
        assert len(cultures) == 2, (
            "a culture naming two lines is two culturing activities, not one: "
            f"got {[c.get('name') for c in cultures]}"
        )

    def test_each_culture_consumes_exactly_one_line(self, tmp_path):
        graph, _ = _build(_two_line_culture(), tmp_path)
        for culture in _processes(graph, "CellCulture"):
            consumed = _ids(culture.get("input"))
            assert len(consumed) == 1, (
                f"{culture.get('name')!r} consumed {len(consumed)} cell lines; "
                "one culture grows one line"
            )

    def test_no_cultured_sample_derives_from_two_lines(self, tmp_path):
        """The co-culture claim itself. This is the assertion the crate failed."""
        graph, by_id = _build(_two_line_culture(), tmp_path)
        for culture in _processes(graph, "CellCulture"):
            for out_id in _ids(culture.get("output")):
                sample = by_id.get(out_id)
                assert sample is not None, f"{out_id} produced but not described"
                lineage = _ids(sample.get("derivesFrom"))
                assert len(lineage) <= 1, (
                    f"{sample.get('name')!r} derives from {len(lineage)} cell "
                    "lines — that asserts a co-culture. A cultured sample derives "
                    "from exactly one line unless it is typed as a co-culture."
                )

    def test_both_lines_are_actually_cultured(self, tmp_path):
        """Splitting must not drop a line — the failure #650 fixed the other way."""
        graph, _ = _build(_two_line_culture(), tmp_path)
        consumed = {
            cid
            for culture in _processes(graph, "CellCulture")
            for cid in _ids(culture.get("input"))
        }
        assert "https://www.cellosaurus.org/CVCL_1700" in consumed
        assert "https://www.cellosaurus.org/CVCL_D357" in consumed


class TestExposureDoesNotRelocateTheMerge:
    def _state(self):
        state = _two_line_culture()
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
        return state

    def test_exposure_consumes_every_cultured_sample_in_its_assay(self, tmp_path):
        graph, _ = _build(self._state(), tmp_path)
        cultured = {
            out
            for culture in _processes(graph, "CellCulture")
            for out in _ids(culture.get("output"))
        }
        exposure = _processes(graph, "Exposure")[0]
        consumed = set(_ids(exposure.get("input")))
        assert cultured and cultured <= consumed, (
            "the exposure must consume every cultured sample of its assay; "
            f"cultured={sorted(cultured)} consumed={sorted(consumed)}"
        )

    def test_one_exposed_sample_per_cultured_sample(self, tmp_path):
        graph, by_id = _build(self._state(), tmp_path)
        exposure = _processes(graph, "Exposure")[0]
        cultured = [c for c in _ids(exposure.get("input")) if c in by_id]
        exposed = [
            r
            for r in _ids(exposure.get("output"))
            if r in by_id and _typed(by_id[r], "Sample")
        ]
        assert len(exposed) == len(cultured), (
            f"{len(cultured)} cultured samples went in and {len(exposed)} exposed "
            "came out — the merge moved down a hop instead of going away"
        )

    def test_each_exposed_sample_derives_from_one_cultured_sample(self, tmp_path):
        graph, by_id = _build(self._state(), tmp_path)
        exposure = _processes(graph, "Exposure")[0]
        for out_id in _ids(exposure.get("output")):
            node = by_id.get(out_id)
            if node is None or not _typed(node, "Sample"):
                continue
            lineage = _ids(node.get("derivesFrom"))
            assert len(lineage) == 1, (
                f"{node.get('name')!r} derives from {len(lineage)} samples; an "
                "exposed sample comes from exactly one cultured sample"
            )
