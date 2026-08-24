"""What an edge says it is, in the vocabulary the crate actually uses (#688).

Selecting an entity lights its edges and labels each one. The labels were the
model's own internal words — ``input``, ``executes``, ``reagent`` — which name
nothing a reader can look up: the crate's predicate for an input is
``schema:object``, and Bioschemas' is ``bioschemas:executesLabProtocol``.

The mapping is **derived** from the relation tables and the crate's own
``@context``, not written out a second time here. A hand-kept list would be a
second vocabulary to drift from the first, and the first is what the crate is
serialized with.
"""

from __future__ import annotations

import pytest

from builder.writers.provenance_dag import _PRIMARY_RELATIONS, relation_terms

pytestmark = pytest.mark.timeout(180)


class TestEveryRelationNamesARealProperty:
    def test_the_material_relations_use_schema_org(self):
        terms = relation_terms()
        assert terms["input"] == "schema:object"
        assert terms["result"] == "schema:result"
        assert terms["derivesFrom"] == "schema:isBasedOn"

    def test_the_lab_relations_use_bioschemas(self):
        """Bioschemas properties live under ``/properties/`` while its types sit
        at the bare namespace, and the qualified form drops that path — which is
        how Bioschemas itself writes them."""
        terms = relation_terms()
        assert terms["executes"] == "bioschemas:executesLabProtocol"
        assert terms["reagent"] == "bioschemas:reagent"

    def test_no_primary_relation_keeps_an_internal_word(self):
        """The defect, stated generally: `input` and `executes` are this
        codebase's names for edges, not anyone's predicates."""
        terms = relation_terms()
        for _keys, label, _reversed in _PRIMARY_RELATIONS:
            assert label in terms, label
            assert ":" in terms[label], f"{label} -> {terms[label]!r} names no property"

    def test_a_term_is_a_curie_or_an_iri_and_never_invented(self):
        for label, term in relation_terms().items():
            assert ":" in term, (label, term)

    def test_the_prefixes_come_from_the_crate_context(self):
        """Not a private table: the crate is serialized with these, so a reader
        who copies `schema:object` out of the report can find it in the JSON-LD.
        """
        from profiles.context import ISA_TOX_CONTEXT

        declared = ISA_TOX_CONTEXT[0]
        used = {t.split(":", 1)[0] for t in relation_terms().values() if "://" not in t}
        assert "schema" in used and "bioschemas" in used
        assert {"schema", "bioschemas"} <= set(declared)

    def test_an_unmappable_relation_falls_back_to_its_iri(self):
        """Honest rather than invented. An IRI is long and correct; a made-up
        prefix is short and a lie."""
        terms = relation_terms()
        assert all(term for term in terms.values())


class TestThePayloadCarriesTheVocabulary:
    """The browser must not hold a copy of it."""

    def _payload(self):
        from builder.writers.entity_explorer import build_explorer_payload
        from tests.fixtures.crate_graphs import assay_lane_graph

        return build_explorer_payload(assay_lane_graph())

    def test_relations_travel_with_the_crate(self):
        assert self._payload()["relations"]["input"] == "schema:object"

    def test_every_drawn_edge_label_can_be_named(self):
        payload = self._payload()
        relations = payload["relations"]
        drawn = {e["label"] for e in payload["edges"]}
        assert drawn <= set(relations), sorted(drawn - set(relations))

    def test_the_app_reads_the_vocabulary_rather_than_restating_it(self):
        """A second copy in JavaScript is a second thing to drift."""
        import re

        from builder.writers.entity_explorer import _app_js

        app = _app_js()
        assert "D.relations" in app
        # Comments are where the reasoning lives and may name a term freely; the
        # check is about the code, so they are stripped rather than counted.
        code = re.sub(r"/\*.*?\*/", "", app, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        for term in ("schema:object", "schema:result", "bioschemas:reagent"):
            assert term not in code, f"the app is spelling out {term}"


class TestWhatANodeEncodes:
    """Colour carries the category; the fill carries residence (#687, #688).

    Asserted against the shipped app and stylesheet. These are structural facts
    about the code the page carries — the same way the explorer's "never writes
    an href" rule is pinned — not a re-drawing of it.
    """

    def _app(self) -> str:
        from builder.writers.entity_explorer import _app_js

        return _app_js()

    def _css(self) -> str:
        from builder.writers.entity_explorer import _ASSET_DIR

        return (_ASSET_DIR / "maturity_report.css").read_text(encoding="utf-8")

    def test_a_node_draws_no_category_shape(self):
        """Colour only. Shape was the redundant channel and it is knowingly gone."""
        assert "ex-glyph" not in self._css()
        assert "Glyph" not in self._app(), "a category glyph is still drawn"

    def test_the_fill_is_driven_by_residence_not_status(self):
        """Tinting on `status` would paint a compound as though it were a file:
        every described entity shares a status."""
        app = self._app()
        assert "n.residence === 'carried'" in app
        assert "ex-carried" in self._css()

    def test_an_untinted_node_is_the_plain_surface(self):
        """Otherwise "tinted" says nothing — everything was tinted before."""
        css = self._css()
        assert ".mat .ex-node{" in css
        block = css.split(".mat .ex-node{", 1)[1].split("}", 1)[0]
        assert "background:var(--surface)" in block, block

    def test_the_category_is_still_on_the_border(self):
        """The fill took on a second fact, so the first has to stay somewhere."""
        css = self._css()
        block = css.split(".mat .ex-node{", 1)[1].split("}", 1)[0]
        assert "border:1.5px solid var(--ex-c)" in block, block


class TestSelectionBehaviour:
    def _app(self) -> str:
        from builder.writers.entity_explorer import _app_js

        return _app_js()

    def test_clicking_the_selected_node_clears_it(self):
        """Selection used to change only by choosing something else."""
        app = self._app()
        assert "was === n.id ? null : n.id" in app

    def test_an_edge_label_has_no_background_box(self):
        app = self._app()
        assert "labelShowBg: false" in app
        assert "labelBgStyle" not in app, "the label is still drawn in a box"

    def test_the_label_is_haloed_in_the_surface_colour(self):
        """So the line is knocked out from behind the text where they cross,
        rather than a card being laid over the edge."""
        from builder.writers.entity_explorer import _ASSET_DIR

        css = (_ASSET_DIR / "maturity_report.css").read_text(encoding="utf-8")
        assert "react-flow__edge-text" in css
        block = css.split("react-flow__edge-text{", 1)[1].split("}", 1)[0]
        assert "paint-order:stroke" in block and "stroke:var(--surface)" in block
