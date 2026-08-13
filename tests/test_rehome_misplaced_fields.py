"""A documented field written on the wrong entity is moved, not deleted.

A real build printed this at exit, twelve times over three assays::

    · Dropped 'detection_instrument' from assay_deiodinase_activity_assay
      (not a term in the crate's JSON-LD context)

and the report then raised "Say which measurement technique was used for
Deiodinase activity assay" — a finding about information the crate had been given
and had thrown away.

The drop rule is right for a key the model invented. These four are declared on
``LabProcess`` in ``ENTITY_DRAFT_SCHEMA``, consumed by ``_build_process``, and
requested by name in ``tools_spec``. They landed on the Assay, which declares
only name/description/identifier, so nothing read them.
"""

from __future__ import annotations

import json

import pytest

from builder.state import CrateState
from builder.tools.builder import assemble_crate
from builder.tools.drafters import (
    draft_assay,
    draft_investigation,
    draft_process,
    draft_study,
)
from builder.tools.rehome import rehome_misplaced_fields

_READOUT_FIELDS = (
    "detection_instrument",
    "instrument_manufacturer",
    "measured_entity",
    "technical_replicate",
)


def _assay_with_chain(state: CrateState, *steps: str):
    """An Assay with a process chain, in the order the pipeline builds it."""
    investigation = draft_investigation(state, {"name": "Investigation"})
    study = draft_study(state, investigation.entity_id, {"name": "Thyroid study"})
    assay = draft_assay(state, study.entity_id, {"name": "Deiodinase activity assay"})
    for step in steps:
        draft_process(state, assay.entity_id, step, {"name": f"{step} step"})
    return assay


def _step(state: CrateState, process_type: str):
    return next(
        e
        for e in state.list_entities("LabProcess")
        if str(e.fields.get("process_type")) == process_type
    )


class TestTheReportedCase:
    def test_the_four_fields_reach_the_process_that_consumes_them(self) -> None:
        state = CrateState()
        # EndpointReadout deliberately NOT first: the chain is CellCulture ->
        # Exposure -> EndpointReadout, so a router that took "the first process"
        # would file the instrument under the cell culture.
        assay = _assay_with_chain(state, "CellCulture", "Exposure", "EndpointReadout")
        assay.set_fields_from_dict(
            dict(zip(_READOUT_FIELDS, ("Wallac 1470", "PerkinElmer", "Radioactivity", "3"))),
            source="llm",
        )

        moved = rehome_misplaced_fields(state)

        assert {m.field for m in moved} == set(_READOUT_FIELDS)
        assert not [f for f in _READOUT_FIELDS if f in assay.fields]
        readout = _step(state, "EndpointReadout")
        assert readout.fields["instrument_manufacturer"] == "PerkinElmer"
        assert not [f for f in _READOUT_FIELDS if f in _step(state, "CellCulture").fields]

    def test_the_value_survives_into_the_built_crate(self) -> None:
        """The point of the exercise. Previously this string was in state, then
        gone from the graph, with only a log line at exit to say so."""
        state = CrateState()
        assay = _assay_with_chain(state, "CellCulture", "EndpointReadout")
        assay.set_fields_from_dict({"instrument_manufacturer": "PerkinElmer"}, source="llm")

        crate = assemble_crate(state, None, materialize_payload=False, include_all_scanned=False)
        graph = crate.metadata.generate()["@graph"]

        assert any("PerkinElmer" in json.dumps(node) for node in graph)


class TestItMovesOnlyWhatItShould:
    def test_a_field_the_entity_declares_is_untouched(self) -> None:
        """`name` is valid on an Assay. Nothing about it is at risk."""
        state = CrateState()
        assay = _assay_with_chain(state, "EndpointReadout")

        rehome_misplaced_fields(state)

        assert assay.fields["name"] == "Deiodinase activity assay"

    def test_a_field_that_would_not_be_dropped_is_untouched(self) -> None:
        """The trigger is "the build would DELETE this", not "this looks
        misfiled" — a value that round-trips fine is nobody's emergency."""
        from builder.tools.rehome import _would_be_dropped

        assert _would_be_dropped("description") is False
        assert _would_be_dropped("detection_instrument") is True

    def test_an_ambiguous_field_is_left_alone(self) -> None:
        """`name` is declared by thirteen types. There is no single right home,
        so guessing one is worse than leaving it where its author put it."""
        from builder.tools.rehome import _field_owners

        assert len(_field_owners()["name"]) > 1

    def test_nothing_moves_when_there_is_no_process_to_receive_it(self) -> None:
        """An Assay with no EndpointReadout has no home for the value. It stays
        put rather than being attached to an unrelated entity of the right class
        — this rescues values, it does not relocate them somewhere arguable."""
        state = CrateState()
        assay = _assay_with_chain(state, "CellCulture")
        assay.set_fields_from_dict({"detection_instrument": "Wallac 1470"}, source="llm")

        assert rehome_misplaced_fields(state) == []
        assert assay.fields["detection_instrument"] == "Wallac 1470"

    def test_an_existing_value_on_the_target_is_never_overwritten(self) -> None:
        """The process's own value was written deliberately and by something that
        knew where it belonged. A rescue must not clobber it."""
        state = CrateState()
        assay = _assay_with_chain(state, "EndpointReadout")
        _step(state, "EndpointReadout").set_fields_from_dict(
            {"detection_instrument": "the process's own value"}, source="llm"
        )
        assay.set_fields_from_dict({"detection_instrument": "the assay's stray copy"}, source="llm")

        assert rehome_misplaced_fields(state) == []
        assert _step(state, "EndpointReadout").fields["detection_instrument"] == (
            "the process's own value"
        )


class TestOwnershipComesFromTheSchema:
    """This module must never carry its own opinion about who owns a field.

    ``ENTITY_DRAFT_SCHEMA`` has always declared that per type. Restating it here
    would create two tables free to disagree, and the disagreement would show up
    as data silently filed under the wrong entity.
    """

    def test_the_table_follows_the_schema(self, monkeypatch) -> None:
        import builder.tools._crate_mapping as mapping
        from builder.tools.rehome import _field_owners

        assert "a_brand_new_field" not in _field_owners()

        import dataclasses

        schema = dict(mapping.ENTITY_DRAFT_SCHEMA)
        schema["LabProcess"] = dataclasses.replace(
            schema["LabProcess"],
            scalar_fields={
                **schema["LabProcess"].scalar_fields,
                "a_brand_new_field": "EndpointReadout: something new.",
            },
        )
        monkeypatch.setattr(mapping, "ENTITY_DRAFT_SCHEMA", schema)

        assert _field_owners().get("a_brand_new_field") == {"LabProcess"}

    def test_the_step_is_read_from_the_field_description(self) -> None:
        """The schema documents each field as "EndpointReadout: …", so the step
        is already written down beside it and is not restated here."""
        from builder.tools.rehome import _process_type_for

        assert _process_type_for("detection_instrument") == "EndpointReadout"
        assert _process_type_for("duration") == "Exposure"
        assert _process_type_for("name") is None


class TestARescueNeverSinksTheBuild:
    def test_a_raising_rehome_is_caught(self, monkeypatch) -> None:
        """A rescue that breaks the export is worse than the deletion it
        prevents, so the call site swallows anything this module throws."""
        import builder.tools.builder as builder_mod

        def _boom(_state):
            raise RuntimeError("rehome exploded")

        monkeypatch.setattr(builder_mod, "rehome_misplaced_fields", _boom)

        state = CrateState()
        _assay_with_chain(state, "EndpointReadout")
        crate = assemble_crate(state, None, materialize_payload=False, include_all_scanned=False)
        assert crate.metadata.generate()["@graph"]


class TestTheDropIsStillReported:
    """A key nobody declares is still dropped — this PR does not claim to keep
    everything. `work_package` and `project_reference` have no owning type, so
    there is nowhere to route them and they remain a known loss."""

    @pytest.mark.parametrize("field", ["work_package", "project_reference"])
    def test_an_unowned_field_is_not_rehomed(self, field: str) -> None:
        from builder.tools.rehome import _field_owners

        assert field not in _field_owners()


class TestProvenanceSurvivesTheMove:
    """Moving a value does not change who supplied it.

    Stamping the moved field with a "rehomed" source would erase the fact that a
    model wrote it — which is precisely what a D5 audit reads ``_completion`` to
    find out. The value's origin is a fact about the value, not about where it
    currently sits.
    """

    def test_the_original_source_is_carried_across(self) -> None:
        state = CrateState()
        assay = _assay_with_chain(state, "EndpointReadout")
        assay.set_fields_from_dict({"detection_instrument": "Wallac 1470"}, source="user")

        rehome_misplaced_fields(state)

        status = _step(state, "EndpointReadout").get_field_status("detection_instrument")
        assert status is not None
        assert status.source == "user", "a human's answer must not be re-attributed"

    def test_an_unattributed_value_is_treated_as_model_supplied(self) -> None:
        """The conservative fallback: unattributed is assumed to be the model's,
        never promoted to verified."""
        state = CrateState()
        assay = _assay_with_chain(state, "EndpointReadout")
        assay.fields["detection_instrument"] = "Wallac 1470"  # no status recorded

        rehome_misplaced_fields(state)

        status = _step(state, "EndpointReadout").get_field_status("detection_instrument")
        assert status is not None
        assert status.source == "llm"


class TestAnUnownedFieldIsKeptAsAPropertyValue:
    """No entity type declares `work_package`, so there is nowhere to route it —
    but deleting it was still the wrong answer.

    `schema:additionalProperty` takes a PropertyValue, the context defines it,
    and the crate already uses it for ISA characteristics. So the value can be
    kept in a form that validates. A WP number somebody typed deliberately was
    being deleted for being unmodelled rather than for being wrong.
    """

    def _study_with(self, **fields: str):
        state = CrateState()
        investigation = draft_investigation(state, {"name": "Investigation"})
        study = draft_study(
            state, investigation.entity_id, {"name": "Thyroid hormone disruption study"}
        )
        study.set_fields_from_dict(dict(fields), source="user")
        crate = assemble_crate(state, None, materialize_payload=False, include_all_scanned=False)
        return study, crate.metadata.generate()["@graph"]

    def test_the_value_survives_as_a_property_value(self) -> None:
        _study, graph = self._study_with(
            project_reference="NWA 1292.19.272", work_package="WP2.4"
        )
        kept = {
            node.get("name"): node.get("value")
            for node in graph
            if node.get("@type") == "PropertyValue"
        }
        assert kept.get("work package") == "WP2.4"
        assert kept.get("project reference") == "NWA 1292.19.272"

    def test_it_hangs_off_the_entity_it_was_written_on(self) -> None:
        """A preserved value that nothing references is an orphan — kept in the
        file, lost to a reader."""
        _study, graph = self._study_with(work_package="WP2.4")
        study_node = next(
            n for n in graph if "Thyroid hormone disruption" in str(n.get("name", ""))
        )
        referenced = {
            ref.get("@id")
            for ref in (
                study_node["additionalProperty"]
                if isinstance(study_node.get("additionalProperty"), list)
                else [study_node.get("additionalProperty")]
            )
            if isinstance(ref, dict)
        }
        wp = next(n for n in graph if n.get("name") == "work package")
        assert wp["@id"] in referenced

    def test_it_is_not_also_emitted_as_a_bare_key(self) -> None:
        """The reason the drop existed. A bare `work_package` key fails BASE, so
        preserving the value must not reintroduce it."""
        _study, graph = self._study_with(work_package="WP2.4")
        assert all("work_package" not in node for node in graph)

    def test_it_is_a_plain_property_value_not_an_isa_parameter(self) -> None:
        """`ParameterValue` stamps additionalType "ParameterValue", asserting the
        value is an ISA experimental parameter. A work-package number is not one,
        and saying so would invent a claim about the data while rescuing it."""
        _study, graph = self._study_with(work_package="WP2.4")
        wp = next(n for n in graph if n.get("name") == "work package")
        assert wp.get("additionalType") is None

    def test_a_field_that_round_trips_is_not_duplicated(self) -> None:
        """The control. `description` is a real context term and is emitted
        normally, so it must not ALSO appear as a PropertyValue."""
        _study, graph = self._study_with(description="A real description.")
        names = [n.get("name") for n in graph if n.get("@type") == "PropertyValue"]
        assert "description" not in names
