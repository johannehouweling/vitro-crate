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


class TestScanFiles:
    """Tests for the scan_files function."""

    def test_empty_directory_returns_empty_list(self, tmp_path):
        """scan_files on an empty directory should return an empty list."""
        result = scan_files(str(tmp_path))
        assert result == []

    def test_single_text_file_returns_one_classification(self, tmp_path):
        """scan_files on a dir with one text file returns one classification
        with the correct path, filename, size, and mime_type."""
        data_file = tmp_path / "readme.txt"
        data_file.write_text("hello world\n")

        result = scan_files(str(tmp_path))

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

        result = scan_files(str(tmp_path))

        assert len(result) == 1
        assert result[0].filename == "visible.txt"

    def test_detects_csv_by_extension_and_content(self, tmp_path):
        """scan_files detects CSV files by .csv extension and content."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2,col3\n1,2,3\n4,5,6\n")

        result = scan_files(str(tmp_path))

        assert len(result) == 1
        fc = result[0]
        assert fc.filename == "data.csv"
        assert fc.mime_type == "text/csv"

    def test_csv_file_has_first_rows_populated(self, tmp_path):
        """scan_files populates first_rows for CSV files."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2,col3\n1,2,3\n4,5,6\n")

        result = scan_files(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1,col2,col3"
        assert fc.first_rows[1] == "1,2,3"

    def test_text_file_has_no_first_rows(self, tmp_path):
        """scan_files does not populate first_rows for plain text files."""
        (tmp_path / "readme.txt").write_text("hello world\n")

        result = scan_files(str(tmp_path))

        assert result[0].first_rows is None

    def test_tsv_file_has_first_rows_populated(self, tmp_path):
        """scan_files populates first_rows for TSV files."""
        tsv_file = tmp_path / "data.tsv"
        tsv_file.write_text("col1\tcol2\n1\t2\n3\t4\n")

        result = scan_files(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1\tcol2"

    def test_nonexistent_directory_returns_empty_list(self, tmp_path):
        """scan_files on a non-existent directory should return [] gracefully."""
        nonexistent = tmp_path / "does_not_exist"
        result = scan_files(str(nonexistent))
        assert result == []

    def test_unreadable_directory_returns_empty_list(self, tmp_path):
        """scan_files on a directory without read permission should
        return [] gracefully."""
        import stat

        unreadable = tmp_path / "no_access"
        unreadable.mkdir()
        # Remove read permission for all
        unreadable.chmod(stat.S_IRUSR)

        result = scan_files(str(unreadable))
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

        result = scan_files(str(zip_path))

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

        result = scan_files(str(zip_path))

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

        result = scan_files(str(zip_path))

        assert isinstance(result, list)
        assert len(result) == 1  # only the file, not the dir entry
        assert result[0].filename == "data.csv"

    def test_corrupt_zip_returns_empty_list(self, tmp_path):
        """scan_files on a corrupt zip returns empty list."""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"this is not a zip file")

        result = scan_files(str(zip_path))

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

        result = scan_files(str(zip_path))

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
        assert "not found" in result["error"].lower() or "does not exist" in result["message"].lower()

    def test_corrupt_zip_returns_error(self, tmp_path):
        """unzip_file returns error dict for a corrupt zip."""
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip archive")

        result = unzip_file(str(zip_path))

        assert "error" in result
        assert "message" in result


class TestReadFileSample:
    """Tests for the read_file_sample function."""

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

        result = scan_files(str(tmp_path))

        assert len(result) == 250
        # Should have at least "Progress: 100/..." and "Progress: 200/..."
        progress_messages = [r for r in caplog.records if "Progress" in r.getMessage()]
        assert len(progress_messages) >= 2, f"Expected >=2 Progress messages, got {len(progress_messages)}: {[r.getMessage() for r in progress_messages]}"

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

        result = scan_files(str(tmp_path))

        assert len(result) == 3
        progress_messages = [r for r in caplog.records if "Progress" in r.getMessage()]
        assert len(progress_messages) == 0, f"Expected no Progress messages for <100 files, got: {[r.getMessage() for r in progress_messages]}"

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

        result = read_multiple_files([str(a), str(b)], lines=5)

        # Should have at least one "Read" per-file log
        read_messages = [r for r in caplog.records if "Read" in r.getMessage() and "in" in r.getMessage()]
        assert len(read_messages) >= 2, f"Expected >=2 'Read ... in' messages, got {len(read_messages)}"


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


class TestPruneHiddenDirs:
    """Tests for Issue #69: prune hidden/.git/__MACOSX subtrees during walk."""

    def test_skips_git_directory_without_descending(self, tmp_path):
        """scan_files skips .git/ subtree entirely when os.walk prunes dirnames."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "objects" / "abc123").write_text("fake pack\n")
        (tmp_path / "data.txt").write_text("real data\n")

        result = scan_files(str(tmp_path))

        filenames = [f.filename for f in result]
        assert "data.txt" in filenames
        assert all(".git" not in f.path for f in result)
        assert len(result) == 1

    def test_skips_dot_hidden_dirs_without_descending(self, tmp_path):
        """scan_files skips hidden directories (names starting with '.') entirely."""
        (tmp_path / ".hidden_dir").mkdir()
        (tmp_path / ".hidden_dir" / "secret.csv").write_text("a,b\n1,2\n")
        (tmp_path / "visible.csv").write_text("c,d\n3,4\n")

        result = scan_files(str(tmp_path))

        filenames = [f.filename for f in result]
        assert "visible.csv" in filenames
        assert all(".hidden_dir" not in f.path for f in result)
        assert len(result) == 1

    def test_skips_macosx_dir_in_direct_scan(self, tmp_path):
        """scan_files skips __MACOSX directories during os.walk."""
        (tmp_path / "__MACOSX").mkdir()
        (tmp_path / "__MACOSX" / "._junk.txt").write_text("junk\n")
        (tmp_path / "real.csv").write_text("x,y\n1,2\n")

        result = scan_files(str(tmp_path))

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

        result = scan_files(str(tmp_path))

        assert all(".cache" not in f.path for f in result)
        assert len(result) == 1
        assert result[0].filename == "good.txt"

    def test_skips_hidden_files_in_walk(self, tmp_path):
        """Files with '.' prefix are still skipped (regression check)."""
        (tmp_path / "visible.txt").write_text("hello\n")
        (tmp_path / ".hidden.txt").write_text("shh\n")

        result = scan_files(str(tmp_path))

        assert len(result) == 1
        assert result[0].filename == "visible.txt"


class TestMaxFilesCap:
    """Tests for Issue #68: cap scanned file count and first_rows preview size."""

    def test_max_files_caps_result_count(self, tmp_path):
        """scan_files respects max_files cap and truncates results."""
        for i in range(150):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        result = scan_files(str(tmp_path), max_files=50)

        assert len(result) == 50

    def test_max_files_default_still_scans_all(self, tmp_path):
        """scan_files with no max_files cap returns all files."""
        for i in range(50):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        result = scan_files(str(tmp_path))

        assert len(result) == 50

    def test_max_files_logs_warning(self, tmp_path, caplog):
        """scan_files logs a warning when truncating due to max_files."""
        import logging
        caplog.set_level(logging.WARNING)

        for i in range(150):
            (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}\n")

        scan_files(str(tmp_path), max_files=50)

        warning_messages = [r for r in caplog.records if "max_files" in r.getMessage().lower()]
        assert len(warning_messages) == 1

    def test_first_rows_caps_line_length(self, tmp_path):
        """scan_files caps each first_rows line to max_line_length characters."""
        long_line = "x" * 500
        csv_content = f"{long_line}\ncol1,col2\n1,2\n"
        (tmp_path / "long.csv").write_text(csv_content)

        result = scan_files(str(tmp_path), max_line_length=100)

        fc = result[0]
        assert fc.first_rows is not None
        assert len(fc.first_rows[0]) == 100  # capped

    def test_first_rows_default_no_cap(self, tmp_path):
        """scan_files without max_line_length returns full lines."""
        line = "x" * 300
        (tmp_path / "data.csv").write_text(f"{line}\na,b\n")

        result = scan_files(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert len(fc.first_rows[0]) == 300  # not capped

    def test_first_rows_short_lines_unaffected(self, tmp_path):
        """scan_files with max_line_length does not affect shorter lines."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n3,4\n")

        result = scan_files(str(tmp_path), max_line_length=100)

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
            result = scan_files(str(tmp_path), max_files=100)

        assert len(result) == 1
        assert result[0].mime_type == "text/csv"
        # _detect_mime_type should be called exactly once (during scan_files),
        # not twice (scan_files + read_file_sample)
        assert mock_mime.call_count == 1

    def test_reduce_stats_maintains_first_rows(self, tmp_path):
        """After reducing stat calls, first_rows for CSV files is still populated."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2\n1,2\n3,4\n")

        result = scan_files(str(tmp_path))

        fc = result[0]
        assert fc.first_rows is not None
        assert fc.first_rows[0] == "col1,col2"

    def test_read_file_sample_accepts_precomputed_info(self, tmp_path):
        """read_file_sample accepts precomputed size and already_text flag to skip syscalls."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b\n1,2\n")
        stat_result = csv_file.stat()

        sample = read_file_sample(
            str(csv_file),
            lines=5,
            precomputed_size=stat_result.st_size,
            already_text=True,
        )

        assert sample is not None
        assert "a,b" in sample