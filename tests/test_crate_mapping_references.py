"""Build-path reference wiring: root funder/about + assay aliases (#180 Lane C).

The deterministic build path (`populate_crate`) resolves references the build
previously dropped, so they round-trip into the crate exactly as the gold crate
(`crates_out/S-VHPS21_rocrate/ro-crate-metadata.json`) emits them:

* Root (the folded Investigation) ``funder`` -> Organization reference(s).
* Root ``about`` -> the DataAnalysis LabProcess (mirrors the Assay
  ``about``->LabProcess wiring, but for the Investigation/root).
* Assay ``measurementMethod`` -> a BAO ``DefinedTerm`` reference.
* Assay ``dataFiles`` / ``resources`` -> the assay's data / resource File refs
  (both expand to schema:hasPart, so the files also stay reachable via hasPart).
* Study/root ``assays`` -> reverse alias to the Assay children.

None of these are fabricated (D5): every reference is resolved from a field
already present on the source entity in state. All assertions read the assembled
JSON-LD graph; no network and no SHACL (no ``build_and_validate``) so the module
is fast.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from profiles.context import ISA_TOX_CONTEXT


def _ent(entity_id, type_, **fields):
    return Entity(
        entity_id=entity_id,
        type=type_,
        fields=fields,
        _provenance=EntityProvenance(created_by="llm"),
    )


def _graph(state: CrateState) -> list[dict]:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(state, crate, None, materialize_payload=False, include_all_scanned=False)
    return crate.metadata.generate()["@graph"]


def _by_id(graph: list[dict], node_id: str) -> dict | None:
    return next((n for n in graph if n.get("@id") == node_id), None)


def _ids(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for it in items:
        rid = it.get("@id") if isinstance(it, dict) else it
        if isinstance(rid, str):
            out.append(rid)
    return out


class TestRootFunder:
    def _state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Funded investigation"
        state.add_entity(
            _ent("org_zon", "Organization", name="ZonMw", ror="0254y9b08")
        )
        state.add_entity(_ent("inv_1", "Investigation", name="Inv", funder="org_zon"))
        return state

    def test_root_funder_references_organization(self):
        graph = _graph(self._state())
        root = _by_id(graph, "./")
        assert root is not None
        assert _ids(root.get("funder")) == ["https://ror.org/0254y9b08"], (
            "root funder must be an {@id} reference to the Organization (gold shape)"
        )

    def test_funder_iri_reference_kept(self):
        """A bare ROR IRI funder (no in-crate Organization) is wired as {@id}."""
        state = CrateState()
        state.metadata.title = "Funded investigation"
        state.add_entity(
            _ent("inv_1", "Investigation", name="Inv", funder="https://ror.org/0254y9b08")
        )
        graph = _graph(state)
        root = _by_id(graph, "./")
        assert root is not None
        assert _ids(root.get("funder")) == ["https://ror.org/0254y9b08"]

    def test_no_funder_when_absent(self):
        state = CrateState()
        state.metadata.title = "Unfunded"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        graph = _graph(state)
        root = _by_id(graph, "./")
        assert root is not None
        assert "funder" not in root, "no fabricated funder when absent (D5)"


class TestRootAbout:
    def _state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Reported investigation"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv", about="report"))
        state.add_entity(
            _ent(
                "report",
                "LabProcess",
                process_type="DataAnalysis",
                name="Crate quality report",
                software="vitro-crate",
            )
        )
        return state

    def test_root_about_references_dataanalysis_labprocess(self):
        graph = _graph(self._state())
        root = _by_id(graph, "./")
        assert root is not None
        assert "#LabProcess_report" in _ids(root.get("about")), (
            "root about must reference the DataAnalysis LabProcess node"
        )
        report = _by_id(graph, "#LabProcess_report")
        assert report is not None
        assert report.get("additionalType") == "DataAnalysis"


class TestAssayMeasurementMethod:
    def test_measurement_method_references_defined_term(self):
        # The BAO DefinedTerm's @id IS its resolvable IRI (gold crate shape:
        # _mint_id returns an IRI entity_id verbatim), and the assay references it.
        state = CrateState()
        state.metadata.title = "Assay crate"
        state.add_entity(
            _ent(
                "http://www.bioassayontology.org/bao#BAO_0010196",
                "DefinedTerm",
                name="uptake transporter inhibition assay",
                term_code="BAO_0010196",
            )
        )
        state.add_entity(
            _ent(
                "assay_1",
                "Assay",
                name="Uptake",
                measurementMethod="http://www.bioassayontology.org/bao#BAO_0010196",
            )
        )
        graph = _graph(state)
        assay = _by_id(graph, "#Assay_assay_1")
        assert assay is not None
        assert assay.get("measurementMethod") == {
            "@id": "http://www.bioassayontology.org/bao#BAO_0010196"
        }, "measurementMethod must be an {@id} reference to the BAO DefinedTerm"

    def test_measurement_method_iri_kept(self):
        state = CrateState()
        state.metadata.title = "Assay crate"
        state.add_entity(
            _ent(
                "assay_1",
                "Assay",
                name="Uptake",
                measurementMethod="http://www.bioassayontology.org/bao#BAO_0010196",
            )
        )
        graph = _graph(state)
        assay = _by_id(graph, "#Assay_assay_1")
        assert assay is not None
        assert assay.get("measurementMethod") == {
            "@id": "http://www.bioassayontology.org/bao#BAO_0010196"
        }

    def test_no_raw_measurement_method_literal(self):
        """measurementMethod must never leak onto the node as a bare string."""
        state = CrateState()
        state.metadata.title = "Assay crate"
        state.add_entity(_ent("assay_1", "Assay", name="Uptake", measurementMethod="bao"))
        # No DefinedTerm named "bao" exists -> unresolvable, non-IRI -> not emitted.
        graph = _graph(state)
        assay = _by_id(graph, "#Assay_assay_1")
        assert assay is not None
        assert assay.get("measurementMethod") != "bao"


class TestAssayDataFilesAndResources:
    def _state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Assay file crate"
        state.add_entity(
            _ent("f_data", "File", name="raw.prism", dest_path="assays/a/dataset/raw.prism")
        )
        state.add_entity(
            _ent("f_res", "File", name="README.txt", dest_path="assays/a/resources/README.txt")
        )
        state.add_entity(
            _ent(
                "assay_1",
                "Assay",
                name="Uptake",
                dataFiles=["f_data"],
                resources=["f_res"],
            )
        )
        return state

    def test_data_files_referenced_under_assay(self):
        graph = _graph(self._state())
        assay = _by_id(graph, "#Assay_assay_1")
        assert assay is not None
        assert _ids(assay.get("dataFiles")) == ["assays/a/dataset/raw.prism"]

    def test_resources_referenced_under_assay(self):
        graph = _graph(self._state())
        assay = _by_id(graph, "#Assay_assay_1")
        assert assay is not None
        assert _ids(assay.get("resources")) == ["assays/a/resources/README.txt"]

    def test_data_and_resource_files_stay_reachable_via_haspart(self):
        """dataFiles/resources expand to schema:hasPart, so the Files stay
        reachable from the assay and not dumped loose on the root."""
        graph = _graph(self._state())
        assay = _by_id(graph, "#Assay_assay_1")
        root = _by_id(graph, "./")
        assert assay is not None and root is not None
        assay_parts = _ids(assay.get("hasPart")) + _ids(assay.get("dataFiles")) + _ids(
            assay.get("resources")
        )
        assert "assays/a/dataset/raw.prism" in assay_parts
        assert "assays/a/resources/README.txt" in assay_parts
        # …and still listed by the root, so the file tree reaches them (#532)
        assert "assays/a/dataset/raw.prism" in _ids(root.get("hasPart"))
        assert "assays/a/resources/README.txt" in _ids(root.get("hasPart"))


class TestAssaysReverseAlias:
    def test_study_assays_references_child_assays(self):
        state = CrateState()
        state.metadata.title = "Study/assay crate"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        graph = _graph(state)
        study = _by_id(graph, "#Study_study_1")
        assert study is not None
        assert "#Assay_assay_1" in _ids(study.get("assays")) + _ids(study.get("hasPart"))

    def test_root_assays_references_child_assays_without_study(self):
        state = CrateState()
        state.metadata.title = "Root/assay crate"
        state.add_entity(_ent("inv_1", "Investigation", name="Inv"))
        state.add_entity(_ent("assay_1", "Assay", name="A"))
        graph = _graph(state)
        root = _by_id(graph, "./")
        assert root is not None
        assert "#Assay_assay_1" in _ids(root.get("assays")) + _ids(root.get("hasPart"))


class TestProcessAdditionalProperty:
    """A LabProcess's additionalProperty references resolve to in-state
    PropertyValue nodes (gold #report_analysis -> [#pv_repro_score])."""

    def _state(self) -> CrateState:
        state = CrateState()
        state.metadata.title = "Analysis crate"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        state.add_entity(
            _ent(
                "pv_repro",
                "PropertyValue",
                name="reproducibility score",
                value=100,
                unitText="percent",
            )
        )
        state.add_entity(
            _ent(
                "report",
                "LabProcess",
                name="Crate quality & reproducibility report",
                process_type="DataAnalysis",
                assay_id="assay_1",
                additionalProperty="pv_repro",
            )
        )
        return state

    def test_dataanalysis_additional_property_references_property_value(self):
        graph = _graph(self._state())
        proc = _by_id(graph, "#LabProcess_report")
        assert proc is not None, "DataAnalysis LabProcess node should exist"
        pv = _by_id(graph, "#PropertyValue_pv_repro")
        assert pv is not None, "the PropertyValue must round-trip into the graph"
        assert pv["@id"] in _ids(proc.get("additionalProperty")), (
            "DataAnalysis additionalProperty must reference the in-state PropertyValue"
        )

    def test_no_additional_property_fabricated_when_unresolvable(self):
        """An additionalProperty pointing at no in-state entity (and no IRI) is
        dropped, never fabricated (D5)."""
        state = CrateState()
        state.metadata.title = "Analysis crate"
        state.add_entity(_ent("study_1", "Study", name="S"))
        state.add_entity(_ent("assay_1", "Assay", name="A", study_id="study_1"))
        state.add_entity(
            _ent(
                "report",
                "LabProcess",
                name="Report",
                process_type="DataAnalysis",
                assay_id="assay_1",
                additionalProperty="does_not_exist",
            )
        )
        graph = _graph(state)
        proc = _by_id(graph, "#LabProcess_report")
        assert proc is not None
        assert _ids(proc.get("additionalProperty")) == []


class TestProtocolFileTyping:
    """#646: a protocol document is typed as one, so it stops drawing as data.

    The scan already decides this. ``classify_file`` stamps every deposit file
    with one of ``FILE_CLASSES``, and it recognises the protocol documents in
    these deposits — "4.1 Deiodinase activity assay.docx", "3.3 Bradford
    protocol.xlsx" — from their names. The crate then dropped that answer:
    the file went in as a plain ``File`` while a separate, fileless
    ``#LabProtocol_...`` stub carried the description, so the graph view painted
    the document as data and a reader following ``executesLabProtocol`` arrived
    at something they could not open.

    Co-typing is the same mechanism a script already uses (``["File",
    "SoftwareSourceCode"]``), and ``LabProtocol`` resolves to
    ``https://bioschemas.org/LabProtocol`` through ``profiles/context.py``, so
    what lands in the crate is the bioschemas type rather than a local string.
    """

    @staticmethod
    def _scanned(path: str, classification: str | None):
        from builder.state import FileClassification

        return FileClassification(
            path=path,
            filename=Path(path).name,
            size=1024,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            classification=classification,
        )

    def _state_with(self, path: str, classification: str | None) -> CrateState:
        """One drafted file, with the scan's record of it.

        The record names the file as the crate ends up carrying it — the scan and
        the builder read one deposit, so a fixture where they disagree would be
        testing a situation that cannot arise.
        """
        from builder.tools.provenance import draft_file

        state = CrateState()
        state.metadata.title = "Protocol crate"
        draft_file(state, path)
        from builder.tools._crate_mapping import _file_dest

        dest = _file_dest(state.list_entities("File")[0])
        state.scanned_files = [self._scanned(dest, classification)]
        return state

    def _node(self, state: CrateState, path: str) -> dict:
        """The one File the state drafts, however the builder named it.

        `draft_file` slugifies a bare filename into ``data/`` when no deposit is
        mounted, and @ids are percent-encoded besides, so the node is found by
        being the drafted File rather than by string-matching the input.
        """
        files = [
            n
            for n in _graph(state)
            if "File" in TestProtocolFileTyping._types(n)
            and n.get("@id") != "ro-crate-metadata.json"
        ]
        assert len(files) == 1, f"expected one drafted File, got {[f['@id'] for f in files]}"
        assert Path(unquote(files[0]["@id"])).stem == Path(path).stem.replace(" ", "_")
        return files[0]

    @staticmethod
    def _types(node: dict) -> list[str]:
        """``@type`` as a list, whether the node carries one or several."""
        value = node.get("@type")
        items = value if isinstance(value, list) else [value]
        return [str(item) for item in items if item is not None]

    def test_a_scanned_protocol_document_is_co_typed_as_a_labprotocol(self) -> None:
        from builder.tools.document_discovery import CLASS_PROTOCOL

        node = self._node(
            self._state_with("4.1 Deiodinase activity assay.docx", CLASS_PROTOCOL),
            "4.1 Deiodinase activity assay.docx",
        )

        assert self._types(node) == ["File", "LabProtocol"]

    def test_the_classifier_itself_decides_which_files_those_are(self) -> None:
        """Pinned to the classifier rather than to a filename literal here: the
        rule is "what the scan called a protocol", and a test that re-implemented
        the naming heuristics would pass while the two drifted apart."""
        from builder.tools.document_discovery import CLASS_PROTOCOL, classify_file

        name = "3.3 Bradford protocol.xlsx"
        assert classify_file(name, "", name)[0] == CLASS_PROTOCOL

        node = self._node(self._state_with(name, CLASS_PROTOCOL), name)
        assert "LabProtocol" in self._types(node)

    def test_a_file_the_scan_called_something_else_stays_a_plain_file(self) -> None:
        """The honesty control: co-typing everything would make the type mean
        nothing. Raw data is not a protocol."""
        from builder.tools.document_discovery import CLASS_RAW_DATA

        node = self._node(self._state_with("004043.csv", CLASS_RAW_DATA), "004043.csv")

        assert node.get("@type") == "File"

    def test_a_file_the_scan_never_classified_stays_a_plain_file(self) -> None:
        """A session saved before the scan stamped classifications, or a file
        drafted without one: absence of evidence is not a protocol."""
        node = self._node(self._state_with("mystery.docx", None), "mystery.docx")

        assert node.get("@type") == "File"

    def test_co_typing_composes_with_the_types_the_draft_already_asked_for(self) -> None:
        """A draft that already names extra types keeps them, File stays first,
        and nothing is repeated."""
        from builder.tools.document_discovery import CLASS_PROTOCOL
        from builder.tools.provenance import draft_file

        state = CrateState()
        state.metadata.title = "Protocol crate"
        draft_file(state, "run.py", additional_types=["SoftwareSourceCode"])
        state.scanned_files = [self._scanned("run.py", CLASS_PROTOCOL)]

        types = self._types(self._node(state, "run.py"))
        assert types[0] == "File"
        assert set(types) == {"File", "SoftwareSourceCode", "LabProtocol"}
        assert len(types) == len(set(types))

    def test_the_graph_view_now_draws_it_as_a_protocol(self) -> None:
        """The point of the change: the report's categoriser reads ``@type``, and
        it checks LabProtocol before File, so the document leaves the data
        category and is drawn — and coloured — as the protocol it is."""
        from builder.tools.document_discovery import CLASS_PROTOCOL
        from builder.writers.provenance_dag import build_crate_graph

        name = "20251114_cell culture protocol H4.docx"
        state = self._state_with(name, CLASS_PROTOCOL)
        drawn_id = self._node(state, name)["@id"]
        model = build_crate_graph({"@graph": _graph(state)}, layer="all", all_edges=True)
        node = next(n for n in model["nodes"] if str(n["id"]) == drawn_id)

        assert node["category"] == "protocol"
        # The control: the same document, had the scan called it raw data,
        # draws as data — so the classification is what moved it, and this test
        # fails for the right reason if the co-typing is removed.
        from builder.tools.document_discovery import CLASS_RAW_DATA

        plain = build_crate_graph(
            {"@graph": _graph(self._state_with(name, CLASS_RAW_DATA))},
            layer="all",
            all_edges=True,
        )
        assert next(n for n in plain["nodes"] if str(n["id"]) == drawn_id)["category"] == "data"


class TestProtocolTypingAgainstTheShapes:
    """Co-typing a document must not cost the crate its conformance (#646).

    The ISA profile's ``FindISAProtocols`` is a two-stage rule: it targets
    ``bioschemas:LabProtocol`` but only infers ``isa-ro-crate:Protocol`` for a
    node some process reaches through ``executesLabProtocol``. Only then do the
    ``ProtocolShouldHave{Name,Description,IntendedUse}`` shapes apply.

    So the co-typing is inert to SHACL as long as nothing points a process at
    the document — which is today's crate, where processes still execute the
    separate fileless stub. This pins that, because the shapes are a dependency
    nobody here controls: a profile release that dropped the ``sh:condition``
    would start reporting every protocol document in every crate, and the
    failure would surface as a mysterious conformance drop rather than as this
    test.
    """

    def _issues(self, classification: str | None) -> list[str]:
        from builder.state import FileClassification
        from builder.tools._crate_mapping import _file_dest
        from builder.tools.provenance import draft_file
        from builder.tools.validation import build_and_validate

        state = CrateState()
        state.metadata.title = "Protocol crate"
        draft_file(state, "cell culture protocol H4.docx")
        dest = _file_dest(state.list_entities("File")[0])
        state.scanned_files = [
            FileClassification(
                path=dest,
                filename=Path(dest).name,
                size=10,
                mime_type="application/msword",
                classification=classification,
            )
        ]
        report = build_and_validate(state, severity="recommended", profile="isa")
        return sorted(
            f"{i.get('severity')}|{i.get('entity_id')}|{i.get('message')}"
            for i in report["issues"]
        )

    def test_co_typing_adds_no_finding_the_crate_did_not_already_have(self) -> None:
        from builder.tools.document_discovery import CLASS_PROTOCOL, CLASS_RAW_DATA

        assert self._issues(CLASS_PROTOCOL) == self._issues(CLASS_RAW_DATA)

    def test_the_document_is_not_asked_to_be_an_isa_protocol(self) -> None:
        """The specific claim behind the equality above: nothing reaches the
        document through executesLabProtocol, so the Protocol shapes never
        target it. Asserted by name so this fails loudly if the profile ever
        promotes on type alone."""
        from builder.tools.document_discovery import CLASS_PROTOCOL

        assert not [i for i in self._issues(CLASS_PROTOCOL) if "Protocol entity" in i]


class TestSourceCodeFileTyping:
    """draft_file gains additional_types + programming_language so a script
    round-trips as @type:[File, SoftwareSourceCode] (gold plot.py)."""

    def test_source_code_file_emits_typed_node(self):
        state = CrateState()
        state.metadata.title = "Code crate"
        from builder.tools.provenance import draft_file

        draft_file(
            state,
            "plot.py",
            additional_types=["SoftwareSourceCode"],
            programming_language="Python",
        )
        graph = _graph(state)
        node = next(
            (n for n in graph if n.get("@id", "").endswith("plot.py")), None
        )
        assert node is not None, "plot.py File node should exist"
        types = node["@type"] if isinstance(node.get("@type"), list) else [node.get("@type")]
        assert "File" in types and "SoftwareSourceCode" in types, (
            f"@type should be [File, SoftwareSourceCode]; got {node.get('@type')}"
        )
        assert node.get("programmingLanguage") == "Python"
        # encodingFormat is still auto-derived from the extension.
        assert node.get("encodingFormat") == "text/x-python"

    def test_plain_file_stays_single_typed(self):
        state = CrateState()
        state.metadata.title = "Code crate"
        from builder.tools.provenance import draft_file

        draft_file(state, "raw.csv")
        graph = _graph(state)
        node = next((n for n in graph if n.get("@id", "").endswith("raw.csv")), None)
        assert node is not None
        assert node.get("@type") == "File", "plain File keeps scalar @type"
        assert "programmingLanguage" not in node
