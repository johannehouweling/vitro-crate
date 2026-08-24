"""The agent is shown the deposit's shape, not just a sample of it (#599).

The context was a flat list of ranked files and one line: "(40 of 1468 scanned
files shown; 1428 not surfaced.)" — 2.7% of the deposit, with no shape. From
that the agent cannot tell how many assays the submission has, how they are
weighted, or what the 1428 unnamed files are.

The census is free: since #591 every scanned file carries a classification, and
#598 already groups them. A deposit whose hidden tail is 1352 instrument
printouts looked identical to one hiding 16 unread protocols.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import FileClassification
from builder.tools.document_discovery import (
    _MAX_CONTEXT_CHARS,
    classify_scanned_files,
    discover_documents,
    format_document_context,
    summarise_deposit,
)

pytestmark = pytest.mark.timeout(300)

FIXTURE = Path("tests/fixtures/svhps22_real_input")
TINY = Path("tests/fixtures/svhps26_real_input")


def _scanned(root: Path) -> list[FileClassification]:
    from builder.tools.scanner import scan_files

    resolved = str(root.resolve())
    files = scan_files(resolved, approved_roots={resolved})
    classify_scanned_files(files, input_root=resolved, approved_roots={resolved})
    return files


@pytest.fixture(scope="module")
def deposit() -> tuple[list[FileClassification], str]:
    if not FIXTURE.exists():  # pragma: no cover - fixture not checked out
        pytest.skip("S-VHPS22 fixture not available")
    files = _scanned(FIXTURE)
    return files, summarise_deposit(files, input_root=str(FIXTURE.resolve()))


class TestTheCensusNamesWhatIsThere:
    def test_every_class_the_deposit_holds_is_counted(self, deposit) -> None:
        files, census = deposit

        present = {f.classification for f in files}
        for classification in present:
            assert classification.replace("_data_file", "").replace("_", " ") in census, (
                f"{classification} missing from:\n{census}"
            )

    def test_the_total_is_stated(self, deposit) -> None:
        files, census = deposit

        assert str(len(files)) in census, census

    def test_the_folders_that_hold_the_files_are_named_with_their_weight(self, deposit) -> None:
        """svhps22 is one study with four assays of very uneven size. None of
        that reached the agent."""
        _, census = deposit

        for folder in ("assay_01_TH_uptake", "assay_02_deiodinase", "assay_03_metabolism"):
            assert folder in census, f"{folder} missing from:\n{census}"


class TestTheCensusEarnsItsSpace:
    def test_a_deposit_that_does_not_branch_gets_no_folder_tree(self) -> None:
        """The trimmed fixtures are a single chain of directories. A "tree" of one
        branch says nothing the class census has not already said, and the issue
        is explicit: a census must not be longer than the file list it summarises.
        """
        if not TINY.exists():  # pragma: no cover
            pytest.skip("fixture not available")
        files = _scanned(TINY)

        census = summarise_deposit(files, input_root=str(TINY.resolve()))

        assert census.count("\n") <= len(files), census

    def test_the_tree_is_bounded_and_says_what_it_elided(self, tmp_path) -> None:
        """A pathological deposit must not spend the whole budget describing
        itself — and the elision is stated, never silent (#587's rule)."""
        files = []
        for folder in range(60):
            path = tmp_path / f"folder_{folder:03d}" / "data.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("well_id,value\nA1,1\n", encoding="utf-8")
            files.append(
                FileClassification(
                    path=str(path), filename="data.csv", size=20, mime_type="text/csv"
                )
            )
        classify_scanned_files(
            files, input_root=str(tmp_path), approved_roots={str(tmp_path.resolve())}
        )

        census = summarise_deposit(files, input_root=str(tmp_path))

        assert census.count("\n") < 30, census
        assert "more" in census.lower(), census


class TestADepositWithoutFoldersIsStillDescribed:
    """Not every submission is a tree, and the degenerate shapes must not lie."""

    def _census(self, tmp_path: Path, layout: list[str]) -> str:
        files = []
        for relative in layout:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("well_id,value\nA1,1\n", encoding="utf-8")
            files.append(
                FileClassification(
                    path=str(path), filename=path.name, size=20, mime_type="text/csv"
                )
            )
        classify_scanned_files(
            files, input_root=str(tmp_path), approved_roots={str(tmp_path.resolve())}
        )
        return summarise_deposit(files, input_root=str(tmp_path))

    def test_a_flat_deposit_gets_the_tally_and_no_tree(self, tmp_path) -> None:
        """Nothing branches, so there is no shape to draw — and a one-row "tree"
        saying "12 files are in no folder" repeats the line above it."""
        census = self._census(tmp_path, [f"file_{i}.csv" for i in range(12)])

        assert census.startswith("Deposit: 12 files")
        assert "Shape" not in census, census

    def test_a_single_deep_chain_gets_no_tree_either(self, tmp_path) -> None:
        census = self._census(tmp_path, [f"a/b/c/d/file_{i}.csv" for i in range(6)])

        assert "Shape" not in census, census

    def test_one_file_is_not_one_files(self, tmp_path) -> None:
        assert self._census(tmp_path, ["only.csv"]).startswith("Deposit: 1 file —")

    def test_the_folder_rows_sum_to_the_total(self, deposit) -> None:
        """The descent walks past folders holding a single child, and files ABOVE
        the trunk were dropped on the way — including svhps22's own descriptor,
        the one file that states the study's identity. 1467 of 1468 were shown
        and nothing said so. A shape whose rows do not add up is a picture of a
        different deposit.
        """
        import re

        files, census = deposit

        stated = re.search(r"Deposit: (\d+)", census)
        assert stated, census
        total = int(stated.group(1))
        rows = [int(n) for n in re.findall(r"^ {2}\S.*?\s(\d+) —", census, re.M)]

        assert total == len(files)
        assert sum(rows) == total, f"{total - sum(rows)} files missing from:\n{census}"


class TestTheContextLeadsWithTheShape:
    def test_the_hidden_tail_is_broken_down_by_class(self, deposit) -> None:
        """"1428 not surfaced" tells the agent nothing about whether the tail is
        worth opening."""
        files, _ = deposit
        root = str(FIXTURE.resolve())
        candidates = discover_documents(files, input_root=root, approved_roots={root})

        context = format_document_context(candidates, deposit=files, input_root=root)

        tail = context.rsplit("(", 1)[-1]
        assert "raw data" in tail, tail
        assert "metadata" in tail, tail

    def test_the_breakdown_counts_the_hidden_files_and_not_the_deposit(self, deposit) -> None:
        """It first reported the whole deposit's tally against the hidden count —
        "14 not surfaced: 15 processed, 14 metadata, 13 protocol, 12 raw", which
        is 54 files' worth of breakdown for 14 files. The engine hand-rolled the
        document dicts and omitted `path`, so a rebuilt candidate had no identity
        to subtract from the inventory. The numbers must add up.
        """
        import re

        files, _ = deposit
        root = str(FIXTURE.resolve())
        candidates = discover_documents(files, input_root=root, approved_roots={root})

        context = format_document_context(candidates, deposit=files, input_root=root)
        tail = context.rsplit("(", 1)[-1]

        hidden = len(files) - len(candidates)
        counted = sum(int(n) for n in re.findall(r"(\d+) [a-z]", tail.split("not surfaced:")[1]))
        assert counted == hidden, tail

    def test_the_shape_comes_before_the_sample(self, deposit) -> None:
        files, _ = deposit
        root = str(FIXTURE.resolve())
        candidates = discover_documents(files, input_root=root, approved_roots={root})

        context = format_document_context(candidates, deposit=files, input_root=root)

        assert context.index("assay_02_deiodinase") < context.index("["), context[:400]

    def test_the_census_does_not_crowd_out_the_documents(self, deposit) -> None:
        """A long tree must not delete the documents it is supposed to introduce
        — the failure `_fair_shares` exists to stop, one layer up."""
        files, _ = deposit
        root = str(FIXTURE.resolve())
        candidates = discover_documents(files, input_root=root, approved_roots={root})

        context = format_document_context(candidates, deposit=files, input_root=root)

        assert len(context.split("\n\n(")[0]) <= _MAX_CONTEXT_CHARS, len(context)
        named = sum(1 for c in candidates if f"] {c.relative_path}" in context)
        assert named == len(candidates), f"{len(candidates) - named} candidates lost to the census"
