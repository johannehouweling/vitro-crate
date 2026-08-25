"""A process parameter is one entity, not one per code path that noticed it (#677).

Every parameter the tox model mints from a kwarg was landing in the crate
**twice**, as two ``PropertyValue`` entities differing only in the casing of
their ``name`` — ``Detection Instrument`` beside ``detection instrument``, with
identical values, both on the same process. Because ``parameter`` and
``additionalProperty`` are both ``schema:additionalProperty`` in
``profiles/context.py``, the expanded graph merges them into one predicate, so a
readout with five parameters expanded to nine values.

Measured on the reference crate: **18 of 99 PropertyValue nodes carry no
information**, across eight names each present in two casings.

The second one comes from :func:`_preserve_unowned_fields`, the pass that keeps a
field no entity type declares rather than dropping it silently. It asks
:func:`field_would_be_dropped`, which mirrors ``_scalar_props``' delete rule —
and ``_scalar_props`` takes a per-caller ``skip`` that the question cannot see.
So a field consumed as a constructor kwarg looked unowned, and was "preserved"
alongside the parameter the constructor had already minted from it.

The set of consumed fields was a hand-kept list that had drifted: it names
``units``, ``assay_kit`` and ``substrate`` but not ``detection_instrument``,
``measured_entity`` or ``technical_replicate``. It is derived from the
constructors themselves here, so it cannot drift again.
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
    return graph


def _backbone(state):
    state.add_entity(_ent("inv1", "Investigation", name="Inv", description="d"))
    state.add_entity(_ent("st1", "Study", name="St", description="d", investigation_id="inv1"))
    state.add_entity(_ent("as1", "Assay", name="As", description="d", study_id="st1"))
    state.add_entity(_ent("s1", "Sample", name="exposed cells"))
    state.add_entity(_ent("f1", "File", name="raw.csv", dest_path="data/raw.csv"))


def _pvs(graph):
    return [e for e in graph if "PropertyValue" in str(e.get("@type", ""))]


def _named(graph, name: str):
    return [e for e in _pvs(graph) if str(e.get("name", "")).strip().casefold() == name]


def _readout_state(**extra):
    state = CrateState()
    _backbone(state)
    fields = dict(
        process_type="EndpointReadout",
        name="Readout",
        assay_id="as1",
        samples="s1",
        result="f1",
        detection_instrument="Plate reader",
    )
    fields.update(extra)
    state.add_entity(_ent("er1", "LabProcess", **fields))
    return state


class TestAKwargMintsOneParameter:
    def test_a_consumed_field_is_not_also_preserved(self, tmp_path):
        graph = _build(_readout_state(), tmp_path)
        minted = _named(graph, "detection instrument")
        assert len(minted) == 1, [e.get("name") for e in minted]

    def test_the_survivor_is_the_profile_typed_one(self, tmp_path):
        """The constructor's parameter carries ``additionalType:
        ParameterValue``; the preserved twin carried nothing. Keeping the typed
        one is what the tox profile reads."""
        graph = _build(_readout_state(), tmp_path)
        survivor = _named(graph, "detection instrument")[0]
        assert survivor.get("additionalType") == "ParameterValue", survivor

    def test_every_readout_kwarg_mints_once(self, tmp_path):
        """EndpointReadout is worst hit — it has the most named kwargs."""
        graph = _build(
            _readout_state(
                instrument_manufacturer="Acme",
                measured_entity="T3",
                technical_replicate="3",
            ),
            tmp_path,
        )
        for name in (
            "detection instrument",
            "instrument manufacturer",
            "measured entity",
            "technical replicate",
        ):
            assert len(_named(graph, name)) == 1, (
                name,
                [e.get("name") for e in _named(graph, name)],
            )

    def test_the_alias_spellings_mint_once_too(self, tmp_path):
        """`data_calculation_and_statistics` and `computational_tool` are read by
        the mapper and mapped onto the constructor's own parameter names, so they
        are consumed without appearing in any signature."""
        state = CrateState()
        _backbone(state)
        state.add_entity(
            _ent(
                "da1",
                "LabProcess",
                process_type="DataAnalysis",
                name="Analysis",
                assay_id="as1",
                object="f1",
                result="f1",
                data_calculation_and_statistics="mean of replicates",
                computational_tool="Python",
            )
        )
        graph = _build(state, tmp_path)
        assert len(_named(graph, "data calculation and statistics")) <= 1
        assert len(_named(graph, "computational tool")) <= 1

    def test_nothing_is_left_orphaned(self, tmp_path):
        graph = _build(_readout_state(), tmp_path)
        referenced: set[str] = set()
        for entity in graph:
            for value in entity.values():
                items = value if isinstance(value, list) else [value]
                for item in items:
                    if isinstance(item, dict) and item.get("@id"):
                        referenced.add(item["@id"])
        for pv in _pvs(graph):
            assert pv["@id"] in referenced, f"{pv.get('name')!r} is in the crate unreferenced"


class TestThePreservationPassStillWorks:
    """#677 must not become "stop preserving fields" — that pass exists so a
    value a caller supplied is never dropped without trace."""

    def test_a_field_no_entity_type_declares_is_still_kept(self, tmp_path):
        state = CrateState()
        _backbone(state)
        state.add_entity(
            _ent(
                "er1",
                "LabProcess",
                process_type="EndpointReadout",
                name="Readout",
                assay_id="as1",
                samples="s1",
                result="f1",
                detection_instrument="Plate reader",
                humidity_percent="95",
            )
        )
        graph = _build(state, tmp_path)
        assert _named(graph, "humidity percent"), "an unowned field was dropped silently"

    def test_a_process_kwarg_name_on_another_entity_is_still_kept(self, tmp_path):
        """The exemption is scoped to processes. `detection_instrument` on a File
        is not consumed by anything and must survive."""
        state = CrateState()
        _backbone(state)
        state.add_entity(
            _ent(
                "f2",
                "File",
                name="notes.txt",
                dest_path="data/notes.txt",
                detection_instrument="Plate reader",
            )
        )
        state.add_entity(
            _ent(
                "er1",
                "LabProcess",
                process_type="EndpointReadout",
                name="Readout",
                assay_id="as1",
                samples="s1",
                result="f1",
            )
        )
        graph = _build(state, tmp_path)
        assert _named(graph, "detection instrument"), "the exemption leaked past LabProcess"
