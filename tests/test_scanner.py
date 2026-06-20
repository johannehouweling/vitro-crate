"""Tests for builder/tools/scanner.py."""

from __future__ import annotations

from pathlib import Path

from builder.tools.scanner import (
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