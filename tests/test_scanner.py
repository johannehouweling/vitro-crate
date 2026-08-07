"""Tests for builder/tools/scanner.py."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from builder.tools.scanner import (
    preview_archive,
    read_file_sample,
    read_multiple_files,
    scan_files,
    unzip_file,
)


def _scan(path, **kwargs):
    """Test helper: scan *path* with it pre-approved.

    The scanner now fails CLOSED (#197): a scan is refused unless the target is
    inside an approved root. These unit tests exercise pure scanning/classification
    behaviour, so we approve the target itself. For an archive we also approve its
    extraction dir (``<stem>_extracted``) so the post-extract recursion is allowed.
    Tests that specifically exercise the guard pass ``approved_roots`` explicitly.
    """
    if "approved_roots" in kwargs:
        return scan_files(path, **kwargs)
    p = Path(path).resolve()
    approved = {str(p)}
    # Archives extract next to themselves; approve that dir too.
    approved.add(str(p.parent / f"{p.stem}_extracted"))
    return scan_files(path, approved_roots=approved, **kwargs)


class TestScanFiles:
    """Tests for the scan_files function."""

    def test_empty_directory_returns_empty_list(self, tmp_path):
        """scan_files on an empty directory should return an empty list."""
        result = _scan(str(tmp_path))
        assert result == []

    def test_single_text_file_returns_one_classification(self, tmp_path):
        """scan_files on a dir with one text file returns one classification
        with the correct path, filename, size, and mime_type."""
        data_file = tmp_path / "readme.txt"
        data_file.write_text("hello world\n")

        result = _scan(str(tmp_path))

        assert len(result) == 1
        fc = result[0]
        assert fc.filename == "readme.txt"
        assert fc.path == str(data_file)
        assert fc.size == 12  # "hello world\n" is 12 bytes
        assert fc.mime_type == "text/plain"

    def test_skips_hidden_files(self, tmp_path):
        """scan_files should skip files whose names start with '.'."""
        # Create a visible file
        (tmp_path / "visible.txt").write_text("I am visible\n")
        # Create a hidden file
        (tmp_path / ".hidden").write_text("shh\n")

        result = _scan(str(tmp_path))

        assert len(result) == 1
        assert result[0].filename == "visible.txt"

    def test_detects_csv_by_extension_and_content(self, tmp_path):
        """scan_files detects CSV files by .csv extension and content."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2,col3\n1,2,3\n4,5,6\n")

        result = _scan(str(tmp_path))

        assert len(result) == 1
        fc = result[0]
        assert fc.filename == "data.csv"
        assert fc.mime_type == "text/csv"

    def test_csv_file_has_first_rows_populated(self, tmp_path):
        """scan_files populates first_rows for CSV files."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2,col3\n1,2,3\n4,5,6\n")

        result = _scan(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1,col2,col3"
        assert fc.first_rows[1] == "1,2,3"

    def test_text_file_has_no_first_rows(self, tmp_path):
        """scan_files does not populate first_rows for plain text files."""
        (tmp_path / "readme.txt").write_text("hello world\n")

        result = _scan(str(tmp_path))

        assert result[0].first_rows is None

    def test_tsv_file_has_first_rows_populated(self, tmp_path):
        """scan_files populates first_rows for TSV files."""
        tsv_file = tmp_path / "data.tsv"
        tsv_file.write_text("col1\tcol2\n1\t2\n3\t4\n")

        result = _scan(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1\tcol2"

    def test_xlsx_file_has_first_rows_populated(self, tmp_path):
        """scan_files populates first_rows for .xlsx files (Issue #179, Commit 2).

        Pre-fix this FAILS: ``.xlsx`` is a zip/OLE2 container, so the scanner's
        ``read_file_sample(already_text=False)`` path hits the NUL-byte binary
        guard and returns ``None``, leaving ``first_rows`` unset — which forces
        every .xlsx down the budget-consuming body-read path in ``_gather_context``.
        The fix routes genuinely tabular office formats through the proper Excel
        reader so the cheap preview is captured.
        """
        import openpyxl

        xlsx_file = tmp_path / "assay.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["compound", "dose", "viability"])
        ws.append(["Methimazole", "10uM", "0.42"])
        wb.save(xlsx_file)

        result = _scan(str(tmp_path))

        fc = next(f for f in result if f.filename == "assay.xlsx")
        assert fc.first_rows is not None, ".xlsx first_rows must be populated"
        # The Excel reader's pipe-delimited content carries the header cells.
        joined = "\n".join(fc.first_rows)
        assert "compound" in joined
        assert "viability" in joined

    def test_nonexistent_directory_returns_empty_list(self, tmp_path):
        """scan_files on a non-existent directory should return [] gracefully."""
        nonexistent = tmp_path / "does_not_exist"
        result = _scan(str(nonexistent))
        assert result == []

    def test_unreadable_directory_returns_empty_list(self, tmp_path):
        """scan_files on a directory without read permission should
        return [] gracefully."""
        import stat

        unreadable = tmp_path / "no_access"
        unreadable.mkdir()
        # Remove read permission for all
        unreadable.chmod(stat.S_IRUSR)

        result = _scan(str(unreadable))
        assert result == []

        # Restore so cleanup works
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


class TestScanFilesArchive:
    """Tests for scanning zip archives (auto-extract behaviour)."""

    def test_zip_file_auto_extracts_to_file_list(self, tmp_path):
        """scan_files on a .zip file auto-extracts and returns FileClassification list."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file1.txt", "hello\n")
            zf.writestr("file2.txt", "world\n")

        result = _scan(str(zip_path))

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].filename in ("file1.txt", "file2.txt")
        assert result[0].size > 0

    def test_zip_archive_single_file(self, tmp_path):
        """scan_files on a zip with one file returns one file record."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "content\n")

        result = _scan(str(zip_path))

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].filename == "a.txt"

    def test_zip_archive_preserves_nested_structure(self, tmp_path):
        """scan_files on a nested zip returns all extracted files."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/", b"")
            zf.writestr("subdir/data.csv", "a,b\n1,2\n")

        result = _scan(str(zip_path))

        assert isinstance(result, list)
        assert len(result) == 1  # only the file, not the dir entry
        assert result[0].filename == "data.csv"

    def test_corrupt_zip_returns_empty_list(self, tmp_path):
        """scan_files on a corrupt zip returns empty list."""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"this is not a zip file")

        result = _scan(str(zip_path))

        assert isinstance(result, list)
        assert result == []

    def test_skips_macos_metadata_dirs(self, tmp_path):
        """scan_files skips __MACOSX resource fork directories from Mac zips."""
        import zipfile

        zip_path = tmp_path / "mac_export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("__MACOSX/._data.csv", b"\x00\x01")
            zf.writestr("__MACOSX/subdir/._notes.txt", b"\x00\x02")
            zf.writestr("data.csv", "a,b\n1,2\n")

        result = _scan(str(zip_path))

        assert isinstance(result, list)
        filenames = [f.filename for f in result]
        assert "data.csv" in filenames
        assert all("__MACOSX" not in f.path for f in result)
        assert len(result) == 1  # only data.csv, not the macOS junk


class TestPreviewArchive:
    """Tests for the preview_archive function."""

    def test_valid_zip_returns_archive_preview(self, tmp_path):
        """preview_archive on a valid zip returns ArchivePreview with entries."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file1.txt", "hello\n")
            zf.writestr("file2.txt", "world\n")

        result = preview_archive(str(zip_path))

        assert result.error is None
        assert result.filename == "data.zip"
        assert result.size_bytes > 0
        assert result.size_mb >= 0.0
        assert result.entry_count == 2
        assert len(result.entries) == 2
        assert result.entries[0]["path"] in ("file1.txt", "file2.txt")
        assert "data.zip" in result.message

    def test_nonexistent_file_returns_error(self, tmp_path):
        """preview_archive on a non-existent file returns error ArchivePreview."""
        result = preview_archive(str(tmp_path / "nonexistent.zip"))

        assert result.error is not None
        assert "not found" in result.error.lower()
        assert result.entry_count == 0
        assert result.entries == []
        assert result.size_bytes == 0

    def test_corrupt_zip_returns_error(self, tmp_path):
        """preview_archive on a corrupt zip returns error ArchivePreview."""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"this is not a zip file")

        result = preview_archive(str(zip_path))

        assert result.error is not None
        assert result.entry_count == 0
        assert result.entries == []
        assert result.size_bytes > 0

    def test_nested_zip_shows_all_entries(self, tmp_path):
        """preview_archive shows nested directory entries."""
        import zipfile

        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("dir/", "")
            zf.writestr("dir/file.csv", "a,b\n1,2\n")
            zf.writestr("readme.txt", "hello\n")

        result = preview_archive(str(zip_path))

        assert result.error is None
        assert result.entry_count == 3
        paths = [e["path"] for e in result.entries]
        assert "dir/" in paths
        assert "dir/file.csv" in paths
        assert "readme.txt" in paths

    def test_to_dict_roundtrip(self, tmp_path):
        """preview_archive result can round-trip through to_dict/from_dict."""
        import zipfile

        from builder.state import ArchivePreview

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file.txt", "content\n")

        result = preview_archive(str(zip_path))
        data = result.to_dict()
        restored = ArchivePreview.from_dict(data)

        assert restored.path == result.path
        assert restored.filename == result.filename
        assert restored.entry_count == result.entry_count
        assert restored.entries == result.entries
        assert restored.error == result.error


class TestUnzipFile:
    """Tests for the unzip_file function."""

    def test_extracts_zip_to_temp_directory(self, tmp_path):
        """unzip_file extracts a zip to a temp dir and returns extracted_to."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("hello.txt", "hello world\n")
            zf.writestr("sub/nested.txt", "nested\n")

        result = unzip_file(str(zip_path))

        assert "extracted_to" in result
        assert "entry_count" in result
        assert "message" in result
        assert result["entry_count"] == 2

        extracted_dir = Path(result["extracted_to"])
        assert (extracted_dir / "hello.txt").exists()
        assert (extracted_dir / "sub/nested.txt").exists()
        assert (extracted_dir / "hello.txt").read_text() == "hello world\n"

    def test_extracts_to_specified_output_dir(self, tmp_path):
        """unzip_file extracts to output_dir when provided."""
        import zipfile

        zip_path = tmp_path / "data.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file.txt", "content\n")

        output_dir = tmp_path / "extracted"
        result = unzip_file(str(zip_path), output_dir=str(output_dir))

        assert result["extracted_to"] == str(output_dir)
        assert result["entry_count"] == 1
        assert (output_dir / "file.txt").exists()

    def test_nonexistent_zip_returns_error(self):
        """unzip_file returns error dict for a non-existent file."""
        result = unzip_file("/tmp/nonexistent_zip_xyz123/test.zip")

        assert "error" in result
        assert (
            "not found" in result["error"].lower() or "does not exist" in result["message"].lower()
        )

    def test_corrupt_zip_returns_error(self, tmp_path):
        """unzip_file returns error dict for a corrupt zip."""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip archive")

        result = unzip_file(str(zip_path))

        assert "error" in result
        assert "message" in result


class TestReadFileSampleDirectory:
    """Issue #240: read_file_sample on a DIRECTORY must return a clear,
    actionable message instead of a silent None that loops the agent.
    """

    def test_directory_content_mode_returns_actionable_message(self, tmp_path):
        result = read_file_sample(str(tmp_path), mode="content")
        assert result is not None
        assert "is a directory" in result
        assert "list_scanned_files" in result

    def test_directory_overview_mode_returns_actionable_message(self, tmp_path):
        # 'overview' was the exact mode the looping run kept calling on a dir.
        result = read_file_sample(str(tmp_path), mode="overview")
        assert result is not None
        assert "is a directory" in result
        assert "list_scanned_files" in result

    def test_missing_path_still_returns_none(self, tmp_path):
        assert read_file_sample(str(tmp_path / "nope.json")) is None


class TestReadFileSampleLinesHonored:
    """Issue #240: the `lines` argument must actually control how much content
    'content' mode returns (for JSON and other text alike).
    """

    def test_lines_controls_amount_returned_for_json(self, tmp_path):
        import json as _json

        obj = {"rows": [{"i": i} for i in range(500)]}
        f = tmp_path / "big.json"
        f.write_text(_json.dumps(obj, indent=2), encoding="utf-8")

        few = read_file_sample(str(f), lines=10, mode="content")
        many = read_file_sample(str(f), lines=1000, mode="content")
        assert few is not None and many is not None
        assert len(many) > len(few)
        # lines=10 returns exactly 10 lines.
        assert few.count("\n") <= 9


class TestReadFileSampleMode:
    """Tests for the mode parameter on read_file_sample."""

    def test_summary_csv(self, tmp_path):
        """mode='summary' on CSV returns column names, row count, and sample."""
        f = tmp_path / "data.csv"
        f.write_text("col1,col2,col3\n1,2,3\n4,5,6\n7,8,9\n")
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "3 columns" in result.lower() or "col1" in result
        assert "3 data rows" in result.lower() or "4" in result
        assert "1, 2, 3" in result or "1,2,3" in result

    def test_summary_tsv(self, tmp_path):
        """mode='summary' on TSV returns column names and row count."""
        f = tmp_path / "data.tsv"
        f.write_text("col1\tcol2\n1\t2\n3\t4\n")
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "col1" in result
        assert "2 data rows" in result.lower() or "3" in result

    def test_summary_json(self, tmp_path):
        """mode='summary' on JSON returns top-level keys and array lengths."""
        f = tmp_path / "data.json"
        f.write_text('{"name": "test", "values": [1, 2, 3], "meta": {"a": 1}}')
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "name" in result
        assert "values" in result
        assert "3" in result

    def test_summary_json_array(self, tmp_path):
        """mode='summary' on a JSON array reports the count."""
        f = tmp_path / "list.json"
        f.write_text('[{"id": 1}, {"id": 2}, {"id": 3}]')
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "3 items" in result.lower() or "3 elements" in result.lower()

    def test_summary_xlsx(self, tmp_path):
        """mode='summary' on a real XLSX returns the Excel format and sheet info.

        Uses a valid workbook written by openpyxl rather than a hand-crafted zip:
        the old fake passed only because the file was misrouted to the CSV
        summarizer, which read the uncompressed zip bytes as mojibake text (#101).
        """
        import pytest
        openpyxl = pytest.importorskip("openpyxl")
        f = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["colA", "colB"])
        ws.append([1, 2])
        wb.save(f)
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "Excel" in result
        assert "Sheet1" in result

    def test_summary_pdf(self, tmp_path):
        """mode='summary' on PDF returns format and page count info."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R>>endobj\n"
            b"4 0 obj<</Type/Page/Parent 2 0 R>>endobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000058 00000 n \n0000000115 00000 n \n0000000162 00000 n \n"
            b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n211\n%%EOF"
        )
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        # No disjunction (the old `"Format: PDF" in result or "PDF" in result`
        # collapsed to just "PDF" appearing anywhere). Assert the transform's real
        # output: the format label AND the page-count line — `_summarize_pdf` emits
        # both via pdfplumber or the /Type/Page fallback, and the fixture declares
        # two pages.
        assert "Format: PDF" in result
        assert "Pages" in result

    def test_summary_docx(self, tmp_path):
        """mode='summary' on a real .docx reports Word format + its real content.

        Built with python-docx (as ``test_summary_xlsx`` uses openpyxl) so the
        assertions are UNCONDITIONAL. The old version hand-crafted a minimal zip and
        hid its assert behind ``if result is not None`` — it passed vacuously whenever
        ``_summarize_docx`` returned None (the zip failing to parse), i.e. it never
        actually verified the docx summary path.
        """
        import pytest

        docx = pytest.importorskip("docx")  # python-docx
        f = tmp_path / "report.docx"
        document = docx.Document()
        document.add_paragraph("Hello")
        document.save(str(f))

        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "Format: Word" in result
        assert "Paragraphs: 1" in result  # the one paragraph was counted
        assert "Hello" in result  # its body text reached the summary

    def test_summary_plain_text(self, tmp_path):
        """mode='summary' on plain text returns line count and sample."""
        f = tmp_path / "readme.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = read_file_sample(str(f), mode="summary")
        assert result is not None
        assert "lines: 5" in result.lower() or "5 lines" in result.lower()
        assert "line1" in result
        assert "line5" in result

    def test_summary_binary_returns_none(self, tmp_path):
        """mode='summary' on binary file returns None."""
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        result = read_file_sample(str(f), mode="summary")
        assert result is None

    def test_overview_includes_metadata(self, tmp_path):
        """mode='overview' includes file metadata and the summary."""
        f = tmp_path / "data.csv"
        f.write_text("a,b\n1,2\n3,4\n")
        result = read_file_sample(str(f), mode="overview")
        assert result is not None
        assert "data.csv" in result
        assert "text/csv" in result.lower() or "csv" in result.lower()
        assert "2 columns" in result.lower() or "a, b" in result

    def test_overview_on_nonexistent_file(self, tmp_path):
        """mode='overview' on nonexistent file returns None."""
        result = read_file_sample(str(tmp_path / "nope.txt"), mode="overview")
        assert result is None

    def test_content_mode_returns_first_lines(self, tmp_path):
        """mode='content' returns first N lines (same as default)."""
        f = tmp_path / "sample.txt"
        f.write_text("line1\nline2\nline3\n")
        result = read_file_sample(str(f), mode="content")
        assert result == "line1\nline2\nline3"

    def test_default_mode_is_content(self, tmp_path):
        """Default mode (no mode kwarg) behaves like mode='content'."""
        f = tmp_path / "sample.txt"
        f.write_text("hello\nworld\n")
        result = read_file_sample(str(f))
        assert result == "hello\nworld"


class TestReadFileSample:
    """Tests for the read_file_sample function (legacy)."""

    def test_returns_first_lines_of_text_file(self, tmp_path):
        """read_file_sample returns the first N lines of a text file."""
        data_file = tmp_path / "sample.txt"
        data_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        result = read_file_sample(str(data_file), lines=3)

        assert result == "line1\nline2\nline3"

    def test_nonexistent_file_returns_none(self, tmp_path):
        """read_file_sample returns None for a non-existent file."""
        nonexistent = tmp_path / "does_not_exist.txt"
        result = read_file_sample(str(nonexistent))
        assert result is None

    def test_lines_parameter_limits_output(self, tmp_path):
        """read_file_sample with lines=5 returns only 5 lines even
        if the file has more."""
        data_file = tmp_path / "long_file.txt"
        data_file.write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")

        result = read_file_sample(str(data_file), lines=5)

        assert result == "line1\nline2\nline3\nline4\nline5"


class TestReadMultipleFiles:
    """Tests for the read_multiple_files function."""

    def test_returns_contents_of_multiple_files(self, tmp_path):
        """read_multiple_files returns contents of each file keyed by path."""
        a = tmp_path / "a.txt"
        a.write_text("hello\n")
        b = tmp_path / "b.txt"
        b.write_text("world\n")

        result = read_multiple_files([str(a), str(b)], lines=5)

        assert result["count"] == 2
        assert result["skipped"] == []
        assert str(a) in result["files"]
        assert str(b) in result["files"]
        assert result["files"][str(a)] == "hello"
        assert result["files"][str(b)] == "world"


class TestScannerTiming:
    """Tests for timing/progress instrumentation in scanner functions."""

    def test_scan_files_logs_progress_and_elapsed(self, tmp_path, caplog):
        """scan_files emits progress every 100 files and total elapsed at end."""
        import logging

        caplog.set_level(logging.DEBUG)

        # Create 250 files so we cross the 100-file progress boundary twice
        for i in range(250):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        result = _scan(str(tmp_path))

        assert len(result) == 250
        # Should have at least "Progress: 100/..." and "Progress: 200/..."
        progress_messages = [r for r in caplog.records if "Progress" in r.getMessage()]
        msgs = [r.getMessage() for r in progress_messages]
        assert len(progress_messages) >= 2, (
            f"Expected >=2 Progress messages, got {len(progress_messages)}: {msgs}"
        )

        # Should have a final "Scan complete" summary
        complete_messages = [r for r in caplog.records if "Scan complete" in r.getMessage()]
        assert len(complete_messages) == 1
        assert "in" in complete_messages[0].getMessage()

    def test_scan_files_small_directory_no_progress(self, tmp_path, caplog):
        """scan_files with fewer than 100 files does NOT emit progress."""
        import logging

        caplog.set_level(logging.DEBUG)

        for i in range(3):
            (tmp_path / f"file_{i}.txt").write_text(f"content {i}\n")

        result = _scan(str(tmp_path))

        assert len(result) == 3
        progress_messages = [r for r in caplog.records if "Progress" in r.getMessage()]
        assert len(progress_messages) == 0, (
            f"Expected no Progress messages for <100 files, got: {progress_messages}"
        )

        # But should still have the final summary
        complete_messages = [r for r in caplog.records if "Scan complete" in r.getMessage()]
        assert len(complete_messages) == 1

    def test_read_multiple_files_logs_per_file_timing(self, tmp_path, caplog):
        """read_multiple_files logs per-file elapsed time at DEBUG."""
        import logging

        caplog.set_level(logging.DEBUG)

        a = tmp_path / "a.txt"
        a.write_text("hello\n")
        b = tmp_path / "b.txt"
        b.write_text("world\n")

        read_multiple_files([str(a), str(b)], lines=5)

        # Should have at least one "Read" per-file log
        read_messages = [
            r for r in caplog.records if "Read" in r.getMessage() and "in" in r.getMessage()
        ]
        assert len(read_messages) >= 2, (
            f"Expected >=2 'Read ... in' messages, got {len(read_messages)}"
        )


class TestApprovedRoots:
    """Tests for the approved-scan-roots guard in scan_files."""

    def test_rejects_path_outside_approved_root(self, tmp_path):
        """scan_files returns empty list when path is not under an approved root."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "bad.txt").write_text("nope\n")

        # Only data_dir is approved
        result = scan_files(str(outside), approved_roots={str(data_dir.resolve())})

        assert result == []

    def test_accepts_path_under_approved_root(self, tmp_path):
        """scan_files scans subdirectories of an approved root."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        sub = data_dir / "sub"
        sub.mkdir()
        (sub / "file.csv").write_text("a,b,c\n1,2,3\n")

        result = scan_files(str(sub), approved_roots={str(data_dir.resolve())})

        assert len(result) == 1
        assert result[0].filename == "file.csv"


class TestApprovedRootsFailClosed:
    """Fail-closed contract for the approved-scan-roots guard (#197).

    With NO approved roots (None or empty) the scanner must REFUSE and never
    walk the target. Forbidden system/home roots must be denied even when
    explicitly present in *approved_roots*.
    """

    def test_none_approved_roots_refuses_and_does_not_walk(self, tmp_path, caplog):
        """None approved_roots ⇒ refuse, return [], and never descend."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.txt").write_text("nested")

        with mock.patch("builder.tools.scanner._safe_walk") as walk:
            result = scan_files(str(tmp_path), approved_roots=None)

        assert result == []
        walk.assert_not_called()  # never traversed
        assert any("not in approved roots" in r.getMessage() for r in caplog.records)

    def test_empty_approved_roots_refuses_and_does_not_walk(self, tmp_path):
        """An empty set ⇒ nothing approved ⇒ refuse without walking."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")

        with mock.patch("builder.tools.scanner._safe_walk") as walk:
            result = scan_files(str(tmp_path), approved_roots=set())

        assert result == []
        walk.assert_not_called()

    def test_filesystem_root_refused_regardless_of_approved_roots(self):
        """scan_files('/') is refused even with an approved root present."""
        with mock.patch("builder.tools.scanner._safe_walk") as walk:
            assert scan_files("/", approved_roots={"/"}) == []
            assert scan_files("/", approved_roots={"/some/dir"}) == []
        walk.assert_not_called()

    def test_denylisted_roots_refused_even_when_explicitly_approved(self, tmp_path):
        """Forbidden roots cannot be scanned even if passed in approved_roots."""
        home = str(Path.home())
        for forbidden in ("/", home, "/System", "/usr", "/etc", "/Users"):
            with mock.patch("builder.tools.scanner._safe_walk") as walk:
                result = scan_files(forbidden, approved_roots={forbidden})
            assert result == [], f"{forbidden} should be refused"
            walk.assert_not_called()

    def test_normal_subdir_under_temp_root_is_allowed(self, tmp_path):
        """A legitimate subdir under an approved (non-forbidden) root scans."""
        root = tmp_path / "project"
        root.mkdir()
        (root / "data.csv").write_text("a,b\n1,2\n")

        result = scan_files(str(root), approved_roots={str(root.resolve())})

        assert len(result) == 1
        assert result[0].filename == "data.csv"


class TestPruneHiddenDirs:
    """Tests for Issue #69: prune hidden/.git/__MACOSX subtrees during walk."""

    def test_skips_git_directory_without_descending(self, tmp_path):
        """scan_files skips .git/ subtree entirely when os.walk prunes dirnames."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "objects" / "abc123").write_text("fake pack\n")
        (tmp_path / "data.txt").write_text("real data\n")

        result = _scan(str(tmp_path))

        filenames = [f.filename for f in result]
        assert "data.txt" in filenames
        assert all(".git" not in f.path for f in result)
        assert len(result) == 1

    def test_skips_dot_hidden_dirs_without_descending(self, tmp_path):
        """scan_files skips hidden directories (names starting with '.') entirely."""
        (tmp_path / ".hidden_dir").mkdir()
        (tmp_path / ".hidden_dir" / "secret.csv").write_text("a,b\n1,2\n")
        (tmp_path / "visible.csv").write_text("c,d\n3,4\n")

        result = _scan(str(tmp_path))

        filenames = [f.filename for f in result]
        assert "visible.csv" in filenames
        assert all(".hidden_dir" not in f.path for f in result)
        assert len(result) == 1

    def test_skips_macosx_dir_in_direct_scan(self, tmp_path):
        """scan_files skips __MACOSX directories during os.walk."""
        (tmp_path / "__MACOSX").mkdir()
        (tmp_path / "__MACOSX" / "._junk.txt").write_text("junk\n")
        (tmp_path / "real.csv").write_text("x,y\n1,2\n")

        result = _scan(str(tmp_path))

        filenames = [f.filename for f in result]
        assert "real.csv" in filenames
        assert all("__MACOSX" not in f.path for f in result)
        assert len(result) == 1

    def test_hidden_subdir_in_nested_structure(self, tmp_path):
        """Hidden subdirectories at any depth are pruned without descent."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / ".cache").mkdir()
        (sub / ".cache" / "deep.txt").write_text("hidden\n")
        (sub / "good.txt").write_text("visible\n")

        result = _scan(str(tmp_path))

        assert all(".cache" not in f.path for f in result)
        assert len(result) == 1
        assert result[0].filename == "good.txt"

    def test_skips_hidden_files_in_walk(self, tmp_path):
        """Files with '.' prefix are still skipped (regression check)."""
        (tmp_path / "visible.txt").write_text("hello\n")
        (tmp_path / ".hidden.txt").write_text("shh\n")

        result = _scan(str(tmp_path))

        assert len(result) == 1
        assert result[0].filename == "visible.txt"


class TestMaxFilesCap:
    """Tests for Issue #68: cap scanned file count and first_rows preview size."""

    def test_max_files_caps_result_count(self, tmp_path):
        """scan_files respects max_files cap and truncates results."""
        for i in range(150):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        result = _scan(str(tmp_path), max_files=50)

        assert len(result) == 50

    def test_max_files_default_still_scans_all(self, tmp_path):
        """scan_files with no max_files cap returns all files."""
        for i in range(50):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        result = _scan(str(tmp_path))

        assert len(result) == 50

    def test_max_files_logs_warning(self, tmp_path, caplog):
        """scan_files logs a warning when truncating due to max_files."""
        import logging

        caplog.set_level(logging.WARNING)

        for i in range(150):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        _scan(str(tmp_path), max_files=50)

        warning_messages = [r for r in caplog.records if "max_files" in r.getMessage().lower()]
        assert len(warning_messages) == 1

    def test_first_rows_caps_line_length(self, tmp_path):
        """scan_files caps each first_rows line to max_line_length characters."""
        long_line = "x" * 500
        csv_content = f"{long_line}\ncol1,col2\n1,2\n"
        (tmp_path / "long.csv").write_text(csv_content)

        result = _scan(str(tmp_path), max_line_length=100)

        fc = result[0]
        assert fc.first_rows is not None
        assert len(fc.first_rows[0]) == 100  # capped

    def test_first_rows_default_no_cap(self, tmp_path):
        """scan_files without max_line_length returns full lines."""
        line = "x" * 300
        (tmp_path / "data.csv").write_text(f"{line}\na,b\n")

        result = _scan(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert len(fc.first_rows[0]) == 300  # not capped

    def test_first_rows_short_lines_unaffected(self, tmp_path):
        """scan_files with max_line_length does not affect shorter lines."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n3,4\n")

        result = _scan(str(tmp_path), max_line_length=100)

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "a,b"


class TestReducedStats:
    """Tests for Issue #67: reduce redundant stat() and MIME calls during scanning."""

    def test_mime_detected_once_per_file(self, tmp_path):
        """scan_files does not call _detect_mime_type a second time for tabular sampling."""
        from builder.tools.scanner import _detect_mime_type

        (tmp_path / "data.csv").write_text("a,b\n1,2\n")

        with mock.patch(
            "builder.tools.scanner._detect_mime_type",
            wraps=_detect_mime_type,
        ) as mock_mime:
            result = _scan(str(tmp_path), max_files=100)

        assert len(result) == 1
        assert result[0].mime_type == "text/csv"
        # _detect_mime_type should be called exactly once (during scan_files),
        # not twice (scan_files + read_file_sample)
        assert mock_mime.call_count == 1

    def test_reduce_stats_maintains_first_rows(self, tmp_path):
        """After reducing stat calls, first_rows for CSV files is still populated."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2\n1,2\n3,4\n")

        result = _scan(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1,col2"

    def test_precomputed_size_is_trusted_for_the_100mb_skip(self, tmp_path):
        """A caller-supplied ``precomputed_size`` DRIVES the >100MB skip — the file is
        not re-stat'd. Passing a huge size for a tiny file must make the sampler bail.

        (The old test passed ``precomputed_size=actual_size`` + ``already_text=True``,
        i.e. values that changed nothing observable — it could not detect the kwargs
        being ignored. These two tests exercise each kwarg with a DIVERGENT value.)
        """
        csv_file = tmp_path / "tiny.csv"
        csv_file.write_text("a,b\n1,2\n")  # a handful of bytes on disk

        # Baseline: with no override the tiny file samples fine.
        assert read_file_sample(str(csv_file), lines=5) is not None
        # A 200MB precomputed_size trips the >100MB guard — proving the arg is trusted
        # over stat().
        skipped = read_file_sample(
            str(csv_file), lines=5, precomputed_size=200 * 1024 * 1024
        )
        assert skipped is None

    def test_already_text_bypasses_the_binary_nul_guard(self, tmp_path):
        """``already_text=True`` trusts the caller that the bytes are text, skipping the
        NUL-byte binary guard that would otherwise reject the file."""
        f = tmp_path / "data.csv"
        f.write_bytes(b"a,b\n1,\x00\n")  # a NUL byte in the first chunk

        # Default: the content-based binary guard rejects the NUL-containing file.
        assert read_file_sample(str(f), lines=5) is None
        # already_text=True: the guard is skipped and the content is returned.
        got = read_file_sample(str(f), lines=5, already_text=True)
        assert got is not None
        assert "a,b" in got


class TestScientificMimeRegistry:
    """Issue #148: scientific-format extensions resolve to real IANA media types.

    mimetypes.guess_type returns None for .mzML/.raw/.wiff/.fcs/etc. and the
    text-sniff fallback then mislabels their binary bytes as text/plain. A small
    extension->media-type registry, consulted before the text sniff, fixes that
    and keeps application/octet-stream as the true default for unknown binaries.
    """

    def test_known_scientific_extensions_map_to_real_media_types(self, tmp_path):
        from builder.tools.scanner import _detect_mime_type

        # extension -> expected media type (not text/plain via the sniff path)
        cases = {
            "x.mzML": "application/x-mzml",
            "x.raw": "application/octet-stream",  # generic vendor raw -> binary
            "x.wiff": "application/octet-stream",
            "x.fcs": "application/vnd.isac.fcs",
            "x.czi": "application/octet-stream",
            "x.nd2": "application/octet-stream",
            "x.lif": "application/octet-stream",
        }
        for name, expected in cases.items():
            p = tmp_path / name
            # Write bytes that would *decode* as UTF-8 text so the old text-sniff
            # fallback would mislabel them as text/plain.
            p.write_bytes(b"some ascii looking header\n")
            assert _detect_mime_type(p) == expected, name

    def test_mzml_not_text_plain(self, tmp_path):
        from builder.tools.scanner import _detect_mime_type

        p = tmp_path / "run.mzML"
        p.write_bytes(b"<?xml version='1.0'?>\n<mzML></mzML>\n")
        mime = _detect_mime_type(p)
        assert mime != "text/plain"
        assert mime != "application/octet-stream"

    def test_unknown_binary_extension_defaults_to_octet_stream(self, tmp_path):
        from builder.tools.scanner import _detect_mime_type

        p = tmp_path / "blob.zzzunknown"
        # Real binary content (NUL bytes) — must not be called text/plain.
        p.write_bytes(b"\x00\x01\x02\x03binary\x00stuff")
        assert _detect_mime_type(p) == "application/octet-stream"

    def test_plain_text_still_text_plain(self, tmp_path):
        from builder.tools.scanner import _detect_mime_type

        p = tmp_path / "notes.unknownext"
        p.write_bytes(b"just some plain readable text\n")
        assert _detect_mime_type(p) == "text/plain"


class TestSelfIngestionGuard:
    """A scan never ingests the tool's own prior output (#416).

    Pointing a run at a folder that holds previous sessions used to scan ~9.5k
    files, including every prior session's payload, and feed them to the drafter
    as billed context. That is not merely slow — it is circular: the builder
    ingests crates it produced as if they were source research data, so a second
    run over a folder is contaminated by the first.
    """

    def _crate(self, directory: Path) -> None:
        """Write a minimal but real crate marker plus some payload."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ro-crate-metadata.json").write_text('{"@graph": []}', encoding="utf-8")
        (directory / "ro-crate-preview.html").write_text("<html></html>", encoding="utf-8")
        payload = directory / "data"
        payload.mkdir(exist_ok=True)
        (payload / "copied_measurements.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    def test_nested_session_crate_is_not_scanned(self, tmp_path):
        """Nothing under sessions/<id>/working_crate/ reaches the inventory."""
        (tmp_path / "real_data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        self._crate(tmp_path / "sessions" / "20260721_115953" / "working_crate")

        names = {r.path for r in _scan(str(tmp_path))}

        assert any(n.endswith("real_data.csv") for n in names), (
            "the genuine source file must still be scanned"
        )
        assert not [n for n in names if "working_crate" in n], (
            f"prior session output was ingested: {sorted(names)}"
        )

    def test_nested_output_crate_is_not_scanned(self, tmp_path):
        """Same for an export sitting under output/<name>_crate/."""
        (tmp_path / "protocol.txt").write_text("method\n", encoding="utf-8")
        self._crate(tmp_path / "output" / "vhps_crate")

        names = {r.path for r in _scan(str(tmp_path))}

        assert any(n.endswith("protocol.txt") for n in names)
        assert not [n for n in names if "vhps_crate" in n]

    def test_scanning_a_crate_directly_still_works(self, tmp_path):
        """The walk root is exempt — pointing at a crate is an explicit request.

        Without this exemption the guard would break reading an existing crate
        back in, which is a supported entry point, not self-ingestion.
        """
        crate = tmp_path / "some_crate"
        self._crate(crate)

        names = {r.path for r in _scan(str(crate))}

        assert any(n.endswith("ro-crate-metadata.json") for n in names), (
            "scanning a crate root directly must still see the manifest"
        )
        assert any(n.endswith("copied_measurements.csv") for n in names)

    def test_a_filename_mentioning_output_is_unaffected(self, tmp_path):
        """The guard keys on the manifest, not on directory or file names.

        A name list (`sessions`, `output`, …) would drift and would also punish
        legitimate input that happens to use those words.
        """
        data = tmp_path / "output_from_instrument"
        data.mkdir()
        (data / "sessions_summary_output.csv").write_text("a\n1\n", encoding="utf-8")

        names = {r.path for r in _scan(str(tmp_path))}

        assert any(n.endswith("sessions_summary_output.csv") for n in names), (
            f"legitimate input was pruned by name: {sorted(names)}"
        )


class TestDefaultFileCap:
    """The #68 cap actually engages on the production path (#416)."""

    def test_cap_has_a_non_zero_default(self, tmp_path):
        """Every production caller passed nothing, so the valve never engaged."""
        import inspect

        from builder.tools.scanner import _DEFAULT_MAX_FILES

        default = inspect.signature(scan_files).parameters["max_files"].default
        assert default == _DEFAULT_MAX_FILES
        assert default > 0, "a 0 default is what let ~9.5k files through"

    def test_default_cap_truncates_and_says_so(self, tmp_path, caplog):
        """A truncated scan is loud — a silent one reads as a complete inventory."""
        import logging

        from builder.tools import scanner

        for i in range(12):
            (tmp_path / f"f{i}.csv").write_text("a\n1\n", encoding="utf-8")

        with mock.patch.object(scanner, "_DEFAULT_MAX_FILES", 5):
            # Re-read the module default through an explicit pass, mirroring what
            # the production caller now gets implicitly.
            with caplog.at_level(logging.WARNING):
                result = scan_files(
                    str(tmp_path), approved_roots={str(tmp_path.resolve())}, max_files=5
                )

        assert len(result) == 5
        assert any("max_files" in r.getMessage() for r in caplog.records), (
            "truncation must be logged"
        )

    def test_engine_initialize_inherits_the_cap(self):
        """The engine passes no max_files, so it must inherit the default.

        This is the gap #68 left open: the cap was plumbed and enforced, but the
        only production call site left it at 'unlimited'.
        """
        import inspect

        from builder import engine as engine_mod

        src = inspect.getsource(engine_mod)
        assert "scan_files(" in src
        assert "max_files" not in src.split("scan_files(")[1].split(")")[0], (
            "engine now passes max_files explicitly — update this test to assert "
            "the value it passes instead of the inherited default"
        )
