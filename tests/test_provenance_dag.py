"""Tests for the provenance DAG renderer (``builder/writers/provenance_dag.py``).

The renderer turns an assembled RO-Crate metadata document (the ``@graph`` from
``crate.metadata.generate()`` or a parsed ``ro-crate-metadata.json``) into a
Mermaid ``flowchart`` of the LabProcess derivation chain — input/output edges
only, generated from real data rather than hand-drawn.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import xml.etree.ElementTree as ET

import pytest
from rocrate.rocrate import ROCrate

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools._crate_mapping import populate_crate
from builder.writers.maturity_report import _CSS_PATH as _CSS_SOURCE
from builder.writers.maturity_report import _load_css
from builder.writers.entity_explorer import (
    build_explorer_payload,
    render_explorer_section,
)
from builder.writers.provenance_dag import (
    CATEGORY_STYLES,
    PATHWAY_TYPES,
    _CTX_COLOUR,
    _CTX_GLYPH,
    _derivation_edges,
    _entity_category,
    _graph_nodes,
    _is_process,
    _node_class,
    _node_class_for_brief,
    _route_hop_ids,
    _tag,
    build_cellline_inventory,
    build_chemical_inventory,
    build_citation_inventory,
    build_crate_graph,
    build_isa_inventory,
    build_people_inventory,
)
from profiles.context import ISA_TOX_CONTEXT
from tests.fixtures.colour import ciede, contrast_on_white, srgb_to_lab


# The colour measures are shared with the MIT-module palette tests
# (tests/fixtures/colour.py); the private names below are the ones this module
# has always used.
_srgb_to_lab = srgb_to_lab
_ciede = ciede
_contrast_on_white = contrast_on_white


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


class TestSharedSelectors:
    """The selections the static views draw are the ones other renderers reuse.

    The derivation chain and the routed bands each used to decide membership
    inside the function that drew it, so a second renderer over the same crate
    could only re-derive the rule and drift from it. Both rules are now named
    functions, and these tests pin them to what the SVG actually draws.
    """

    def test_derivation_edges_point_downstream(self) -> None:
        nodes = _graph_nodes(_full_chain_graph())

        edges = _derivation_edges(nodes)

        # material --object--> process, process --result--> data.
        assert ("#cellline", "#cc", "object") in edges
        assert all(kind in ("object", "result") for _s, _d, kind in edges)
        for src, dst, kind in edges:
            assert src in nodes and dst in nodes, (src, dst)
            assert _is_process(nodes[dst] if kind == "object" else nodes[src])

    def test_derivation_edges_skip_references_outside_the_crate(self) -> None:
        """An off-graph ref has nothing to attach to; the SVG never drew one."""
        nodes = _graph_nodes(
            {
                "@graph": [
                    {"@id": "./", "@type": "Dataset"},
                    {
                        "@id": "#p",
                        "@type": "LabProcess",
                        "input": {"@id": "https://example.org/elsewhere"},
                        "result": {"@id": "#kept"},
                    },
                    {"@id": "#kept", "@type": "File"},
                ]
            }
        )

        edges = _derivation_edges(nodes)

        assert edges == [("#p", "#kept", "result")]

    def test_the_chain_svg_draws_exactly_the_derivation_endpoints(self) -> None:
        """The equivalence the explorer's LabProcesses view rests on: the view is
        the endpoint set of these edges, so a change to either that does not move
        the other is a change that makes the toggle lie about the chain."""
        graph = _full_chain_graph()
        nodes = _graph_nodes(graph)
        endpoints = {e[0] for e in _derivation_edges(nodes)} | {
            e[1] for e in _derivation_edges(nodes)
        }

        members = next(
            v["members"]
            for v in build_explorer_payload(graph)["views"]
            if v["key"] == "processes"
        )

        assert set(members) == endpoints
        # …and nothing else: a node the chain does not touch stays out.
        assert "http://purl.obolibrary.org/obo/NCIT_C16403" not in members

    def test_route_hop_ids_walks_process_then_via(self) -> None:
        """A compound reached through a table: two hops, rightmost last."""
        assert _route_hop_ids("#proc", "data/table.csv") == ["#proc", "data/table.csv"]

    def test_route_hop_ids_collapses_a_process_that_is_its_own_via(self) -> None:
        """A CellCulture consuming its own cell line: drawing the process in two
        columns with an edge between them would depict a step the crate has not."""
        assert _route_hop_ids("#proc", "#proc") == ["#proc"]

    def test_route_hop_ids_of_an_unlinked_member_is_empty(self) -> None:
        assert _route_hop_ids(None, None) == []
        assert _route_hop_ids("#proc", None) == []


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
        payload = build_explorer_payload(self._graph())
        members = next(v["members"] for v in payload["views"] if v["key"] == "people")
        links = {(e["src"], e["dst"]) for e in payload["edges"]}

        assert "#org_a" in members and "#org_b" in members
        # The person is credited, and BOTH affiliations are edges out of them —
        # a second affiliation used to be dropped rather than drawn.
        assert ("./", "#p") in links
        assert ("#p", "#org_a") in links and ("#p", "#org_b") in links

    def test_topology_agrees_that_both_are_reachable(self) -> None:
        model = build_crate_graph(self._graph(), all_edges=True)
        assert model["counts"]["orphan"] == 0


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
    out = render_explorer_section(graph)
    assert 'id="entity-explorer"' in out
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


def _citation_graph(
    *,
    cite: bool = True,
    doi: bool = True,
    dangling_author: bool = False,
    authors: bool = True,
) -> dict:
    """A crate citing one paper, optionally broken the ways real crates are.

    ``cite=False`` drops the root's ``citation`` (the paper is in the crate and
    nothing points at it); ``doi=False`` gives the article a local ``@id`` and no
    identifier; ``dangling_author`` adds the ``#CitationAuthor_…`` stub the
    publication resolver mints for a Crossref author with no ORCID, which no node
    in the ``@graph`` answers to (#532).
    """
    article_id = "https://doi.org/10.1007/s00204-024-03787-2" if doi else "#Publication_oatp"
    article: dict = {
        "@id": article_id,
        "@type": "ScholarlyArticle",
        "name": "Two novel in vitro assays for OATP1C1",
        "datePublished": "2024",
    }
    if authors:
        article["author"] = [{"@id": "https://orcid.org/0000-0002-1825-0097"}] + (
            [{"@id": "#CitationAuthor_Zhongli_Chen"}] if dangling_author else []
        )
    root: dict = {"@id": "./", "@type": "Dataset", "name": "Crate"}
    if cite:
        root["citation"] = [{"@id": article_id}]
    return {
        "@graph": [
            {"@id": "ro-crate-metadata.json", "about": {"@id": "./"}},
            root,
            article,
            {
                "@id": "https://orcid.org/0000-0002-1825-0097",
                "@type": "Person",
                "name": "Josiah Carberry",
            },
        ]
    }


class TestBuildCitationInventory:
    """``build_citation_inventory`` models the crate's citations.

    A citation fails two ways: its ROUTE fails when nothing points at the
    article, and its IDENTITY fails when the work carries no DOI or its credit
    list points at ``@id``\\ s the crate does not contain (#532).
    """

    def _article(self, graph: dict) -> dict:
        articles = build_citation_inventory(graph)["articles"]
        assert len(articles) == 1
        return articles[0]

    def test_resolves_the_citing_entity_and_the_doi(self) -> None:
        article = self._article(_citation_graph())
        assert article["state"] == "cited"
        assert article["source"] == "./"
        assert article["edge"] == "citation"
        assert article["doi"] == "10.1007/s00204-024-03787-2"

    def test_doi_keeps_the_slash_in_its_suffix(self) -> None:
        # The ORCID/ROR fallback reads the tail after the LAST slash; a DOI
        # contains one, so that route would report "s00204-024-03787-2" — an
        # identifier that resolves to nothing.
        assert "/" in self._article(_citation_graph())["doi"]

    def test_article_nothing_points_at_is_uncited(self) -> None:
        article = self._article(_citation_graph(cite=False))
        assert article["state"] == "uncited"
        assert article["source"] is None
        assert article["fields"]["Cited in the crate"] is False

    def test_article_without_a_doi_is_not_identified(self) -> None:
        article = self._article(_citation_graph(doi=False))
        assert article["doi"] is None
        assert article["fields"]["Resolvable DOI"] is False

    def test_author_id_no_node_answers_to_is_kept_and_marked(self) -> None:
        # Dropping it would leave the article looking fully attributed — which is
        # exactly how #532 stayed invisible in the JSON.
        article = self._article(_citation_graph(dangling_author=True))
        by_id = {a["id"]: a for a in article["authors"]}
        assert by_id["https://orcid.org/0000-0002-1825-0097"]["resolved"] is True
        assert by_id["#CitationAuthor_Zhongli_Chen"]["resolved"] is False
        assert article["fields"]["Every author resolves to an entity"] is False

    def test_resolved_author_carries_its_orcid(self) -> None:
        article = self._article(_citation_graph())
        assert article["authors"][0]["pid"] == "0000-0002-1825-0097"

    def test_article_with_no_credit_list_scores_resolution_as_not_applicable(self) -> None:
        # No authors is one absence, not two; scoring "every author resolves" as
        # a miss would count the same gap twice.
        article = self._article(_citation_graph(authors=False))
        assert article["fields"]["Authors listed"] is False
        assert article["fields"]["Every author resolves to an entity"] is None

    def test_contributor_only_credit_list_still_counts_as_authors(self) -> None:
        graph = _citation_graph()
        graph["@graph"][2]["contributor"] = graph["@graph"][2].pop("author")
        assert self._article(graph)["fields"]["Authors listed"] is True

    def test_author_repeated_as_contributor_is_one_person(self) -> None:
        graph = _citation_graph()
        graph["@graph"][2]["contributor"] = list(graph["@graph"][2]["author"])
        assert len(self._article(graph)["authors"]) == 1

    def test_placeholder_date_is_not_a_publication_date(self) -> None:
        # The resolver stringifies a missing Crossref field, so a shipped crate
        # carries `"datePublished": "None"`. Scoring that as present would paint
        # a green column for an article that states no date at all.
        graph = _citation_graph()
        graph["@graph"][2]["datePublished"] = "None"
        assert self._article(graph)["fields"]["Publication date"] is False

    def test_counts_summarise_route_identity_and_credit(self) -> None:
        counts = build_citation_inventory(_citation_graph(dangling_author=True))["counts"]
        assert counts["total"] == 1
        assert counts["cited"] == 1
        assert counts["uncited"] == 0
        assert counts["doi_backed"] == 1
        assert counts["authors"] == 2
        assert counts["unresolved_authors"] == 1

    def test_the_same_broken_author_id_on_two_papers_is_one_fix(self) -> None:
        graph = _citation_graph(dangling_author=True)
        second = dict(graph["@graph"][2], **{"@id": "#Publication_second", "name": "Second paper"})
        graph["@graph"].append(second)
        graph["@graph"][1]["citation"].append({"@id": "#Publication_second"})
        counts = build_citation_inventory(graph)["counts"]
        assert counts["total"] == 2
        assert counts["unresolved_authors"] == 1

    def test_crate_without_articles_is_empty(self) -> None:
        inv = build_citation_inventory({"@graph": [{"@id": "#f", "@type": "File", "name": "a"}]})
        assert inv["articles"] == []
        assert inv["groups"] == []
        assert inv["counts"]["total"] == 0


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
        assert "Entity coverage" in page


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
    def _labels(graph: list[dict]) -> list[str]:
        """Every node label the crate's own graph model yields.

        Read from the model rather than from a rendered figure: the badge is
        `_display_name`'s doing, and asserting it through whichever renderer
        happens to exist is how these tests came to depend on one that has since
        been deleted (#618)."""
        return [n["label"] for n in build_explorer_payload({"@graph": graph})["nodes"]]

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
        labels = self._labels(self._graph("AUTOGENERATED — Condition table"))

        assert f"{self._BADGE} Condition table" in labels
        assert "AUTOGENERATED" not in " ".join(labels)

    def test_the_name_survives_the_truncation_that_used_to_eat_it(self) -> None:
        """The regression itself: the old label was all prefix and no filename."""
        labels = self._labels(self._graph("AUTOGENERATED — Condition table"))

        assert not any(label.startswith("AUTOGENERATED") for label in labels)
        # The filename survives, which is what the prefix used to crowd out.
        assert f"{self._BADGE} Condition table" in labels, labels

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
        labels = self._labels(graph)

        assert f"{self._BADGE} Condition table" in labels
        assert f"{self._BADGE} Raw measurements" in labels

    def test_the_title_keeps_the_crate_s_own_wording(self) -> None:
        """The badge abbreviates the label, never the fact.

        The tooltip has no width limit, so it carries the marker spelled out —
        anyone checking the metadata reads what the crate actually says.
        """
        payload = build_explorer_payload({"@graph": self._graph("AUTOGENERATED — Condition table")})

        names = [n["name"] for n in payload["nodes"]]
        assert "AUTOGENERATED — Condition table" in names

    def test_a_depositor_file_is_left_alone(self) -> None:
        """Honesty control: the badge must mark generated files, not all files."""
        labels = self._labels(self._graph("plate_map.csv"))

        assert "plate_map.csv" in labels
        assert self._BADGE not in " ".join(labels)

    def test_a_name_merely_mentioning_the_word_is_not_badged(self) -> None:
        """Anchored at the start — a depositor may legitimately use the word."""
        labels = self._labels(self._graph("Notes on AUTOGENERATED data"))

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
        labels = self._labels(self._graph(name))

        assert f"{self._BADGE} Condition table" in labels, labels

    def test_a_bare_marker_falls_back_to_the_node_id(self) -> None:
        """`_autogenerated_name("")` yields the bare marker; a lone badge says nothing."""
        labels = self._labels(self._graph("AUTOGENERATED"))

        assert self._BADGE in " ".join(labels)
        assert not any(label.strip() == self._BADGE for label in labels), labels

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




class TestCategoryRegistry:
    """One entity type, one colour and one shape, in every view.

    Before the registry the same type was drawn three different ways: a File was
    magenta in the maturity report, yellow in the crate graph and brown in the
    provenance DAG, and a Dataset was a barred indigo block in the ISA tab and an
    anonymous grey box in the cell-line and people tabs. Colour that changes
    between tabs does not merely look untidy — it teaches the reader that colour
    carries no meaning, which costs them the one channel the overview map has.
    """

    def test_every_category_has_a_unique_glyph(self) -> None:
        """The interactive explorer draws a glyph where the SVG views draw an
        outline, and it obeys the same rule: shape is the channel that survives
        greyscale, print and colour vision deficiency, so two categories sharing
        a glyph would leave colour as the only thing telling them apart."""
        glyphs = [style.glyph for style in CATEGORY_STYLES.values()] + [_CTX_GLYPH]

        assert len(set(glyphs)) == len(glyphs), glyphs

    def test_every_glyph_is_path_data_inside_the_fourteen_pixel_box(self) -> None:
        """The glyph is rendered into ``viewBox="0 0 14 14"``. A coordinate
        outside that box is clipped by the viewport, and a clipped glyph is a
        different shape from the one the legend promises."""
        for category, glyph in [
            *((k, s.glyph) for k, s in CATEGORY_STYLES.items()),
            ("ctx", _CTX_GLYPH),
        ]:
            assert glyph.startswith(("M", "m")), (category, glyph)
            assert re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9 .,-]+", glyph), (category, glyph)
            for literal in re.findall(r"-?\d+(?:\.\d+)?", glyph):
                assert -14.0 <= float(literal) <= 14.0, (category, glyph, literal)

    def test_no_view_hardcodes_a_category_colour(self) -> None:
        """The registry has to be the only place a category colour is written.

        Comparing the generated CSS against the registry would compare the
        registry with itself and pass no matter what. What can still go wrong is
        someone writing a colour back into a view by hand — which is exactly how
        the three palettes diverged — so this asserts the *sources* are free of
        them: the stylesheet declares no ``--cat-*`` of its own, and every
        category stroke in rendered Mermaid output is the registry's.
        """
        source = _CSS_SOURCE.read_text(encoding="utf-8")

        assert not re.search(r"--cat-[a-z]+\s*:", source), "the stylesheet hardcodes a palette"

        # And the page carries no category colour the registry did not put there:
        # the explorer's palette is generated into the payload, so a hand-written
        # one would show up as a colour the registry does not know.
        payload = build_explorer_payload(_full_chain_graph())
        assert {c["colour"] for c in payload["categories"].values()} == {
            *(s.colour for s in CATEGORY_STYLES.values()),
            _CTX_COLOUR,
        }

    def test_the_substituted_stylesheet_carries_the_registry_colours(self) -> None:
        """The wiring check for the placeholder: the values actually arrive."""
        declared = dict(re.findall(r"--cat-([a-z]+):(#[0-9a-f]{6})", _load_css()))

        for category, style in CATEGORY_STYLES.items():
            assert declared.get(category) == style.colour, category

    def test_every_category_is_declared_in_the_report(self) -> None:
        """A category the stylesheet never declares has no colour to be drawn in.

        This is what the generated CSS buys: the hand-written stylesheet had a
        colour for protocols in one view and none anywhere else. The per-category
        node and tile *rules* went with the figures that used them (#618); the
        properties stay, and the substitution has to actually happen.
        """
        css = _load_css()

        assert "__CATEGORY_STYLES__" not in css
        for category in (*CATEGORY_STYLES, "ctx"):
            assert re.search(rf"--cat-{category}\s*:", css), category

    def test_colours_are_far_enough_apart_to_tell_apart(self) -> None:
        """The overview is 13px tiles, where colour is most of the signal.

        CIE76 dE, the same measure used to pick the palette. dE 20 is the floor
        for "clearly different side by side"; the hand-picked palette this
        replaced had a closest pair at 14.
        """
        pairs = [
            (_ciede(a.colour, b.colour), name_a, name_b)
            for (name_a, a), (name_b, b) in itertools.combinations(CATEGORY_STYLES.items(), 2)
        ]
        worst = min(pairs)

        assert worst[0] >= 20, f"{worst[1]} vs {worst[2]}: dE {worst[0]:.1f}"

    def test_a_dataset_is_a_container_in_every_view_that_draws_one(self) -> None:
        """The concrete regression: `_node_class_for_brief` had no Dataset arm.

        An Investigation drew as a barred indigo block in the ISA tab and a grey
        rounded box in the cell-line and people tabs — the same entity, twice.
        """
        assert _node_class_for_brief({"tag": "Dataset · Investigation"}) == "container"
        assert _node_class_for_brief({"tag": "Dataset"}) == "container"
        assert _node_class({"@type": "Dataset"}) == "container"

    def test_an_organisation_is_not_filed_as_a_person(self) -> None:
        """The people view has always drawn the two apart; the overview had not.

        `_entity_category` folded Organization into "agent", so one institution
        was slate-blue in the people tab and purple in the all-entities tab.
        """
        assert _entity_category({"@type": "Organization"}) == "org"
        assert _entity_category({"@type": "Person"}) == "agent"
        assert _node_class_for_brief({"tag": "Organization"}) == "org"

    def test_a_key_event_is_science_not_plumbing(self) -> None:
        """``annotation`` is the bucket for "an entity that qualifies another
        rather than taking part in the work". A key event is not that: it is
        what the assay measures, and the most domain-specific thing a toxicology
        crate carries. Once #627 drew them, sixteen key events and an adverse
        outcome pathway reached the canvas in the fallback colour, beside csvw
        columns, licences and the build's own ``CreateAction`` (#643).
        """
        assert _entity_category({"@type": ["KeyEvent", "DefinedTerm"]}) == "pathway"
        assert _entity_category({"@type": ["AdverseOutcomePathway", "DefinedTerm"]}) == "pathway"
        assert _node_class_for_brief({"tag": "KeyEvent"}) == "pathway"

    def test_the_link_between_two_key_events_is_part_of_the_pathway(self) -> None:
        """A ``KeyEventRelationship`` is what makes a pathway a pathway rather
        than a bag of events, and the crate mints one per relation — nineteen of
        them on a real deposit, against sixteen key events. Nothing ``mentions``
        one, so a rule that recognised only what an assay points at would leave
        the chain's every link drawn as vocabulary and captioned ``DefinedTerm``
        while the events it connects were drawn as science."""
        relationship = {"@type": ["KeyEventRelationship", "DefinedTerm"]}

        assert _entity_category(relationship) == "pathway"
        assert _tag(relationship) == "KeyEventRelationship"

    def test_an_ordinary_term_is_still_an_annotation(self) -> None:
        """The control. The rule is keyed to the two types the ISA-Tox profile
        defines, not to everything a crate happens to type ``DefinedTerm`` — a
        csvw column's term and a process parameter still qualify the work rather
        than taking part in it."""
        assert _entity_category({"@type": "DefinedTerm"}) == "annotation"
        assert _entity_category({"@type": ["PropertyValue", "DefinedTerm"]}) == "annotation"

    def test_a_pathway_type_is_both_coloured_and_captioned_by_its_own_name(self) -> None:
        """Two rules that must not drift apart: #627 made the caption prefer the
        domain type over the generic one it refines, #643 made the colour follow
        the same types. A type in one list and not the other is drawn as science
        and captioned as vocabulary, or the reverse — and no test of either half
        alone would notice."""
        for domain_type in PATHWAY_TYPES:
            node = {"@type": [domain_type, "DefinedTerm"]}

            assert _entity_category(node) == "pathway", domain_type
            assert _tag(node) == domain_type, domain_type

    def test_the_work_is_drawn_more_strongly_than_what_qualifies_it(self) -> None:
        """Why the pathway takes the olive instead of an eleventh colour.

        The ten are a ring at one lightness — L* ~47 at the highest chroma the
        sRGB gamut allows for each hue — and it is full: against a frozen
        palette the best eleventh colour anywhere in the gamut reaches dE 22.7,
        under the floor ``test_colours_are_far_enough_apart_to_tell_apart``
        pins. So the ring stays at ten and *which* entities earn a saturated one
        becomes the rule: vivid for what takes part in the work, muted for what
        comments on it, grey for what the crate never said (``ctx``). Drawing a
        key event more faintly than a csvw column restates #643 in a different
        colour.
        """
        assert self._chroma(CATEGORY_STYLES["pathway"].colour) >= 40
        assert self._chroma(CATEGORY_STYLES["annotation"].colour) <= 25

    @staticmethod
    def _chroma(colour: str) -> float:
        """C* — how saturated a colour is, independent of hue and lightness."""
        _, a, b = srgb_to_lab(colour)
        return math.hypot(a, b)

    @staticmethod
    def _graph(author: list[dict] | None) -> dict:
        article: dict = {
            "@id": "#a1",
            "@type": "ScholarlyArticle",
            "name": "A paper",
            "identifier": "10.6019/s-vhps22",
        }
        if author is not None:
            article["author"] = author
        return {
            "@graph": [
                {"@id": "./", "@type": "Dataset", "citation": [{"@id": "#a1"}]},
                article,
                {"@id": "https://ror.org/02catss52", "@type": "Organization", "name": "Brown"},
                {"@id": "#p1", "@type": "Person", "name": "A Person"},
            ]
        }

    def test_an_organization_author_keeps_the_organization_shape(self) -> None:
        """One entity, one colour and one shape, in EVERY view (ac3fc9b).

        A credit list is not all Persons — a Crossref affiliation resolves to an
        Organization — so painting the whole list with the agent block gives one
        entity two shapes across the report, which is the rule's whole point.
        """
        graph = self._graph([{"@id": "https://ror.org/02catss52"}, {"@id": "#p1"}])
        by_id = {n["id"]: n for n in build_explorer_payload(graph)["nodes"]}

        assert by_id["https://ror.org/02catss52"]["category"] == "org"
        assert by_id["#p1"]["category"] == "agent"

    def test_an_article_crediting_nobody_is_not_reported_as_fully_resolved(self) -> None:
        """The vacuous-truth guard.

        An empty credit list has no unresolved reference, so every warning stays
        silent and the green note would report that "every author resolves" about
        a paper that credits nobody. That is the same shape as the "MIT coverage
        0%" claim #311 removed: a confident statement about something never
        examined.
        """
        from builder.writers.maturity_report import _render_citations_panel
        from builder.writers.provenance_dag import build_citation_inventory

        panel, _badge = _render_citations_panel(build_citation_inventory(self._graph(None)))

        assert "every author resolves" not in panel
        assert "credit nobody" in panel

    def test_a_real_credit_list_still_earns_the_clean_note(self) -> None:
        """The control: the warning above must not fire on a crate that is fine."""
        from builder.writers.maturity_report import _render_citations_panel
        from builder.writers.provenance_dag import build_citation_inventory

        panel, _badge = _render_citations_panel(
            build_citation_inventory(self._graph([{"@id": "#p1"}]))
        )

        assert "every author resolves" in panel
        assert "credit nobody" not in panel
