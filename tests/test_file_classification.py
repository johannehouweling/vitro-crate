"""One classification per file, decided by content before path (#591).

Which experimental step produced a file used to be decided by
``composites._path_tier``, which read only the folder names: does a directory
contain the token "raw", or "process"? Exactly one match answered; both or
neither and the file was skipped. On ``svhps22`` that left 15 of 54 files
unresolved — including the single most valuable table in the deposit, 1048 tidy
per-condition rows filed under a folder called ``EDCs``, which says nothing. On
``svhps21`` and ``svhps26`` it resolved *nothing*: both file every measurement
under one ``Raw data + individual processed data/``, which names both tiers.

So the answer comes from the file, in this order:

1. **content** — the instrument's own column headers (``I-125 CPM``,
   ``Protocol ID``), a GraphPad project's XML root, a ``Field``/``Value``
   metadata template, the vocabulary a document's prose covers;
2. **filename** — ``Combined … _tidy``, ``SOP``, ``README``, ``.prism``;
3. **path** — the folder tier, and only when the file itself said nothing.

Four values, and every scanned file gets exactly one. ``metadata`` covers the
deposit record, the assay-metadata workbooks, publications and plate maps;
``protocol`` covers SOPs, lab protocols and analysis scripts; the other two are
the data tiers a derivation chain is wired from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.state import FileClassification
from builder.tools.document_discovery import (
    CLASS_METADATA,
    CLASS_PROCESSED_DATA,
    CLASS_PROTOCOL,
    CLASS_RAW_DATA,
    FILE_CLASSES,
    classification_of,
    classify_file,
    classify_scanned_files,
)

# The LabLogic gamma-counter export, verbatim from svhps22's `004668.csv`.
_COUNTER_EXPORT = (
    "Protocol ID,Protocol name,Measurement date & time,Completion status,Run ID,"
    "Rack,Det,Pos,Time,Sample code,I-125 Counts,I-125 CPM,I-125 Error %,I-125 Info\n"
    "61,TELLING 1 min,2024-03-27 08:39:02,0,4668,1,1,1,60.04,,780044.36,807981.98,0.11,"
)
# svhps22's tidy analysis output: one row per condition, no instrument columns.
_TIDY_TABLE = (
    "run_id,biosample_type,biosample_id,test_substance_id,"
    "exposure_concentration_value,exposure_concentration_unit,"
    "exposure_duration_value,exposure_duration_unit,assay_endpoint,replicate_id,"
    "measurement_type,measurement_value,measurement_unit"
)
# The VHP4Safety assay-metadata workbook, as the scanner's summary preview shows it.
_METADATA_TEMPLATE = (
    "Format: Excel (.xlsx) General information: 15 rows x 4 cols; columns: "
    "Parameter, Standard or ontology reference, Value, Comments"
)
_PRISM_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<GraphPadPrismFile xmlns="http://graphpad.com/prism/Prism.htm" PrismXMLVersion="5.00">'
)


def _classify(filename: str, preview: str = "", path: str = "") -> str:
    return classify_file(filename, preview, path or filename)[0]


class TestContentOutranksThePath:
    """The case the folder rule cannot reach: both tiers in one directory.

    svhps21 and svhps26 deposit every run into a single ``Raw data + individual
    processed data/``. The folder names both tiers, so it declares neither — and
    two files sitting side by side inside it belong to different steps.
    """

    _COMBINED = "Assay_OATP1C1/raw data+individual processed data/220825_RA/"

    def test_the_instrument_export_in_a_combined_folder_is_raw(self):
        assert (
            _classify(
                "220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.xlsx",
                _COUNTER_EXPORT,
                self._COMBINED + "220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.xlsx",
            )
            == CLASS_RAW_DATA
        )

    def test_the_graphpad_project_beside_it_is_processed(self):
        assert (
            _classify(
                "220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.pzfx",
                _PRISM_XML,
                self._COMBINED + "220825_RA_CHO-K1_hOATP1C1_P1_Timecourse.pzfx",
            )
            == CLASS_PROCESSED_DATA
        )

    def test_a_readme_inside_a_raw_folder_is_not_raw_data(self):
        """The crude first pass filed `assay1_rawdata/README.txt` as a measurement."""
        assert (
            _classify("README.txt", "", "characterisation/assay1_rawdata/README.txt")
            == CLASS_METADATA
        )

    def test_a_publication_inside_a_processed_folder_is_not_processed_data(self):
        assert (
            _classify(
                "Krebs et al (2018) - Normalization of Data for Viability (ALTEX).pdf",
                "",
                "Study wide processed data/Data for statistical analysis/"
                "Krebs et al (2018) - Normalization of Data for Viability (ALTEX).pdf",
            )
            == CLASS_METADATA
        )


class TestContentDecidesTheTier:
    def test_instrument_columns_make_a_table_raw(self):
        assert _classify("004668.csv", _COUNTER_EXPORT) == CLASS_RAW_DATA

    def test_a_derived_summary_sheet_makes_a_workbook_processed(self):
        preview = (
            "Format: Excel (.xlsx) Summary: 12 rows x 5 cols Cells: 38 rows x 13 cols "
            "layout 27-03: 58 rows x 17 cols Conversion Deio3: 28 rows x 28 cols"
        )
        assert _classify("20240327_Xn reeks Sk H4.xlsx", preview) == CLASS_PROCESSED_DATA

    def test_a_workbook_holding_both_is_the_processed_one(self):
        """svhps22's own README: the processed workbook embeds its raw tab.

        `Raw data 27-03` is a sheet INSIDE the per-experiment processed file —
        "Raw data files matching the processed data in the file can be found in
        processsed data file: tab: 'Raw data "date"'".
        """
        preview = (
            "Format: Excel (.xlsx) Summary: 12 rows x 5 cols "
            "Raw data 27-03: 293 rows x 22 cols; columns: Protocol ID, Protocol name, "
            "Measurement date & time, Run ID, I-125 CPM"
        )
        assert _classify("20240327_Xn reeks Sk H4.xlsx", preview) == CLASS_PROCESSED_DATA

    def test_a_parameter_value_template_is_metadata_not_a_data_table(self):
        """A tabular METADATA workbook: the old vocabulary could only call it
        `data_table`, because form and function were the same label."""
        assert (
            _classify("Assay-metadata-CHO-K1_OATP1C1-v1.1.xlsx", _METADATA_TEMPLATE)
            == CLASS_METADATA
        )

    def test_a_structured_deposit_record_is_metadata(self):
        preview = (
            '{ "accno" : "S-VHPS26", "attributes" : [ { "name" : "Template", '
            '"value" : "VHP4Safety" }, { "name" : "Title", "value" : "Inhibition" }, '
            '{ "name" : "DOI", "value" : "10.1" } ], "type" : "submission"'
        )
        assert _classify("S-VHPS26.json", preview) == CLASS_METADATA

    def test_a_procedure_document_is_a_protocol(self):
        preview = (
            "Standard operating procedure for the uptake assay. Materials and methods: "
            "incubate the plate for 60 minutes at 37 C, then wash twice and pipette "
            "200 uL of lysis buffer into each well."
        )
        assert _classify("TH 250425.docx", preview) == CLASS_PROTOCOL

    def test_a_readme_describing_the_folder_is_metadata(self):
        preview = (
            "Processed file are sorted per experiment. Raw data files matching the "
            "processed data in the file can be found in processsed data file."
        )
        assert _classify("README.txt", preview) == CLASS_METADATA

    def test_one_hit_each_is_not_a_verdict(self):
        """svhps22's study README covers one term of each vocabulary. The two
        lists differ in length, so that came out 8.3% against 7.7% — a winner
        decided by the denominator, which handed a study description to
        `protocol` on the strength of the word "incubated". Below a real margin
        the prose has said nothing, and the filename answers."""
        preview = (
            "# Study README Template ## Study Title Neural cell screening models "
            "## Study Description Cells were incubated at 37 C."
        )

        assert _classify("README.txt", preview) == CLASS_METADATA

    def test_a_clear_margin_still_decides(self):
        """The same shape one term further apart: an assay README that describes
        how the work was run IS a protocol (owner's rule on #591)."""
        preview = (
            "# Assay README Template ## Assay Title Thyroid hormone uptake "
            "## Assay Description Cells were incubated in uptake buffer."
        )

        assert _classify("README-template.txt", preview) == CLASS_PROTOCOL


class TestTheFilenameDecidesWhenContentIsSilent:
    """Every ``.docx`` and most ``.pdf`` in these deposits preview as nothing."""

    @pytest.mark.parametrize(
        "filename",
        [
            "3.2 Protocol transporter assay radioactive T3 T4_EN.docx",
            "OATP1C1 SOP TH 250425.docx",
            "4.1 Deiodinase activity assay.docx",
        ],
    )
    def test_a_named_protocol_is_a_protocol(self, filename):
        assert _classify(filename, "") == CLASS_PROTOCOL

    @pytest.mark.parametrize(
        "filename",
        ["analysis.py", "normalise.R", "run_stats.sh", "fit_curves.ipynb"],
    )
    def test_an_analysis_script_is_a_protocol(self, filename):
        """`protocol` covers how the work was done, computational or benchside."""
        assert _classify(filename, "") == CLASS_PROTOCOL

    @pytest.mark.parametrize(
        "filename",
        [
            "Combined uptake data EDCs_tidy.csv",
            "Combined uptake data EDCs.xlsx",
            "2024-09-26 combined results NH-3 & TBBPA.xlsx",
            "Endogenous D2 and D3 activity.prism",
            "Figure 12 T3 curve KLF9.png",
        ],
    )
    def test_a_derived_name_is_processed(self, filename):
        assert _classify(filename, "") == CLASS_PROCESSED_DATA

    def test_the_most_valuable_table_in_the_deposit_resolves(self):
        """1048 tidy rows under a folder named `EDCs`, which declares no tier —
        skipped entirely by the folder rule (#591)."""
        assert (
            _classify(
                "Combined uptake data EDCs_tidy.csv",
                _TIDY_TABLE,
                "assay_01_TH_uptake/EDCs/Combined uptake data EDCs_tidy.csv",
            )
            == CLASS_PROCESSED_DATA
        )

    def test_a_named_raw_export_is_raw(self):
        assert _classify("2024-10-30 SK sily n3 Raw data.eds", "") == CLASS_RAW_DATA

    @pytest.mark.parametrize(
        "filename", ["Metadataveldenlijst_1.2.0.xlsx", "sample_sheet.csv", "plate map.xlsx"]
    )
    def test_a_metadata_name_is_metadata(self, filename):
        assert _classify(filename, "") == CLASS_METADATA

    def test_what_a_file_is_outranks_which_tier_it_would_be(self):
        """`Normalization` is a derived-data word; this is still a paper."""
        assert (
            _classify("Krebs et al (2018) - Normalization of Data (ALTEX).pdf", "")
            == CLASS_METADATA
        )

    def test_a_publication_is_recognised_by_name_and_not_by_content(self):
        """The gap engine asks this to notice a deposited article nobody recorded.

        Content is the wrong place to ask: the deposit record names a ``DOI``
        field and an assay README shows a worked citation, so both read as papers
        on all three real deposits and neither is one.
        """
        from builder.tools.document_discovery import looks_like_publication

        assert looks_like_publication("Krebs et al (2018) - Normalization (ALTEX).pdf")
        assert not looks_like_publication("S-VHPS22.json")
        assert not looks_like_publication("README-template.txt")


class TestThePathIsTheLastResort:
    def test_an_unreadable_printout_in_a_raw_folder_is_raw(self):
        """A timestamp for a filename and no extractable text: the folder is all
        that is left, and it says raw."""
        assert (
            _classify(
                "20221020064804 WASH.pdf",
                "Format: PDF Pages: ~1",
                "characterisation/20221019_H4/raw data/Dejodase 191022/"
                "20221020064804 WASH.pdf",
            )
            == CLASS_RAW_DATA
        )

    def test_a_silent_table_in_a_processed_folder_is_processed(self):
        assert (
            _classify(
                "2023-03-01 0-60 min SKNAS.xlsx",
                "",
                "characterisation uptake/assay1_processeddata/2023-03-01 0-60 min SKNAS.xlsx",
            )
            == CLASS_PROCESSED_DATA
        )

    @pytest.mark.parametrize(
        ("path", "declared"),
        [
            # the conventions the three real deposits actually use
            ("a/raw data/x.csv", CLASS_RAW_DATA),
            ("a/assay1_rawdata/x.csv", CLASS_RAW_DATA),
            ("a/Raw/x.csv", CLASS_RAW_DATA),
            ("a/RAW DATA/x.csv", CLASS_RAW_DATA),
            ("a/processed data/x.csv", CLASS_PROCESSED_DATA),
            ("a/assay1_processeddata/x.csv", CLASS_PROCESSED_DATA),
            ("a/assay4_EDCs_processed data/x.csv", CLASS_PROCESSED_DATA),
            ("a/data processing/x.csv", CLASS_PROCESSED_DATA),
            # one directory naming both tiers — svhps21 and svhps26
            ("a/Raw data + individual processed data/x.csv", None),
            ("a/raw data+individual processed data/x.csv", None),
            # neither tier named
            ("a/EDCs/x.csv", None),
            ("a/characterisation/x.csv", None),
            ("x.csv", None),
            # substring traps: `raw` is inside "drawings", and `process` is
            # inside "unprocessed", which names the OPPOSITE tier
            ("a/drawings/x.csv", None),
            ("a/unprocessed/x.csv", None),
            ("a/preprocessed/x.csv", None),
        ],
    )
    def test_what_a_directory_declares(self, path, declared):
        from builder.tools.document_discovery import _directory_class

        result = _directory_class(path)
        assert (result[0] if result else None) == declared

    def test_a_word_processor_document_is_never_placed_by_its_folder(self):
        """Bench notes filed beside the measurements are not measurements.

        A `.pdf` in the same folder IS read as raw — four of svhps22's raw
        outputs are counter printouts with a timestamp for a name — but nothing
        except a person writes a `.docx`, so the folder says nothing about one.
        """
        assert _classify("bench notes.docx", "", "raw data/bench notes.docx") == CLASS_METADATA
        assert (
            _classify("2024-02-1616.13.29.pdf", "", "raw data/2024-02-1616.13.29.pdf")
            == CLASS_RAW_DATA
        )

    def test_a_folder_naming_both_tiers_declares_neither(self):
        assert (
            _classify(
                "220331_SK_MCT8_MDCK1_P3.xls",
                "",
                "Assay_MCT8-MDCK1/Raw data + individual processed data/220331_SK/"
                "220331_SK_MCT8_MDCK1_P3.xls",
            )
            == CLASS_RAW_DATA
        ), "no tier is declared, so this is the default — not the folder's first token"


class TestTheDefaults:
    def test_an_unsignalled_data_format_defaults_to_raw(self):
        """Least-transformed is the safer assertion for a table nothing describes."""
        assert _classify("export.csv", "") == CLASS_RAW_DATA

    def test_an_unsignalled_document_defaults_to_metadata(self):
        """A document is not a measurement; calling it one would put prose in the
        derivation chain as the instrument's output."""
        assert _classify("5.2.2 RNeasy 2 DNAse stappen.pdf", "") == CLASS_METADATA

    def test_every_file_gets_exactly_one_of_the_four(self):
        assert set(FILE_CLASSES) == {
            CLASS_METADATA, CLASS_PROTOCOL, CLASS_RAW_DATA, CLASS_PROCESSED_DATA
        }, "FILE_CLASSES is what the rest of the codebase documents itself against"
        for name in ("a.csv", "b.docx", "c.png", "d", "e.zip", "f.json", "g.pzfx"):
            assert _classify(name, "") in FILE_CLASSES

    def test_the_reason_names_which_signal_decided(self):
        _, reason = classify_file("004668.csv", _COUNTER_EXPORT, "raw data/004668.csv")
        assert "content" in reason.lower(), reason


class TestClassifyingTheWholeDeposit:
    """What gets wired into the crate must not depend on what fits in a prompt.

    ``discover_documents`` caps at 20 candidates because its job is filling a
    12 000-character context. Classification runs over everything.
    """

    def _files(self, root: Path, count: int) -> list[FileClassification]:
        files = []
        for n in range(count):
            path = root / "raw data" / f"m{n:03d}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_COUNTER_EXPORT, encoding="utf-8")
            files.append(
                FileClassification(
                    path=str(path), filename=path.name, size=path.stat().st_size,
                    mime_type="text/csv",
                )
            )
        return files

    def test_more_files_than_the_ranking_shows_are_all_classified(self, tmp_path):
        files = self._files(tmp_path, 30)

        classify_scanned_files(
            files, input_root=str(tmp_path), approved_roots={str(tmp_path.resolve())}
        )

        assert all(f.classification == CLASS_RAW_DATA for f in files)
        assert len(files) == 30, "nothing was dropped by a ranking cap"

    def test_the_stamp_survives_a_round_trip(self, tmp_path):
        file = self._files(tmp_path, 1)[0]
        classify_scanned_files(
            [file], input_root=str(tmp_path), approved_roots={str(tmp_path.resolve())}
        )

        restored = FileClassification.from_dict(file.to_dict())

        assert restored.classification == CLASS_RAW_DATA

    def test_an_unstamped_file_is_classified_without_reading_the_disk(self):
        """`composites` asks this question with no approved roots and no deposit
        mounted, so it must answer from the inventory record alone."""
        file = FileClassification(
            path="/deposit/assay_01/raw data/004668.csv",
            filename="004668.csv",
            size=926,
            mime_type="text/csv",
            first_rows=list(_COUNTER_EXPORT.splitlines()),
        )

        assert file.classification is None
        assert classification_of(file, input_root="/deposit") == CLASS_RAW_DATA

    def test_a_stamped_file_is_taken_at_its_word(self):
        file = FileClassification(
            path="/deposit/raw data/x.csv", filename="x.csv", size=1, mime_type="text/csv"
        )
        file.classification = CLASS_PROCESSED_DATA

        assert classification_of(file, input_root="/deposit") == CLASS_PROCESSED_DATA


# A derived table, decided by its own headers rather than by where it sits:
# `_TIDY_TABLE` needs its `_tidy` filename to place it, and these tests are
# about folders whose files are named only by a run number.
_SUMMARY_TABLE = (
    "test_substance,concentration_uM,replicate_mean,replicate_sd,"
    "normalised_response,ic50\n"
    "amiodarone,10,0.82,0.04,0.61,3.4"
)
# svhps22's SOP prose, which classifies on the procedure vocabulary alone.
_SOP_PROSE = (
    "Standard operating procedure for the uptake assay. Materials and methods: "
    "incubate the plate for 60 minutes at 37 C, then wash twice and pipette "
    "200 uL of lysis buffer into each well."
)


def _write(root: Path, relative: str, text: str) -> FileClassification:
    """One scanned file on disk, as the scanner would have inventoried it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return FileClassification(
        path=str(path), filename=path.name, size=path.stat().st_size, mime_type="text/plain"
    )


def _classify_all(root: Path, files: list[FileClassification]) -> dict[str, str]:
    classify_scanned_files(
        files, input_root=str(root), approved_roots={str(root.resolve())}
    )
    return {f.path: f.classification or "" for f in files}


@pytest.fixture
def opened(monkeypatch) -> list[str]:
    """Every path classification actually read off the disk."""
    import builder.tools.document_discovery as discovery

    seen: list[str] = []
    real = discovery._safe_preview

    def counting(path, *args, **kwargs):
        seen.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(discovery, "_safe_preview", counting)
    return seen


class TestSamplingAFolderInsteadOfOpeningIt:
    """A deposit is a handful of homogeneous folders, not N distinct things (#598).

    Classifying svhps22 opened all 1468 scanned files to learn that 1358 of them
    were the same gamma-counter printout. Those 1468 fall into 149
    ``(directory, extension)`` groups, the largest holding 84 PDFs: opening one
    of those tells you what the other 83 are, and opening all 84 costs 84 PDF
    parses to learn nothing.

    Coverage is what must not change — every file still carries a class (#591).
    Only how that class is DERIVED does.
    """

    def test_the_reads_do_not_grow_with_the_folder(self, tmp_path, opened):
        """The sample is a fixed cost, not a share of the folder."""

        def folder(name: str, count: int) -> int:
            root = tmp_path / name
            files = [_write(root, f"raw data/{n:03d}.csv", _COUNTER_EXPORT) for n in range(count)]
            opened.clear()
            _classify_all(root, files)
            return len(opened)

        for_twenty = folder("a", 20)

        assert folder("b", 200) == for_twenty, "ten times the folder cost ten times the reads"
        assert for_twenty < 20, f"nothing was saved: {for_twenty} reads for 20 files"

    def test_every_file_still_carries_a_class(self, tmp_path):
        files = [_write(tmp_path, f"raw data/{n:03d}.csv", _COUNTER_EXPORT) for n in range(80)]

        _classify_all(tmp_path, files)

        assert all(f.classification == CLASS_RAW_DATA for f in files)

    def test_a_folder_smaller_than_the_sample_is_opened_in_full(self, tmp_path, opened):
        files = [_write(tmp_path, f"raw data/{n}.csv", _COUNTER_EXPORT) for n in range(2)]

        _classify_all(tmp_path, files)

        assert len(opened) == 2

    @pytest.mark.parametrize("odd_at", [0, 40, 79], ids=["first", "middle", "last"])
    def test_a_folder_whose_sample_disagrees_is_opened_in_full(self, tmp_path, opened, odd_at):
        """Disagreement means the folder is heterogeneous and cannot be summarised.

        The odd file is planted at each end as well as the middle, because a
        sample taken as "the first three" would never reach the last one — and
        a deposit sorted by date puts the exception at the end as often as not.
        """
        texts = [_COUNTER_EXPORT] * 80
        texts[odd_at] = _SUMMARY_TABLE
        files = [_write(tmp_path, f"raw data/{n:03d}.csv", t) for n, t in enumerate(texts)]

        classes = _classify_all(tmp_path, files)

        assert classes[files[odd_at].path] == CLASS_PROCESSED_DATA
        assert len(opened) == 80, "a folder that disagrees with itself was still sampled"

    def test_a_folder_of_protocols_is_opened_in_full(self, tmp_path, opened):
        """Only instrument output is summarised, however large the folder.

        The first build steps run on the metadata and the protocols, and a
        propagated file has no preview to give them — on svhps22 those two
        classes are 36 files of 1468, so reading every one costs nothing.
        """
        files = [_write(tmp_path, f"protocols/sop_{n}.txt", _SOP_PROSE) for n in range(12)]

        classes = _classify_all(tmp_path, files)

        assert set(classes.values()) == {CLASS_PROTOCOL}
        assert len(opened) == 12

    def test_a_folder_of_derived_workbooks_is_opened_in_full(self, tmp_path, opened):
        """Only instrument output is summarised.

        svhps26 files six per-plate workbooks in each run directory. Summarising
        those cost eight of the twenty ranked slots — a propagated file has no
        preview, and a workbook with no preview is ranked on its filename alone,
        which is the defect #587 fixed — while saving 20 reads of 91.
        """
        files = [_write(tmp_path, f"analysis/{n:03d}.csv", _SUMMARY_TABLE) for n in range(40)]

        classes = _classify_all(tmp_path, files)

        assert set(classes.values()) == {CLASS_PROCESSED_DATA}
        assert len(opened) == 40

    @pytest.mark.parametrize(
        ("size", "expect_full"), [(11, True), (12, False)], ids=["under", "at"]
    )
    def test_a_folder_too_small_to_be_worth_it_is_opened_in_full(
        self, tmp_path, opened, size, expect_full
    ):
        """Four times the sample, below which the saving is a rounding error."""
        files = [_write(tmp_path, f"raw data/{n:03d}.csv", _COUNTER_EXPORT) for n in range(size)]

        _classify_all(tmp_path, files)

        assert (len(opened) == size) is expect_full, f"{len(opened)} reads for {size} files"

    def test_two_folders_are_not_one_sample(self, tmp_path):
        raw = [_write(tmp_path, f"raw data/{n:03d}.csv", _COUNTER_EXPORT) for n in range(40)]
        derived = [_write(tmp_path, f"analysis/{n:03d}.csv", _SUMMARY_TABLE) for n in range(40)]

        classes = _classify_all(tmp_path, raw + derived)

        assert {classes[f.path] for f in raw} == {CLASS_RAW_DATA}
        assert {classes[f.path] for f in derived} == {CLASS_PROCESSED_DATA}

    def test_two_extensions_in_one_folder_are_not_one_sample(self, tmp_path):
        """An extension is what makes a folder's files the same kind of thing."""
        counters = [_write(tmp_path, f"mixed/{n:03d}.csv", _COUNTER_EXPORT) for n in range(40)]
        notes = [_write(tmp_path, f"mixed/sop_{n:03d}.txt", _SOP_PROSE) for n in range(40)]

        classes = _classify_all(tmp_path, counters + notes)

        assert {classes[f.path] for f in counters} == {CLASS_RAW_DATA}
        assert {classes[f.path] for f in notes} == {CLASS_PROTOCOL}


class TestSamplingAgreesWithOpeningEverything:
    """The classes must not move on the real deposits (#598).

    Asserted as a property rather than a frozen list: the same files are
    classified twice, once sampled and once with the sample large enough to
    cover every folder, and the two must agree. A committed baseline would
    answer a narrower question and go stale the first time the classifier
    learns something.
    """

    FIXTURES = (
        Path("tests/fixtures/svhps21_real_input"),
        Path("tests/fixtures/svhps22_real_input"),
        Path("tests/fixtures/svhps26_real_input"),
    )

    def _scan(self, root: Path) -> list[FileClassification]:
        from builder.tools.scanner import scan_files

        return scan_files(str(root.resolve()), approved_roots={str(root.resolve())})

    @pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
    def test_the_sample_reaches_the_same_verdict_as_the_full_read(self, fixture, monkeypatch):
        import builder.tools.document_discovery as discovery

        if not fixture.exists():  # pragma: no cover - fixture not checked out
            pytest.skip(f"{fixture} not available")

        sampled = _classify_all(fixture, self._scan(fixture))
        monkeypatch.setattr(discovery, "_EXEMPLARS_PER_GROUP", 10_000)
        in_full = _classify_all(fixture, self._scan(fixture))

        assert sampled == in_full
        assert sampled, f"{fixture} scanned nothing"

    def test_a_content_only_exception_in_a_summarised_folder_is_the_accepted_cost(
        self, tmp_path, monkeypatch
    ):
        """The one case sampling cannot see, pinned so it stays deliberate.

        The fixtures above are trimmed and hold no folder large enough to
        summarise, so their agreement is nearly free. This is the case they
        cannot cover, and the honest answer is that a full read wins it: a
        derived table in the interior of 80 identical printouts, whose name,
        extension and directory all say raw and only whose CONTENT does not.

        Three things keep it narrow — the sample reaches both ends and the
        middle, a folder under four times the sample is never summarised, and
        only instrument output ever is. On the three real deposits it costs
        nothing: all 1622 files keep the class a full read gives them.
        """
        import builder.tools.document_discovery as discovery

        texts = [_COUNTER_EXPORT] * 80
        texts[63] = _SUMMARY_TABLE
        files = [_write(tmp_path, f"raw data/{n:03d}.csv", t) for n, t in enumerate(texts)]

        sampled = _classify_all(tmp_path, files)
        monkeypatch.setattr(discovery, "_EXEMPLARS_PER_GROUP", 10_000)
        in_full = _classify_all(tmp_path, files)

        assert in_full[files[63].path] == CLASS_PROCESSED_DATA
        assert sampled[files[63].path] == CLASS_RAW_DATA, "the blind spot moved"
        assert {p: c for p, c in sampled.items() if p != files[63].path} == {
            p: c for p, c in in_full.items() if p != files[63].path
        }, "and it is confined to that one file"
