"""Tests for builder/tools/file_readers.py size ceilings (Issue #148).

file_readers previously capped at 1 MB and silently returned None for 1-100 MB
files, while scanner.read_file_sample / extract_pdf_text cap at 100 MB. This
mismatch meant read_file('big.csv') gave the agent a bare None for a perfectly
readable mid-size file. The readers already cap rows/lines so memory stays
bounded; the byte ceiling is unified to 100 MB.
"""

from __future__ import annotations

import json

from builder.tools import file_readers
from builder.tools.file_readers import _MAX_BYTES, _TEXT_BUDGET_BYTES, read_file


class TestSizeCeiling:
    def test_max_bytes_matches_scanner_ceiling(self):
        """file_readers and scanner MUST use the same byte ceiling (#148 unified them).

        Asserts PARITY against scanner's actual constant — not just an inline literal —
        so a future change that bumps one reader's ceiling but not the other (the #148
        divergence this guards) fails here.
        """
        from builder.tools import scanner

        assert _MAX_BYTES == scanner._MAX_FILE_BYTES == 100 * 1024 * 1024

    def test_read_file_5mb_csv_returns_content(self, tmp_path):
        csv = tmp_path / "big.csv"
        # ~5 MB CSV: a header plus enough rows to exceed the old 1 MB cap.
        with csv.open("w", encoding="utf-8") as f:
            f.write("col1,col2,col3\n")
            # each row ~ 30 bytes; ~180k rows -> ~5 MB
            for i in range(180_000):
                f.write(f"{i},value_{i},extra_{i}\n")
        assert csv.stat().st_size > 1_000_000  # would have been skipped before

        result = read_file(str(csv), max_lines=10)
        assert result is not None
        assert "col1,col2,col3" in result

    def test_read_file_over_ceiling_still_returns_none(self, tmp_path, monkeypatch):
        # Above the unified ceiling we still skip (returns None) rather than
        # streaming an unbounded file into the model.
        monkeypatch.setattr(file_readers, "_MAX_BYTES", 100, raising=True)
        csv = tmp_path / "huge.csv"
        csv.write_text("a,b\n" + "1,2\n" * 1000)
        assert read_file(str(csv), max_bytes=100) is None


class TestTextBudget:
    """Issue #240: text/JSON is returned IN FULL up to a generous byte budget,
    not truncated at a tiny line cap, so a weak model isn't tricked into a
    'let me read the rest' loop over a small file.
    """

    def test_32kb_pretty_json_returned_in_full(self, tmp_path):
        # A ~32 KB pretty-printed JSON is well under the budget and must come
        # back COMPLETE — the prior 100-line cap dropped the tail, so the LLM
        # never saw fields deep in the file and looped 'read the rest'.
        obj = {
            "study": {"title": "Silychristin exposure in MDCK cells"},
            "rows": [{"i": i, "v": f"value_{i}"} for i in range(700)],
        }
        js = tmp_path / "S-VHPS26.json"
        js.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        size = js.stat().st_size
        assert 20_000 < size < _TEXT_BUDGET_BYTES  # genuinely mid-size, under budget

        result = read_file(str(js))
        assert result is not None
        # First AND last content present -> the whole file was returned.
        assert "Silychristin exposure in MDCK cells" in result
        assert "value_699" in result
        # No truncation marker for an under-budget file.
        assert "[truncated" not in result

    def test_over_budget_text_returns_content_plus_explicit_marker(
        self, tmp_path, monkeypatch
    ):
        # When a file genuinely exceeds the byte budget, return the content
        # shown PLUS an unmistakable marker telling the model exactly how much
        # was shown and that re-reading will NOT return more (so it stops).
        monkeypatch.setattr(file_readers, "_TEXT_BUDGET_BYTES", 1024, raising=True)
        big = tmp_path / "big.txt"
        big.write_text("X" * 5000, encoding="utf-8")

        result = read_file(str(big))
        assert result is not None
        # Content up to the budget is present.
        assert result.count("X") >= 1024
        # Explicit, machine-stable truncation marker.
        assert "[truncated" in result
        assert "do not re-read" in result
        # The marker names both the shown amount and the full size.
        assert "1.0 KiB" in result
        assert "4.9 KiB" in result

    def test_under_budget_text_has_no_marker(self, tmp_path):
        small = tmp_path / "small.txt"
        small.write_text("hello\nworld\n", encoding="utf-8")
        result = read_file(str(small))
        assert result == "hello\nworld"
        assert "[truncated" not in result


class TestDirectoryHandling:
    """Issue #240: a DIRECTORY passed to read_file must return a CLEAR,
    actionable message — never a silent None that makes the LLM loop.
    """

    def test_read_file_on_directory_returns_actionable_message(self, tmp_path):
        result = read_file(str(tmp_path))
        assert result is not None
        assert isinstance(result, str)
        assert "is a directory" in result
        assert "list_scanned_files" in result

    def test_read_file_on_missing_path_still_returns_none(self, tmp_path):
        # A genuinely missing path stays None (the agent-loop maps that to its
        # 'unreadable' message); only a directory gets the new guidance.
        assert read_file(str(tmp_path / "does_not_exist.csv")) is None

    def test_directory_message_lists_concrete_child_file_paths(self, tmp_path):
        # A weak model kept re-calling read_file on a directory even after the
        # abstract "use list_scanned_files" hint. Listing the directory's actual
        # readable file children (concrete paths) gives it something to read NEXT,
        # breaking the loop. (Follow-up to #240.)
        (tmp_path / "Assay_Meta_data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (tmp_path / "results.xlsx").write_bytes(b"PK\x03\x04stub")
        (tmp_path / "nested").mkdir()  # subdir must NOT be listed as a readable file

        result = read_file(str(tmp_path))
        assert result is not None
        assert "is a directory" in result
        # Concrete, immediately-readable child file paths are surfaced.
        assert str(tmp_path / "Assay_Meta_data.csv") in result
        assert str(tmp_path / "results.xlsx") in result
        # The subdirectory is not offered as a file to read.
        assert str(tmp_path / "nested") not in result

    def test_directory_message_handles_empty_directory(self, tmp_path):
        # An empty directory has no children to offer; it must still return the
        # actionable directory message (never None / never crash).
        result = read_file(str(tmp_path))
        assert result is not None
        assert "is a directory" in result
        assert "list_scanned_files" in result
