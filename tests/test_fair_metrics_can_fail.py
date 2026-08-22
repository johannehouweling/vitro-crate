"""The FAIR metrics must be able to fail (#670).

A crate holding two empty entities and no payload at all "meets" **9 of the 18**
assessed RDA indicators today, including *"Plurality of accurate and relevant
attributes are provided to allow reuse"* over two entities whose ``fields`` are
``{}``. Those checks score the builder's construction, not the data: eleven of
them are literally ``len(state.list_entities()) > 0``.

This module pins the floor. It is the definition of done for #670, and it is
deliberately written before any check changes so that the rewrites have a target
they cannot drift past.

## The line this module draws

The empty crate assembles to an eight-node skeleton — a root ``Dataset``, the
``ro-crate-metadata.json`` descriptor, the not-stated licence placeholder, two
profile IRIs, one ``Study`` Dataset, and the ``SoftwareApplication`` /
``CreateAction`` pair recording the build. Everything in it is minted by the
serialiser. Nothing in it came from data, because there is no data.

So an indicator may only pass here if it asks a question **about the packaging**
that the packaging genuinely answers. An indicator whose published text names
*the data*, or names attributes, must fail — a crate with no data cannot evidence
its data, and saying otherwise is what makes the axis unpublishable.

``_ALLOWED`` and ``_MUST_FAIL`` below carry that split per indicator, with the
published text and the reason, so a future change that moves one across the line
has to say so in the diff.

## Why the DSM assertions are the anti-inflation pin

The tempting fix for the reproducibility half of #670 — routing the DSM checks
through the assembled ``@graph`` — inflates rather than corrects. ``DSM-1-C1``
(``study_summary``) and ``DSM-1-R0`` (``has_descriptor``) read
``state.metadata.title``, ``None`` on 29 of 32 real sessions; the assembled root
always carries a name, because ``builder.tools.builder._apply_root_name`` derives
one and falls back to a constant. Ported as-is they would lift 12 crates from DSM
0 to DSM 1 for free. ``test_the_ladder_stays_on_the_floor_when_scored_from_the_graph``
is what makes that regression loud.
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.mit_assessment import _assemble_graph

# Indicators a crate with no data may still honestly meet: each is a property of
# the RO-Crate packaging, which really is standards-conformant even when empty.
_ALLOWED: dict[str, str] = {
    "RDA-I1-01M": (
        "Metadata uses knowledge representation expressed in standardised format — "
        "the crate really is JSON-LD carrying a standard @context."
    ),
    "RDA-I1-02M": (
        "Metadata uses machine-understandable knowledge representation — same "
        "serialisation, and it is machine-understandable whether or not it is empty."
    ),
    "RDA-R1.2-01M": (
        "Metadata includes provenance information according to community-specific "
        "standards — the minted CreateAction/SoftwareApplication pair is genuine "
        "RO-Crate provenance of the packaging act. Honest, though it says nothing "
        "about data provenance, because there is no data to have any."
    ),
    "RDA-R1.3-01M": (
        "Metadata complies with a community standard — the root carries conformsTo "
        "the RO-Crate and ISA-Tox profile IRIs."
    ),
    "RDA-R1.3-02M": (
        "Metadata is expressed in compliance with a machine-understandable community "
        "standard — same two profile IRIs, and they resolve."
    ),
}

# Indicators that pass today and must not. Each names the false claim it makes.
_MUST_FAIL: dict[str, str] = {
    "RDA-F1-02D": (
        '"Data is identified by a globally unique identifier" — there is no data. '
        "`all(bool(e.entity_id) for e in entities)` is vacuously true over two "
        "placeholders and never asks whether any data exists to be identified."
    ),
    "RDA-F3-01M": (
        '"Metadata includes the identifier for the data" — no data, so no identifier '
        "for it. The check is `len(state.list_entities()) > 0`."
    ),
    "RDA-I2-01M": (
        '"Metadata uses FAIR-compliant vocabularies" — the crate annotates nothing '
        "with a controlled term. The check is `len(state.list_entities()) > 0`; the "
        "honest predicate counts entities carrying a resolvable external vocabulary "
        "term, which `air_assessment._check_descriptive_metadata_rich` already does."
    ),
    "RDA-R1-01M": (
        '"Plurality of accurate and relevant attributes are provided to allow reuse" '
        "— both entities have `fields == {}`, i.e. zero attributes. The check is "
        "`len(state.list_entities()) >= 2`, which counts entities, not attributes. "
        "This is the flagship absurdity of #670."
    ),
}


def _empty_crate() -> CrateState:
    """Two typed entities, no fields, no files, no metadata — and nothing else.

    Deliberately not a bare ``CrateState``: the entities exist so that every
    ``len(state.list_entities()) > 0`` / ``>= 2`` tautology is satisfied. That is
    the point — the crate is empty of *content* while clearing every entity-count
    bar the current checks set.
    """
    state = CrateState()
    for entity_id, entity_type in (("e1", "Investigation"), ("e2", "Study")):
        state.add_entity(
            Entity(
                entity_id=entity_id,
                type=entity_type,
                fields={},
                _provenance=EntityProvenance(created_by="llm"),
            )
        )
    return state


@pytest.fixture(scope="module")
def empty_state() -> CrateState:
    return _empty_crate()


@pytest.fixture(scope="module")
def empty_graph(empty_state: CrateState) -> dict:
    return {"@graph": _assemble_graph(empty_state) or []}


def _passing(state: CrateState, graph: dict | None = None) -> set[str]:
    report = assess_fair_maturity(state, graph=graph) if graph else assess_fair_maturity(state)
    return {r["id"] for r in report.indicator_results if r["passed"] is True}


class TestAnEmptyCrateCannotEvidenceItsData:
    """The RDA floor, scored both ways — a reader gets the graph, we get both."""

    def test_the_crate_really_is_empty(self, empty_state, empty_graph) -> None:
        """Guard the premise: if the skeleton ever grows content, this file lies."""
        assert all(e.fields == {} for e in empty_state.list_entities())
        assert empty_state.metadata.title is None
        graph_types = [n.get("@type") for n in empty_graph["@graph"]]
        assert "File" not in graph_types, "no payload may appear in the skeleton"

    @pytest.mark.parametrize(("indicator", "why"), sorted(_MUST_FAIL.items()))
    def test_indicators_about_data_must_fail(
        self, empty_state, empty_graph, indicator, why
    ) -> None:
        assert indicator not in _passing(empty_state), why
        assert indicator not in _passing(empty_state, empty_graph), f"{why} (from the graph)"

    @pytest.mark.parametrize(("indicator", "why"), sorted(_ALLOWED.items()))
    def test_indicators_about_the_packaging_may_pass(
        self, empty_state, empty_graph, indicator, why
    ) -> None:
        """Pinned so that a rewrite which fixes the tautologies by making everything
        strict gets caught too — "cannot pass" is as broken as "cannot fail".

        Scored from the graph, because that is now the only place they are scored
        from: all five read the crate a reader receives, and answer "not assessed"
        without one (see TestTheAnswerIsReproducibleFromTheCrateAlone).
        """
        assert indicator in _passing(empty_state, empty_graph), why

    def test_nothing_outside_the_allowlist_passes(self, empty_state, empty_graph) -> None:
        """The closed half of the pin — a new tautology cannot slip in unnamed."""
        for label, passing in (
            ("state", _passing(empty_state)),
            ("graph", _passing(empty_state, empty_graph)),
        ):
            unexpected = passing - set(_ALLOWED)
            assert not unexpected, (
                f"scored from {label}, an empty crate meets {sorted(unexpected)}. "
                "Either the check is a tautology, or it belongs in _ALLOWED with a "
                "written reason."
            )


class TestTheDsmLadderStaysOnTheFloor:
    """An empty crate is DSM 0, and must stay 0 however the ladder is scored."""

    def test_the_ladder_is_on_the_floor_today(self, empty_state) -> None:
        assert assess_fair_maturity(empty_state).dsm_level == 0

    def test_the_ladder_stays_on_the_floor_when_scored_from_the_graph(
        self, empty_state, empty_graph
    ) -> None:
        """The anti-inflation pin (see this module's docstring).

        Porting DSM-1-C1 and DSM-1-R0 to the graph without rewriting their
        predicates flips this to 1 or more — and would flip 12 real crates off the
        floor for free. Expect published scores to go DOWN, never up, when a check
        moves to the graph.
        """
        assert assess_fair_maturity(empty_state, graph=empty_graph).dsm_level == 0


class TestTheNewPredicatesDiscriminate:
    """The other half of the rule: each check must be able to *pass*, too.

    Replacing a tautology with a predicate that is false on every crate would swap
    one useless indicator for its mirror image and still look like progress — the
    suite would go green either way. These build the passing case explicitly, by
    supplying the one thing each check asks for, so "false everywhere" cannot be
    mistaken for "correctly strict".

    They are written as mutations of an assembled crate rather than as pins on a
    fixture's score, because the crates on hand are essentially one deposit
    (S-VHPS22) and a threshold tuned to them would not survive the next one.
    """

    @staticmethod
    def _crate() -> dict:
        """A crate with a payload File, a bound column, and a described subject."""
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Thyroid uptake assay",
                    "description": "MCT8-MDCK1 uptake of T3 across eight doses.",
                    "datePublished": "2026-01-01",
                    "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
                    "identifier": "10.6019/S-VHPS21",
                    "author": {"@id": "https://orcid.org/0000-0002-1825-0097"},
                    "hasPart": [{"@id": "data/uptake.csv"}],
                },
                {"@id": "data/uptake.csv", "@type": ["File", "csvw:Table"], "name": "uptake.csv"},
                {
                    "@id": "#col_dose",
                    "@type": "csvw:Column",
                    "propertyUrl": "http://purl.obolibrary.org/obo/CHEBI_23888",
                },
                {
                    "@id": "#MolecularEntity_t3",
                    "@type": "MolecularEntity",
                    "name": "Triiodothyronine",
                    "identifier": "CHEBI:18258",
                },
            ]
        }

    def _verdict(self, name: str, graph: dict):
        from builder.tools.assessment_graph import as_verdict
        from builder.tools.fair_assessment import FAIR_CHECKS

        return as_verdict(FAIR_CHECKS[name](CrateState(), graph)).value

    def test_all_five_pass_on_a_crate_that_earns_them(self) -> None:
        graph = self._crate()
        for name in (
            "pid_form",
            "every_entity_has_id",
            "metadata_refs_data",
            "fair_vocabularies",
            "reuse_attributes",
        ):
            assert self._verdict(name, graph) is True, f"{name} cannot pass at all"

    def test_dropping_the_root_pid_fails_both_identifier_indicators(self) -> None:
        """F1-01M and F1-02D share one root cause and must move together: nothing
        writes a DOI to the root, so relative File paths compose against nothing."""
        graph = self._crate()
        next(n for n in graph["@graph"] if n["@id"] == "./").pop("identifier")
        assert self._verdict("pid_form", graph) is False
        assert self._verdict("every_entity_has_id", graph) is False

    def test_a_file_carrying_its_own_iri_is_identified_without_a_root_pid(self) -> None:
        """The other way to satisfy F1-02D — how a crate referencing data held in an
        external repository (GEO, PRIDE) earns it."""
        graph = self._crate()
        next(n for n in graph["@graph"] if n["@id"] == "./").pop("identifier")
        file_node = next(n for n in graph["@graph"] if n["@id"] == "data/uptake.csv")
        file_node["contentUrl"] = "https://ftp.ebi.ac.uk/biostudies/uptake.csv"
        assert self._verdict("every_entity_has_id", graph) is True

    def test_a_crate_with_no_payload_names_no_data(self) -> None:
        """The ISA backbone trap: hasPart is still non-empty, and F3-01M still fails."""
        graph = self._crate()
        root = next(n for n in graph["@graph"] if n["@id"] == "./")
        root["hasPart"] = [{"@id": "#Study_s1"}]
        graph["@graph"].append({"@id": "#Study_s1", "@type": "Dataset", "additionalType": "Study"})
        assert root["hasPart"], "the trap only bites while hasPart is non-empty"
        assert self._verdict("metadata_refs_data", graph) is False

    def test_unbound_columns_fail_the_vocabulary_indicator(self) -> None:
        graph = self._crate()
        next(n for n in graph["@graph"] if n["@id"] == "#col_dose").pop("propertyUrl")
        assert self._verdict("fair_vocabularies", graph) is False

    def test_a_missing_licence_fails_reuse_however_many_other_attributes_there_are(
        self,
    ) -> None:
        """Anchored on the four RO-Crate requires, not on a count — so a crate cannot
        buy its way past a missing licence with keywords and a contact point."""
        graph = self._crate()
        root = next(n for n in graph["@graph"] if n["@id"] == "./")
        root.pop("license")
        root |= {
            "keywords": ["thyroid"],
            "publisher": {"@id": "#org"},
            "citation": {"@id": "#paper"},
            "contactPoint": {"@id": "#person"},
        }
        assert self._verdict("reuse_attributes", graph) is False

    def test_a_name_only_subject_fails_reuse(self) -> None:
        """"Nobody can reuse a chemical identified only by a common name."""
        graph = self._crate()
        next(n for n in graph["@graph"] if n["@id"] == "#MolecularEntity_t3").pop("identifier")
        assert self._verdict("reuse_attributes", graph) is False


class TestTheAnswerIsReproducibleFromTheCrateAlone:
    """#670's second defect: a reader holding only the JSON must get our numbers.

    All five checks now read the graph, so a reader scoring the published crate with
    an empty CrateState gets exactly what the report says.
    """

    def test_a_reader_with_no_session_state_gets_the_same_verdicts(self) -> None:
        graph = TestTheNewPredicatesDiscriminate._crate()
        ours = assess_fair_maturity(_empty_crate(), graph=graph)
        theirs = assess_fair_maturity(CrateState(), graph=graph)
        mine = {r["id"]: r["passed"] for r in ours.indicator_results}
        yours = {r["id"]: r["passed"] for r in theirs.indicator_results}
        differing = {k: (mine[k], yours[k]) for k in mine if mine[k] != yours[k]}
        assert not differing, (
            f"these indicators still depend on session state a reader never sees: "
            f"{differing}"
        )

    def test_without_a_graph_they_say_not_assessed_rather_than_guess(self) -> None:
        """"Not assessed" leaves the denominator; False counts against the crate."""
        results = {
            r["id"]: r["passed"]
            for r in assess_fair_maturity(_empty_crate()).indicator_results
        }
        for indicator in (
            "RDA-F1-01M",
            "RDA-F1-02D",
            "RDA-F3-01M",
            "RDA-I1-01M",
            "RDA-I1-02M",
            "RDA-I2-01M",
            "RDA-R1-01M",
            "RDA-R1.2-01M",
            "RDA-R1.3-01M",
            "RDA-R1.3-02M",
        ):
            assert results[indicator] is None, (
                f"{indicator} answered {results[indicator]!r} with no crate to read"
            )
