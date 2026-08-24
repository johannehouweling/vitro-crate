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


class TestTheReadoutMeasuresWhatTheExposureProduced:
    """The surviving half of #650, live in S-VHPS22.

    ``_chain_processes`` redirects a readout off the cultured sample onto the
    exposed one, but grouped by ASSAY — and culturing is study-level, shared
    between the deiodinase and metabolism assays. The deiodinase group held no
    CellCulture, so its ``cultured_ids`` was empty, the guard never matched, and
    its readout kept consuming the culture while an exposure sat between them.
    """

    def _borrowed_culture(self, *, with_exposure=True):
        """assay_2 exposes and measures cells that assay_1 grew."""
        state = CrateState()
        state.add_entity(_ent("assay_1", "Assay", name="Metabolism"))
        state.add_entity(_ent("assay_2", "Assay", name="Deiodinase"))
        state.add_entity(
            _ent("cell_a", "CellLineSample", name="SK-N-AS", accession="CVCL_1700")
        )
        state.add_entity(_ent("sample_cult", "Sample", name="cultured"))
        state.add_entity(
            _ent(
                "proc_cult",
                "LabProcess",
                name="Culture SK-N-AS",
                process_type="CellCulture",
                assay_id="assay_1",
                cell_line="cell_a",
                culture_medium="CT medium",
                result="sample_cult",
            )
        )
        if with_exposure:
            state.add_entity(
                _ent(
                    "proc_exp",
                    "LabProcess",
                    name="2-hour D3 activity exposure",
                    process_type="Exposure",
                    assay_id="assay_2",
                    samples="sample_cult",
                    duration="2 hours",
                )
            )
        state.add_entity(
            _ent(
                "proc_read",
                "LabProcess",
                name="D3 deiodinase activity readout",
                process_type="EndpointReadout",
                assay_id="assay_2",
                samples="sample_cult",
                detection_instrument="UPLC",
                endpoint="T3 conversion",
            )
        )
        return state

    def test_readout_consumes_the_exposed_sample_not_the_cultured_one(self, tmp_path):
        graph, by_id = _build(self._borrowed_culture(), tmp_path)
        exposure = _processes(graph, "Exposure")[0]
        readout = _processes(graph, "EndpointReadout")[0]
        exposed = {
            r
            for r in _ids(exposure.get("output"))
            if r in by_id and _typed(by_id[r], "Sample")
        }
        consumed = set(_ids(readout.get("input")))
        assert exposed, "the exposure produced no exposed sample to measure"
        assert consumed & exposed, (
            "the readout measured the cultured sample while an exposure sat "
            f"between them: consumed={sorted(consumed)} exposed={sorted(exposed)}"
        )

    def test_a_readout_with_no_exposure_still_measures_the_culture(self, tmp_path):
        """A characterisation run is the truth, not the defect.

        An assay with no Exposure measures the culture, and nothing should
        redirect it — there is no exposed sample and inventing one would assert
        an intervention that never happened.
        """
        graph, _ = _build(self._borrowed_culture(with_exposure=False), tmp_path)
        readout = _processes(graph, "EndpointReadout")[0]
        assert _ids(readout.get("input")), (
            "a readout in an exposure-free assay keeps consuming the culture"
        )


class TestAProtocolSitsWhereItIsUsed:
    """Invariant 4 — the protocol is the reused entity, so reuse decides placement.

    ``_link_to_study``'s own docstring already says it: "a protocol that governs
    several assays is a study-level document and the backbone should say so". The
    implementation keyed on the protocol's KIND instead — every culture protocol
    went to the Study, however many assays actually followed it.
    """

    def _two_assays_one_line_each(self):
        """Each assay grows a different line, so neither document is shared."""
        state = CrateState()
        state.metadata.input_path = "/deposit"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A1", study_id="study_1"))
        state.add_entity(_ent("assay_2", "Assay", name="A2", study_id="study_1"))
        state.add_entity(_ent("cell_a", "CellLineSample", name="SK-N-AS"))
        state.add_entity(_ent("cell_b", "CellLineSample", name="H4"))
        for eid, nm in (
            ("proto_sk", "cell culture protocol SK-N-AS.docx"),
            ("proto_h4", "cell culture protocol H4.docx"),
        ):
            state.add_entity(
                _ent(
                    eid,
                    "File",
                    name=nm,
                    path=f"cell_line_protocols/{nm}",
                    additional_types=["LabProtocol"],
                )
            )
        for n, (assay, line) in enumerate(
            (("assay_1", "cell_a"), ("assay_2", "cell_b")), start=1
        ):
            state.add_entity(
                _ent(
                    f"proc_{n}",
                    "LabProcess",
                    name=f"Culture {n}",
                    process_type="CellCulture",
                    assay_id=assay,
                    cell_line=line,
                    culture_medium="DMEM",
                )
            )
        return state

    def test_a_protocol_used_by_one_assay_is_not_hoisted_to_the_study(self, tmp_path):
        _, by_id = _build(self._two_assays_one_line_each(), tmp_path)
        study_parts = _ids(by_id["#Study_study_1"].get("hasPart"))
        hoisted = [p for p in study_parts if "cell_line_protocols" in str(p)]
        assert not hoisted, (
            "each document is followed by exactly one assay, so none is a "
            f"study-level protocol: {hoisted}"
        )

    def test_a_protocol_used_by_one_assay_nests_under_that_assay(self, tmp_path):
        _, by_id = _build(self._two_assays_one_line_each(), tmp_path)
        a1 = _ids(by_id["#Assay_assay_1"].get("hasPart"))
        a2 = _ids(by_id["#Assay_assay_2"].get("hasPart"))
        assert any("SK-N-AS" in str(p) for p in a1), (
            f"assay_1 follows the SK-N-AS document: {a1}"
        )
        assert any("H4" in str(p) for p in a2), f"assay_2 follows the H4 document: {a2}"

    def test_a_shared_protocol_is_still_study_level(self, tmp_path):
        """The other half: reuse across assays is what makes it study-level."""
        state = self._two_assays_one_line_each()
        state.get_entity("proc_2").set_fields_from_dict(
            {"cell_line": "cell_a"}, source="llm"
        )
        _, by_id = _build(state, tmp_path)
        study_parts = _ids(by_id["#Study_study_1"].get("hasPart"))
        assert any("SK-N-AS" in str(p) for p in study_parts), (
            f"both assays grow SK-N-AS, so its document is study-level: {study_parts}"
        )
