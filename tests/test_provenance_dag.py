"""Tests for the provenance DAG renderer (``builder/writers/provenance_dag.py``).

The renderer turns an assembled RO-Crate metadata document (the ``@graph`` from
``crate.metadata.generate()`` or a parsed ``ro-crate-metadata.json``) into a
Mermaid ``flowchart`` of the LabProcess derivation chain — input/output edges
only, generated from real data rather than hand-drawn.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET

import pytest
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from builder.writers.provenance_dag import (
    build_cellline_inventory,
    build_chemical_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
    render_celllines_svg,
    render_chemicals_svg,
    render_isa_svg,
    render_mermaid_html,
    render_overview_svg,
    render_people_svg,
    render_provenance_mermaid,
    render_provenance_svg,
)
from profiles.context import ISA_TOX_CONTEXT


def _attr(el: ET.Element, name: str) -> str:
    """A geometry attribute the renderer must always emit.

    ``Element.get`` is optional-typed, so every ``float(el.get(...))`` in the
    viewBox-containment tests is a type error waiting to become a bare
    ``AttributeError: 'NoneType'``. Failing here names the element that lost the
    attribute instead.
    """
    value = el.get(name)
    assert value is not None, f"<{el.tag}> is missing {name!r}"
    return value


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

    def test_large_band_tiles_into_a_grid_not_a_column(self) -> None:
        # Every member drawn, but stacked one-per-row a 22-compound band rendered
        # as a 210x1476 ribbon — narrower than a phone, taller than the page.
        import xml.etree.ElementTree as ET

        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(22):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i:02d}"}
            )
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        root = ET.fromstring(svg)
        _, _, width, height = (float(v) for v in _attr(root, "viewBox").split())
        assert width > height, f"band is still a vertical ribbon ({width}x{height})"
        # Distinct x positions prove a grid rather than a single column.
        xs = {
            _attr(e, "points").split()[0].split(",")[0]
            for e in root.iter()
            if e.tag == "polygon" and (e.get("class") or "").startswith("n ")
        }
        assert len(xs) > 1, "all members share one x — still one column"

    def test_one_connector_per_band_not_per_member(self) -> None:
        # Every member of a band reaches the experiment the same way — that is
        # what defines the band — so N parallel edges restate one fact N times.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#t"}]},
                {"@id": "#s", "@type": "Sample", "name": "cells"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "name": "Exposure",
                    "object": {"@id": "#s"},
                    "result": {"@id": "#t"},
                },
                {
                    "@id": "#t",
                    "@type": ["File", "csvw:Table"],
                    "name": "Condition table",
                    "about": [{"@id": f"#c{i}"} for i in range(9)],
                },
                *[
                    {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i}"}
                    for i in range(9)
                ],
            ]
        }
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        # One edge per HOP (process→table, table→group), not one per compound.
        assert svg.count('class="e e-link"') == 2
        assert "about" in svg  # the grouped connector still carries its label
        for i in range(9):
            assert f"Compound {i}" in svg

    def test_unlinked_band_gets_one_break_stub(self) -> None:
        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(12):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i}"}
            )
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        assert svg.count('class="e e-break"') == 1, "one broken route, one marker"

    def test_every_member_of_a_large_band_is_named(self) -> None:
        # No "+N more" aggregate: the picture exists so a reader can see WHICH
        # compounds are unwired, and an elided tail hides exactly that.
        graph = {"@graph": [{"@id": "./", "@type": "Dataset"}]}
        for i in range(9):
            graph["@graph"].append(
                {"@id": f"#c{i}", "@type": "MolecularEntity", "name": f"Compound {i}"}
            )
        svg = render_chemicals_svg(build_chemical_inventory(graph))
        for i in range(9):
            assert f"Compound {i}" in svg, f"compound {i} elided from the diagram"
        assert "more" not in svg

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
        svg = render_chemicals_svg(build_chemical_inventory(_chemicals_graph()))
        root = ET.fromstring(svg)  # also asserts well-formedness
        _, _, width, height = (float(v) for v in _attr(root, "viewBox").split())
        xs: list[float] = []
        ys: list[float] = []
        for el in root.iter():
            if el.tag == "polygon" and el.get("class", "").startswith("n "):
                for point in _attr(el, "points").split():
                    px, py = point.split(",")
                    xs.append(float(px))
                    ys.append(float(py))
            elif el.tag == "text":
                xs.append(float(_attr(el, "x")))
                ys.append(float(_attr(el, "y")))
        assert xs and ys
        assert 0 <= min(xs) and max(xs) <= width
        assert 0 <= min(ys) and max(ys) <= height

    def test_empty_inventory_returns_empty(self) -> None:
        assert render_chemicals_svg(build_chemical_inventory(_no_compound_graph())) == ""


def _cellline_graph(*, wire: bool = True) -> dict:
    """A crate with a CellLineSample and a CellCulture that may or may not use it.

    The defect this view exists to catch: the culture consumes a freshly minted
    generic ``Sample`` (``#generic``) instead of the declared line, leaving the
    line described in the crate and consumed by nothing.
    """
    culture_input = "#cho" if wire else "#generic"
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#cultured"}]},
            {"@id": "#generic", "@type": "Sample", "name": "Input sample"},
            {
                "@id": "#culture",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "CHO-K1 culture",
                "input": {"@id": culture_input},
                "output": {"@id": "#cultured"},
            },
            {"@id": "#cultured", "@type": "Sample", "name": "Cultured cells"},
            {
                "@id": "#cho",
                "@type": "Sample",
                "additionalType": "CellLine",
                "name": "CHO-K1",
                "identifier": "CVCL_0214",
                "sampleType": {"@id": "#term"},
                "additionalProperty": [{"@id": "#organ"}, {"@id": "#passage"}],
            },
            {"@id": "#term", "@type": "DefinedTerm", "name": "cell line"},
            {"@id": "#organ", "@type": "PropertyValue", "name": "Organ", "value": "ovary"},
            {"@id": "#passage", "@type": "PropertyValue", "name": "passage", "value": "12"},
        ]
    }


class TestBuildCellLineInventory:
    """``build_cellline_inventory`` resolves each cell line's route into the
    experiment and how completely the culture is characterised."""

    def test_cellline_consumed_by_the_culture_is_wired(self) -> None:
        inv = build_cellline_inventory(_cellline_graph(wire=True))
        assert [c["name"] for c in inv["celllines"]] == ["CHO-K1"]
        line = inv["celllines"][0]
        assert line["state"] == "wired"
        # The process references the line directly — it is both process and via.
        assert line["route"]["process"] == "#culture"
        assert line["route"]["edge"] == "input"

    def test_cellline_the_culture_skipped_is_unlinked(self) -> None:
        # The exact defect: a generic Sample was minted and consumed instead.
        inv = build_cellline_inventory(_cellline_graph(wire=False))
        assert inv["celllines"][0]["state"] == "unlinked"
        assert inv["counts"]["wired"] == 0

    def test_cultured_sample_lineage_counts_as_a_route(self) -> None:
        graph = _cellline_graph(wire=False)
        for node in graph["@graph"]:
            if node["@id"] == "#cultured":
                node["derivesFrom"] = {"@id": "#cho"}
        inv = build_cellline_inventory(graph)
        line = inv["celllines"][0]
        assert line["state"] == "wired"
        assert line["route"]["edge"] == "derivesFrom"
        assert line["route"]["via"] == "#cultured"

    def test_rrid_and_characteristics_are_scored(self) -> None:
        line = build_cellline_inventory(_cellline_graph())["celllines"][0]
        assert line["rrid"] == "CVCL_0214"
        assert line["fields"] == {
            "Cellosaurus RRID": True,
            "Typed as a cell line": True,
            "Organ": True,
            "Tissue": False,  # never recorded by this crate
            "Passage": True,
        }
        assert (line["met"], line["total"]) == (4, 5)

    def test_rrid_read_from_a_cellosaurus_url(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "https://www.cellosaurus.org/CVCL_0031",
                    "@type": "Sample",
                    "additionalType": "CellLine",
                    "name": "HepG2",
                }
            ]
        }
        assert build_cellline_inventory(graph)["celllines"][0]["rrid"] == "CVCL_0031"

    def test_sample_typed_only_by_term_is_recognised(self) -> None:
        # No additionalType — the line is identified solely by its sampleType
        # DefinedTerm, which an externally-authored crate may well do.
        graph = {
            "@graph": [
                {
                    "@id": "#line",
                    "@type": "Sample",
                    "name": "MDCK",
                    "sampleType": {"@id": "#t"},
                },
                {"@id": "#t", "@type": "DefinedTerm", "name": "cell line"},
            ]
        }
        assert len(build_cellline_inventory(graph)["celllines"]) == 1

    def test_plain_sample_is_not_a_cell_line(self) -> None:
        graph = {"@graph": [{"@id": "#s", "@type": "Sample", "name": "Cultured cells"}]}
        assert build_cellline_inventory(graph)["celllines"] == []

    def test_crate_without_cell_lines_is_empty(self) -> None:
        inv = build_cellline_inventory(_no_compound_graph())
        assert inv["celllines"] == []
        assert inv["counts"]["total"] == 0


class TestRenderCellLinesSvg:
    """The cell-line diagram reuses the compound view's bands — a break reads the
    same in both — and the material stadium, because a cell line IS a Sample."""

    def test_direct_process_route_draws_the_process_once(self) -> None:
        # `input` makes the process itself the referrer; drawing it in two
        # columns joined by a `result` edge would depict a step that does not
        # exist in the crate.
        svg = render_celllines_svg(build_cellline_inventory(_cellline_graph(wire=True)))
        assert svg.count('class="n n-process"') == 1
        assert '"result"' not in svg and ">result<" not in svg
        assert "CHO-K1" in svg

    def test_cell_line_uses_the_material_shape(self) -> None:
        svg = render_celllines_svg(build_cellline_inventory(_cellline_graph()))
        assert "n-material" in svg

    def test_unlinked_line_gets_a_break_marker(self) -> None:
        svg = render_celllines_svg(build_cellline_inventory(_cellline_graph(wire=False)))
        assert "e-break" in svg and "✗" in svg and "unwired" in svg

    def test_empty_inventory_returns_empty(self) -> None:
        assert render_celllines_svg(build_cellline_inventory(_no_compound_graph())) == ""

    def test_escapes_crate_controlled_names(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "#l",
                    "@type": "Sample",
                    "additionalType": "CellLine",
                    "name": "<script>alert(1)</script>",
                }
            ]
        }
        svg = render_celllines_svg(build_cellline_inventory(graph))
        assert "<script>alert(1)</script>" not in svg
        assert "&lt;script&gt;" in svg


class TestAffiliationReachability:
    """``affiliation`` is an edge of the crate graph (#85).

    Nothing else in a crate references an affiliation-only Organization, so
    omitting the relation reported every ROR-backed institution as an orphan
    while the crate was in fact correct — and the People view showed the very
    same institution as connected. The two must agree.
    """

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Crate",
                    "author": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
                },
                {
                    "@id": "https://orcid.org/0000-0002-1825-0097",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                    "affiliation": {"@id": "https://ror.org/05gq02987"},
                },
                {
                    "@id": "https://ror.org/05gq02987",
                    "@type": "Organization",
                    "name": "Brown University",
                },
            ]
        }

    def test_affiliated_organisation_is_not_an_orphan(self) -> None:
        model = build_crate_graph(self._graph(), all_edges=True)
        org = next(n for n in model["nodes"] if n["id"] == "https://ror.org/05gq02987")
        assert org["orphan"] is False
        assert model["counts"]["orphan"] == 0

    def test_topology_and_people_view_agree(self) -> None:
        graph = self._graph()
        orphans = {
            n["id"] for n in build_crate_graph(graph, all_edges=True)["nodes"] if n["orphan"]
        }
        unattached = {
            a["id"] for a in build_people_inventory(graph)["agents"] if a["state"] == "unattached"
        }
        assert orphans == unattached == set()

    def test_contributor_only_person_is_not_an_orphan(self) -> None:
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "contributor": [{"@id": "#p"}]},
                {"@id": "#p", "@type": "Person", "name": "Jane Doe"},
            ]
        }
        model = build_crate_graph(graph, all_edges=True)
        assert model["counts"]["orphan"] == 0


class TestRouteClaimsMatchTopology:
    """"Nothing in the crate references this" must be TRUE when the panel says it.

    The routed views and the graph-topology strip render inside the same section.
    When the views scanned a narrower relation vocabulary than
    ``build_crate_graph``, an entity reached only by ``result`` / ``derivesFrom``
    / ``hasPart`` was reported as referenced by nothing a few lines above a strip
    that counted it as reachable.
    """

    def _graph(self, relation: str) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#holder"}]},
                {"@id": "#holder", "@type": "File", "name": "holder.csv", relation: {"@id": "#c"}},
                {"@id": "#c", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            ]
        }

    def test_secondary_relations_are_not_reported_as_unreferenced(self) -> None:
        for relation in ("result", "derivesFrom", "hasPart"):
            state = build_chemical_inventory(self._graph(relation))["chemicals"][0]["state"]
            assert state != "unlinked", f"{relation} edge reported as no reference at all"

    def test_unlinked_agrees_with_the_topology_orphan_flag(self) -> None:
        for relation in ("result", "derivesFrom", "hasPart", "about", "mentions"):
            graph = self._graph(relation)
            unlinked = {
                c["id"]
                for c in build_chemical_inventory(graph)["chemicals"]
                if c["state"] == "unlinked"
            }
            orphans = {
                n["id"] for n in build_crate_graph(graph, all_edges=True)["nodes"] if n["orphan"]
            }
            assert unlinked <= orphans, (
                f"{relation}: view claims unreferenced, topology says reachable"
            )

    def test_canonical_route_still_wins_over_a_secondary_one(self) -> None:
        # A compound reachable both canonically and loosely must report the
        # canonical route — the added relations are a fallback, not a reordering.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#c"}, {"@id": "#t"}]},
                {"@id": "#s", "@type": "Sample", "name": "cells"},
                {
                    "@id": "#p",
                    "@type": "LabProcess",
                    "additionalType": "Exposure",
                    "name": "Exposure",
                    "object": {"@id": "#s"},
                    "result": {"@id": "#t"},
                },
                {
                    "@id": "#t",
                    "@type": ["File", "csvw:Table"],
                    "name": "Condition table",
                    "about": [{"@id": "#c"}],
                },
                {"@id": "#c", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            ]
        }
        route = build_chemical_inventory(graph)["chemicals"][0]["route"]
        assert route["edge"] == "about"
        assert route["process"] == "#p"


class TestCoAffiliation:
    """Every ``affiliation`` a person declares is modelled, not just the first.

    Taking only the first left a co-affiliated researcher's second institution
    referenced by nothing — which this view reports as an unattached duplicate
    and advises the reader to delete. Wrong, and destructively so.
    """

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "name": "Crate", "author": [{"@id": "#p"}]},
                {
                    "@id": "#p",
                    "@type": "Person",
                    "name": "Jointly Affiliated",
                    "affiliation": [{"@id": "#org_a"}, {"@id": "#org_b"}],
                },
                {"@id": "#org_a", "@type": "Organization", "name": "Institute A"},
                {"@id": "#org_b", "@type": "Organization", "name": "Institute B"},
            ]
        }

    def test_second_affiliation_is_not_reported_unattached(self) -> None:
        agents = {a["name"]: a for a in build_people_inventory(self._graph())["agents"]}
        assert agents["Institute A"]["state"] == "affiliated"
        assert agents["Institute B"]["state"] == "affiliated"
        assert agents["Jointly Affiliated"]["affiliations"] == ["#org_a", "#org_b"]

    def test_both_affiliations_are_drawn(self) -> None:
        svg = render_people_svg(build_people_inventory(self._graph()))
        assert "Institute A" in svg and "Institute B" in svg
        # Two affiliation edges out of the one person, and no "link missing" stub.
        assert svg.count('class="e e-link"') == 3  # author + two affiliations
        assert "e-break" not in svg

    def test_topology_agrees_that_both_are_reachable(self) -> None:
        model = build_crate_graph(self._graph(), all_edges=True)
        assert model["counts"]["orphan"] == 0


class TestRenderPeopleSvgCompleteness:
    """The people diagram draws EVERY agent — no "+N more" aggregate.

    This view exists so a person can check the attribution entity by entity, and
    an elided tail is exactly where a duplicated institution or a missing-ORCID
    author would hide.
    """

    def _many(self, n: int) -> dict:
        graph: dict = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "name": "Crate", "author": []},
            ]
        }
        for i in range(n):
            graph["@graph"][1]["author"].append({"@id": f"#p{i}"})
            graph["@graph"].append(
                {
                    "@id": f"#p{i}",
                    "@type": "Person",
                    "name": f"Author {i:02d}",
                    "affiliation": {"@id": f"#org{i}"},
                }
            )
            graph["@graph"].append(
                {"@id": f"#org{i}", "@type": "Organization", "name": f"Institute {i:02d}"}
            )
        return graph

    def test_every_person_and_organisation_is_drawn(self) -> None:
        svg = render_people_svg(build_people_inventory(self._many(9)))
        assert "more" not in svg
        for i in range(9):
            assert f"Author {i:02d}" in svg, f"person {i} elided"
            assert f"Institute {i:02d}" in svg, f"organisation {i} elided"

    def test_band_height_covers_every_drawn_row(self) -> None:
        # With one organisation per person the rows cannot collide, but the band
        # must still be tall enough that nothing escapes the viewBox.
        import xml.etree.ElementTree as ET

        root = ET.fromstring(render_people_svg(build_people_inventory(self._many(9))))
        _, _, width, height = (float(v) for v in _attr(root, "viewBox").split())
        ys: list[float] = []
        xs: list[float] = []
        for el in root.iter():
            if el.tag == "rect" and (el.get("class") or "").startswith("n "):
                x, y = float(_attr(el, "x")), float(_attr(el, "y"))
                xs += [x, x + float(_attr(el, "width"))]
                ys += [y, y + float(_attr(el, "height"))]
        assert ys and 0 <= min(ys) and max(ys) <= height
        assert 0 <= min(xs) and max(xs) <= width


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


def _isa_graph(*, detach_study: bool = False, empty_assay: bool = False) -> dict:
    """An Investigation → Study → Assay hierarchy, optionally broken.

    ``detach_study`` drops the Study from the Investigation's ``hasPart`` (present
    in the crate, outside the hierarchy — a break that still validates);
    ``empty_assay`` removes the Assay's processes.
    """
    inv_parts = [] if detach_study else [{"@id": "#study"}]
    assay: dict = {
        "@id": "#assay",
        "@type": "Dataset",
        "additionalType": "Assay",
        "name": "Transport assay",
        "identifier": "inv/study/assay",
        "description": "One measurement campaign.",
    }
    if not empty_assay:
        assay["about"] = [{"@id": "#p"}]
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "OATP1C1 investigation",
                "identifier": "inv",
                "description": "The question.",
                "hasPart": inv_parts,
            },
            {
                "@id": "#study",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "Time-course study",
                "identifier": "inv/study",
                "description": "A body of work.",
                "hasPart": [{"@id": "#assay"}],
            },
            assay,
            {
                "@id": "#p",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
            },
        ]
    }


class TestBuildIsaInventory:
    """``build_isa_inventory`` models the Investigation / Study / Assay backbone.

    The hierarchy is expressed only as ``hasPart`` between Datasets that differ by
    ``additionalType``, so it is invisible in the JSON and breaks in ways that
    still validate.
    """

    def _by_level(self, graph: dict) -> dict:
        return {n["level"]: n for n in build_isa_inventory(graph)["nodes"]}

    def test_resolves_the_three_levels_and_their_links(self) -> None:
        nodes = self._by_level(_isa_graph())
        assert set(nodes) == {"Investigation", "Study", "Assay"}
        assert nodes["Study"]["parent"] == "./"
        assert nodes["Assay"]["parent"] == "#study"
        assert nodes["Assay"]["processes"] == ["#p"]
        assert all(n["state"] == "linked" for n in nodes.values())

    def test_root_is_the_investigation_even_without_additional_type(self) -> None:
        graph = _isa_graph()
        del graph["@graph"][1]["additionalType"]
        assert self._by_level(graph)["Investigation"]["id"] == "./"

    def test_container_no_parent_lists_is_detached(self) -> None:
        nodes = self._by_level(_isa_graph(detach_study=True))
        assert nodes["Study"]["state"] == "detached"
        assert nodes["Study"]["fields"]["Listed by its parent"] is False
        # The Investigation has no parent to be listed by — n/a, not a miss.
        assert nodes["Investigation"]["fields"]["Listed by its parent"] is None

    def test_assay_with_no_process_does_not_count_as_populated(self) -> None:
        nodes = self._by_level(_isa_graph(empty_assay=True))
        assert nodes["Assay"]["fields"]["Contains the next level"] is False
        assert nodes["Study"]["fields"]["Contains the next level"] is True

    def test_file_parts_are_not_structural_children(self) -> None:
        # An Assay listing only data files still contains no ISA level below it.
        graph = _isa_graph(empty_assay=True)
        graph["@graph"][3]["hasPart"] = [{"@id": "#f"}]
        graph["@graph"].append({"@id": "#f", "@type": "File", "name": "raw.csv"})
        assert self._by_level(graph)["Assay"]["fields"]["Contains the next level"] is False

    def test_counts_summarise_the_hierarchy(self) -> None:
        counts = build_isa_inventory(_isa_graph())["counts"]
        assert counts["investigations"] == 1
        assert counts["studies"] == 1
        assert counts["assays"] == 1
        assert counts["processes"] == 1
        assert counts["detached"] == 0

    def test_crate_without_isa_containers_is_empty(self) -> None:
        assert build_isa_inventory({"@graph": [{"@id": "#f", "@type": "File"}]})["nodes"] == []


class TestRenderIsaSvg:
    """The ISA diagram: one column per level, one row per container, nothing elided."""

    def test_draws_every_container_with_haspart_edges(self) -> None:
        svg = render_isa_svg(build_isa_inventory(_isa_graph()))
        for label in ("OATP1C1 investigation", "Time-course study", "Transport assay"):
            assert label in svg, f"{label} missing from the ISA diagram"
        assert "hasPart" in svg
        assert svg.count('class="n n-container"') == 3

    def test_assay_tag_carries_its_process_count(self) -> None:
        svg = render_isa_svg(build_isa_inventory(_isa_graph()))
        assert "1 PROC" in svg.upper()

    def test_detached_container_is_marked_and_still_drawn(self) -> None:
        svg = render_isa_svg(build_isa_inventory(_isa_graph(detach_study=True)))
        assert "Time-course study" in svg  # never dropped for lack of a parent
        assert "e-break" in svg and "unwired" in svg

    def test_empty_inventory_returns_empty(self) -> None:
        assert render_isa_svg(build_isa_inventory({"@graph": []})) == ""

    def test_escapes_crate_controlled_names(self) -> None:
        graph = _isa_graph()
        graph["@graph"][2]["name"] = "<script>alert(1)</script>"
        svg = render_isa_svg(build_isa_inventory(graph))
        assert "<script>alert(1)</script>" not in svg
        assert "&lt;script&gt;" in svg


class TestIdentifierFallbackIsNotFabricated:
    """The registry-URL fallback must identify, not merely match a hostname."""

    def _chem(self, nid: str) -> dict:
        inv = build_chemical_inventory(
            {"@graph": [{"@id": nid, "@type": "MolecularEntity", "name": "X"}]}
        )
        return inv["chemicals"][0]["identifiers"]

    def test_registry_resource_url_yields_the_identifier(self) -> None:
        assert self._chem("https://pubchem.ncbi.nlm.nih.gov/compound/6623") == {
            "PubChem CID": "6623"
        }

    def test_other_paths_on_the_registry_host_yield_nothing(self) -> None:
        # /bioassay/1234 lives on the PubChem host and identifies no compound;
        # counting it would inflate the identification score with a wrong value.
        assert self._chem("https://pubchem.ncbi.nlm.nih.gov/bioassay/1234") == {}

    def test_tail_that_is_not_shaped_like_the_identifier_is_rejected(self) -> None:
        assert self._chem("https://pubchem.ncbi.nlm.nih.gov/compound/not-a-cid") == {}

    def test_identifier_node_that_is_itself_a_registry_url_is_read(self) -> None:
        graph = {
            "@graph": [
                {
                    "@id": "#p",
                    "@type": "Person",
                    "name": "Josiah Carberry",
                    "identifier": [{"@id": "https://orcid.org/0000-0002-1825-0097"}],
                }
            ]
        }
        agent = build_people_inventory(graph)["agents"][0]
        assert agent["identifiers"] == {"ORCID": "0000-0002-1825-0097"}
        assert agent["pid_scheme"] == "ORCID"


class TestMalformedGraphDegradesGracefully:
    """A single bad node must cost one row, not the whole report.

    ``build_maturity_html`` runs inside ``export_crate``, so an exception here
    loses the crate its entire maturity report.
    """

    def test_non_string_id_is_skipped_not_fatal(self) -> None:
        graph = {
            "@graph": [
                {"@id": 5, "@type": "Person", "name": "Numeric id"},
                {"@id": {"nested": 1}, "@type": "MolecularEntity", "name": "Object id"},
                {"@id": "#ok", "@type": "Person", "name": "Fine"},
                {"@id": "#c", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            ]
        }
        assert [a["name"] for a in build_people_inventory(graph)["agents"]] == ["Fine"]
        assert [c["name"] for c in build_chemical_inventory(graph)["chemicals"]] == [
            "Aflatoxin B1"
        ]
        assert build_cellline_inventory(graph)["celllines"] == []
        assert build_isa_inventory(graph)["nodes"] == []
        # …and the topology model the report renders beside them survives too.
        assert build_crate_graph(graph, all_edges=True)["counts"]["layer1"] >= 1

    def test_whole_report_still_renders(self) -> None:
        from builder.writers.maturity_report import build_maturity_html
        from tests.fixtures.vhps_golden_crates import vhps_fixture_state

        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "name": "Crate"},
                {"@id": 5, "@type": "Person", "name": "Numeric id"},
                {"@id": "#c", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            ]
        }
        page = build_maturity_html(vhps_fixture_state("S-VHPS21"), graph=graph)
        assert "Graph views" in page


class TestDeterministicOrdering:
    """The embedded artifact must be byte-stable for a given ``@graph``.

    Sorting a set-derived list on a key that can tie leaves the order to the
    per-process string hash seed — and same-named agents are exactly the
    duplicate-entity case these views exist to surface.
    """

    def _graph(self) -> dict:
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Crate",
                    "author": [{"@id": f"#p{i}"} for i in range(6)],
                },
                *[
                    {"@id": f"#p{i}", "@type": "Person", "name": "Same Name"}
                    for i in range(6)
                ],
                *[
                    {"@id": f"#c{i}", "@type": "MolecularEntity", "name": "Same Compound"}
                    for i in range(6)
                ],
            ]
        }

    def test_agent_and_compound_order_is_stable_across_hash_seeds(self) -> None:
        import subprocess
        import sys

        script = (
            "import json,sys;"
            "from builder.writers.provenance_dag import "
            "build_people_inventory, build_chemical_inventory;"
            "g=json.load(sys.stdin);"
            "print([a['id'] for a in build_people_inventory(g)['agents']],"
            "[c['id'] for c in build_chemical_inventory(g)['chemicals']])"
        )
        payload = json.dumps(self._graph())
        outputs = set()
        for seed in ("0", "1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            res = subprocess.run(
                [sys.executable, "-c", script],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            outputs.add(res.stdout.strip())
        assert len(outputs) == 1, f"ordering varies with the hash seed: {outputs}"


class TestRelationVocabularyIsComplete:
    """Every predicate that carries an in-crate reference must be traversable.

    Reachability — and therefore the orphan count the maturity report shows — is
    only as complete as ``_PRIMARY_RELATIONS`` + ``_SECONDARY_RELATIONS``. A
    reference predicate missing from those sets is invisible to the walk, so
    perfectly-wired entities get reported as orphans and a reader is sent hunting
    for a defect that does not exist.

    This has now happened four times (``affiliation``, ``instrument``,
    ``identifier``, and the whole AOP subgraph — 121 of ~250 references in a real
    crate). The pattern is always the same: a predicate is added to the context or
    a model, and nothing tells the graph writer about it. So instead of a fifth
    fix, this asserts the invariant directly.
    """

    def _covered(self) -> set[str]:
        from builder.writers.provenance_dag import _all_relations

        return {key for keys, _label, _rev in _all_relations(all_edges=True) for key in keys}

    def _invisible(self, graph: list[dict]) -> dict[str, int]:
        """Predicates carrying an in-crate reference the walk cannot follow."""
        from builder.writers.provenance_dag import _graph_nodes

        nodes = _graph_nodes(graph)
        covered = self._covered()
        missed: dict[str, int] = {}
        for node in nodes.values():
            for key, value in node.items():
                if key.startswith("@") or key in covered:
                    continue
                for item in value if isinstance(value, list) else [value]:
                    target = (
                        item.get("@id")
                        if isinstance(item, dict)
                        else (item if isinstance(item, str) and item.startswith("#") else None)
                    )
                    if target and target in nodes:
                        missed[key] = missed.get(key, 0) + 1
        return missed

    def test_assembled_crate_has_no_invisible_reference(self, tmp_path) -> None:
        # Built through the real mapper, so any predicate the builder can emit is
        # exercised — this is what would have caught `identifier` and the AOP keys.
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.context import ISA_TOX_CONTEXT
        from tests.fixtures.vhps_golden_crates import vhps_fixture_state

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(vhps_fixture_state("S-VHPS21"), crate, tmp_path, materialize_payload=False)
        missed = self._invisible(crate.metadata.generate()["@graph"])
        assert not missed, (
            "reference predicates invisible to the reachability walk "
            f"(add them to _PRIMARY_RELATIONS/_SECONDARY_RELATIONS): {missed}"
        )

    def test_known_reference_predicates_are_all_covered(self) -> None:
        # The specific ones that have bitten, named so a regression is legible
        # rather than surfacing as a mystery orphan count.
        covered = self._covered()
        for predicate in (
            "affiliation",
            "instrument",
            "identifier",
            "has_key_event",
            "has_key_event_relationship",
            "has_molecular_initiating_event",
            "has_adverse_outcome",
            "upstream_event",
            "downstream_event",
            "contributor",
            "contactPoint",
        ):
            assert predicate in covered, f"{predicate!r} is not a traversable relation"

    def test_aop_subgraph_is_reachable_end_to_end(self) -> None:
        # AOP -> KeyEventRelationship -> upstream/downstream KeyEvent. Every hop
        # used to be invisible, so the entire subgraph read as orphaned.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#study"}]},
                {
                    "@id": "#study",
                    "@type": "Dataset",
                    "additionalType": "Study",
                    "aop": [{"@id": "#aop"}],
                },
                {
                    "@id": "#aop",
                    "@type": "AdverseOutcomePathway",
                    "has_key_event_relationship": [{"@id": "#ker"}],
                    "has_molecular_initiating_event": [{"@id": "#mie"}],
                },
                {
                    "@id": "#ker",
                    "@type": "KeyEventRelationship",
                    "upstream_event": {"@id": "#mie"},
                    "downstream_event": {"@id": "#ao"},
                },
                {"@id": "#mie", "@type": "KeyEvent", "name": "MIE"},
                {"@id": "#ao", "@type": "KeyEvent", "name": "Adverse outcome"},
            ]
        }
        orphans = [
            n["id"] for n in build_crate_graph(graph, all_edges=True)["nodes"] if n["orphan"]
        ]
        assert orphans == [], f"AOP subgraph reported as orphaned: {orphans}"

    def test_identifier_property_values_are_reachable(self) -> None:
        # A compound's CAS / PubChem CID nodes are referenced via `identifier`;
        # they must ride along with the compound, not read as dangling.
        graph = {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "additionalType": "Investigation",
                    "hasPart": [{"@id": "#study"}],
                },
                {
                    "@id": "#study",
                    "@type": "Dataset",
                    "additionalType": "Study",
                    "chemicals": [{"@id": "#c"}],
                },
                {
                    "@id": "#c",
                    "@type": "MolecularEntity",
                    "name": "Aflatoxin B1",
                    "identifier": [{"@id": "#cas"}, {"@id": "#cid"}],
                },
                {"@id": "#cas", "@type": "PropertyValue", "name": "CAS", "value": "1162-65-8"},
                {"@id": "#cid", "@type": "PropertyValue", "name": "PubChem CID", "value": "186907"},
            ]
        }
        orphans = [
            n["id"] for n in build_crate_graph(graph, all_edges=True)["nodes"] if n["orphan"]
        ]
        assert orphans == [], f"identifier PropertyValues reported as orphaned: {orphans}"


class TestAutogeneratedBadge:
    """A file the crate generated itself is badged, not spelled out.

    The builder prefixes such names with `AUTOGENERATED — ` so no reader mistakes
    a scaffold for measured data. In a diagram that prefix is 16 characters
    against an 18-character label budget, so every generated file rendered as
    `AUTOGENERATED — C…`: the warning survived, the filename did not, and two
    generated files were indistinguishable on the page.
    """

    _BADGE = "⚠️"

    @staticmethod
    def _graph(name: str) -> list[dict]:
        return [
            {"@id": "./", "@type": "Dataset"},
            {
                "@id": "#p1",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#s1"},
                "result": {"@id": "data/ct.csv"},
            },
            {"@id": "#s1", "@type": "Sample", "name": "CHO-K1 culture"},
            {"@id": "data/ct.csv", "@type": ["File", "csvw:Table"], "name": name},
        ]

    @staticmethod
    def _labels(svg: str) -> list[str]:
        return re.findall(r'<text class="name"[^>]*>([^<]*)</text>', svg)

    def test_marker_matches_the_builders(self) -> None:
        """The writer holds the marker as a literal to stay stdlib-only.

        That is only safe while the two agree, so this is the coupling: if the
        builder ever renames the marker, the badge silently stops appearing and
        nothing else would notice.
        """
        from builder.tools._crate_mapping import AUTOGENERATED_MARKER
        from builder.writers.provenance_dag import _AUTOGENERATED_MARKER

        assert _AUTOGENERATED_MARKER == AUTOGENERATED_MARKER

    def test_generated_file_is_badged_and_keeps_its_name(self) -> None:
        svg = render_provenance_svg(self._graph("AUTOGENERATED — Condition table"))

        assert f"{self._BADGE} Condition table" in self._labels(svg)
        assert "AUTOGENERATED" not in " ".join(self._labels(svg))

    def test_the_name_survives_the_truncation_that_used_to_eat_it(self) -> None:
        """The regression itself: the old label was all prefix and no filename."""
        labels = self._labels(render_provenance_svg(self._graph("AUTOGENERATED — Condition table")))

        assert not any(label.startswith("AUTOGENERATED") for label in labels)
        assert not any(label.endswith("…") for label in labels), labels

    def test_two_generated_files_stay_distinguishable(self) -> None:
        """What the truncation cost: both used to render `AUTOGENERATED — C…`."""
        graph = self._graph("AUTOGENERATED — Condition table")
        graph[1]["result"] = [{"@id": "data/ct.csv"}, {"@id": "data/raw.csv"}]
        graph.append(
            {
                "@id": "data/raw.csv",
                "@type": ["File", "csvw:Table"],
                "name": "AUTOGENERATED — Raw measurements",
            }
        )
        labels = self._labels(render_provenance_svg(graph))

        assert f"{self._BADGE} Condition table" in labels
        assert f"{self._BADGE} Raw measurements" in labels

    def test_the_title_keeps_the_crate_s_own_wording(self) -> None:
        """The badge abbreviates the label, never the fact.

        The tooltip has no width limit, so it carries the marker spelled out —
        anyone checking the metadata reads what the crate actually says.
        """
        svg = render_provenance_svg(self._graph("AUTOGENERATED — Condition table"))

        assert "AUTOGENERATED — Condition table" in svg

    def test_a_depositor_file_is_left_alone(self) -> None:
        """Honesty control: the badge must mark generated files, not all files."""
        labels = self._labels(render_provenance_svg(self._graph("plate_map.csv")))

        assert "plate_map.csv" in labels
        assert self._BADGE not in " ".join(labels)

    def test_a_name_merely_mentioning_the_word_is_not_badged(self) -> None:
        """Anchored at the start — a depositor may legitimately use the word."""
        labels = self._labels(render_provenance_svg(self._graph("Notes on AUTOGENERATED data")))

        assert self._BADGE not in " ".join(labels)

    @pytest.mark.parametrize(
        "name",
        [
            "AUTOGENERATED — Condition table",
            "AUTOGENERATED - Condition table",
            "AUTOGENERATED: Condition table",
            "autogenerated — Condition table",
            "AUTOGENERATED Condition table",
        ],
        ids=["em-dash", "hyphen", "colon", "lowercase", "no-separator"],
    )
    def test_separator_variants_leave_no_stray_punctuation(self, name: str) -> None:
        """A half-stripped prefix (`⚠️ — Condition table`) is worse than none."""
        labels = self._labels(render_provenance_svg(self._graph(name)))

        assert f"{self._BADGE} Condition table" in labels, labels

    def test_a_bare_marker_falls_back_to_the_node_id(self) -> None:
        """`_autogenerated_name("")` yields the bare marker; a lone badge says nothing."""
        labels = self._labels(render_provenance_svg(self._graph("AUTOGENERATED")))

        assert self._BADGE in " ".join(labels)
        assert not any(label.strip() == self._BADGE for label in labels), labels

    def test_the_mermaid_renderer_badges_it_too(self) -> None:
        """Same diagram, other output — they must not disagree."""
        from builder.writers.provenance_dag import render_provenance_mermaid

        out = render_provenance_mermaid(self._graph("AUTOGENERATED — Condition table"))

        assert f"{self._BADGE} Condition table" in out


class TestUnreachableIsSplitByShape:
    """"Unreachable" is two different problems with two different repair costs.

    An entity joined to nothing needs a link of its own. An island of entities
    already joined to each other needs ONE link between them all — reachability
    here is undirected, so attaching any member attaches the whole island.
    Reporting a flat count conflates them and overstates the work, and it gets
    worse the more structure the crate already has: a well-connected island of
    thirty reads as thirty problems when it is one.
    """

    @staticmethod
    def _graph() -> list[dict]:
        return [
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#ok"}]},
            {"@id": "#ok", "@type": "File", "name": "reachable"},
            # Two entities joined to nothing at all.
            {"@id": "#lone1", "@type": "Person", "name": "Alone A"},
            {"@id": "#lone2", "@type": "Person", "name": "Alone B"},
            # One island of three, wired to each other but not to the root.
            {
                "@id": "#i1",
                "@type": "MolecularEntity",
                "name": "Cmpd",
                "mentions": [{"@id": "#i2"}],
            },
            {"@id": "#i2", "@type": "LabProcess", "name": "Proc", "result": {"@id": "#i3"}},
            {"@id": "#i3", "@type": "File", "name": "Result"},
        ]

    def _model(self) -> dict:
        return build_crate_graph(self._graph(), all_edges=True)

    def test_the_two_kinds_are_counted_separately(self) -> None:
        counts = self._model()["counts"]

        assert counts["isolated"] == 2
        assert counts["stranded"] == 3

    def test_orphan_stays_the_union(self) -> None:
        """Existing consumers read `orphan`; splitting must not move their numbers."""
        counts = self._model()["counts"]

        assert counts["orphan"] == counts["isolated"] + counts["stranded"] == 5

    def test_the_repair_count_is_groups_not_entities(self) -> None:
        """The whole point: 5 unreachable entities, 3 missing links."""
        counts = self._model()["counts"]

        assert counts["unreachable_clusters"] == 3
        assert counts["unreachable_clusters"] < counts["orphan"]

    def test_an_island_shares_one_group_number(self) -> None:
        by_id = {n["id"]: n for n in self._model()["nodes"]}

        island = {by_id[i]["cluster"] for i in ("#i1", "#i2", "#i3")}
        assert len(island) == 1, "entities linked to each other must share a group"
        assert by_id["#i1"]["cluster_size"] == 3

    def test_lone_entities_get_their_own_groups(self) -> None:
        by_id = {n["id"]: n for n in self._model()["nodes"]}

        assert by_id["#lone1"]["cluster"] != by_id["#lone2"]["cluster"]
        assert by_id["#lone1"]["cluster_size"] == 1

    def test_reachable_entities_carry_no_group(self) -> None:
        by_id = {n["id"]: n for n in self._model()["nodes"]}

        assert by_id["#ok"]["reach"] == "linked"
        assert by_id["#ok"]["cluster"] is None
        assert by_id["./"]["reach"] == "linked"

    def test_growing_the_island_does_not_grow_the_work(self) -> None:
        """The property that makes the count worth reporting.

        Adding entities to an existing island raises the unreachable count and
        leaves the number of missing links exactly where it was.
        """
        graph = self._graph()
        graph.append({"@id": "#i4", "@type": "File", "name": "More"})
        graph[5]["result"] = [{"@id": "#i3"}, {"@id": "#i4"}]
        counts = build_crate_graph(graph, all_edges=True)["counts"]

        assert counts["orphan"] == 6
        assert counts["unreachable_clusters"] == 3

    def test_an_external_reference_does_not_join_two_islands(self) -> None:
        """Only in-crate edges can carry reachability, so only they merge groups.

        Two entities that both cite the same ORCID are not one link from being
        fixed — that edge can never reach the root.
        """
        graph = self._graph()
        orcid = "https://orcid.org/0000-0002-1825-0097"
        graph.append({"@id": "#p1", "@type": "Person", "name": "P1", "identifier": {"@id": orcid}})
        graph.append({"@id": "#p2", "@type": "Person", "name": "P2", "identifier": {"@id": orcid}})
        counts = build_crate_graph(graph, all_edges=True)["counts"]

        assert counts["unreachable_clusters"] == 5, "external hop must not merge groups"

    def test_group_numbering_is_deterministic(self) -> None:
        first = {n["id"]: n["cluster"] for n in self._model()["nodes"]}
        second = {n["id"]: n["cluster"] for n in self._model()["nodes"]}

        assert first == second

    def test_a_fully_connected_crate_reports_no_work(self) -> None:
        counts = build_crate_graph(
            [
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#ok"}]},
                {"@id": "#ok", "@type": "File", "name": "reachable"},
            ],
            all_edges=True,
        )["counts"]

        assert counts["orphan"] == 0
        assert counts["isolated"] == counts["stranded"] == 0
        assert counts["unreachable_clusters"] == 0

    def test_the_map_marks_the_two_kinds_differently(self) -> None:
        svg = render_overview_svg(self._model())

        assert "orphan isolated" in svg
        assert "orphan stranded" in svg

    def test_a_tooltip_names_the_group_so_members_can_be_found(self) -> None:
        """A 13px tile can only say something is wrong; the tooltip says what."""
        svg = render_overview_svg(self._model())

        assert "linked to nothing at all" in svg
        assert "one link reconnects them all" in svg


class TestLabelsStayDistinct:
    """Two entities must never draw as the same text.

    A diagram that renders distinct entities identically is worse than one with
    a clumsier label: the reader cannot tell them apart, and may reasonably read
    it as the crate containing a duplicate. On the exported deposits this was not
    an edge case — 26 crates had at least one such pair, 117 tiles in total,
    because process and file names in this domain share long prefixes AND long
    suffixes by convention.
    """

    @staticmethod
    def _labels(svg: str) -> list[str]:
        return re.findall(r'<text class="name"[^>]*>([^<]*)</text>', svg)

    @staticmethod
    def _chain(*names: str) -> list[dict]:
        """A provenance chain whose files carry *names*."""
        graph: list[dict] = [{"@id": "./", "@type": "Dataset"}]
        graph.append(
            {
                "@id": "#p1",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#s1"},
                "result": [{"@id": f"data/f{i}.csv"} for i in range(len(names))],
            }
        )
        graph.append({"@id": "#s1", "@type": "Sample", "name": "Culture"})
        for i, name in enumerate(names):
            graph.append({"@id": f"data/f{i}.csv", "@type": "File", "name": name})
        return graph

    def test_names_sharing_a_long_prefix_stay_distinct(self) -> None:
        """The common shape: a run number at the very end of a long filename."""
        svg = render_provenance_svg(
            self._chain("220825_RA_CHO-K1_plate_run1.csv", "220825_RA_CHO-K1_plate_run2.csv")
        )
        labels = self._labels(svg)

        assert len(set(labels)) == len(labels), labels
        assert any("run1" in label for label in labels), labels
        assert any("run2" in label for label in labels), labels

    def test_names_sharing_a_prefix_AND_a_suffix_stay_distinct(self) -> None:
        """The hard shape: what distinguishes them is in the middle.

        Cutting either end throws away the only identifying part, which is why
        a plain head- or middle-truncation cannot solve this.
        """
        labels = self._labels(
            render_provenance_svg(
                self._chain(
                    "Culture neural cell lines for deiodinase assay output",
                    "Culture neural cell lines for thyroid transport assay output",
                )
            )
        )

        assert len(set(labels)) == len(labels), labels
        assert any("deiodinase" in label for label in labels), labels

    def test_a_strict_prefix_keeps_its_plain_label(self) -> None:
        """`X` beside `X study`: one has nothing extra to show."""
        labels = self._labels(
            render_provenance_svg(
                self._chain("Whole-cell metabolism assay", "Whole-cell metabolism assay study")
            )
        )

        assert len(set(labels)) == len(labels), labels

    def test_three_way_collisions_resolve(self) -> None:
        labels = self._labels(
            render_provenance_svg(
                self._chain(
                    "proc_culture_sk_n_as_cells output sample",
                    "proc_culture_sk_n_as_and_mo313_cells output sample",
                    "proc_culture_sk_n_as_h4_and_mo313_cells output sample",
                )
            )
        )

        assert len(set(labels)) == len(labels), labels

    def test_two_groups_resolving_to_the_same_core_are_split(self) -> None:
        """Fixing one group can land it on another group's label.

        Both families reduce to the same distinguishing core here, so without a
        second pass — and without keeping a little of the original opening — the
        two fixes cancel each other out.
        """
        labels = self._labels(
            render_provenance_svg(
                self._chain(
                    "Culture neural cell lines for deiodinase assay",
                    "Culture neural cell lines for transport assay",
                    "Input (Culture neural cell lines for deiodinase assay)",
                    "Input (Culture neural cell lines for transport assay)",
                )
            )
        )

        assert len(set(labels)) == len(labels), labels

    def test_identical_names_are_left_alone(self) -> None:
        """Honesty control: a real duplicate is the crate's fact to report.

        Two entities that genuinely carry one name must keep one label — inventing
        a difference would hide a duplicate the reader should see.
        """
        labels = self._labels(render_provenance_svg(self._chain("plate.csv", "plate.csv")))

        assert labels.count("plate.csv") == 2, labels

    def test_short_names_are_never_mangled(self) -> None:
        """Nothing that already fits should acquire an ellipsis."""
        labels = self._labels(render_provenance_svg(self._chain("run1.csv", "run2.csv")))

        assert "run1.csv" in labels and "run2.csv" in labels
        assert not any("…" in label for label in labels), labels

    def test_labels_stay_within_the_node(self) -> None:
        """The node is 138px wide; a label that overflows it is not a fix."""
        labels = self._labels(
            render_provenance_svg(
                self._chain(
                    "Culture neural cell lines for deiodinase assay output sample",
                    "Culture neural cell lines for thyroid transport assay output sample",
                )
            )
        )

        assert all(len(label) <= 20 for label in labels), labels

    def test_the_title_still_carries_the_full_name(self) -> None:
        """Whatever the label does, the tooltip stays the unabridged truth."""
        svg = render_provenance_svg(
            self._chain("220825_RA_CHO-K1_plate_run1.csv", "220825_RA_CHO-K1_plate_run2.csv")
        )

        assert "220825_RA_CHO-K1_plate_run1.csv" in svg
        assert "220825_RA_CHO-K1_plate_run2.csv" in svg

    def test_it_is_deterministic(self) -> None:
        graph = self._chain("a_long_shared_prefix_x.csv", "a_long_shared_prefix_y.csv")

        assert self._labels(render_provenance_svg(graph)) == self._labels(
            render_provenance_svg(graph)
        )

    def test_the_isa_view_resolves_too(self) -> None:
        """The ISA renderer rebuilds its brief dict, so it can drop the label."""
        graph = [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "CHO-K1 hOATP1C1 transporter assay",
                "hasPart": [{"@id": "#st1"}],
            },
            {
                "@id": "#st1",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "CHO-K1 hOATP1C1 time-course assay",
            },
        ]
        labels = self._labels(render_isa_svg(build_isa_inventory(graph)))

        assert len(set(labels)) == len(labels), labels
