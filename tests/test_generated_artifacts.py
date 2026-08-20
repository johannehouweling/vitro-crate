"""The three files the build generates describe themselves.

`ro-crate-preview.html`, `ro-crate-graph.mmd` and `ro-crate-metadata-maturity.html`
are written by us, under those names, every time. They were being added with
almost nothing said about them, so the base profile asked them the same questions
it asks any entity — and nobody could answer, because nobody had noticed.

Two of the three are ordinary omissions. The third is not: the maturity report is
rendered FROM the validation result, so its byte count depends on how many
findings there were, and stating it changes the graph, which changes the findings,
which changes the size. Measured across three exports of one crate: 121,626 /
96,414 / 99,458 bytes. There is no fixed point, so the size is not stated and the
finding is not shown — the file itself stays in the crate, fully described.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools.builder import export_crate
from builder.tools.drafters import draft_investigation

# `ro-crate-graph.mmd` was the third; it is no longer written (#618).
ARTIFACTS = ("ro-crate-preview.html", "ro-crate-metadata-maturity.html")


@pytest.fixture(scope="module")
def written():
    state = CrateState()
    draft_investigation(state, {"name": "T", "description": "D"})
    with tempfile.TemporaryDirectory() as td:
        export_crate(state, output_path=td, validate=False)
        doc = json.loads((Path(td) / "ro-crate-metadata.json").read_text())
    return {n["@id"]: n for n in doc["@graph"]}


class TestTheyAreInTheCrate:
    @pytest.mark.parametrize("artifact", ARTIFACTS)
    def test_the_file_is_present(self, written, artifact):
        """Nothing here removes a file — only a property, and only from one."""
        assert artifact in written

    @pytest.mark.parametrize("artifact", ARTIFACTS)
    def test_it_says_what_it_is(self, written, artifact):
        node = written[artifact]
        assert node.get("name"), f"{artifact} has no human-readable name"
        assert node.get("about") == {"@id": "./"}


class TestSizes:
    def test_no_mermaid_graph_is_written(self, written):
        """The one artifact whose size WAS knowable is gone (#618) — nothing read
        the Mermaid it held, so the crate stopped carrying it."""
        assert "ro-crate-graph.mmd" not in written

    def test_the_maturity_report_does_not(self, written):
        """Circular: its content is a function of the validation result.

        Stating the size changes the graph, which changes the findings, which
        changes the size. Iterating does not converge, so it is not stated.
        """
        assert "contentSize" not in written["ro-crate-metadata-maturity.html"]

    def test_the_report_is_still_fully_described(self, written):
        node = written["ro-crate-metadata-maturity.html"]
        assert node.get("name") and node.get("description")
        assert node.get("encodingFormat") == "text/html"


class TestTheFindingIsNotShown:
    def test_the_unanswerable_finding_is_dropped(self):
        from builder.tools.validation import _is_unanswerable

        assert _is_unanswerable(
            {
                "entity_id": "./ro-crate-metadata-maturity.html",
                "message": "File Data Entities SHOULD have a `contentSize` property",
            }
        )

    def test_it_is_scoped_to_that_one_property_on_that_one_file(self):
        """Not a list that grows — the single artifact whose content is a function
        of the answer. Any other file, or any other property, still reports."""
        from builder.tools.validation import _is_unanswerable

        assert not _is_unanswerable(
            {"entity_id": "./ro-crate-metadata-maturity.html", "message": "SHOULD have a `name`"}
        )
        assert not _is_unanswerable(
            {"entity_id": "./data/x.csv", "message": "SHOULD have a `contentSize` property"}
        )
        assert not _is_unanswerable(
            {"entity_id": "./ro-crate-graph.mmd", "message": "SHOULD have a `contentSize` property"}
        )
