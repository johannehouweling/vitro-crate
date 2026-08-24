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

**What the DSM rewrite actually did.** Fifteen checks moved onto the graph, and
``DSM-1-C0`` (``unique_id``) moved with them — deliberately, because it is the rung
that holds the corrected ``study_summary`` down. No crate in the corpus carries a
persistent identifier for its datasets, so the published ladder collapsed from
``{0: 32, 2: 6}`` to ``{0: 38}`` over the 38 sessions+EMPTY, and the Level-1-granted
ladder from ``{1: 32, 2: 6}`` to ``{1: 38}``. Nothing rose anywhere; six crates fell
from 2 to 0. Scores going down is the rule here, not a bug.

Nine of the rewrites were refuted and are not here. They are named and pinned in
``_DSM_STATE_BOUND`` below, as a burn-down: that list may shrink and may never grow.
"""

from __future__ import annotations

import copy

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


# ---------------------------------------------------------------------------
# The DSM half of #670. Same principle, applied to the FAIRplus ladder.
# ---------------------------------------------------------------------------

# DSM checks a crate with no data may still honestly meet. Each is a property of the
# packaging: the descriptor really is machine-readable and really does declare its
# schemas, whether or not anything was put in the crate.
_DSM_ALLOWED: dict[str, str] = {
    "descriptor_machine_readable": (
        'DSM-1-R4 "Dataset Descriptor is available in Machine Readable Format" — '
        "ro-crate-metadata.json is JSON-LD. True for the same honest reason as "
        "RDA-I1-01M."
    ),
    "general_schema": (
        'DSM-1-R3 "A representation of the Dataset Descriptor conforming to a relevant '
        'General Purpose Metadata Schema is available" — the descriptor declares '
        "conformsTo RO-Crate, which is that schema."
    ),
    "domain_standard": (
        'DSM-3-R3 "Descriptor uses a community-defined metadata standard" — the same '
        "question as RDA-R1.3-01M, sharing its function, and allowlisted there for the "
        "same reason: the root really does declare the ISA-Tox profile."
    ),
    "community_domain_model": (
        "DSM-3-C2/R1/R4 — conformance to a community domain model, declared as a "
        "resolvable profile IRI on the root. Honest, but note the predicate must "
        "actually test for a domain profile: matching any IRI containing the substring "
        '"profile" is how this one passes for the wrong reason.'
    ),
}

# DSM checks that pass on the empty crate and must not. Every one of these names the
# *dataset*, its *fields*, or its *values* — none of which exist here.
_DSM_MUST_FAIL: dict[str, str] = {
    "context_fields": 'DSM-1-R1 "Contextual Metadata is represented at summary level"',
    "data_machine_readable": 'DSM-1-R5 "Dataset(s) available in Machine Readable Format" — there are no datasets',
    "data_structured": 'DSM-2-R5 — same published text as DSM-1-R5, at Level 2',
    "dataset_hierarchy": 'DSM-1-R2 "Data intended for sharing and reuse have a purposely defined representation as Datasets"',
    "dataset_metadata": 'DSM-1-C2 "Dataset Descriptor(s) includes Identifying & Descriptive Dataset-Level metadata"',
    "field_level_metadata": 'DSM-2-C6 "Dataset Descriptor includes Field-level Metadata" — the crate has no fields',
    "generic_model": 'DSM-2-R3 "Dataset Descriptor(s) adopt a Metadata Schema representation that describes the locally defined Dataset Model"',
    "linked_data": 'DSM-4-R2 "Dataset(s) are standardised to a defined Semantic Data Model"',
    "machine_interpretable": 'DSM-4-R4 "A Semantic Data Model describing the data is represented in a Machine Readable and Machine Interpretable format"',
    "semantic_model": 'DSM-4-R3 "A Semantic Data Model used for data harmonisation across Datasets is formally defined"',
    "standard_field_metadata": 'DSM-3-C6 "Dataset Descriptor includes standard-compliant Field-level Metadata"',
}


# The DSM checks that still answer from ``CrateState`` alone, and why each one is
# still there. This is a **burn-down**: every entry names a rewrite that was written,
# measured against all 62 crates, and refuted — in each case by a mutation that makes
# the crate *worse* while raising the score, or by a serialisation edit that carries no
# information at all. The list may shrink. It may never grow.
#
# Nine rewrites were refuted across these eight registry entries: ``domain_model``
# backs both DSM-2-C1 (``domain_model_content``) and DSM-2-R1
# (``domain_model_representation``), and both proposals for it were rejected.
_DSM_STATE_BOUND: dict[str, str] = {
    "access_info": (
        "DSM-1-C3 — proposed as `scope: na`. Half the indicator (the access protocol) "
        "really is a property of the repository, but the other half (FsF-A1-01M, access "
        "level and conditions) is answerable from the crate today by aliasing "
        "`air_assessment._check_access_conditions`, so scoping it na would suppress a "
        "true finding."
    ),
    "context_fields": (
        "DSM-1-R1 — its only discriminating limb is root-transitive reachability, which "
        "is borrowed from an open `verify_isa_reachability` REQUIRED bug: adding one "
        "`mentions` sweep from the root, which is what that validator's own fix text "
        "instructs, makes it True on 61 of 62 crates."
    ),
    "domain_model": (
        "DSM-2-C1 — the only discriminating limb accepts a reference that need not "
        "resolve (one dangling `{'@id': '#nowhere'}` flips 40 of 54 failures), and what "
        "it actually reads corpus-wide is grant administration: 'project reference', "
        "'work package', 'contact person' and nothing else. DSM-2-R1 — self-reference "
        "flips 35 of 35, retyping the offending nodes flips 34, and deleting them flips "
        "31: a crate scores better for throwing its context away than for modelling it "
        "badly."
    ),
    "generic_model": (
        "DSM-2-R3 — one appended node defeats it. Attachment carries 100% of the "
        "discrimination and is satisfied by any Dataset anywhere in the graph, so "
        "`{'@id': '#shim', '@type': 'Dataset', 'mentions': [every csvw:Table]}` flips "
        "all four real failures to True."
    ),
    "has_descriptor": (
        "DSM-1-R0 — its one discriminating limb is 'the root's name must not equal its "
        "own type label', and **deleting the root's `additionalType` flips 13 of 13 "
        "failures to True**. Declaring less raises the score; so does renaming the root "
        "'Investigations'."
    ),
    "resolvable_terms": (
        "DSM-3-C5 — the population excludes `csvw:Column`, and that exclusion is what "
        "the verdict rests on: 2260 nodes in the corpus are typed both Column and "
        "DefinedTerm and hold their resolvable identifier in `propertyUrl`, a slot the "
        "predicate does not read. Dropping the exclusion moves 48 crates."
    ),
    "semantic_model": (
        "DSM-4-R3 — the anti-synthesis limb does not resist synthesis. **Truncating "
        "each `propertyUrl` IRI by one path segment**, copying the column title as the "
        "term name and emitting `x:<counter>` as the code — all pure string operations "
        "on IRIs the crate already holds — flips it True on 7 of the 7 crates where "
        "that limb decides the answer."
    ),
    "value_level_metadata": (
        "DSM-2-C7 — two zero-knowledge edits make it pass on 61 of 61 crates that "
        "declare columns: spelling the numeric datatypes `xsd:unsignedInt`, which the "
        "hand-typed quantitative list does not know, and emitting one constant "
        "`inDefinedTermSet` per column, which is a builder correctness improvement "
        "rather than an attack. An earlier proposal also **flipped True when you "
        "deleted PropertyValue nodes**."
    ),
}

# The subset of ``_DSM_STATE_BOUND`` an empty crate still meets — the tautologies #670
# did not remove. They are listed in ``_DSM_MUST_FAIL`` with the false claim each one
# makes, and their assertions there are marked ``xfail(strict=True)``: the assertion
# still runs, and the day one of them starts failing honestly the suite goes red and
# demands the entry be deleted from here.
_DSM_STILL_TAUTOLOGICAL: frozenset[str] = frozenset(
    {"context_fields", "generic_model", "semantic_model"}
)


def _must_fail_params() -> list:
    """``_DSM_MUST_FAIL`` as parameters, with the known tautologies pinned as xfail."""
    return [
        pytest.param(
            check,
            why,
            marks=pytest.mark.xfail(
                strict=True, reason=f"still state-bound: {_DSM_STATE_BOUND[check]}"
            ),
        )
        if check in _DSM_STILL_TAUTOLOGICAL
        else pytest.param(check, why)
        for check, why in sorted(_DSM_MUST_FAIL.items())
    ]


def _dsm_verdicts(state: CrateState, graph: dict) -> dict[str, object]:
    from builder.tools.assessment_graph import as_verdict
    from builder.tools.fair_assessment import DSM_CHECKS

    return {name: as_verdict(fn(state, graph)).value for name, fn in DSM_CHECKS.items()}


class TestTheDsmIndicatorsCannotBeMetByAnEmptyCrate:
    """The DSM half of #670 — the ladder's rungs must ask about the dataset.

    Fifteen checks read True on a crate holding two empty entities and no payload.
    Eleven of them name the dataset, its fields or its values in their published text,
    so they are stating something the crate cannot evidence. The other four are about
    the packaging and may honestly pass; they are enumerated in ``_DSM_ALLOWED``.

    Eight of the eleven now fail, because they read the crate. The remaining three are
    ``_DSM_STILL_TAUTOLOGICAL``: every graph-based rewrite proposed for them was
    refuted, so they keep their ``len(state.list_entities()) > 0`` bodies and their
    assertions here are pinned ``xfail(strict=True)`` rather than deleted. The claim
    each one makes is still written down, and the pin still runs.
    """

    @pytest.mark.parametrize(("check", "why"), _must_fail_params())
    def test_dataset_indicators_must_fail(self, empty_state, empty_graph, check, why) -> None:
        assert _dsm_verdicts(empty_state, empty_graph)[check] is not True, why

    @pytest.mark.parametrize(("check", "why"), sorted(_DSM_ALLOWED.items()))
    def test_packaging_indicators_may_pass(self, empty_state, empty_graph, check, why) -> None:
        assert _dsm_verdicts(empty_state, empty_graph)[check] is True, why

    def test_nothing_outside_the_allowlist_passes(self, empty_state, empty_graph) -> None:
        """The closed half of the pin — a new DSM tautology cannot slip in unnamed.

        Pinned as an equality, not as a subset, so it is closed in both directions: a
        new tautology fails it, and so does a fixed one, which forces the entry out of
        ``_DSM_STILL_TAUTOLOGICAL`` instead of letting the debt list rot.
        """
        passing = {k for k, v in _dsm_verdicts(empty_state, empty_graph).items() if v is True}
        assert passing - set(_DSM_ALLOWED) == set(_DSM_STILL_TAUTOLOGICAL), (
            f"an empty crate meets {sorted(passing)}. Either the check is a new "
            "tautology, or it belongs in _DSM_ALLOWED with a written reason — and a "
            "check that stopped being a tautology must leave _DSM_STILL_TAUTOLOGICAL."
        )

    def test_every_dsm_check_reads_the_crate(self, empty_state, empty_graph) -> None:
        """The burn-down pin: exactly these checks still answer from CrateState alone.

        ``_state_check`` marks a wrapped state-only check with ``__wrapped_check__``;
        that attribute is how you enumerate what still needs moving. #670 moved fifteen
        and left these eight registry entries — nine refuted rewrites, since
        ``domain_model`` backs both DSM-2-C1 and DSM-2-R1 — behind.

        Each survived two rounds of design and adversarial verification, and each was
        refuted by an edit that carries no information: ``has_descriptor``'s
        discriminating limb flips True when you **delete** the root's
        ``additionalType``; ``semantic_model`` becomes True corpus-wide when you
        **truncate each propertyUrl IRI by one path segment**; ``value_level_metadata``
        flipped True when you **deleted PropertyValue nodes**. ``_DSM_STATE_BOUND``
        carries the rest, one line each.

        This is an equality, so the number can only go down: fixing one means deleting
        its entry, and adding a new state-bound check means this test goes red.
        """
        from builder.tools.fair_assessment import DSM_CHECKS

        state_bound = {n for n, fn in DSM_CHECKS.items() if hasattr(fn, "__wrapped_check__")}
        assert state_bound == set(_DSM_STATE_BOUND), (
            "the set of DSM checks that still score the session rather than the crate "
            f"has changed: {sorted(state_bound)}. If one was fixed, delete its entry "
            "from _DSM_STATE_BOUND; nothing may be added to it."
        )


class TestTheRewrittenDsmPredicatesDiscriminate:
    """The other half of the DSM rule: a rewritten check must be able to *pass*, too.

    ``unique_id`` is False on all 62 crates on hand and ``cross_dataset_refs``' external
    limb is dead on every one of them, so neither is exercised on the passing side by
    any fixture. These build the passing case by hand, so "false everywhere" cannot be
    mistaken for "correctly strict".
    """

    @staticmethod
    def _verdict(name: str, graph: dict):
        from builder.tools.assessment_graph import as_verdict
        from builder.tools.fair_assessment import DSM_CHECKS

        return as_verdict(DSM_CHECKS[name](CrateState(), graph))

    @staticmethod
    def _two_dataset_crate() -> dict:
        """A root, one Assay holding a deposited file, and a related dataset elsewhere."""
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
                {
                    "@id": "./",
                    "@type": "Dataset",
                    "name": "Thyroid uptake assay",
                    "description": "MCT8-MDCK1 uptake of T3.",
                    "hasPart": [{"@id": "assays/a1/"}],
                    "isBasedOn": {"@id": "https://doi.org/10.5281/zenodo.1"},
                },
                {
                    "@id": "assays/a1/",
                    "@type": "Dataset",
                    "additionalType": "Assay",
                    "name": "Uptake assay",
                    "hasPart": [{"@id": "assays/a1/uptake.csv"}],
                },
                {
                    "@id": "assays/a1/uptake.csv",
                    "@type": "File",
                    "name": "uptake.csv",
                    "encodingFormat": "text/csv",
                },
            ]
        }

    def test_a_cited_dataset_counts_only_when_the_crate_types_it_as_one(self) -> None:
        """DSM-2-C5's external limb needs a ``Dataset``-typed node, not a bare reference.

        A crate may relate itself to a dataset it does not carry — that is how a reuse
        crate earns the indicator — but the reference alone says nothing about what was
        cited. Pinned in all three states because the difference is one ``@type``.
        """
        graph = self._two_dataset_crate()
        assert self._verdict("cross_dataset_refs", graph).value is False

        stub = {"@id": "https://doi.org/10.5281/zenodo.1", "@type": "Dataset"}
        graph["@graph"].append(stub)
        assert self._verdict("cross_dataset_refs", graph).value is True

        stub["@type"] = "CreativeWork"
        assert self._verdict("cross_dataset_refs", graph).value is False

    def test_dsm_1_r4_answers_exactly_what_the_rda_check_answers(self) -> None:
        """DSM-1-R4's own ``rda_ref`` names RDA-I1-02M, so it delegates to that check.

        Pinned as **equality of verdict**, not as ``DSM_CHECKS[...] is
        _check_jsonld_context``: the registry entry is a delegating wrapper carrying the
        docstring, so an identity assertion is false while the claim it stands for —
        that the two axes cannot disagree about one crate — is true.
        """
        from builder.tools.fair_assessment import _check_jsonld_context

        conformant = copy.deepcopy(self._two_dataset_crate())
        conformant["@graph"][0]["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.2"}

        unprofiled = copy.deepcopy(self._two_dataset_crate())  # descriptor, no conformsTo

        headless = copy.deepcopy(self._two_dataset_crate())
        headless["@graph"] = headless["@graph"][1:]  # no descriptor node at all

        for label, graph in (
            ("conformant", conformant),
            ("no profile declared", unprofiled),
            ("no descriptor node", headless),
        ):
            mine = self._verdict("descriptor_machine_readable", graph)
            theirs = _check_jsonld_context(CrateState(), graph)
            assert mine == theirs, f"{label}: DSM-1-R4 {mine} vs RDA-I1-02M {theirs}"
        assert self._verdict("descriptor_machine_readable", conformant).value is True
        assert self._verdict("descriptor_machine_readable", headless).value is False

    @staticmethod
    def _schematised_table(content_size: int) -> dict:
        """One CSV declaring two fully described columns, at a stated byte size."""
        return {
            "@graph": [
                {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
                {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "data/plate.csv"}]},
                {
                    "@id": "data/plate.csv",
                    "@type": ["File", "csvw:Table"],
                    "name": "plate.csv",
                    "encodingFormat": "text/csv",
                    "contentSize": str(content_size),
                    "tableSchema": {"@id": "#schema"},
                },
                {
                    "@id": "#schema",
                    "@type": "csvw:Schema",
                    "columns": [{"@id": "#col_well"}, {"@id": "#col_dose"}],
                },
                {
                    "@id": "#col_well",
                    "@type": "csvw:Column",
                    "titles": "well",
                    "datatype": "string",
                    "propertyUrl": {"@id": "http://purl.obolibrary.org/obo/OBI_0000073"},
                },
                {
                    "@id": "#col_dose",
                    "@type": "csvw:Column",
                    "titles": "dose",
                    "datatype": "double",
                    "propertyUrl": {"@id": "http://purl.obolibrary.org/obo/CHEBI_23888"},
                },
            ]
        }

    def test_a_header_only_table_cannot_become_populated_by_its_encoding(self) -> None:
        """DSM-3-C6's row test must have margin against a BOM and against CRLF.

        The derived header of ``well,dose`` is 10 bytes assuming LF. A crate that wrote
        the identical empty file with a UTF-8 BOM and RFC 4180's CRLF stores 14, and an
        exact-equality cut would call that a populated table — which is how the whole
        0-of-62 result on this corpus sat one byte away from 39-of-62 with no data
        added. Every byte count in this test is a specification quantity, not a
        threshold: 10 = the header, +3 = the BOM, +1 = CRLF for LF, +2 = the shortest
        possible two-column record.
        """
        header = len("well,dose\n")
        for size, why in (
            (header, "the header alone"),
            (header + 1, "the header with a CRLF terminator"),
            (header + 3, "the header behind a UTF-8 BOM"),
            (header + 4, "the header behind a BOM and terminated CRLF"),
        ):
            verdict = self._verdict("standard_field_metadata", self._schematised_table(size))
            assert verdict.value is False, f"{why} was read as holding rows: {verdict}"
            assert "no rows" in verdict.evidence

        populated = self._verdict(
            "standard_field_metadata", self._schematised_table(header + 4 + 2)
        )
        assert populated.value is True, (
            f"a table one whole record longer than the noise floor must count: {populated}"
        )

    def test_unique_id_passes_only_when_the_crate_is_identified_outside_itself(self) -> None:
        """DSM-1-C0 is False on all 62 crates on hand, so its passing case is built here.

        Both limbs must be satisfiable together: every shared Dataset assigned an
        identifier, and the crate globally identified. A bare accession is neither.
        """
        graph = self._two_dataset_crate()
        root = next(n for n in graph["@graph"] if n["@id"] == "./")
        assay = next(n for n in graph["@graph"] if n["@id"] == "assays/a1/")
        assay["identifier"] = "a1"

        root["identifier"] = "S-VHPS22"
        assert self._verdict("unique_id", graph).value is False, (
            "an accession is unique inside BioStudies and ambiguous outside it"
        )

        root["identifier"] = "https://doi.org/10.5281/zenodo.1234567"
        assert self._verdict("unique_id", graph).value is True

        del assay["identifier"]
        assert self._verdict("unique_id", graph).value is False, (
            "a root PID does not identify a Dataset the crate never named"
        )


class TestInflationCannotHideBehindAFailingLevelOne:
    """The ladder is cumulative, so a Level-2+ tautology is invisible until L1 passes.

    ``_compute_dsm_level`` stops at the first level with a failure. Today Level 1 fails
    on 32 of 38 real sessions — because ``unique_id``, ``study_summary`` and
    ``has_descriptor`` read ``CrateState`` fields nothing writes — so **every crate is
    capped at 0 and no change above Level 1 can move the published number at all**.

    That makes "the published ladder did not rise" worthless as a safety check for
    Levels 2-4: it is guaranteed by a defect, not by the predicates under test. An
    adversarial review of #670's first DSM proposals measured exactly this — two
    Level-2 predicates that moved the published distribution not at all lifted **24
    crates a level** once Level 1 was allowed to pass.

    So this measures the ladder with Level 1 forced True. That is the number a
    Level-2+ change has to defend, and it is the one that stays honest after the
    Level-1 checks are eventually fixed.
    """

    @staticmethod
    def _level_with_level_one_granted(state: CrateState, graph: dict) -> int:
        """The DSM level this crate would reach if Level 1 were satisfied."""
        from builder.tools.assessment_graph import Verdict
        from builder.tools.fair_assessment import (
            DSM_INDICATORS_PATH,
            _assessable_indicators,
            _load_yaml,
            dsm_verdicts,
        )

        data = _load_yaml(DSM_INDICATORS_PATH)
        assert data is not None
        answers = dsm_verdicts(state, data, graph)
        level_one = {
            str(ind.get("id")) for ind, _ in _assessable_indicators(data, 1)
        }
        granted = {
            k: (Verdict(True, "granted") if k in level_one else v)
            for k, v in answers.items()
        }

        levels = sorted(
            {
                lvl
                for ind in data.get("indicators", [])
                if isinstance(lvl := ind.get("level"), int) and lvl >= 1
            }
        )
        reached = 0
        for level in levels:
            answered = [
                v.value
                for ind, _ in _assessable_indicators(data, level)
                if (v := granted.get(str(ind.get("id")))) is not None
                and v.value is not None
            ]
            if not answered or not all(answered):
                break
            reached = level
        return reached

    def test_the_empty_crate_reaches_no_further_than_level_one(
        self, empty_state, empty_graph
    ) -> None:
        """Even handed Level 1 outright, a crate with no data must climb no higher.

        Levels 2-4 are about the dataset's fields, values and semantics. A crate with
        none of those cannot evidence them, so anything above 1 here is a Level-2+
        tautology — the class of defect the published number cannot currently see.
        """
        reached = self._level_with_level_one_granted(empty_state, empty_graph)
        assert reached <= 1, (
            f"with Level 1 granted, an empty crate reaches DSM {reached}. Some "
            "Level-2+ indicator is satisfied by the builder's scaffolding rather than "
            "by data."
        )
