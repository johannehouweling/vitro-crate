"""Tests for builder/tools/scanner.py."""

from __future__ import annotations

import pytest

from builder.tools.scanner import read_file_sample, scan_files


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