"""A file is described from its CONTENT, or not at all.

"Add a description for metabolism_assay_metadata.xlsx, 20231213_BCA SK uptake
23-11.xlsx … and 40 others" is the largest single action in a real report, and a
model can close it: the files are in the crate and readable.

D5 is not in the way — it governs IDENTIFIERS, which may only come from an
authoritative lookup. A sentence saying what a spreadsheet holds is the same kind
of value as the study description and protocol names the model already writes,
and it carries the same ``source="llm"`` provenance.

What IS in the way is describing a file from its NAME. That is a guess, and a
guess in ``description`` reads exactly like a curator's sentence once it is in
the crate. Most of this module tests the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import CrateState
from builder.tools.file_descriptions import describe_payload_files
from builder.tools.provenance import draft_file


@pytest.fixture
def crate_with_files(tmp_path: Path):
    """A state with one readable file and one whose content cannot be read."""
    (tmp_path / "uptake.csv").write_text(
        "well,compound,absorbance\nA1,T4,0.812\nA2,T4,0.799\n", encoding="utf-8"
    )
    state = CrateState()
    state.metadata.input_path = str(tmp_path)
    draft_file(state, name="uptake.csv", path=str(tmp_path / "uptake.csv"))
    # Never created on disk: a file the crate references but cannot read.
    draft_file(state, name="protocol.docx", path=str(tmp_path / "protocol.docx"))
    return state


def _entity(state: CrateState, name: str):
    return next(e for e in state.list_entities("File") if e.fields.get("name") == name)


class TestTheContentIsTheEvidence:
    def test_an_unreadable_file_is_never_even_sent_to_the_model(self, crate_with_files) -> None:
        """The refusal this module exists for. `protocol.docx` has a name that
        practically writes its own description, which is exactly why it must not
        be allowed to."""
        sent: list[list[dict]] = []

        def _describe(files):
            sent.append(files)
            return ["A description." for _ in files]

        describe_payload_files(crate_with_files, describe_fn=_describe)

        assert [f["name"] for batch in sent for f in batch] == ["uptake.csv"]
        assert _entity(crate_with_files, "protocol.docx").fields.get("description") is None

    def test_a_readable_file_is_described(self, crate_with_files) -> None:
        written = describe_payload_files(
            crate_with_files,
            describe_fn=lambda files: ["Per-well absorbance readings." for _ in files],
        )

        assert len(written) == 1
        assert _entity(crate_with_files, "uptake.csv").fields["description"] == (
            "Per-well absorbance readings."
        )

    def test_the_model_sees_the_actual_content(self, crate_with_files) -> None:
        """Not just the name — otherwise "grounded in the content" is a claim the
        code does not keep."""
        seen: list[str] = []

        def _describe(files):
            seen.extend(str(f["preview"]) for f in files)
            return ["x" for _ in files]

        describe_payload_files(crate_with_files, describe_fn=_describe)

        assert any("absorbance" in preview for preview in seen)


class TestItWritesNothingItCannotStandBehind:
    def test_an_empty_description_is_a_decline_not_a_value(self, crate_with_files) -> None:
        """The model is told to return "" when the preview supports nothing
        specific. That must not become an empty description on the entity."""
        written = describe_payload_files(
            crate_with_files, describe_fn=lambda files: ["" for _ in files]
        )

        assert written == []
        assert _entity(crate_with_files, "uptake.csv").fields.get("description") is None

    def test_a_mismatched_batch_is_discarded_whole(self, crate_with_files) -> None:
        """Descriptions are matched to files by POSITION, so a short list would
        shift every sentence onto the wrong file — silently, and in a field that
        reads as curated prose."""
        written = describe_payload_files(crate_with_files, describe_fn=lambda files: [])

        assert written == []
        assert _entity(crate_with_files, "uptake.csv").fields.get("description") is None

    def test_a_raising_model_does_not_sink_the_build(self, crate_with_files) -> None:
        def _boom(_files):
            raise RuntimeError("provider down")

        assert describe_payload_files(crate_with_files, describe_fn=_boom) == []

    def test_an_existing_description_is_left_alone(self, crate_with_files) -> None:
        """A curator's sentence is not improved by replacing it with a model's."""
        entity = _entity(crate_with_files, "uptake.csv")
        entity.set_fields_from_dict({"description": "Written by a human."}, source="user")

        describe_payload_files(
            crate_with_files, describe_fn=lambda files: ["Model text." for _ in files]
        )

        assert entity.fields["description"] == "Written by a human."


class TestProvenanceSaysWhoWroteIt:
    def test_a_generated_description_is_recorded_as_the_models(
        self, crate_with_files
    ) -> None:
        """The answer to "is this curated?" lives in the crate's own completion
        record, so no separate flag is needed — but only if it is actually set."""
        describe_payload_files(
            crate_with_files, describe_fn=lambda files: ["Generated." for _ in files]
        )

        status = _entity(crate_with_files, "uptake.csv").get_field_status("description")
        assert status is not None
        assert status.source == "llm"


class TestFilesystemAccessStaysFailClosed:
    def test_nothing_is_read_without_an_approved_root(self, tmp_path: Path) -> None:
        """A description is enrichment and never a reason to widen filesystem
        access (#197). With no approved root and no input path, the preview is
        empty and the file is skipped."""
        (tmp_path / "secret.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        state = CrateState()
        state.metadata.input_path = None
        draft_file(state, name="secret.csv", path=str(tmp_path / "secret.csv"))

        sent: list = []
        describe_payload_files(
            state, describe_fn=lambda files: sent.append(files) or ["x" for _ in files]
        )

        assert sent == []


class TestABinaryFileIsStillDescribableFromItsContent:
    """The feature was inert on a real deposit until this.

    `read_file_sample(mode="content")` returns the first LINES of a file, which
    is right for CSV and text and is **nothing at all** for a binary format.
    Measured against the shipped corpus, every .xlsx and .docx came back 0
    characters — and a deposit is mostly .xlsx and .docx, so "describe the payload
    files" described none of them.

    The file-type summary is still the file's own content — sheet names, column
    headers, sample paragraphs — just the part that survives not being plain
    text. Driven against committed fixtures rather than a synthesised workbook,
    because what broke here was the behaviour of the real readers on real files.
    """

    FIXTURES = Path(__file__).parent / "fixtures"

    def _describable(self, source: Path, tmp_path: Path) -> list[dict]:
        import shutil

        shutil.copy(source, tmp_path / source.name)
        state = CrateState()
        state.metadata.input_path = str(tmp_path)
        draft_file(state, name=source.name, path=str(tmp_path / source.name))

        seen: list[dict] = []

        def _describe(files):
            seen.extend(files)
            return ["A description." for _ in files]

        describe_payload_files(state, describe_fn=_describe)
        return seen

    def test_an_xlsx_reaches_the_model_with_its_sheets_and_columns(
        self, tmp_path: Path
    ) -> None:
        source = self.FIXTURES / "svhps22_real_input" / "Metadataveldenlijst_1.2.0.xlsx"
        assert source.is_file(), "fixture missing; this test would pass vacuously"

        sent = self._describable(source, tmp_path)

        assert len(sent) == 1, "the workbook was skipped as unreadable"
        preview = sent[0]["preview"]
        assert "Excel" in preview
        # The part that makes a description possible: what is actually in it.
        assert "rows" in preview and "columns" in preview

    def test_a_docx_reaches_the_model_with_its_text(self, tmp_path: Path) -> None:
        source = (
            self.FIXTURES
            / "svhps22_real_input"
            / "assay_01_TH_uptake"
            / "3.2 Protocol transporter assay radioactive T3 T4_EN.docx"
        )
        assert source.is_file(), "fixture missing; this test would pass vacuously"

        sent = self._describable(source, tmp_path)

        assert len(sent) == 1, "the document was skipped as unreadable"
        assert "Word" in sent[0]["preview"]

    def test_plain_text_still_uses_its_actual_lines(self, tmp_path: Path) -> None:
        """The control. The summary is a FALLBACK, not a replacement — the rows
        of a CSV are better evidence than a description of its shape."""
        (tmp_path / "uptake.csv").write_text(
            "well,compound,absorbance\nA1,T4,0.812\n", encoding="utf-8"
        )
        state = CrateState()
        state.metadata.input_path = str(tmp_path)
        draft_file(state, name="uptake.csv", path=str(tmp_path / "uptake.csv"))

        seen: list[dict] = []
        describe_payload_files(
            state,
            describe_fn=lambda files: seen.extend(files) or ["x" for _ in files],
        )

        assert "A1,T4,0.812" in seen[0]["preview"]
        assert "Format:" not in seen[0]["preview"]

    def test_a_file_that_is_not_there_is_still_refused(self, tmp_path: Path) -> None:
        """The fallback must not become a way to describe a file that does not
        exist — the summary reader returns nothing for it too, and nothing is
        exactly what should reach the model."""
        state = CrateState()
        state.metadata.input_path = str(tmp_path)
        draft_file(state, name="absent.xlsx", path=str(tmp_path / "absent.xlsx"))

        seen: list[dict] = []
        describe_payload_files(
            state,
            describe_fn=lambda files: seen.extend(files) or ["x" for _ in files],
        )

        assert seen == []
