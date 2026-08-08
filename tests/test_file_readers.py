"""Tests for builder/tools/file_readers.py size ceilings (Issue #148).

file_readers previously capped at 1 MB and silently returned None for 1-100 MB
files, while scanner.read_file_sample / extract_pdf_text cap at 100 MB. This
mismatch meant read_file('big.csv') gave the agent a bare None for a perfectly
readable mid-size file. The readers already cap rows/lines so memory stays
bounded; the byte ceiling is unified to 100 MB.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from builder.tools import file_readers
from builder.tools.file_readers import _MAX_BYTES, _TEXT_BUDGET_BYTES, read_file


def _folded_series_covers(cell: str, compacted: str) -> bool:
    """True when *cell* is a series label ``X_N`` folded into a present ``X_A-B`` (#419).

    Checks the RANGE, not just the prefix, and checks the PAYLOAD, not just the
    label. A fold that narrowed to ``…_1-2`` leaves levels 3-8 lost; a fold that
    kept a wide ``…_1-8`` label while emitting three values loses five. Both
    return False here — the second is the exact shape an earlier version of this
    helper waved through.

    The label is matched at a cell boundary so a *suffix* of some other row's
    prefix cannot claim the exemption: a bare ``Concentration_3`` must not be
    excused by ``Chemical_1_Concentration_2-8``.
    """
    match = re.match(r"^(?P<prefix>.+)_(?P<index>\d+)$", cell)
    if match is None:
        return False
    index = int(match["index"])
    pattern = rf"\| {re.escape(match['prefix'])}_(\d+)-(\d+) \|([^|]*)\|"
    for lo, hi, payload in re.findall(pattern, compacted):
        if not (int(lo) <= index <= int(hi)):
            continue
        # The folded row must still carry one member per index it claims.
        if len(payload.split(",")) == int(hi) - int(lo) + 1:
            return True
    return False


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


# ---------------------------------------------------------------------------
# Shared text compactors (#378)
# ---------------------------------------------------------------------------

_METADATA_XLSX = (
    "tests/fixtures/svhps26_real_input/Assay_OATP1C1/"
    "Assay-metadata-CHO-K1_OATP1C1-v1.1.xlsx"
)
_DESCRIPTOR_JSON = "tests/fixtures/svhps26_real_input/S-VHPS26.json"


class TestCompactGridText:
    """`compact_grid_text` densifies `[Sheet: …]` + pipe-row output (#378).

    The extraction leaf gets one bounded slice per file. On the real S-VHPS26
    workbook the signal (cell line, RRID, author, chemicals 2-5) sits past any
    affordable cap, so the fix is to remove boilerplate rather than to raise the
    cap further.
    """

    def test_drops_comments_column_and_empty_cells(self):
        """Compaction must shrink the workbook while keeping every signal token.

        Non-tautological: the input is the committed real deposit read through
        the real `read_excel`, and the assertion is loss-of-noise plus
        survival-of-signal — not a byte count the implementation could satisfy
        by truncating.
        """
        from builder.tools.file_readers import compact_grid_text, read_excel

        raw = read_excel(_METADATA_XLSX, max_rows=100)
        assert raw is not None
        compacted = compact_grid_text(raw)

        assert len(compacted) < len(raw)
        # The per-sheet instruction column is pure boilerplate.
        assert "Enter the name of your assay" not in compacted
        for token in ("Dr. Fabian Wagenaars", "CVCL_0214", "diclofenac", "ECACC"):
            assert token.lower() in compacted.lower(), f"compaction lost {token!r}"

    def test_never_drops_a_non_empty_cell(self):
        """HONESTY CONTROL for the trap in #378's honest notes.

        The depositor filled the FIRST sheet in column 2 (`Standard or ontology
        reference`), not column 3 (`Value`) — so a rule that drops rows whose
        Value cell is empty looks right and silently destroys the author, the
        ORCID, the DOI, the assay name and the description. Every non-empty cell
        outside the Comments column must survive.

        Survival is checked PER SHEET, not against the whole document, because
        #419's dedup is sheet-scoped: a future change that widened it to the whole
        workbook would erase every later sheet's annotations while a document-wide
        substring check stayed green.

        One shape is exempt: a **series label** ``X_N`` folded into ``X_A-B``, and
        only when the folded row still carries one value per index it claims (see
        :func:`_folded_series_covers`). A deduped IRI needs no exemption — it is
        still stated once in its own sheet, so the per-sheet check finds it.

        The exemption cannot hide the trap this control exists for: the author,
        ORCID, DOI, assay name and description are not series labels, so the
        "drop rows with an empty Value cell" rule still reddens the assertion.

        Run at BOTH read depths — the suite used to inspect 100 rows while the
        pipeline read 500, leaving the lossy rules unexercised over 5x the rows
        they actually run on in production.
        """
        from builder.tools.file_readers import _MAX_ROWS, compact_grid_text, read_excel

        for max_rows in (100, _MAX_ROWS):
            raw = read_excel(_METADATA_XLSX, max_rows=max_rows)
            assert raw is not None
            self._assert_no_cell_dropped(raw, compact_grid_text(raw), max_rows)

    @staticmethod
    def _sheet_blocks(text: str) -> dict[str, str]:
        """Split ``[Sheet: name]`` output into one block of text per sheet."""
        blocks: dict[str, list[str]] = {}
        current = ""
        for line in text.split("\n"):
            if line.strip().startswith("[Sheet:"):
                current = line.strip()
            blocks.setdefault(current, []).append(line)
        return {name: "\n".join(lines) for name, lines in blocks.items()}

    def _assert_no_cell_dropped(self, raw: str, compacted: str, max_rows: int) -> None:
        raw_sheets = self._sheet_blocks(raw)
        compact_sheets = self._sheet_blocks(compacted)

        dropped: list[str] = []
        for sheet, raw_block in raw_sheets.items():
            # A sheet compacted to nothing keeps an empty block, which is a real
            # answer (every row was label-only); missing entirely is not.
            compact_block = compact_sheets.get(sheet, "")
            guidance = self._guidance_indices(raw_block)
            for line in raw_block.split("\n"):
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
            # Scoped to rows that actually CARRY data — two or more non-empty
            # cells outside the sheet's guidance columns. A label-only row
            # (`| Accession |  |  | … |`, a field the depositor left blank) is
            # legitimately compacted away, and the repeated header row is dropped
            # by design. Neither weakens what this control exists to catch:
            # `| Corresponding person | Dr. Fabian Wagenaars |  | … |` has a
            # blank *Value* cell and two non-empty ones, so the trap rule still
            # reddens this assertion.
                if cells and cells[0] == "Parameter":
                    continue
                payload = [c for i, c in enumerate(cells) if c and i not in guidance]
                if len(payload) < 2:
                    continue
                for cell in payload:
                    if cell in compact_block:
                        continue
                    if _folded_series_covers(cell, compact_block):
                        continue
                    dropped.append(f"{sheet} {cell}")
        assert not dropped, f"compaction dropped non-empty cells at {max_rows} rows: {dropped[:5]}"

        # Independent of any column rule: these are the values a human deposited,
        # and no compaction rule may cost us one. Derived by reading the workbook,
        # not by re-running the implementation — this is what keeps the control
        # from merely agreeing with whatever the code currently does.
        for value in (
            "Dr. Fabian Wagenaars",           # corresponding person
            "F.M.A.Wagenaars@uu.nl",          # contact e-mail
            "0000-0003-4766-7358",            # ORCID
            "CVCL_0214",                      # cell-line RRID
            "OATP1C1; AOP wiki ID: 2376",     # key event, in a guidance-bearing row
            "T4 uptake",                      # endpoint
        ):
            assert value in compacted, f"compaction lost depositor data {value!r}"

        # The exemption must be EARNED, not assumed: prove the fixture actually
        # exercises folding, or a future change could start dropping cells into
        # an exemption nobody is testing.
        assert "Chemical_2_Concentration_1-8" in compacted, "series folding never fired"
        assert compacted.count("http://semanticscience.org/resource/CHEMINF_000446") == 1, (
            "the repeated CAS IRI should be stated exactly once per sheet"
        )

    @staticmethod
    def _guidance_indices(raw_block: str) -> set[int]:
        """Guidance-column indices for a sheet, read off its own header row.

        Computed here from the raw text rather than imported, so this control
        keeps its own opinion about which columns are scaffolding. A guidance
        column that is the header's last also claims the unheadered overflow
        cells past it — the S-VHPS26 sheets split one long Comments entry across
        two cells.
        """
        for line in raw_block.split("\n"):
            if not line.startswith("|"):
                continue
            header = [c.strip().casefold() for c in line.strip("|").split("|")]
            names = {"comments", "tips", "beschrijving", "description", "toelichting"}
            found = {i for i, c in enumerate(header) if c in names}
            if found and max(found) == len(header) - 1:
                found |= set(range(max(found), 64))
            return found
        return set()

    _IRI ="http://nmrML.org/nmrCV#NMR:1000095"

    def _series(self, values: list[str], iri: str | None = None) -> str:
        """A `[Sheet:]` + header + one `Chemical_1_Concentration_N` row per value."""
        rows = ["[Sheet: Chemical Information]", "| Parameter | Standard | Value |"]
        for i, value in enumerate(values, 1):
            middle = f" {iri} |" if iri else ""
            rows.append(f"| Chemical_1_Concentration_{i} |{middle} {value} |")
        return "\n".join(rows)

    def test_refuses_to_fold_values_containing_the_separator(self):
        """A comma inside a value makes the folded list unrecoverable (#419).

        `1,2-dichloroethane` and the decimal comma an EU depositor may type
        (`0,03`) both turn a 3-member fold into a 5- or 6-item string with no way
        back. Folding must decline rather than emit an ambiguous row.
        """
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._series(["1,2-dichloroethane", "benzene", "0,03"]))

        assert "1,2-dichloroethane" in out
        assert "0,03" in out
        assert "Concentration_1-3" not in out, f"folded a comma-bearing series: {out}"

    def test_refuses_to_fold_when_a_value_slot_is_an_ontology_iri(self):
        """A blank dose level must never be re-presented AS its column's IRI (#419).

        The dedup pass strips the annotation from rows 2..N, so a row whose value
        was blank collapses to `[label, IRI]` and looks the same shape as its
        filled neighbours. Folding then joined the IRI into the value list and the
        leaf read a URL as a concentration.
        """
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._series(["0.003", "", "0.03", "0.1"], iri=self._IRI))

        folded = [ln for ln in out.split("\n") if re.search(r"_\d+-\d+ \|", ln)]
        assert not any(self._IRI in ln for ln in folded), (
            f"an ontology IRI was emitted inside a dose list: {out}"
        )
        # The blank level keeps its own row rather than vanishing into a fold.
        assert "Chemical_1_Concentration_2 |" in out, out

    def test_never_renumbers_a_zero_padded_index(self):
        """`Aliquot_007` must not come back as `Aliquot_7` (#419).

        The short-run path rebuilt the label from `int(index)`, rewriting
        identifiers even when nothing folded — and two distinct rows could
        collide on one label.
        """
        from builder.tools.file_readers import compact_grid_text

        grid = "[Sheet: S]\n| Aliquot_007 | James Bond |\n| Aliquot_1 | Jane Doe |"
        out = compact_grid_text(grid)

        assert "Aliquot_007" in out, f"zero-padded index was rewritten: {out}"
        assert "Aliquot_1" in out

    def test_folds_a_clean_series_with_consistent_padding(self):
        """CONTROL for the three refusals above — the fold must still fire.

        Without this, any of the guards could regress into "never fold" and the
        refusal tests would all still pass while #419 silently came back.
        """
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._series(["0.003", "0.01", "0.03", "0.1"], iri=self._IRI))

        # Row 1 states the IRI and so differs in shape from rows 2-4, which the
        # dedup has stripped; the fold therefore starts at 2. That split is the
        # documented interaction, not a miss.
        assert "Chemical_1_Concentration_2-4" in out, f"clean series did not fold: {out}"
        assert "0.01, 0.03, 0.1" in out

    def test_is_a_noop_on_text_that_is_not_a_grid(self):
        """HONESTY CONTROL: the compactor must not mangle arbitrary prose."""
        from builder.tools.file_readers import compact_grid_text

        prose = "A cell based in vitro assay.\nNo pipes here at all.\n"
        assert compact_grid_text(prose) == prose.strip()


class TestCompactAttributeJson:
    """`compact_attribute_json` flattens BioStudies `{name,value,valqual}` trees."""

    def test_keeps_valqual_payloads(self):
        """A naive flattener drops `valqual` and loses the AOP URL and BAO ids.

        Non-tautological: asserts specific domain tokens survive compaction of
        the committed real descriptor, so a flattener that keeps only
        `name=value` fails.
        """
        from builder.tools.file_readers import compact_attribute_json

        raw = Path(_DESCRIPTOR_JSON).read_text(encoding="utf-8")
        compacted = compact_attribute_json(raw)

        assert len(compacted) < len(raw)
        for token in ("https://aopwiki.org/aops/610", "BAO_0010001"):
            assert token in compacted, f"compaction lost {token!r}"

    def test_moves_the_late_signal_within_an_affordable_slice(self):
        """The point of compaction: signal must land inside a 2,000-char slice.

        In the raw pretty-printed descriptor the licence, AOP URL and first
        author all sit past 2,900 chars, so today's slice carries none of them.
        """
        from builder.tools.file_readers import compact_attribute_json

        raw = Path(_DESCRIPTOR_JSON).read_text(encoding="utf-8")
        compacted = compact_attribute_json(raw)

        head = compacted[:2000]
        for token in ("aopwiki", "Wagenaars"):
            assert token.lower() in head.lower(), f"{token!r} still past a 2000-char slice"

    def test_returns_input_unchanged_when_not_an_attribute_tree(self):
        """HONESTY CONTROL: non-BioStudies JSON must survive untouched."""
        from builder.tools.file_readers import compact_attribute_json

        plain = json.dumps({"hello": "world"})
        assert compact_attribute_json(plain) == plain


class TestLegacyXlsReader:
    """Legacy BIFF ``.xls`` is a different container from OOXML (#417).

    ``read_excel`` was openpyxl-only, so every pre-2007 workbook raised
    ``InvalidFileException`` and was logged as a full ERROR traceback — once per
    file, on a corpus full of them, which read like a crash while the scan was in
    fact fine. ``read_file`` never routed ``.xls`` at all, so those files
    contributed nothing to the crate.
    """

    def _fake_xlrd(self, monkeypatch, rows):
        """A stand-in for xlrd exercising the conversion logic.

        Authoring a valid BIFF8 workbook byte-by-byte is not worth it, and xlwt
        is not a dependency — but the parts that can be WRONG are ours: the date
        conversion, the integral-float rendering and the empty-row skip.
        """
        import datetime
        import sys
        import types

        class Cell:
            def __init__(self, ctype, value):
                self.ctype, self.value = ctype, value

        class Sheet:
            name = "Plate"

            def __init__(self, data):
                self._rows = data
                self.nrows = len(data)

            def row(self, index):
                return self._rows[index]

        class Book:
            datemode = 0

            def sheets(self):
                return [Sheet(rows)]

            def release_resources(self):
                return None

        module = types.ModuleType("xlrd")
        # setattr, not attribute assignment: a bare ModuleType declares no such
        # attributes, so the checker rejects `module.XL_CELL_EMPTY = ...` even
        # though building a stub module this way is the point of the fixture.
        setattr(module, "XL_CELL_EMPTY", 0)
        setattr(module, "XL_CELL_DATE", 3)
        setattr(
            module,
            "xldate",
            types.SimpleNamespace(
                xldate_as_datetime=lambda value, mode: datetime.datetime(2022, 3, 17)
            ),
        )
        setattr(module, "open_workbook", lambda path, **kwargs: Book())
        monkeypatch.setitem(sys.modules, "xlrd", module)
        return Cell

    def test_corrupt_xls_warns_once_without_a_traceback(self, tmp_path, caplog) -> None:
        import logging

        from builder.tools.file_readers import read_excel

        bad = tmp_path / "legacy.xls"
        bad.write_bytes(b"\xd0\xcf\x11\xe0 not really a workbook")
        with caplog.at_level(logging.DEBUG):
            assert read_excel(str(bad)) is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "legacy.xls" in warnings[0].getMessage()
        # The traceback survives at DEBUG for anyone who wants it, but never
        # lands at WARNING/ERROR where a corpus of these looks like a crash.
        assert warnings[0].exc_info is None

    def test_biff_rows_render_like_the_ooxml_branch(self, monkeypatch, tmp_path) -> None:
        from builder.tools.file_readers import read_excel

        Cell = self._fake_xlrd(monkeypatch, [])
        rows = [
            [Cell(1, "well_id"), Cell(1, "value")],
            [Cell(2, 7.0), Cell(2, 4.5)],
            [Cell(0, None), Cell(0, None)],
            [Cell(2, 8.0), Cell(1, "")],
        ]
        Cell = self._fake_xlrd(monkeypatch, rows)
        path = tmp_path / "legacy.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        text = read_excel(str(path))
        assert text is not None
        assert "[Sheet: Plate]" in text
        # Integral floats render as ints — openpyxl does, and a well_id of "7.0"
        # breaks the downstream string comparisons.
        assert "| 7 | 4.5 |" in text
        # The all-empty row is skipped, exactly like the OOXML branch.
        assert text.count("\n|") == 3

    def test_date_serials_are_converted_not_leaked(self, monkeypatch, tmp_path) -> None:
        # xlrd hands back a raw serial float; leaking 44637.0 where the sheet
        # says 2022-03-17 would put corrupted data in the crate (D5).
        from builder.tools.file_readers import read_excel

        Cell = self._fake_xlrd(monkeypatch, [])
        rows = [[Cell(1, "run_date")], [Cell(3, 44637.0)]]
        self._fake_xlrd(monkeypatch, rows)
        path = tmp_path / "legacy.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        text = read_excel(str(path))
        assert text is not None
        assert "2022-03-17" in text
        assert "44637" not in text

    def test_read_file_routes_legacy_xls(self, monkeypatch, tmp_path) -> None:
        from builder.tools.file_readers import read_file

        Cell = self._fake_xlrd(monkeypatch, [])
        self._fake_xlrd(monkeypatch, [[Cell(1, "a")], [Cell(2, 1.0)]])
        path = tmp_path / "legacy.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        assert read_file(str(path)) is not None

    def test_missing_xlrd_says_so_without_crashing(self, monkeypatch, tmp_path) -> None:
        import builtins

        from builder.tools.file_readers import read_excel

        real_import = builtins.__import__

        def _no_xlrd(name, *args, **kwargs):
            if name == "xlrd":
                raise ImportError("no xlrd")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_xlrd)
        path = tmp_path / "legacy.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        assert read_excel(str(path)) is None


class TestGuidanceColumnCompaction:
    """Authoring-guidance columns are scaffolding, not metadata (#421).

    The rule these cover generalises the #419 one beyond the VHP assay-metadata
    template family. S-VHPS22's top-level file is an RIVM template
    (`Metadataveldenlijst_1.2.0.xlsx`) whose six columns are
    `Veldnaam | Optionaliteit | Hoe vaak in te vullen | Beschrijving | Tips |
    Hier invullen`: 17 of 38 rows are filled, carrying 610 characters of real
    metadata behind 9,913 characters of Dutch instructions. Under the old
    trailing-`Comments` rule none of it was removable, so the file burned the
    whole tier-0 budget and still got cut before Licentie, Versie and the rest.
    """

    _RIVM = "\n".join(
        [
            "[Sheet: Metadata]",
            "| Veldnaam | Optionaliteit | Hoe vaak in te vullen | Beschrijving | Tips "
            "| Hier invullen |",
            "| Titel | Verplicht | Eenmalig | De titel van de dataset zoals getoond in de "
            "catalogus | Bijv. 'Effect van X op Y' | Thyroid hormone transport in CHO-K1 |",
            "| Auteur | Verplicht | Per auteur | De volledige naam van de auteur "
            "| Voornaam Achternaam | Fabian Wagenaars |",
            "| ORCID | Aanbevolen | Per auteur | Persistent identifier voor de auteur "
            "| Zie orcid.org | 0009-0000-5074-6239 |",
            "| Licentie | Verplicht | Eenmalig | De licentie waaronder de dataset "
            "beschikbaar is | Bijv. CC-BY-4.0 | |",
        ]
    )

    def test_dutch_guidance_columns_are_dropped(self):
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._RIVM)

        assert "De titel van de dataset" not in out
        assert "Voornaam Achternaam" not in out
        assert "Verplicht" not in out
        assert "Eenmalig" not in out

    def test_the_depositor_values_all_survive(self):
        """The 610 characters that are the entire point of the file."""
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._RIVM)

        assert "Thyroid hormone transport in CHO-K1" in out
        assert "Fabian Wagenaars" in out
        assert "0009-0000-5074-6239" in out
        # …still paired with the field they answer, not orphaned into a bare list.
        assert "| ORCID | 0009-0000-5074-6239 |" in out

    def test_it_fits_the_budget_it_used_to_blow(self):
        from builder.tools.file_readers import compact_grid_text

        assert len(compact_grid_text(self._RIVM)) < len(self._RIVM) / 2

    def test_middle_columns_are_dropped_by_index_not_position(self):
        """The RIVM guidance columns are 1-4 of 6; a trailing-column rule cannot reach them."""
        from builder.tools.file_readers import compact_grid_text

        rows = [ln for ln in compact_grid_text(self._RIVM).split("\n") if ln.startswith("|")]

        assert all(ln.count("|") == 3 for ln in rows), rows

    def test_an_unfilled_template_row_is_dropped_whole(self):
        """No answer means no row — keeping its instructions is the noise being removed."""
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(self._RIVM)

        assert "Licentie" not in out
        assert "CC-BY-4.0" not in out

    def test_a_description_column_holding_real_content_is_untouched(self):
        """The risk the header vocabulary creates, and the guard against it.

        `Description` names scaffolding in a fill-in-the-blanks template and real
        content in a workbook that simply has a description column. Here the
        answer column is empty and `Description` carries the study, so nothing is
        droppable and every column stays.
        """
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(
            "\n".join(
                [
                    "[Sheet: Study]",
                    "| Parameter | Description | Value |",
                    "| Study title | Thyroid hormone disruption screen in CHO-K1 |  |",
                    "| Study aim | Identify inhibitors of OATP1C1-mediated uptake |  |",
                ]
            )
        )

        assert "Thyroid hormone disruption screen in CHO-K1" in out
        assert "Identify inhibitors of OATP1C1-mediated uptake" in out

    def test_the_same_header_is_dropped_when_the_answer_column_is_filled(self):
        """Same header, opposite verdict — the sheet's content decides, not the word."""
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(
            "\n".join(
                [
                    "[Sheet: Study]",
                    "| Parameter | Description | Value |",
                    "| Study title | The title of the study | Thyroid screen |",
                    "| Study aim | The aim of the study | Find OATP1C1 inhibitors |",
                ]
            )
        )

        assert "The title of the study" not in out
        assert "Thyroid screen" in out
        assert "Find OATP1C1 inhibitors" in out

    def test_the_decision_is_per_sheet(self):
        """A five-sheet template mixes layouts; sheet 1 has no authority over sheet 2."""
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(
            "\n".join(
                [
                    "[Sheet: Guided]",
                    "| Parameter | Description | Value |",
                    "| Endpoint | The endpoint measured | T4 uptake |",
                    "[Sheet: Freeform]",
                    "| Parameter | Description | Value |",
                    "| Study aim | Identify OATP1C1 inhibitors |  |",
                ]
            )
        )

        assert "The endpoint measured" not in out, "sheet 1's guidance should go"
        assert "Identify OATP1C1 inhibitors" in out, "sheet 2's content should stay"

    def test_guidance_matching_ignores_case_and_spacing(self):
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(
            "\n".join(
                [
                    "[Sheet: Metadata]",
                    "| Veldnaam |  Hoe vaak in te vullen  | TIPS | Hier invullen |",
                    "| Titel | Eenmalig | Bijv. een korte titel | Thyroid screen |",
                ]
            )
        )

        assert out.endswith("| Titel | Thyroid screen |"), out

    def test_a_value_is_never_matched_against_the_vocabulary(self):
        """Header-scoped only: a cell whose *text* is a guidance word is still data."""
        from builder.tools.file_readers import compact_grid_text

        out = compact_grid_text(
            "\n".join(
                [
                    "[Sheet: Metadata]",
                    "| Veldnaam | Hier invullen |",
                    "| Documenttype | Beschrijving |",
                ]
            )
        )

        assert "| Documenttype | Beschrijving |" in out
