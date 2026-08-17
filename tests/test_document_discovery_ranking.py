"""Discovery ranks what carries the facts, not what reads like prose (#587).

The ranking decides what the agent sees at all — 20 candidates out of 54 files,
against a 12 000-character context cap — and it used to award +0.12 for
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
        # "20 candidates" hid 34 files. Same rule the maturity report follows:
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
    marker = f"[{candidate.kind}/{candidate.role}] {candidate.relative_path}"
    assert marker in context, f"{candidate.relative_path} is not in the context"
    start = context.index(marker)
    later = [
        context.index(m)
        for m in (f"[{o.kind}/{o.role}] {o.relative_path}" for o in ranked)
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
        marker = f"[{table.kind}/{table.role}] {table.relative_path}"
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
            if f"[{c.kind}/{c.role}] {c.relative_path}" in context
        ]
        narrative = [c for c in present if c.kind == "narrative"]
        assert len(narrative) >= 5, (
            f"only {len(narrative)} descriptive files reached the context: "
            f"{[c.relative_path for c in present]}"
        )
