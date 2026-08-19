"""Discovery ranks what carries the facts, not what reads like prose (#587).

The ranking decides what the agent sees at all — a capped subset of the deposit,
against a bounded context — and it used to award +0.12 for
"prose-like" text and up to +0.24 for sitting near the root. A spreadsheet can
never earn the first, so the deposit's 1048-row measurement table scored 0.44
against a top-level README's 0.65, despite carrying twice the content evidence.

That is not a tidiness problem. It is the same root cause as the largest content
defect in the built crates: the chemistry was mined from SOP prose while the
structured workbook holding 20 chemicals x 8 concentrations went unread.

These tests run against the real S-VHPS22 fixture rather than synthetic files,
because the ordering is a claim about real deposits and the fixture is one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import FileClassification
from builder.tools.document_discovery import discover_documents, format_document_context

FIXTURE = Path("tests/fixtures/svhps22_real_input")

DESCRIPTOR = "S-VHPS22.json"
MEASUREMENTS = "assay_01_TH_uptake/EDCs/Combined uptake data EDCs_tidy.csv"
TOP_README = "README.txt"
WORKBOOKS = (
    "assay_01_TH_uptake/TH_assay_metadata.xlsx",
    "assay_02_deiodinase/deiodinase_assay_metadata.xlsx",
    "assay_03_metabolism/metabolism_assay_metadata.xlsx",
    "assay_04_TRactivation/tractivation_assay_metadata.xlsx",
)


def _scan() -> list[FileClassification]:
    from builder.tools.scanner import scan_files

    root = str(FIXTURE.resolve())
    return scan_files(root, approved_roots={root})


@pytest.fixture(scope="module")
def ranked():
    if not FIXTURE.exists():  # pragma: no cover - fixture not checked out
        pytest.skip("S-VHPS22 fixture not available")
    root = str(FIXTURE.resolve())
    return discover_documents(_scan(), input_root=root, approved_roots={root})


def _rank_of(ranked, relative: str) -> int:
    for i, c in enumerate(ranked):
        if c.relative_path.replace("\\", "/") == relative:
            return i
    raise AssertionError(f"{relative} did not surface at all: {[c.relative_path for c in ranked]}")


def _by_path(ranked, relative: str):
    return ranked[_rank_of(ranked, relative)]


class TestKindIsDecidedByShape:
    """A keyword hit is not evidence of what a file IS."""

    def test_the_submission_descriptor_is_recognised_as_one(self, ranked) -> None:
        # Was "[Publication]" on a single keyword match, when it is the deposit's
        # authoritative record: accession, licence, authors, 21 chemicals.
        assert _by_path(ranked, DESCRIPTOR).kind == "descriptor"

    def test_a_measurement_table_is_tabular_not_a_protocol(self, ranked) -> None:
        assert _by_path(ranked, MEASUREMENTS).kind == "tabular"

    def test_a_readme_is_narrative(self, ranked) -> None:
        assert _by_path(ranked, TOP_README).kind == "narrative"


class TestTheFactsOutrankTheProse:
    def test_the_descriptor_comes_first(self, ranked) -> None:
        assert ranked[0].relative_path.replace("\\", "/") == DESCRIPTOR

    def test_the_measurement_table_outranks_a_top_level_readme(self, ranked) -> None:
        assert _rank_of(ranked, MEASUREMENTS) < _rank_of(ranked, TOP_README)

    def test_every_assay_metadata_workbook_surfaces(self, ranked) -> None:
        # They sat at 14, 16, 17, 18 — below seven READMEs — and carry the
        # per-assay structured fields the crate is built from.
        missing = [w for w in WORKBOOKS if w not in {
            c.relative_path.replace("\\", "/") for c in ranked
        }]
        assert missing == [], missing

    def test_narrative_still_surfaces(self, ranked) -> None:
        """Not an inversion: orientation material must still be there.

        The failure mode being fixed is one axis crowding out the other, so a
        ranking that buried every README would be the same defect mirrored.
        """
        kinds = [c.kind for c in ranked]
        # Balance, not a floor: an earlier attempt satisfied ">= 3 of each" while
        # surfacing 16 tabular and 4 narrative — the original defect mirrored, and
        # a floor is too weak to catch it. Neither kind may take most of the list.
        for kind in ("narrative", "tabular"):
            share = kinds.count(kind) / len(kinds)
            assert 0.25 <= share <= 0.6, f"{kind} takes {share:.0%} of the list: {kinds}"


class TestScoresDiscriminate:
    def test_depth_alone_does_not_decide_the_order(self, ranked) -> None:
        # The old weighting gave +0.24 for depth 0 and <=0.06 below depth 2 —
        # over a third of the top score, awarded for location alone.
        top = ranked[:5]
        assert any(c.relative_path.count("/") >= 1 for c in top), (
            f"the top of the ranking is all root-level files: {[c.relative_path for c in top]}"
        )

    def test_a_richer_table_outranks_a_thin_one(self, ranked) -> None:
        """Row count is evidence; the scorer should use it."""
        rich = _by_path(ranked, MEASUREMENTS)
        thin = [
            c for c in ranked
            if c.kind == "tabular" and c.relative_path.replace("\\", "/") != MEASUREMENTS
        ]
        if not thin:  # pragma: no cover - fixture always has more than one table
            pytest.skip("only one tabular candidate")
        assert rich.score >= max(c.score for c in thin)


class TestNothingIsDroppedSilently:
    def test_the_context_says_how_much_it_left_out(self, ranked) -> None:
        # A silent cap hid 34 of the fixture's files. Same rule the report follows:
        # a cap that bites says how many it hid.
        context = format_document_context(ranked, total_scanned=len(_scan()))
        assert "not surfaced" in context or "not listed" in context, context[-400:]


class TestItDoesNotKnowWhatBioStudiesIs:
    """The detector must not encode one repository's dialect (#587).

    Every fixture here is a BioStudies deposit, so a detector keyed on that
    dialect — an earlier version tested for `accno` plus an attribute tree —
    passes all of the above while being unable to recognise an ISA-JSON,
    DataCite, Zenodo or in-house record. AGENTS.md §Input Formats forbids
    exactly that: a metadata file is "a generic metadata source, not a
    special-cased input type … regardless of the metadata file's format or
    schema", and the documented most common case is a directory with no record
    at all.
    """

    @staticmethod
    def _ranked(tmp_path: Path):
        from builder.tools.scanner import scan_files

        root = str(tmp_path.resolve())
        return discover_documents(
            scan_files(root, approved_roots={root}),
            input_root=root,
            approved_roots={root},
        )

    def test_a_record_in_an_unrelated_dialect_is_recognised(self, tmp_path: Path) -> None:
        # DataCite-shaped: nothing in common with BioStudies but the fact that
        # it is a structured record.
        (tmp_path / "datacite.json").write_text(
            """{
              "doi": "10.1234/example",
              "titles": [{"title": "An unrelated deposit"}],
              "creators": [{"name": "Doe, Jane", "affiliation": "Somewhere"}],
              "publisher": "Some Repository",
              "publicationYear": 2026,
              "rightsList": [{"rights": "CC-BY-4.0"}],
              "subjects": [{"subject": "toxicology"}]
            }""",
            encoding="utf-8",
        )
        (tmp_path / "notes.txt").write_text("Some free text about the assay.\n" * 20)

        ranked = self._ranked(tmp_path)
        record = next(c for c in ranked if c.filename == "datacite.json")
        assert record.kind == "descriptor"
        assert ranked[0].filename == "datacite.json"

    def test_a_yaml_record_is_recognised_too(self, tmp_path: Path) -> None:
        (tmp_path / "study.yaml").write_text(
            "title: A deposit\nauthors:\n  - name: Jane\n"
            "licence: CC-BY-4.0\norganism: Homo sapiens\nassay: uptake\n",
            encoding="utf-8",
        )
        ranked = self._ranked(tmp_path)
        assert next(c for c in ranked if c.filename == "study.yaml").kind == "descriptor"

    def test_a_deposit_with_no_record_still_ranks(self, tmp_path: Path) -> None:
        """The documented most common case: raw data and prose, no record.

        Nothing may crash, and nothing may be promoted to a record it is not.
        """
        (tmp_path / "README.txt").write_text("How this experiment was run.\n" * 20)
        (tmp_path / "results.csv").write_text(
            "well_id,compound,concentration_value,measurement_value\n"
            + "\n".join(f"A{i},BPA,{i},{i * 2}" for i in range(30)),
            encoding="utf-8",
        )

        ranked = self._ranked(tmp_path)

        assert ranked, "a deposit with no record produced no ranking at all"
        assert not any(c.kind == "descriptor" for c in ranked)
        assert {c.kind for c in ranked} == {"narrative", "tabular"}

    def test_a_thin_config_is_not_mistaken_for_a_record(self, tmp_path: Path) -> None:
        # Density is the evidence: a two-field config file is not a deposit record.
        (tmp_path / "config.json").write_text('{"debug": true}', encoding="utf-8")
        (tmp_path / "README.txt").write_text("Notes.\n" * 20)

        ranked = self._ranked(tmp_path)
        config = next((c for c in ranked if c.filename == "config.json"), None)
        assert config is None or config.kind != "descriptor"


def _block_for(context: str, candidate, ranked) -> str:
    """The context text belonging to one candidate.

    Bounded by the NEXT candidate's marker rather than by a blank line: a
    README's own text contains blank lines, so splitting on them truncates the
    block to its first paragraph and makes a full preview look empty.
    """
    marker = f"[{candidate.kind}/{candidate.classification}] {candidate.relative_path}"
    assert marker in context, f"{candidate.relative_path} is not in the context"
    start = context.index(marker)
    later = [
        context.index(m)
        for m in (f"[{o.kind}/{o.classification}] {o.relative_path}" for o in ranked)
        if m in context and context.index(m) > start
    ]
    return context[start : min(later) if later else len(context)]


class TestDataTablesContributeShapeNotRows:
    """The model is told what a table IS, not what it contains (#587).

    A language model deciding what the study is does not need measurement rows —
    those are the deterministic readers' job. One CSV preview took 3080 of the
    12 000-character budget to say what its header says in a line, and the
    protocols describing how the experiment was run were cut for lack of room.
    """

    def test_a_data_table_contributes_its_shape_only(self, ranked) -> None:
        context = format_document_context(ranked, total_scanned=99)
        table = _by_path(ranked, MEASUREMENTS)
        marker = f"[{table.kind}/{table.classification}] {table.relative_path}"
        assert marker in context, "the table is not named in the context at all"

        block = _block_for(context, table, ranked)
        assert "data table" in block and "row(s)" in block
        # Its header names the variables; its rows do not appear.
        assert "test_substance_id" in block, block[:200]
        assert "Amiodarone" not in block, "measurement rows reached the context"

    def test_descriptive_files_still_contribute_their_text(self, ranked) -> None:
        # The inverse must hold, or the fix is just a different kind of blindness.
        context = format_document_context(ranked, total_scanned=99)
        readme = _by_path(ranked, TOP_README)
        block = _block_for(context, readme, ranked)
        assert len(block) > 200, f"a README contributed almost nothing: {block!r}"

    def test_the_budget_now_reaches_the_descriptive_files(self, ranked) -> None:
        """The point of the change: prose is what was being starved."""
        context = format_document_context(ranked, total_scanned=99)
        present = [
            c for c in ranked
            if f"[{c.kind}/{c.classification}] {c.relative_path}" in context
        ]
        narrative = [c for c in present if c.kind == "narrative"]
        assert len(narrative) >= 5, (
            f"only {len(narrative)} descriptive files reached the context: "
            f"{[c.relative_path for c in present]}"
        )


class TestNoOneFileCrowdsOutTheRest:
    """Spending the budget in rank order let a long document delete cheap ones.

    Every ``.docx`` protocol in this deposit previews as a paragraph count — a
    couple of hundred characters — and they rank below the READMEs, which run to
    ~2 500 each. Three READMEs exhausted the context and all five protocols
    vanished from the listing: not shortened, never named, and the agent cannot
    read a file it was not told exists. The share is max-min fair instead.
    """

    def test_every_ranked_candidate_is_named(self, ranked) -> None:
        context = format_document_context(ranked, total_scanned=99)

        missing = [
            c.relative_path
            for c in ranked
            if f"[{c.kind}/{c.classification}] {c.relative_path}" not in context
        ]

        assert missing == [], f"{len(missing)} ranked file(s) never named: {missing}"

    def test_the_cheap_entries_are_not_the_ones_cut(self, ranked) -> None:
        """A protocol costs ~200 chars; it must never be dropped for a README."""
        context = format_document_context(ranked, total_scanned=99)

        protocols = [c for c in ranked if c.relative_path.lower().endswith(".docx")]

        assert protocols, "the fixture must carry .docx protocols for this to mean anything"
        for candidate in protocols:
            block = _block_for(context, candidate, ranked)
            assert block.strip(), f"{candidate.relative_path} was cut entirely"

    def test_the_ceiling_still_holds(self, ranked) -> None:
        """Fitting everything must not be achieved by spending without limit.

        The ceiling moved from 12 000 to 18 000 with the slot cap (#595): at 40
        slots the old budget squeezed the median entry from 433 characters to
        302, and an entry that says less is the cost the issue warned about.
        18 000 buys twice the files at their full size. What must not change is
        that there IS a ceiling and the listing respects it.
        """
        from builder.tools.document_discovery import _MAX_CONTEXT_CHARS

        context = format_document_context(ranked, total_scanned=99)

        blocks = context.split("\n\n(")[0]

        assert len(blocks) <= _MAX_CONTEXT_CHARS, len(blocks)

    def test_a_block_too_small_to_name_its_file_is_not_written(self, ranked) -> None:
        """Fairness cuts the other way at the bottom of the budget.

        Split far enough, every share lands INSIDE the ``[kind/class] path``
        header and the listing becomes a column of ``[nar […]`` — twenty entries
        naming nothing, which is strictly worse than the rank-order spend this
        replaced, since that at least bought one readable entry. The agent cannot
        read a file it was not told exists, and half a header does not tell it.
        No production caller passes a budget this small today; the guard is here
        so none can.
        """
        for budget in (200, 600, 1_200, 2_000, 12_000):
            context = format_document_context(ranked, max_chars=budget, total_scanned=99)
            for candidate in ranked:
                header = f"[{candidate.kind}/{candidate.classification}] {candidate.relative_path}"
                truncated = [
                    header[:n].rstrip() + " […]"
                    for n in range(1, len(header))
                    if header[:n].rstrip() + " […]" in context
                ]

                assert not truncated, (
                    f"budget {budget}: emitted {truncated[0]!r}, which names no file"
                )

    def test_what_does_fit_is_still_emitted(self, ranked) -> None:
        """Refusing the unnameable must not empty a budget that can afford some."""
        context = format_document_context(ranked, max_chars=2_000, total_scanned=99)

        named = sum(
            1
            for c in ranked
            if f"[{c.kind}/{c.classification}] {c.relative_path}" in context
        )

        assert named, "a 2 000-char budget must still name somebody"


class TestTheSlotsAreAllocatedByWhatTheFilesAre:
    """20 slots, four classes, and the ranking used to spend them on one axis (#595).

    The cap is the agent's whole view of the submission — a file that misses it
    is never named, and the agent cannot read a file it was not told exists. The
    ranking decided those slots by "how document-like is this?" over a
    population #591 can classify into four, so on the real deposits whole tiers
    vanished: svhps26 named 14 interchangeable plate readouts and **zero** of
    its 8 GraphPad analysis files, each carrying a kilobyte of readable content.

    Slots are now allocated the way `format_document_context` already allocates
    its CHARACTERS — max-min fair, so no class can crowd out another — with two
    differences that measurement forced:

    - every class present takes a floor first, so a tier is never wholly absent;
    - `raw_data_file` takes its floor and stands out of the redistribution.
      #598 established it is the one tier whose members are interchangeable: a
      sixth gamma-counter printout says nothing the first five did not, while a
      sixth protocol is a different experiment.

    Within a class the kind interleave still decides, because form is what makes
    an entry worth reading and class is what makes it worth naming.
    """

    from builder.tools.document_discovery import (
        CLASS_METADATA,
        CLASS_PROCESSED_DATA,
        CLASS_PROTOCOL,
        CLASS_RAW_DATA,
    )

    def _candidates(self, spec: list[tuple[str, str, float]]):
        """`(classification, kind, score)` triples as ranked candidates."""
        from builder.tools.document_discovery import DocumentationCandidate

        return [
            DocumentationCandidate(
                path=f"/d/{i}", filename=f"{i}.txt", relative_path=f"{i}.txt",
                kind=kind, classification=cls, score=score, preview="x",
            )
            for i, (cls, kind, score) in enumerate(spec)
        ]

    def _allocate(self, candidates, limit: int):
        from builder.tools.document_discovery import _allocate_slots

        return _allocate_slots(candidates, limit)

    def _counts(self, chosen) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in chosen:
            counts[candidate.classification] = counts.get(candidate.classification, 0) + 1
        return counts

    def test_a_tier_is_never_crowded_out_entirely(self) -> None:
        """svhps26's shape: one tier numerous, another tiny and invisible."""
        spec = [(self.CLASS_RAW_DATA, "tabular", 0.9 - i * 0.001) for i in range(40)]
        spec += [(self.CLASS_PROCESSED_DATA, "opaque", 0.3 - i * 0.001) for i in range(8)]

        counts = self._counts(self._allocate(self._candidates(spec), 20))

        assert counts.get(self.CLASS_PROCESSED_DATA, 0) > 0, counts

    def test_the_interchangeable_tier_never_costs_a_distinct_one_a_slot(self) -> None:
        """Every raw file outranks every protocol, and every protocol is still named.

        Raw takes what is left over rather than what it outranks — leaving a
        slot empty would help nobody, but taking one from a tier whose files
        differ from each other loses something no other file says.
        """
        spec = [(self.CLASS_RAW_DATA, "tabular", 0.9 - i * 0.001) for i in range(40)]
        spec += [(self.CLASS_PROTOCOL, "narrative", 0.3 - i * 0.001) for i in range(12)]

        counts = self._counts(self._allocate(self._candidates(spec), 20))

        assert counts[self.CLASS_PROTOCOL] == 12, counts
        assert counts[self.CLASS_RAW_DATA] == 8, counts

    def test_a_class_with_less_than_its_share_gives_the_rest_back(self) -> None:
        spec = [(self.CLASS_METADATA, "narrative", 0.9)]
        spec += [(self.CLASS_PROTOCOL, "narrative", 0.5 - i * 0.001) for i in range(30)]

        counts = self._counts(self._allocate(self._candidates(spec), 20))

        assert counts[self.CLASS_METADATA] == 1
        assert counts[self.CLASS_PROTOCOL] == 19, counts

    def test_every_slot_is_still_filled(self) -> None:
        spec = [(self.CLASS_RAW_DATA, "tabular", 0.9 - i * 0.001) for i in range(50)]

        assert len(self._allocate(self._candidates(spec), 20)) == 20

    def test_fewer_candidates_than_slots_names_them_all(self) -> None:
        spec = [(self.CLASS_METADATA, "narrative", 0.9), (self.CLASS_PROTOCOL, "narrative", 0.5)]

        assert len(self._allocate(self._candidates(spec), 20)) == 2

    def test_within_a_class_rank_decides_and_nothing_overrides_it(self) -> None:
        """Class balance replaces the kind interleave rather than nesting inside it.

        Interleaving by kind INSIDE a class re-created the defect #587 fixed:
        metadata's quota alternated, so READMEs scoring 0.578 and 0.521 displaced
        assay-metadata workbooks scoring 0.670 and 0.657 — a lower-scoring file
        beating a higher-scoring one in the same class. Kind balance is a
        property of the whole list, and the measured list holds it: 50% tabular
        against 42% narrative on the real fixture.
        """
        spec = [(self.CLASS_METADATA, "tabular", 0.9 - i * 0.001) for i in range(10)]
        spec += [(self.CLASS_METADATA, "narrative", 0.4 - i * 0.001) for i in range(10)]

        chosen = self._allocate(self._candidates(spec), 8)

        assert [c.score for c in chosen] == sorted((c.score for c in chosen), reverse=True)
        assert all(c.kind == "tabular" for c in chosen), [c.kind for c in chosen]

    def test_the_descriptor_still_leads(self, ranked) -> None:
        assert ranked[0].relative_path.replace("\\", "/") == DESCRIPTOR


class TestTheRealDepositIsRepresented:
    """Measured on the fixture the way #595 requires it reported: per class."""

    def test_no_class_takes_more_than_a_third_of_the_list(self, ranked) -> None:
        """It was metadata 9 of 20 against protocol 6, raw 1, processed 4."""
        counts: dict[str, int] = {}
        for candidate in ranked:
            counts[candidate.classification] = counts.get(candidate.classification, 0) + 1

        for cls, count in counts.items():
            assert count <= len(ranked) / 3 + 1, f"{cls} takes {count} of {len(ranked)}: {counts}"

    def test_every_class_the_deposit_holds_is_named(self, ranked) -> None:
        named = {c.classification for c in ranked}

        assert named == {"metadata", "protocol", "raw_data_file", "processed_data_file"}, named
