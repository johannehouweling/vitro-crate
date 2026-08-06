"""Tests for the provenance DAG renderer (``builder/writers/provenance_dag.py``).

The renderer turns an assembled RO-Crate metadata document (the ``@graph`` from
``crate.metadata.generate()`` or a parsed ``ro-crate-metadata.json``) into a
Mermaid ``flowchart`` of the LabProcess derivation chain — input/output edges
only, generated from real data rather than hand-drawn.
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from builder.writers.provenance_dag import (
    build_chemical_inventory,
    render_chemicals_svg,
    render_mermaid_html,
    render_provenance_mermaid,
    render_provenance_svg,
)
from profiles.context import ISA_TOX_CONTEXT


def _full_chain_graph() -> dict:
    """A realistic serialized ``@graph`` for the four-step derivation chain.

    Mirrors how ro-crate-py serializes the tox LabProcess subtypes: input under
    ``input`` / ``object``, output under ``output`` / ``result``, references as
    ``{"@id": ...}`` nodes, the discriminator under ``additionalType``.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork"},
            {"@id": "./", "@type": "Dataset", "additionalType": "Investigation"},
            # The cell-based test system: a CellLine Sample carrying a categorical
            # sampleType (DefinedTerm "cell line") and a Cellosaurus identity, per
            # the ISA-Tox profile. The derived/cultured Sample below is NOT a
            # CellLine and deliberately carries no sampleType.
            {
                "@id": "#cellline",
                "@type": "Sample",
                "additionalType": "CellLine",
                "name": "HepG2",
                "sampleType": {"@id": "http://purl.obolibrary.org/obo/NCIT_C16403"},
                "identifier": "CVCL_0027",
            },
            {
                "@id": "http://purl.obolibrary.org/obo/NCIT_C16403",
                "@type": "DefinedTerm",
                "name": "cell line",
            },
            {
                "@id": "#cc",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "Cell Culture",
                "input": {"@id": "#cellline"},
                "output": {"@id": "#cultured"},
            },
            {
                "@id": "#cultured",
                "@type": "Sample",
                "name": "Cultured cells",
                "derivesFrom": {"@id": "#cellline"},
            },
            {
                "@id": "#exp",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cultured"},
                "result": {"@id": "#table"},
            },
            {
                "@id": "#table",
                "@type": ["File", "csvw:Table"],
                "name": "Condition table",
                "about": [{"@id": "#cultured"}, {"@id": "#compound"}],
            },
            {"@id": "#compound", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            {
                "@id": "#er",
                "@type": "LabProcess",
                "additionalType": "EndpointReadout",
                "name": "Endpoint Readout",
                "input": {"@id": "#table"},
                "output": {"@id": "#raw"},
            },
            {"@id": "#raw", "@type": "File", "name": "Raw measurements"},
            {
                "@id": "#da",
                "@type": "LabProcess",
                "additionalType": "DataAnalysis",
                "name": "Data Analysis",
                "input": {"@id": "#raw"},
                "output": {"@id": "#fig"},
            },
            {"@id": "#fig", "@type": "File", "name": "Figures"},
            {"@id": "#person", "@type": "Person", "name": "Jane Doe"},
        ]
    }


def test_returns_mermaid_flowchart() -> None:
    out = render_provenance_mermaid(_full_chain_graph())
    assert out.startswith("flowchart LR")


def test_all_four_process_discriminators_present() -> None:
    out = render_provenance_mermaid(_full_chain_graph())
    for disc in ("CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"):
        assert disc in out, f"{disc} process node missing from DAG"


def test_input_and_output_edges_rendered() -> None:
    out = render_provenance_mermaid(_full_chain_graph())
    # input (object) edge points INTO the process; output (result) points OUT.
    assert "object" in out and "result" in out
    # The chain's material/data labels appear.
    for label in ("HepG2", "Cultured cells", "Condition table", "Raw measurements", "Figures"):
        assert label in out, f"{label} node missing from DAG"


def test_direction_is_configurable() -> None:
    out = render_provenance_mermaid(_full_chain_graph(), direction="TD")
    assert out.startswith("flowchart TD")


def test_compound_connected_through_table_via_about() -> None:
    out = render_provenance_mermaid(_full_chain_graph(), include_annotations=True)
    assert "Aflatoxin B1" in out
    assert "about" in out


def test_annotations_can_be_omitted() -> None:
    out = render_provenance_mermaid(_full_chain_graph(), include_annotations=False)
    # Without annotations the compound (reachable only via the table's `about`)
    # drops out of the derivation DAG.
    assert "Aflatoxin B1" not in out


def test_non_provenance_entities_excluded() -> None:
    """People/orgs not on the derivation chain must not clutter the DAG."""
    out = render_provenance_mermaid(_full_chain_graph())
    assert "Jane Doe" not in out


def test_accepts_bare_graph_list() -> None:
    graph = _full_chain_graph()["@graph"]
    out = render_provenance_mermaid(graph)
    assert "Exposure" in out


def test_fenced_wraps_in_code_block() -> None:
    out = render_provenance_mermaid(_full_chain_graph(), fenced=True)
    assert out.startswith("```mermaid\n")
    assert out.rstrip().endswith("```")


def test_empty_graph_is_handled() -> None:
    out = render_provenance_mermaid({"@graph": []})
    assert out.startswith("flowchart LR")


def test_render_mermaid_html_embeds_source_and_loads_mermaid() -> None:
    mermaid = render_provenance_mermaid(_full_chain_graph())
    html = render_mermaid_html(mermaid, title="My DAG")
    assert html.startswith("<!DOCTYPE html>")
    assert "mermaid.esm.min.mjs" in html  # the renderer is loaded
    assert "My DAG" in html
    # The source is embedded as a JS string; the flowchart keyword survives.
    assert "flowchart LR" in html
    assert "mermaid.render(" in html


def test_render_mermaid_html_escapes_label_markup_safely() -> None:
    """The <br/> in node labels is embedded as JS string data, not live HTML."""
    html = render_mermaid_html(render_provenance_mermaid(_full_chain_graph()))
    # json.dumps escapes the source into a quoted literal containing <br/>.
    assert "<br/>" in html


class TestRenderProvenanceSvg:
    """``render_provenance_svg`` draws the derivation chain as a self-contained,
    offline inline ``<svg>`` (no external assets, no script) for embedding in the
    maturity report."""

    def test_returns_inline_svg_element(self) -> None:
        svg = render_provenance_svg(_full_chain_graph())
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
        assert "viewBox" in svg

    def test_chain_nodes_and_edges_present(self) -> None:
        svg = render_provenance_svg(_full_chain_graph())
        # Every material/data label on the derivation chain is drawn.
        for label in ("HepG2", "Cultured cells", "Condition table", "Figures"):
            assert label in svg, f"{label} missing from provenance SVG"
        # Process discriminators appear as node tags (uppercased).
        assert "EXPOSURE" in svg.upper()
        # Both edge kinds are drawn (object = input, result = output).
        assert "e-object" in svg and "e-result" in svg

    def test_no_process_chain_returns_empty(self) -> None:
        # A graph with data but no LabProcess has no derivation chain to draw.
        svg = render_provenance_svg(
            {"@graph": [{"@id": "#f", "@type": "File", "name": "orphan.csv"}]}
        )
        assert svg == ""

    def test_escapes_crate_controlled_names(self) -> None:
        graph = {
            "@graph": [
                {"@id": "#s", "@type": "Sample", "name": "<script>alert(1)</script>"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "object": {"@id": "#s"},
                    "result": {"@id": "#d"},
                },
                {"@id": "#d", "@type": "File", "name": "out.csv"},
            ]
        }
        svg = render_provenance_svg(graph)
        assert "<script>alert(1)</script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_self_contained_no_external_assets(self) -> None:
        svg = render_provenance_svg(_full_chain_graph())
        assert "http://" not in svg and "https://" not in svg
        assert "<script" not in svg.lower()

    def test_branching_second_output_is_drawn(self) -> None:
        # One process with two results (a branch) — both outputs must be drawn.
        graph = {
            "@graph": [
                {"@id": "#s", "@type": "Sample", "name": "Input sample"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "EndpointReadout",
                    "object": {"@id": "#s"},
                    "result": [{"@id": "#d1"}, {"@id": "#d2"}],
                },
                {"@id": "#d1", "@type": "File", "name": "result.csv"},
                {"@id": "#d2", "@type": ["File", "csvw:Table"], "name": "raw.csv"},
            ]
        }
        svg = render_provenance_svg(graph)
        assert "result.csv" in svg and "raw.csv" in svg

    def test_accepts_bare_graph_list(self) -> None:
        svg = render_provenance_svg(_full_chain_graph()["@graph"])
        assert svg.startswith("<svg")


def _chemicals_graph() -> dict:
    """A crate exercising all four ways a compound can (fail to) reach a process.

    ``#c_about`` is wired the canonical way (Exposure → condition table →
    ``about``); ``#c_valueurl`` only through the table's ``compound`` column
    (table → tableSchema → columns → ``valueUrl``); ``#c_mentioned`` is named by
    the Study but produced by nothing; ``#c_orphan`` is referenced by nobody.
    """
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#study"}]},
            {
                "@id": "#study",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "Tox study",
                "chemicals": [{"@id": "#c_mentioned"}],
            },
            {"@id": "#cells", "@type": "Sample", "name": "Cultured cells"},
            {
                "@id": "#exposure",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure step",
                "object": {"@id": "#cells"},
                "result": {"@id": "#table"},
            },
            {
                "@id": "#table",
                "@type": ["File", "csvw:Table"],
                "name": "Condition table",
                "about": [{"@id": "#c_about"}],
                "tableSchema": {"@id": "#schema"},
            },
            {
                "@id": "#schema",
                "@type": ["csvw:Schema", "CreativeWork"],
                "columns": [{"@id": "#col"}],
            },
            {
                "@id": "#col",
                "@type": "csvw:Column",
                "titles": "compound",
                "valueUrl": {"@id": "#c_valueurl"},
            },
            {
                "@id": "#c_about",
                "@type": "MolecularEntity",
                "name": "Aflatoxin B1",
                "inchikey": "OQIQSTLJSLGHID-WNWIJWBNSA-N",
                "smiles": "CO",
                "formula": "C17H12O6",
                "mass": "312.3",
                "identifier": [{"@id": "#cas"}, {"@id": "#cid"}, {"@id": "#dtx"}],
            },
            {"@id": "#c_valueurl", "@type": "MolecularEntity", "name": "Bisphenol A"},
            {"@id": "#c_mentioned", "@type": "MolecularEntity", "name": "Mentioned only"},
            {"@id": "#c_orphan", "@type": "MolecularEntity", "name": "Orphan compound"},
            {"@id": "#cas", "@type": "PropertyValue", "name": "CAS", "value": "1162-65-8"},
            {
                "@id": "#cid",
                "@type": "PropertyValue",
                "name": "PubChem CID",
                "propertyID": {"@id": "https://pubchem.ncbi.nlm.nih.gov/compound"},
                "value": "186907",
            },
            {
                "@id": "#dtx",
                "@type": "PropertyValue",
                "name": "DTXSID",
                "value": "DTXSID9020035",
            },
        ]
    }


def _no_compound_graph() -> dict:
    """A crate that declares no MolecularEntity at all — the Chemicals view is
    not applicable, which must read differently from "declared but unwired"."""
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#f"}]},
            {"@id": "#f", "@type": "File", "name": "result.csv"},
        ]
    }


class TestBuildChemicalInventory:
    """``build_chemical_inventory`` resolves each compound's route into the
    experiment and how completely it is identified."""

    def _by_name(self, inv: dict) -> dict:
        return {c["name"]: c for c in inv["chemicals"]}

    def test_classifies_every_route_state(self) -> None:
        chems = self._by_name(build_chemical_inventory(_chemicals_graph()))
        # The canonical ISA route: the compound hangs off the Exposure's result.
        assert chems["Aflatoxin B1"]["state"] == "wired"
        # Reached only through the condition table's compound column.
        assert chems["Bisphenol A"]["state"] == "wired"
        # Named by the Study, but no process produces the Study.
        assert chems["Mentioned only"]["state"] == "mentioned"
        # Referenced by nothing at all.
        assert chems["Orphan compound"]["state"] == "unlinked"

    def test_wired_route_names_the_producing_process(self) -> None:
        route = self._by_name(build_chemical_inventory(_chemicals_graph()))["Aflatoxin B1"][
            "route"
        ]
        assert route["process"] == "#exposure"
        assert route["via"] == "#table"
        assert route["edge"] == "about"

    def test_valueurl_route_walks_the_csvw_containment_chain(self) -> None:
        # column → schema → table → the process that produced it: the compound is
        # three hops from anything a process results in, and must still resolve.
        route = self._by_name(build_chemical_inventory(_chemicals_graph()))["Bisphenol A"][
            "route"
        ]
        assert route["process"] == "#exposure"
        assert route["via"] == "#table"
        assert route["edge"] == "valueUrl"

    def test_counts_summarise_routes_and_identification(self) -> None:
        counts = build_chemical_inventory(_chemicals_graph())["counts"]
        assert counts == {
            "total": 4,
            "wired": 2,
            "mentioned": 1,
            "unlinked": 1,
            "fields_met": 7,  # only Aflatoxin B1 is fully identified
            "fields_total": 28,  # 4 compounds × 7 fields
        }

    def test_identifier_property_values_are_resolved_by_scheme(self) -> None:
        chem = self._by_name(build_chemical_inventory(_chemicals_graph()))["Aflatoxin B1"]
        assert chem["identifiers"] == {
            "CAS": "1162-65-8",
            "PubChem CID": "186907",
            "DTXSID": "DTXSID9020035",
        }
        assert chem["met"] == chem["total"] == 7

    def test_registry_url_id_counts_as_that_identifier(self) -> None:
        # The builder mints a PubChem compound URL as the @id; that identifies the
        # substance whether or not a PubChem CID PropertyValue is also present.
        graph = {
            "@graph": [
                {
                    "@id": "https://pubchem.ncbi.nlm.nih.gov/compound/6623",
                    "@type": "MolecularEntity",
                    "name": "Bisphenol A",
                }
            ]
        }
        chem = build_chemical_inventory(graph)["chemicals"][0]
        assert chem["identifiers"]["PubChem CID"] == "6623"
        assert chem["resolvable"] is True

    def test_bare_cas_string_identifier_is_counted(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "#c",
                    "@type": "MolecularEntity",
                    "name": "Compound",
                    "identifier": "1162-65-8",
                }
            ]
        }
        assert build_chemical_inventory(graph)["chemicals"][0]["identifiers"] == {
            "CAS": "1162-65-8"
        }

    def test_compound_to_compound_reference_is_not_a_route(self) -> None:
        # Two compounds referencing each other must not mask that neither is
        # reachable from the experiment.
        graph = {
            "@graph": [
                {"@id": "#a", "@type": "MolecularEntity", "name": "A", "about": {"@id": "#b"}},
                {"@id": "#b", "@type": "MolecularEntity", "name": "B", "about": {"@id": "#a"}},
            ]
        }
        inv = build_chemical_inventory(graph)
        assert inv["counts"]["unlinked"] == 2

    def test_crate_without_compounds_is_empty(self) -> None:
        inv = build_chemical_inventory(_no_compound_graph())
        assert inv["chemicals"] == []
        assert inv["groups"] == []
        assert inv["counts"]["total"] == 0

    def test_full_chain_fixture_compound_is_wired(self) -> None:
        # The realistic four-step fixture links its compound the canonical way
        # (condition table --about--> MolecularEntity), so it must read as wired.
        inv = build_chemical_inventory(_full_chain_graph())
        assert [c["state"] for c in inv["chemicals"]] == ["wired"]

    def test_accepts_bare_graph_list(self) -> None:
        inv = build_chemical_inventory(_chemicals_graph()["@graph"])
        assert inv["counts"]["total"] == 4


class TestRenderChemicalsSvg:
    """``render_chemicals_svg`` draws the compound routes as a self-contained,
    offline inline ``<svg>`` — same shapes/geometry as the derivation chain."""

    def test_returns_inline_svg_with_every_route_band(self) -> None:
        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
        for label in ("Exposure step", "Condition table", "Aflatoxin B1", "Orphan compound"):
            assert label in svg, f"{label} missing from chemicals SVG"

    def test_unwired_compounds_are_visually_distinguished(self) -> None:
        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        # A broken route is drawn as a dashed stub ending in ✗, never omitted.
        assert "e-break" in svg
        assert "✗" in svg
        assert "unwired" in svg

    def test_fully_wired_crate_has_no_break_marker(self) -> None:
        graph = _chemicals_graph()
        graph["@graph"] = [
            n for n in graph["@graph"] if n["@id"] not in ("#c_mentioned", "#c_orphan")
        ]
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        assert "e-break" not in svg
        assert "unwired" not in svg

    def test_one_band_per_route_not_per_relation(self) -> None:
        # `about` and `valueUrl` reach different compounds through the SAME table:
        # one band, one process node, both mechanisms named on the edge.
        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        assert svg.count('class="n n-process"') == 1
        assert "about · valueUrl" in svg

    def test_large_group_folds_its_tail_into_an_aggregate(self) -> None:
        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(9):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i}"}
            )
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        # 3 named + an aggregate naming the remainder — nothing silently dropped.
        assert "Compound 0" in svg and "Compound 2" in svg
        assert "Compound 8" not in svg
        assert "6 more" in svg

    def test_escapes_crate_controlled_names(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "#c",
                    "@type": "MolecularEntity",
                    "name": "<script>alert(1)</script>",
                }
            ]
        }
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        assert "<script>alert(1)</script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_self_contained_no_external_assets(self) -> None:
        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        assert "http://" not in svg and "https://" not in svg
        assert "<script" not in svg.lower()

    def test_geometry_stays_inside_the_viewbox(self) -> None:
        # The page scrolls the SVG horizontally; anything drawn outside the
        # viewBox would simply be invisible.
        import xml.etree.ElementTree as ET

        def attr(el: ET.Element, name: str) -> str:
            """A geometry attribute the renderer must always emit.

            ``Element.get`` is optional-typed, and a missing coordinate here
            would otherwise crash with a bare ``AttributeError: 'NoneType'``
            instead of naming the element that lost it.
            """
            value = el.get(name)
            assert value is not None, f"<{el.tag}> is missing {name!r}"
            return value

        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        root = ET.fromstring(svg)  # also asserts well-formedness
        _, _, width, height = (float(v) for v in attr(root, "viewBox").split())
        xs: list[float] = []
        ys: list[float] = []
        for el in root.iter():
            if el.tag == "polygon" and el.get("class", "").startswith("n "):
                for point in attr(el, "points").split():
                    px, py = point.split(",")
                    xs.append(float(px))
                    ys.append(float(py))
            elif el.tag == "text":
                xs.append(float(attr(el, "x")))
                ys.append(float(attr(el, "y")))
        assert xs and ys
        assert 0 <= min(xs) and max(xs) <= width
        assert 0 <= min(ys) and max(ys) <= height

    def test_empty_inventory_returns_empty(self) -> None:
        assert render_chemicals_svg(build_chemical_inventory(_no_compound_graph())) == ""


def _exposure_state() -> CrateState:
    state = CrateState()
    state.metadata.title = "Exposure crate"
    state.add_entity(
        Entity(
            entity_id="proc_exp",
            type="LabProcess",
            fields={"process_type": "Exposure", "name": "Exposure step"},
            _provenance=EntityProvenance(created_by="llm"),
        )
    )
    return state


def test_renders_from_real_assembled_crate(tmp_path) -> None:
    """End-to-end: render straight off ro-crate-py's serialized graph."""
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    populate_crate(_exposure_state(), crate, tmp_path, materialize_payload=False)
    graph = crate.metadata.generate()
    out = render_provenance_mermaid(graph)
    assert out.startswith("flowchart LR")
    # The Exposure process and its synthesized condition-table result appear,
    # with a result edge between them.
    assert "Exposure" in out
    assert "Condition table" in out
    assert "result" in out
