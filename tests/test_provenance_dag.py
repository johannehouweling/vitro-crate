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
    render_mermaid_html,
    render_provenance_mermaid,
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
