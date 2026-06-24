"""Tests for the full crate entity-graph model + renderer (GitHub #130).

``build_crate_graph`` turns a serialized RO-Crate ``@graph`` into a deterministic
node/edge model: nodes classified into the three paper layers (packaging /
structural / domain), each reference marked in-crate / external-identifier-backed
/ dangling, with orphans flagged and a cumulative ``--layer`` filter.
``render_crate_graph`` formats that model as a layered Mermaid flowchart.

The layer classification mirrors the authoritative spec derived from the models,
context, and shapes (see the crate-graph-inventory workflow).
"""

from __future__ import annotations

import pytest

from builder.writers.provenance_dag import (
    build_crate_graph,
    normalize_layer,
    render_crate_graph,
)


def _crate() -> dict:
    """A comprehensive crate exercising all three layers + every node status."""
    return {
        "@graph": [
            # plumbing — excluded from the graph, used only to find the root
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
                "conformsTo": [{"@id": "https://w3id.org/ro/crate/1.1"}],
            },
            # root — Investigation discriminator, but must classify as PACKAGING (L1)
            {
                "@id": "./",
                "@type": "Dataset",
                "additionalType": "Investigation",
                "name": "VHP Investigation",
                "hasPart": [{"@id": "#study1"}],
                "author": {"@id": "https://orcid.org/0000-0002-1825-0097"},
            },
            # in-crate Person whose @id IS an ORCID (in-crate wins over URI-shape)
            {
                "@id": "https://orcid.org/0000-0002-1825-0097",
                "@type": "Person",
                "name": "Jane Doe",
            },
            # Study (L2) → external AOP (not a node)
            {
                "@id": "#study1",
                "@type": "Dataset",
                "additionalType": "Study",
                "name": "Hepatotox study",
                "hasPart": [{"@id": "#assay1"}],
                "mentions": {"@id": "https://aopwiki.org/aops/37"},
            },
            # Assay (L2) → external KeyEvent; about wires its processes
            {
                "@id": "#assay1",
                "@type": "Dataset",
                "additionalType": "Assay",
                "name": "Viability assay",
                "hasPart": [{"@id": "#raw"}],
                "about": [{"@id": "#cc"}, {"@id": "#exp"}, {"@id": "#er"}, {"@id": "#da"}],
                "mentions": {"@id": "https://aopwiki.org/events/55"},
            },
            # CellLine sample (L3 — additionalType promotes over base Sample)
            {
                "@id": "#hepg2",
                "@type": "Sample",
                "additionalType": "CellLine",
                "name": "HepG2",
                "identifier": "CVCL_0027",
                "sampleType": {"@id": "#cellline-term"},
            },
            # DefinedTerm (L2 structural)
            {"@id": "#cellline-term", "@type": "DefinedTerm", "name": "cell line"},
            # plain Sample (L2) with derivesFrom lineage
            {
                "@id": "#cult",
                "@type": "Sample",
                "name": "Cultured HepG2",
                "derivesFrom": {"@id": "#hepg2"},
            },
            # LabProtocol (L2)
            {"@id": "#proto", "@type": "LabProtocol", "name": "Culture protocol"},
            # CellCulture (L3)
            {
                "@id": "#cc",
                "@type": "LabProcess",
                "additionalType": "CellCulture",
                "name": "Cell Culture",
                "input": {"@id": "#hepg2"},
                "output": {"@id": "#cult"},
                "executesLabProtocol": {"@id": "#proto"},
            },
            # Exposure (L3) → condition table
            {
                "@id": "#exp",
                "@type": "LabProcess",
                "additionalType": "Exposure",
                "name": "Exposure",
                "object": {"@id": "#cult"},
                "result": {"@id": "#tbl"},
            },
            # MolecularEntity (L3)
            {"@id": "#aflb1", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
            # condition table: File + csvw:Table → reclassified to L3
            {
                "@id": "#tbl",
                "@type": ["File", "csvw:Table"],
                "name": "Condition table",
                "about": [{"@id": "#aflb1"}, {"@id": "#hepg2"}],
                "tableSchema": {"@id": "#schema"},
            },
            {
                "@id": "#schema",
                "@type": ["csvw:Schema", "CreativeWork"],
                "name": "Condition table schema",
                "columns": [{"@id": "#col1"}],
            },
            {
                "@id": "#col1",
                "@type": "csvw:Column",
                "titles": "compound",
                "valueUrl": {"@id": "#aflb1"},
            },
            # EndpointReadout (L3)
            {
                "@id": "#er",
                "@type": "LabProcess",
                "additionalType": "EndpointReadout",
                "name": "Endpoint Readout",
                "input": {"@id": "#tbl"},
                "output": {"@id": "#raw"},
            },
            # raw File (L1 packaging)
            {"@id": "#raw", "@type": "File", "name": "Raw measurements"},
            # DataAnalysis (L3) → DANGLING output (not a node, not a URI)
            {
                "@id": "#da",
                "@type": "LabProcess",
                "additionalType": "DataAnalysis",
                "name": "Data Analysis",
                "input": {"@id": "#raw"},
                "output": {"@id": "#missing_fig"},
            },
            # ORPHAN file — not referenced by anything
            {"@id": "#orphan_note", "@type": "File", "name": "Stray note"},
        ]
    }


def _by_id(model: dict) -> dict[str, dict]:
    return {n["id"]: n for n in model["nodes"]}


# --- normalize_layer --------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("all", 3),
        (None, 3),
        ("1", 1),
        (1, 1),
        ("crate", 1),
        ("2", 2),
        ("isa", 2),
        ("3", 3),
        ("isa-tox", 3),
        ("tox", 3),
    ],
)
def test_normalize_layer(value, expected) -> None:
    assert normalize_layer(value) == expected


# --- layer classification ---------------------------------------------------


def test_root_is_packaging_despite_investigation_additionaltype() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    assert nodes["./"]["layer"] == 1


def test_classification_is_prefix_agnostic() -> None:
    """A crate aliasing the csvw namespace to a non-standard prefix still lands
    its table/schema/column in the domain layer (local-name matching)."""
    graph = {
        "@graph": [
            {"@id": "./", "@type": "Dataset"},
            {"@id": "#t", "@type": ["File", "mycsv:Table"], "name": "T"},
            {"@id": "#s", "@type": "mycsv:Schema", "name": "S"},
            {"@id": "#c", "@type": "mycsv:Column", "name": "C"},
            {"@id": "#p", "@type": "schema:Person", "name": "P"},
        ]
    }
    nodes = _by_id(build_crate_graph(graph))
    assert nodes["#t"]["layer"] == 3
    assert nodes["#s"]["layer"] == 3
    assert nodes["#c"]["layer"] == 3
    assert nodes["#p"]["layer"] == 1  # schema:Person → Person → packaging


def test_bare_string_valueurl_produces_edge() -> None:
    """ro-crate-py serializes CSVW valueUrl as a bare string; it must still wire
    an edge (not be dropped as a literal)."""
    graph = {
        "@graph": [
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#col"}]},
            {"@id": "#col", "@type": "csvw:Column", "valueUrl": "#aflb1"},
            {"@id": "#aflb1", "@type": "MolecularEntity", "name": "Aflatoxin B1"},
        ]
    }
    model = build_crate_graph(graph, all_edges=True)
    assert _edge(model, "#col", "#aflb1") is not None
    # a plain-string literal under a reference-ish key is NOT turned into an edge
    nodes = _by_id(model)
    assert "culture" not in nodes


def test_string_literal_not_treated_as_reference() -> None:
    graph = {
        "@graph": [
            {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "#proto"}]},
            {"@id": "#proto", "@type": "LabProtocol", "intendedUse": "culture"},
        ]
    }
    model = build_crate_graph(graph, all_edges=True)
    assert model["counts"]["dangling"] == 0  # "culture" is a literal, not a ref


def test_layer_classification() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    assert nodes["#study1"]["layer"] == 2  # Study
    assert nodes["#assay1"]["layer"] == 2  # Assay
    assert nodes["#hepg2"]["layer"] == 3  # CellLine sample
    assert nodes["#cult"]["layer"] == 2  # plain Sample
    assert nodes["#cellline-term"]["layer"] == 2  # DefinedTerm
    assert nodes["#proto"]["layer"] == 2  # LabProtocol
    assert nodes["#cc"]["layer"] == 3  # CellCulture
    assert nodes["#exp"]["layer"] == 3  # Exposure
    assert nodes["#aflb1"]["layer"] == 3  # MolecularEntity
    assert nodes["#tbl"]["layer"] == 3  # File+csvw:Table override
    assert nodes["#schema"]["layer"] == 3  # csvw:Schema
    assert nodes["#col1"]["layer"] == 3  # csvw:Column
    assert nodes["#raw"]["layer"] == 1  # plain File
    assert nodes["https://orcid.org/0000-0002-1825-0097"]["layer"] == 1  # Person


# --- functional category (node colour/shape, orthogonal to layer) -----------


def test_entity_category_is_functional_not_layer() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    assert nodes["./"]["category"] == "container"  # root Dataset
    assert nodes["#study1"]["category"] == "container"
    assert nodes["#assay1"]["category"] == "container"
    assert nodes["#cc"]["category"] == "process"  # CellCulture
    assert nodes["#exp"]["category"] == "process"
    assert nodes["#proto"]["category"] == "protocol"
    assert nodes["#hepg2"]["category"] == "material"  # CellLine Sample
    assert nodes["#cult"]["category"] == "material"
    assert nodes["#aflb1"]["category"] == "chemical"  # MolecularEntity
    assert nodes["#tbl"]["category"] == "data"  # File + csvw:Table
    assert nodes["#raw"]["category"] == "data"
    assert nodes["#cellline-term"]["category"] == "annotation"  # DefinedTerm
    assert nodes["https://orcid.org/0000-0002-1825-0097"]["category"] == "agent"


def test_render_colours_by_category_and_tints_layer_boxes() -> None:
    out = render_crate_graph(_crate())
    # node fill = functional category
    assert "classDef cat_process" in out
    assert "classDef cat_material" in out
    assert "classDef cat_chemical" in out
    # the three layer boxes carry a subtle per-layer wash (style on the subgraph)
    assert "style layer1_g fill:" in out
    assert "style layer2_g fill:" in out
    assert "style layer3_g fill:" in out
    # layer is no longer encoded as a node fill class
    assert "classDef layer1 " not in out


# --- node status: in-crate / external / dangling ----------------------------


def test_in_crate_person_with_orcid_id_is_not_external() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    person = nodes["https://orcid.org/0000-0002-1825-0097"]
    assert person["status"] == "in_crate"
    assert person["identifier_backed"] is True  # resolvable @id


def test_external_identifier_backed_nodes() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    aop = nodes["https://aopwiki.org/aops/37"]
    assert aop["status"] == "external"
    assert aop["identifier_backed"] is True
    assert nodes["https://aopwiki.org/events/55"]["status"] == "external"


def test_dangling_reference_flagged() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    assert nodes["#missing_fig"]["status"] == "dangling"
    assert nodes["#missing_fig"]["identifier_backed"] is False


def test_orphan_flagged() -> None:
    nodes = _by_id(build_crate_graph(_crate()))
    assert nodes["#orphan_note"]["orphan"] is True
    # A well-connected node is not an orphan.
    assert nodes["#exp"]["orphan"] is False
    assert nodes["./"]["orphan"] is False  # root is never an orphan


def test_descriptor_and_preview_excluded() -> None:
    ids = {n["id"] for n in build_crate_graph(_crate())["nodes"]}
    assert "ro-crate-metadata.json" not in ids


# --- edges ------------------------------------------------------------------


def _edge(model: dict, src: str, dst: str) -> dict | None:
    for e in model["edges"]:
        if e["src"] == src and e["dst"] == dst:
            return e
    return None


def test_process_input_edge_is_reversed() -> None:
    """input/object/samples point FROM the consumed entity INTO the process."""
    model = build_crate_graph(_crate())
    assert _edge(model, "#hepg2", "#cc") is not None  # HepG2 --input--> CellCulture
    assert _edge(model, "#cc", "#hepg2") is None


def test_process_output_and_haspart_edges() -> None:
    model = build_crate_graph(_crate())
    assert _edge(model, "#cc", "#cult") is not None  # process --result--> output
    assert _edge(model, "./", "#study1") is not None  # root --hasPart--> study
    assert _edge(model, "#exp", "#tbl") is not None
    assert _edge(model, "#cult", "#hepg2") is not None  # derivesFrom


def test_annotation_edges() -> None:
    model = build_crate_graph(_crate())
    assert _edge(model, "./", "https://orcid.org/0000-0002-1825-0097") is not None  # author
    assert _edge(model, "#study1", "https://aopwiki.org/aops/37") is not None  # mentions
    assert _edge(model, "#tbl", "#aflb1") is not None  # about (compound via table)


# --- cumulative layer filter ------------------------------------------------


def test_layer_filter_all_keeps_everything() -> None:
    model = build_crate_graph(_crate(), layer="all")
    ids = {n["id"] for n in model["nodes"]}
    assert "#aflb1" in ids and "#study1" in ids and "#raw" in ids


def test_layer_filter_isa_drops_domain() -> None:
    model = build_crate_graph(_crate(), layer="isa")  # layer 2
    layers = {n["id"]: n["layer"] for n in model["nodes"] if n["status"] == "in_crate"}
    assert all(v <= 2 for v in layers.values())
    assert "#aflb1" not in layers  # MolecularEntity (L3) dropped
    assert "#study1" in layers  # Study (L2) kept
    assert model["hidden_count"] > 0


def test_layer_filter_crate_keeps_only_packaging() -> None:
    model = build_crate_graph(_crate(), layer="crate")  # layer 1
    in_crate = [n for n in model["nodes"] if n["status"] == "in_crate"]
    assert all(n["layer"] == 1 for n in in_crate)
    ids = {n["id"] for n in in_crate}
    assert "./" in ids and "#raw" in ids  # packaging survives
    assert "#study1" not in ids  # structural dropped


def test_counts_present() -> None:
    counts = build_crate_graph(_crate())["counts"]
    assert counts["layer1"] >= 1 and counts["layer2"] >= 1 and counts["layer3"] >= 1
    assert counts["external"] >= 2  # the two AOP-Wiki refs
    assert counts["dangling"] >= 1
    assert counts["orphan"] >= 1


# --- renderer (Mermaid formatting) ------------------------------------------


def test_render_has_flowchart_and_layer_subgraphs() -> None:
    out = render_crate_graph(_crate(), direction="TD")
    assert out.startswith("flowchart TD")
    assert "Packaging" in out and "Structural" in out and "Domain" in out


def test_render_has_legend_and_outside_group() -> None:
    out = render_crate_graph(_crate(), include_legend=True)
    assert "Legend" in out
    # the "outside the crate" grouping makes the in-crate/external split explicit
    assert "Outside" in out or "outside" in out


def test_render_includes_entity_names() -> None:
    out = render_crate_graph(_crate())
    for name in ("HepG2", "Exposure", "Aflatoxin B1", "Condition table", "Jane Doe"):
        assert name in out


def test_render_layer_filter_excludes_domain_names() -> None:
    out = render_crate_graph(_crate(), layer="isa")
    assert "Hepatotox study" in out  # L2 kept
    assert "Aflatoxin B1" not in out  # L3 dropped


def test_render_legend_can_be_omitted() -> None:
    assert "Legend" not in render_crate_graph(_crate(), include_legend=False)


def test_render_empty_graph() -> None:
    out = render_crate_graph({"@graph": []})
    assert out.startswith("flowchart TD")
