"""The inspector names properties the way the crate does (#688).

The Properties tab listed an entity's raw JSON keys. The crate's own
``@context`` is what those keys mean — ``input`` and ``object`` are one
predicate, ``studies`` and ``assays`` and ``hasPart`` are another — so a reader
saw the serializer's shorthand and had to know the context to read it.

The Overview names each property as the crate expands it, and shows one row per
predicate rather than one per spelling.
"""

from __future__ import annotations

import pytest

from builder.writers.provenance_dag import property_terms

pytestmark = pytest.mark.timeout(180)


class TestPropertyNamesComeFromTheContext:
    def test_a_bioschemas_property_is_named_as_one(self):
        assert property_terms()["executesLabProtocol"] == "bioschemas:executesLabProtocol"

    def test_the_friendly_aliases_expand_to_what_they_alias(self):
        """`input` is the builder's readable name for a LabProcess's input; the
        predicate is schema:object, and that is what the crate carries."""
        terms = property_terms()
        assert terms["input"] == "schema:object"
        assert terms["object"] == "schema:object"

    def test_the_grouped_containment_aliases_all_name_haspart(self):
        terms = property_terms()
        assert {terms[k] for k in ("studies", "assays", "protocols", "hasPart")} == {
            "schema:hasPart"
        }

    def test_the_two_parameter_predicates_stay_distinct(self):
        """The builder emits both keys for the same values on purpose: the two
        profiles it claims ask for parameters under different predicates, and
        dropping either loses a conformance it advertises. They are two
        predicates, so the Overview must not merge them into one row.
        """
        terms = property_terms()
        assert terms["parameter"] == "schema:additionalProperty"
        assert terms["parameterValue"] == "bioschemas:parameterValue"

    def test_every_term_names_something_lookupable(self):
        for key, term in property_terms().items():
            assert ":" in term, (key, term)


class TestThePayloadCarriesThem:
    def _payload(self):
        from builder.writers.entity_explorer import build_explorer_payload
        from tests.fixtures.crate_graphs import assay_lane_graph

        return build_explorer_payload(assay_lane_graph())

    def test_properties_travel_with_the_crate(self):
        assert self._payload()["properties"]["input"] == "schema:object"

    def test_a_key_the_context_does_not_map_falls_back_to_the_vocab(self):
        """The crate declares ``@vocab: http://schema.org/``, so a bare key it
        does not name expands to schema:<key> — that is the crate's own rule, not
        a guess made here."""
        assert self._payload()["vocab_prefix"] == "schema"

    def test_the_app_does_not_restate_the_vocabulary(self):
        import re

        from builder.writers.entity_explorer import _app_js

        code = re.sub(r"/\*.*?\*/", "", _app_js(), flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        assert "D.properties" in _app_js()
        for term in ("schema:hasPart", "bioschemas:executesLabProtocol"):
            assert term not in code, term


class TestTheTabIsTheOverview:
    def _app(self) -> str:
        from builder.writers.entity_explorer import _app_js

        return _app_js()

    def test_it_is_called_the_overview(self):
        app = self._app()
        assert "'Overview'" in app
        assert "'Properties'" not in app

    def test_the_links_tab_names_relations_the_same_way(self):
        """A relation shown in the panel and the same relation shown on an edge
        must not be two different words for one predicate."""
        app = self._app()
        assert "term(pair[0])" in app

    def test_the_json_and_links_tabs_are_still_offered(self):
        app = self._app()
        assert "'json'" in app and "'links'" in app


class TestTheUntrustedTextRuleStillHolds:
    """#169: the payload carries the crate verbatim, `javascript:` URLs and all.

    The design asked for values to be linked wherever a URL exists. The explorer
    may not write an anchor, so a URL is offered as something to copy instead —
    a clipboard write navigates nowhere and executes nothing.
    """

    def test_the_app_still_writes_no_navigation_sink(self):
        from builder.writers.entity_explorer import _app_js

        source = _app_js()
        for sink in ("href", "src=", "window.open", "location.assign", "innerHTML", "<a "):
            assert sink not in source, sink

    def test_a_url_value_is_offered_for_copying(self):
        from builder.writers.entity_explorer import _app_js

        assert "clipboard" in _app_js()
