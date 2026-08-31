"""Every tox process shape that declares `schema:object sh:minCount 1` must be
satisfiable by the build alone (2026-08-25 tox model/shape audit).

The shapes at profiles/shapes/tox/2_lab_process_cell_culture.ttl:34,
3_lab_process_exposure.ttl:41 and 4_lab_process_endpoint_readout.ttl:64 all
declare a REQUIRED schema:object. A build that can emit a process with none of
them produces a crate that fails the profile the tool asserts end to end.

Each test perturbs a minimal state one way and asserts the tox verdict, so a
failure names the perturbation rather than the fixture.
"""

from __future__ import annotations

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.validation import build_and_validate


def _state_with(entity_id: str, fields: dict) -> CrateState:
    """A CrateState holding exactly one LabProcess, with nothing else to link to."""
    state = CrateState()
    state.metadata.title = "Floor probe"
    state.add_entity(
        Entity(
            entity_id=entity_id,
            type="LabProcess",
            fields=fields,
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def _tox_issues(state: CrateState, prop: str) -> list[dict]:
    """REQUIRED tox issues on *prop*, e.g. 'http://schema.org/object'."""
    result = build_and_validate(state, severity="required", profile="tox")
    return [i for i in result["issues"] if i.get("property") == prop]


class TestExposureInputFloor:
    """An Exposure with no cell culture still declares what it exposed (#650)."""

    def test_exposure_with_no_resolvable_cells_still_has_an_object(self):
        state = _state_with(
            "proc_exp",
            {"process_type": "Exposure", "name": "Amiodarone exposure"},
        )

        assert _tox_issues(state, "http://schema.org/object") == []

    def test_the_synthesized_input_is_a_sample_not_an_empty_list(self):
        """The floor must add a node, not merely silence the count.

        Guards the lazy fix: emitting `"input": [""]` or dropping the key would
        also clear the minCount in some validators but says nothing about what
        was exposed.
        """
        from builder.tools.builder import assemble_crate

        state = _state_with(
            "proc_exp",
            {"process_type": "Exposure", "name": "Amiodarone exposure"},
        )
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        node = next(
            e
            for e in crate.get_entities()
            if e.properties().get("additionalType") == "Exposure"
        )
        objects = node.properties().get("input")
        assert objects, "Exposure emitted no schema:object at all"
        assert not isinstance(objects, str)


class TestEndpointReadoutInputFloor:
    """A readout says what it measured, even with no exposure in its assay.

    `tox:EndpointReadoutConsumesExposedMaterial` landed 2026-08-24 (282de2d).
    `_chain_processes` rewires a readout's input only when the same assay has an
    Exposure that produced a Sample, so the characterisation case the shape's own
    comment calls legitimate is the one that fails.
    """

    def test_readout_with_no_exposure_in_its_assay_still_has_an_object(self):
        state = _state_with(
            "proc_ro",
            {
                "process_type": "EndpointReadout",
                "name": "Viability readout",
                "endpoint": "Cell viability",
                "measured_entity": "ATP",
            },
        )

        assert _tox_issues(state, "http://schema.org/object") == []

    def test_the_crate_never_serializes_a_null_input(self):
        """`"input": null` is JSON the reader has to defend against.

        The null is not what fails the shape — deleting the key fails it
        identically — but the floor must leave a node behind, not a null.
        """
        from builder.tools.builder import assemble_crate

        state = _state_with(
            "proc_ro",
            {"process_type": "EndpointReadout", "name": "Viability readout"},
        )
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)
        node = next(
            e
            for e in crate.get_entities()
            if e.properties().get("additionalType") == "EndpointReadout"
        )
        assert node.properties().get("input") is not None

    def test_the_floor_never_displaces_the_exposed_sample(self):
        """The placeholder is a fallback, not a competitor (#650, #678).

        `_chain_processes` skips a readout whose consumed set is not a subset of
        the cultured samples — one naming its own material knows better than we
        do. A floor applied while the process is BUILT makes that set the
        placeholder, so the rescue skips the readout and it measures a
        synthesized node instead of the sample the exposure produced. The star
        graph #650 removed comes straight back, and nothing consumes the exposed
        sample at all.

        Invisible to the rest of the suite: `draft_process_chain` always supplies
        `object`, so the floor never fires anywhere else and the existing
        chain assertions stay green either way.
        """
        from builder.state import FileClassification
        from builder.tools.builder import assemble_crate
        from builder.tools.composites import draft_process_chain, scaffold_isa_backbone

        state = CrateState()
        state.metadata.title = "Chain floor probe"
        state.metadata.input_path = "/deposit"
        state.scanned_files = [
            FileClassification(
                path="/deposit/assay/raw data/plate.csv",
                filename="plate.csv",
                size=4096,
                mime_type="text/csv",
            ),
        ]
        ids = scaffold_isa_backbone(
            state,
            investigation={"name": "Inv", "description": "d", "identifier": "INV-1"},
            study={"name": "Study", "description": "d"},
            assay={"name": "Assay", "description": "d"},
        )
        draft_process_chain(
            state,
            ids["assay_id"],
            chain=[
                {"process_type": "CellCulture", "hints": {"name": "Seed"}},
                {"process_type": "Exposure", "hints": {"name": "Dose", "duration": "24 h"}},
                {
                    "process_type": "EndpointReadout",
                    "hints": {"name": "Read", "detection_instrument": "Plate reader"},
                },
            ],
        )
        # The case the floor targets: a readout the drafter left with no input.
        readout_draft = next(
            e
            for e in state.list_entities("LabProcess")
            if e.fields.get("process_type") == "EndpointReadout"
        )
        for key in ("samples", "object", "input"):
            readout_draft.fields.pop(key, None)

        graph = assemble_crate(
            state, output_dir=None, materialize_payload=False, include_all_scanned=False
        ).metadata.generate()["@graph"]

        def _ids(value):
            items = value if isinstance(value, list) else [value]
            return {i.get("@id") for i in items if isinstance(i, dict)}

        readout = next(n for n in graph if n.get("additionalType") == "EndpointReadout")
        exposure = next(n for n in graph if n.get("additionalType") == "Exposure")
        consumed = _ids(readout.get("input")) | _ids(readout.get("object"))
        exposed = _ids(exposure.get("output")) | _ids(exposure.get("result"))
        assert consumed & exposed, (
            "the floor displaced the exposed sample; the readout consumes "
            f"{consumed} while the exposure produced {exposed}"
        )


class TestInputRequiredTypesIsJustifiedByTheBuildNotTheShapes:
    """`_INPUT_REQUIRED_TYPES` excludes three types the shapes DO constrain.

    Its comment claimed only DataAnalysis declares a REQUIRED `schema:object`.
    Four shapes declare it. The exclusion is correct anyway, because the build
    floors the other three — this pins that reason so the set cannot be narrowed
    back on the false premise.
    """

    _SHAPES_DECLARING_REQUIRED_OBJECT = {
        "CellCulture": ("2_lab_process_cell_culture.ttl", "LabProcessCellCulture"),
        "Exposure": ("3_lab_process_exposure.ttl", "LabProcessExposure"),
        "EndpointReadout": (
            "4_lab_process_endpoint_readout.ttl",
            "LabProcessEndpointReadout",
        ),
        "DataAnalysis": ("5_lab_process_data_analysis.ttl", "LabProcessDataAnalysis"),
    }

    def test_every_named_shape_declares_a_required_object_at_violation_severity(self):
        """Read the shapes as RDF, not as text — including this one.

        Asserting that "schema:object", "sh:minCount 1" and "sh:Violation" each
        appear SOMEWHERE in a file passes on a file where they sit in three
        unrelated property shapes, which is most of them. The claim being pinned
        is that ONE property shape on the process's own target class carries all
        three, so that is what is queried.
        """
        from pathlib import Path

        from rdflib import Graph, Namespace
        from rdflib.namespace import SH

        schema = Namespace("http://schema.org/")
        tox = Namespace("https://w3id.org/ro/crate/isa-tox/1.0/")
        shapes_dir = Path("profiles/shapes/tox")

        unconstrained = {}
        for ptype, (filename, target) in self._SHAPES_DECLARING_REQUIRED_OBJECT.items():
            graph = Graph().parse(shapes_dir / filename, format="turtle")
            floors = [
                prop
                for shape in graph.subjects(SH.targetClass, tox[target])
                for prop in graph.objects(shape, SH.property)
                if (prop, SH.path, schema.object) in graph
                and (prop, SH.minCount, None) in graph
                and (prop, SH.severity, SH.Violation) in graph
            ]
            if not floors:
                unconstrained[ptype] = filename

        assert not unconstrained, (
            "these shapes no longer declare a REQUIRED schema:object at Violation "
            f"severity on their own target class: {unconstrained}"
        )

    def test_the_three_excluded_types_are_floored_by_the_build(self):
        """The actual justification for excluding them from repair.

        Each builds an object with no state to link to, so the REQUIRED
        violation the repair rule exists to fix cannot occur for them. Collected
        rather than asserted per type: the loop used to abort at the first
        failure, so a red run could only ever name one of the three.
        """
        from builder.tools.repair import _INPUT_REQUIRED_TYPES

        unfloored = {}
        for ptype in ("CellCulture", "Exposure", "EndpointReadout"):
            assert ptype not in _INPUT_REQUIRED_TYPES
            state = _state_with("proc", {"process_type": ptype, "name": f"{ptype} step"})
            issues = _tox_issues(state, "http://schema.org/object")
            if issues:
                unfloored[ptype] = [i["message"] for i in issues]

        assert not unfloored, f"unfloored by the build: {unfloored}"


class TestPlaceholderRescueDoesNotDefeatTheD5Loop:
    """A value `_pv` refused must not reappear under the same predicate.

    `_pv` drops a placeholder so the shape's "MUST have at least one
    additionalProperty" fires as the prompt to go and fill it in.
    `_preserve_unowned_fields` filtered only empties, so a field it rescued
    re-attached a placeholder under schema:additionalProperty — the predicate the
    shape counts, since `profiles/context.py` maps `parameter` there too. A
    CellCulture whose only stated parameter was "not recorded" satisfied the MUST.

    #677 closed this for fields a process constructor consumes, `culture_medium`
    among them, which is why the medium case below passes unaided and stands as a
    regression guard rather than a red test. Every other dropped field still
    reached the graph.
    """

    @staticmethod
    def _culture_state(medium: str, work_package: str | None = None) -> CrateState:
        state = CrateState()
        state.metadata.title = "Placeholder probe"
        fields = {
            "process_type": "CellCulture",
            "name": "Culture step",
            "culture_medium": medium,
        }
        if work_package is not None:
            fields["work_package"] = work_package
        state.add_entity(
            Entity(
                entity_id="proc_cc",
                type="LabProcess",
                fields=fields,
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
        return state

    def test_a_placeholder_medium_leaves_the_shape_free_to_fire(self):
        """The whole point: an unstated medium must be VISIBLE as unstated."""
        state = self._culture_state("unknown")

        issues = _tox_issues(state, "http://schema.org/additionalProperty")

        assert issues, (
            "A CellCulture whose only parameter is 'unknown' passed the "
            "additionalProperty MUST — the placeholder was re-published"
        )

    def test_a_real_medium_still_satisfies_the_shape(self):
        """Guards the over-correction: only placeholders are suppressed."""
        state = self._culture_state("DMEM + 10% FBS")

        assert _tox_issues(state, "http://schema.org/additionalProperty") == []

    def test_an_unmodelled_field_is_still_rescued_when_it_says_something(self):
        """The rescue's actual purpose survives: keep a real unmodelled value.

        `work_package` is the field the rescue was written for — a WP number
        somebody typed deliberately, deleted for being unmodelled.
        """
        from builder.tools.builder import assemble_crate

        state = self._culture_state("DMEM + 10% FBS", work_package="WP4")
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)

        values = [
            e.properties().get("value")
            for e in crate.get_entities()
            if e.properties().get("@type") == "PropertyValue"
        ]
        assert "WP4" in values

    def test_an_unmodelled_field_holding_a_placeholder_is_not_rescued(self):
        """Same rule, applied to the rescue's own kind of field."""
        from builder.tools.builder import assemble_crate

        state = self._culture_state("DMEM + 10% FBS", work_package="not recorded")
        crate = assemble_crate(state, output_dir=None, materialize_payload=False)

        values = [
            e.properties().get("value")
            for e in crate.get_entities()
            if e.properties().get("@type") == "PropertyValue"
        ]
        assert "not recorded" not in values
