"""Real-input scan/context regression over two further genuine VHP4Safety deposits.

``tests/test_pipeline_real_input.py`` drives the full producer over S-VHPS26, a
deposit with exactly ONE assay, ONE ``*metadata*`` workbook and ONE procedure
document. That shape is the happy path for :func:`_gather_context`'s tiered
budget, so it cannot exercise what happens when a deposit is larger or built
from older tooling. These two fixtures cover the shapes it cannot:

``tests/fixtures/svhps21_real_input/`` — the MCT8-MDCK1 transporter deposit
    (BioStudies S-VHPS21). Same single-assay shape as S-VHPS26, but written with
    OLDER tooling throughout: raw measurements are legacy OLE2 ``.xls`` rather
    than ``.xlsx``, and analyses are ``.prism``/``.pzf`` rather than ``.pzfx``.
    Neither older format is readable by the libraries S-VHPS26 exercises, which
    is the point. It also has NO procedure document at all — its own README
    promises an SOP that was never deposited — so it is the real-world control
    for a deposit whose protocol layer is missing rather than merely unread.

``tests/fixtures/svhps22_real_input/`` — the TH-DNT neural-cell screening study
    (BioStudies S-VHPS22): FOUR assays under one study, each with its own
    ``*_assay_metadata.xlsx`` and its own protocol documents, plus a study-wide
    field-definition workbook and a ``cell_line_protocols/`` folder shared by
    every assay — the only study-level protocol layer in the suite, and the
    distinction ARC draws between ``studies/<study>/protocols/`` and
    ``assays/<assay>/protocols/``. Five priority-0 files and more than twenty
    priority-2 documents in one scan.

Both are committed verbatim from the depositors' raw submission folders, in the
real nested layout (paths carrying spaces, ``+``, ``&``, commas and Dutch
wording), subset only by dropping files — never by truncating one.

These tests deliberately stop at the SCAN + CONTEXT layer rather than running
``run_pipeline``. That layer is where both fixtures earn their keep, it is fully
deterministic (no LLM seam to stub, no SHACL passes), and it keeps the module
fast enough to leave the CI shard balance alone.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from builder.agents.pipeline.pipeline import (
    _MAX_CONTEXT_CHARS,
    _gather_context,
    _metadata_read_priority,
)
from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.document_discovery import (
    CLASS_PROCESSED_DATA,
    CLASS_RAW_DATA,
    _safe_preview,
    classify_file,
    classification_of,
    preview_mode_for,
)
from builder.tools.file_readers import read_docx, read_file
from builder.tools.hitl import SimulatedHumanInterface

FIXTURES = Path(__file__).parent / "fixtures"
SVHPS21 = FIXTURES / "svhps21_real_input"
SVHPS22 = FIXTURES / "svhps22_real_input"


def _classify_at(path: Path, root: Path) -> str:
    """Classify a fixture file exactly as a run would — real preview, real path."""
    preview = _safe_preview(
        str(path), {str(root.resolve())}, 3_000, mode=preview_mode_for(path.name)
    )
    return classify_file(path.name, preview, str(path.relative_to(root)))[0]

# ---------------------------------------------------------------------------
# Honesty tokens — lowercase strings carried in real document CONTENT and in NO
# filename in either fixture, so a scan or body-read regression empties them.
# ---------------------------------------------------------------------------

# Every token below is FILE-EXCLUSIVE: it occurs in exactly one fixture file and
# in no filename. That property is what makes these gates rather than decoration.
# S-VHPS21.json restates much of the deposit — the RRID `cvcl_0592`, the species
# `canis lupus familiaris`, the plate reader `multiskan` and the gene symbol
# `slc16a2` all appear in the descriptor as well — so gating on any of those
# would keep passing with the workbook and README reads entirely broken. They are
# deliberately not used.

# From the assay-metadata workbook only: the expression vector, the DOI of the
# paper describing the transfected line (an identifier living in a CELL), and a
# value on the final Endpoint Readout sheet. The last is the deepest signal in
# the file, so it reddens first if the tier-0 share or the compaction regresses.
_S21_WORKBOOK_TOKENS = (
    "pcdna3.1",
    "10.1677/jme-09-0043",
    "aop wiki id: 2258",
)
# From README.txt prose only: the acronym every path spells `MDCK1` written out
# in full, the readout chemistry, and a sample-digestion step. `sandell-kolthoff`
# is the token the S-VHPS26 suite had to drop — there it survived only in a
# README copy-pasted from THIS deposit, and here it is native.
_S21_README_TOKENS = (
    "madin-darby canine kidney",
    "sandell-kolthoff",
    "uv irradiation",
)
# Test chemicals named in workbook cells, deep into the table. Each is the honest
# form of an abbreviation the run folders use instead — `BSP`, `ICG` — and
# `desipramine` is the strongest gate in the fixture: every filename and folder
# misspells it `Desipramide`, so only a genuine cell read matches it.
_S21_CHEMICAL_TOKENS = (
    "bromosulfophthalein",
    "desipramine",
    "indocyanine green",
    "pentachlorophenol",
)
# From the legacy .xls plate-reader export only: the instrument model as the
# machine wrote it, and the run's execution timestamp. `multiskan` on its own
# also appears in the workbook and the descriptor; the full model string does
# not, so only a genuine .xls read matches it.
_S21_XLS_TOKENS = (
    "multiskan fc with incubator",
    "31.03.2022 17:03:40",
)
_S21_RUN = "220331_SK_MCT8_MDCK1_P3_BSP+Desipramide"

# Extensions that carry measurements rather than description, so a role
# assertion is about data placement and not about a README's classification.
_DATA_SUFFIXES = frozenset(
    {".csv", ".xls", ".xlsx", ".pzfx", ".pzf", ".prism", ".eds", ".pdf", ".png"}
)

_S22_ASSAYS = (
    "assay_01_TH_uptake",
    "assay_02_deiodinase",
    "assay_03_metabolism",
    "assay_04_TRactivation",
)
# The four per-assay metadata workbooks, by the basename the digest renders.
# S-VHPS22 is gated on emitted BODY rather than on content tokens: the four
# assays share a cell panel, so every RRID, CAS number, funder and contact in one
# workbook also appears in at least one other and usually in S-VHPS22.json too.
# No token here can distinguish "assay_03's workbook was read" from "some other
# workbook was read", which is exactly the claim that matters.
_S22_ASSAY_WORKBOOKS = (
    "TH_assay_metadata.xlsx",
    "deiodinase_assay_metadata.xlsx",
    "metabolism_assay_metadata.xlsx",
    "tractivation_assay_metadata.xlsx",
)


@pytest.fixture(scope="module")
def svhps21_context() -> str:
    """The digest S-VHPS21 produces, gathered once for the whole module."""
    return _gather_context(_scanning_engine(SVHPS21)).lower()


@pytest.fixture(scope="module")
def svhps22_engine() -> AgentEngine:
    """An engine that has scanned S-VHPS22, reused across the module."""
    return _scanning_engine(SVHPS22)


def _scanning_engine(input_dir: Path) -> AgentEngine:
    """A headless engine that has SCANNED *input_dir* off disk via the real guard."""
    engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
    engine.initialize(input_path=str(input_dir))
    return engine


def _scanned_names(engine: AgentEngine) -> set[str]:
    return {Path(f.path).name for f in engine.state.scanned_files}


def _file_blocks(context: str) -> list[str]:
    """The per-file ``- <filename>[: <body>]`` entries of the rendered digest.

    ``_gather_context`` joins its sections with a blank line, so the listing is
    cut at the section that follows it — otherwise the final file's entry
    absorbs the whole document-discovery digest and every char count inflates.
    """
    _, _, listing = context.partition("Scanned files:\n")
    if not listing:
        return []
    listing = listing.split("\n\nDiscovered documentation:")[0]
    return [block for block in listing.split("\n- ") if block.strip()]


def _emitted_chars(context: str, stem: str) -> int:
    """Characters the digest emitted for the FIRST entry starting with *stem*.

    ``_gather_context`` renders one entry per scanned file, so an entry no longer
    than its own filename means the body was starved to nothing. Several real
    deposits repeat a basename (four of S-VHPS22's folders each hold a
    ``README.txt``), so this deliberately answers for one entry only — summing
    across the whole digest goes through :func:`_file_blocks`.
    """
    for block in _file_blocks(context):
        if block.lstrip("- ").lower().startswith(stem.lower()):
            return len(block)
    return 0


# ---------------------------------------------------------------------------
# The fixtures ARE the real deposits (guards them silently going synthetic).
# ---------------------------------------------------------------------------


class TestFixturesAreReal:
    def test_svhps21_is_the_real_mct8_deposit(self) -> None:
        assert SVHPS21.is_dir(), f"missing fixture: {SVHPS21}"
        descriptor = json.loads((SVHPS21 / "S-VHPS21.json").read_text())
        assert descriptor["accno"] == "S-VHPS21"
        title = next(
            a["value"] for a in descriptor["attributes"] if a["name"] == "Title"
        )
        assert "MCT8" in title

        workbook = SVHPS21 / "Assay_MCT8-MDCK1" / "Assay-metadata-MCT8-MDCK1-v1.1.xlsx"
        assert workbook.is_file(), "the real assay-metadata workbook must be committed"
        # Real measurement files, in the archive's nested layout — the directory
        # name carries both a space and a '+'.
        prisms = sorted(
            (SVHPS21 / "Assay_MCT8-MDCK1" / "Raw data + individual processed data")
            .rglob("*.prism")
        )
        assert prisms, "real GraphPad measurement files must be committed"

    def test_svhps22_is_the_real_four_assay_study(self) -> None:
        assert SVHPS22.is_dir(), f"missing fixture: {SVHPS22}"
        descriptor = json.loads((SVHPS22 / "S-VHPS22.json").read_text())
        assert descriptor["accno"] == "S-VHPS22"

        for assay in _S22_ASSAYS:
            folder = SVHPS22 / assay
            assert folder.is_dir(), f"{assay} must be committed"
            assert sorted(folder.glob("*metadata*.xlsx")), (
                f"{assay} must carry its own metadata workbook"
            )

    def test_svhps21_genuinely_has_no_procedure_document(self) -> None:
        """The missing-SOP case, as deposited (NOT an incomplete fixture).

        S-VHPS26 pairs its workbook with a real SOP ``.docx``; this deposit has
        none, and its own README lists a Standard Operating Procedure among the
        files it claims to contain. Asserting the absence keeps a later
        well-meaning "the fixture forgot the SOP" addition from quietly turning
        this deposit into a duplicate of S-VHPS26.
        """
        readme = (SVHPS21 / "Assay_MCT8-MDCK1" / "README.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "Standard Operating Procedure" in readme, (
            "the README must still be the one that promises an SOP"
        )
        procedures = [
            p
            for p in SVHPS21.rglob("*")
            if p.is_file() and p.suffix.lower() in {".docx", ".doc", ".odt", ".rtf"}
        ]
        assert not procedures, f"S-VHPS21 deposits no procedure document: {procedures}"


# ---------------------------------------------------------------------------
# Scanning: recursion, inventory, and role classification over real layouts.
# ---------------------------------------------------------------------------


class TestScanning:
    def test_svhps21_scan_recurses_and_splits_raw_from_processed(self) -> None:
        engine = _scanning_engine(SVHPS21)
        names = _scanned_names(engine)

        assert "S-VHPS21.json" in names
        assert "Assay-metadata-MCT8-MDCK1-v1.1.xlsx" in names
        # Recursion through "Raw data + individual processed data/<run>/".
        assert any(n.endswith(".prism") for n in names)

        classes = {
            classification_of(f, input_root=str(SVHPS21)) for f in engine.state.scanned_files
        }
        assert {CLASS_RAW_DATA, CLASS_PROCESSED_DATA} <= classes
        assert not any(n.endswith((".py", ".pyc")) for n in names)
        assert ".DS_Store" not in names

    def test_svhps22_scan_reaches_every_assay(self) -> None:
        """All four assays must be inventoried from ONE scan of the study root."""
        engine = _scanning_engine(SVHPS22)
        paths = [f.path for f in engine.state.scanned_files]

        for assay in _S22_ASSAYS:
            assert any(f"/{assay}/" in p for p in paths), f"{assay} never scanned"

        names = _scanned_names(engine)
        # Each assay's own metadata workbook, and the study-wide field list.
        assert "Metadataveldenlijst_1.2.0.xlsx" in names
        assert len([n for n in names if "metadata" in n.lower()]) >= 5

    def test_svhps22_scan_survives_awkward_real_filenames(self) -> None:
        """Real deposits carry '&', ',', spaces and Dutch words in path segments."""
        paths = [f.path for f in _scanning_engine(SVHPS22).state.scanned_files]
        assert any("&" in p for p in paths), "a path containing '&' must survive"
        assert any(", " in p for p in paths), "a path containing ', ' must survive"
        assert any("runnen" in p.lower() for p in paths)


# ---------------------------------------------------------------------------
# Context fidelity: what actually reaches the bounded extraction leaf.
# ---------------------------------------------------------------------------


class TestSvhps21ContextFidelity:
    """S-VHPS21 is the healthy shape — every layer of it must reach the leaf."""

    @pytest.mark.parametrize("token", _S21_WORKBOOK_TOKENS)
    def test_metadata_workbook_body_reaches_the_leaf(
        self, svhps21_context: str, token: str
    ) -> None:
        assert token in svhps21_context, f"workbook signal starved: {token}"

    @pytest.mark.parametrize("token", _S21_README_TOKENS)
    def test_readme_prose_reaches_the_leaf(
        self, svhps21_context: str, token: str
    ) -> None:
        assert token in svhps21_context, f"README prose starved: {token}"

    @pytest.mark.parametrize("token", _S21_CHEMICAL_TOKENS)
    def test_test_chemicals_reach_the_leaf(
        self, svhps21_context: str, token: str
    ) -> None:
        assert token in svhps21_context, f"test chemical starved: {token}"

    def test_metadata_outweighs_bulk_data(self) -> None:
        """STARVATION CONTROL — metadata-first must hold in CHARS, not just order."""
        context = _gather_context(_scanning_engine(SVHPS21))
        metadata = _emitted_chars(context, "Assay-metadata-MCT8-MDCK1")
        bulk = _emitted_chars(context, "220331_SK_MCT8_MDCK1_P3_BSP+Desipramide.prism")
        assert metadata > bulk, f"metadata {metadata} chars vs bulk {bulk} chars"


class TestSvhps22ContextFidelity:
    """S-VHPS22 is the shape the tiered budget was never exercised against."""

    def test_the_budget_ceiling_is_still_honoured(
        self, svhps22_engine: AgentEngine
    ) -> None:
        """Whatever else changes, the emitted digest must stay bounded.

        Pinned first so a fix for the starvation below cannot 'succeed' by simply
        handing every file a bigger slice.
        """
        context = _gather_context(svhps22_engine)
        emitted = sum(len(block) for block in _file_blocks(context))
        assert emitted <= _MAX_CONTEXT_CHARS * 2, (
            f"emitted {emitted} chars against a {_MAX_CONTEXT_CHARS} ceiling"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "_gather_context reserves each lower tier's share PER FILE and "
            "uncapped, so 19 priority-2 documents reserve 38,000 chars against a "
            "16,000 ceiling and every priority-0 workbook floors at zero. Remove "
            "this marker with the fix."
        ),
    )
    def test_every_assay_metadata_workbook_reaches_the_leaf(
        self, svhps22_engine: AgentEngine
    ) -> None:
        """The payoff this fixture exists for.

        A four-assay study carries its cell lines, RRIDs, CAS numbers, detection
        instruments and people in five priority-0 workbooks. The leaf can only
        propose what it was shown, so a workbook that emits nothing is a whole
        assay the crate cannot describe — silently.

        Asserted against the contract (metadata-first, #179/#378) rather than
        against the present behaviour, so that fixing the reservation turns it
        green rather than requiring the assertion to be rewritten.
        """
        context = _gather_context(svhps22_engine)
        starved = {
            workbook: _emitted_chars(context, workbook)
            for workbook in _S22_ASSAY_WORKBOOKS
            if _emitted_chars(context, workbook) <= len(workbook) + 8
        }
        assert not starved, (
            f"{len(starved)} of {len(_S22_ASSAY_WORKBOOKS)} assay metadata "
            f"workbooks emitted filename only: {starved}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Same uncapped per-file reservation: tier 1 is reserved out of "
            "existence by the tier-2 and tier-3 crowd. Remove with the fix."
        ),
    )
    def test_the_biostudies_descriptor_is_never_starved(
        self, svhps22_engine: AgentEngine
    ) -> None:
        """The descriptor is the deposit's only structured identity record.

        Losing it to a crowd of protocol documents is strictly worse than
        truncating any one of them, and it is the exact trade
        ``test_a_second_metadata_file_does_not_starve_the_tiers_below`` forbids
        in the other direction.
        """
        context = _gather_context(svhps22_engine)
        emitted = _emitted_chars(context, "S-VHPS22.json")
        assert emitted > len("S-VHPS22.json") + 8, (
            f"the descriptor emitted {emitted} chars — filename only"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Metadata-first inverts on this deposit: tier 0 emits 144 chars "
            "against tier 2's 16,095. Remove with the fix."
        ),
    )
    def test_priority_zero_outranks_priority_two(
        self, svhps22_engine: AgentEngine
    ) -> None:
        """Metadata-first stated as the invariant it is, independent of tokens.

        The tier-0 workbooks must not collectively emit less than the tier-2
        documents do; that ordering is the whole point of the weighted budget.
        Counted over the rendered entries so repeated basenames (four folders
        each hold a ``README.txt``) are each counted once.
        """
        context = _gather_context(svhps22_engine)
        by_tier: Counter[int] = Counter()
        for block in _file_blocks(context):
            filename = block.lstrip("- ").split(":", 1)[0].strip()
            by_tier[_metadata_read_priority(filename)] += len(block)
        assert by_tier[0] > by_tier[2], (
            f"tier-0 metadata emitted {by_tier[0]} chars, "
            f"tier-2 documents emitted {by_tier[2]}"
        )


# ---------------------------------------------------------------------------
# Role classification for the older GraphPad formats S-VHPS21 introduced.
# ---------------------------------------------------------------------------


class TestLegacyXlsMeasurements:
    """S-VHPS21's raw measurement layer is legacy binary ``.xls``, all 27 files.

    openpyxl supports only the zipped OOXML formats and cannot open an OLE2
    ``.xls``, so until ``read_excel`` gained its xlrd fallback every one of
    them contributed its filename and nothing else — silently, because the
    exception naming xlrd as the remedy was swallowed by a blanket handler.
    """

    def test_the_fixture_pairs_raw_and_processed_from_one_run(self) -> None:
        """The deposit's real shape: each run folder holds both formats.

        Guards the ``.xls`` half against being dropped again as 'just bulk
        data' — it is the only legacy-format measurement file in the suite.
        """
        run = (
            SVHPS21
            / "Assay_MCT8-MDCK1"
            / "Raw data + individual processed data"
            / _S21_RUN
        )
        assert (run / f"{_S21_RUN}.xls").is_file(), "the raw .xls must be committed"
        assert (run / f"{_S21_RUN}.prism").is_file(), "its processed sibling too"
        assert _classify_at(run / f"{_S21_RUN}.xls", SVHPS21) == CLASS_RAW_DATA
        assert _classify_at(run / f"{_S21_RUN}.prism", SVHPS21) == CLASS_PROCESSED_DATA

    def test_the_legacy_workbook_is_not_silently_empty(self) -> None:
        """xlrd — already an installed dependency — recovers the whole sheet.

        Stated as the control: the bytes are readable, so the gap below is a
        reader-selection choice rather than an unreadable file.
        """
        xlrd = pytest.importorskip("xlrd")
        path = (
            SVHPS21
            / "Assay_MCT8-MDCK1"
            / "Raw data + individual processed data"
            / _S21_RUN
            / f"{_S21_RUN}.xls"
        )
        book = xlrd.open_workbook(path)
        assert "General_Info" in book.sheet_names()
        sheet = book.sheet_by_name("General_Info")
        text = " ".join(
            str(v) for r in range(sheet.nrows) for v in sheet.row_values(r)
        ).lower()
        for token in _S21_XLS_TOKENS:
            assert token in text, f"{token} not recoverable even with xlrd"

    @staticmethod
    def _xls_path() -> Path:
        return (
            SVHPS21
            / "Assay_MCT8-MDCK1"
            / "Raw data + individual processed data"
            / _S21_RUN
            / f"{_S21_RUN}.xls"
        )

    @pytest.mark.parametrize("token", _S21_XLS_TOKENS)
    def test_the_pipeline_reader_extracts_the_legacy_workbook(self, token: str) -> None:
        """``read_file`` is the entry point ``_gather_context`` reads bodies through.

        Asserted here rather than on the gathered context because the tokens sit
        around offset 5,000 and a ``.xls`` ranks priority 3, whose per-file slice
        is 500 chars — widening that is the budget question, not this one.
        """
        text = read_file(str(self._xls_path()), compact=False, max_lines=100)
        assert text, "read_file returned nothing for a legacy .xls"
        assert token in text.lower(), f"{token} missing from the extracted body"

    def test_the_legacy_workbook_is_more_than_a_filename_in_the_digest(self) -> None:
        """What the leaf actually receives: a body, not a bare name.

        Before the xlrd fallback this entry was 45 characters — the filename and
        nothing else — indistinguishable from a file the scanner had refused.
        """
        context = _gather_context(_scanning_engine(SVHPS21))
        name = f"{_S21_RUN}.xls"
        emitted = _emitted_chars(context, name)
        assert emitted > len(name) + 8, f"the .xls emitted {emitted} chars"


class TestGraphPadRoleClassification:
    """GraphPad Prism writes three interchangeable project extensions.

    S-VHPS26 deposits ``.pzfx``, S-VHPS21 deposits ``.prism`` and ``.pzf``. All
    three hold the SAME thing — a Prism project of fitted curves and analyses —
    so all three are processed data. ``.pzf`` is simply the legacy binary
    spelling of ``.pzfx``, and a deposit using it gets its analysis output
    exported into the crate's ``raw_data/`` tree.
    """

    @pytest.mark.parametrize("suffix", [".prism", ".pzfx", ".pzf"])
    def test_every_graphpad_project_is_processed_data(self, suffix: str) -> None:
        assert (
            classify_file(f"All compounds including best-fit{suffix}", "")[0]
            == CLASS_PROCESSED_DATA
        )


class TestTheDepositsOwnFilingConventions:
    """Each deposit states raw-vs-processed differently, and all of them work.

    S-VHPS22 files its qPCR exports into ``assay4_EDCs_raw data/`` beside
    ``assay4_EDCs_processed data/``, where neither member's own name says which
    it is; S-VHPS21 puts both into one ``Raw data + individual processed data/``,
    where the folder cannot say. Classification reads the file first and the
    folder last, so both conventions resolve (#591).
    """

    @pytest.mark.parametrize(
        ("relpath", "expected"),
        [
            (
                "assay_01_TH_uptake/characterisation uptake/assay1_rawdata/004043.csv",
                CLASS_RAW_DATA,
            ),
            (
                "assay_01_TH_uptake/characterisation uptake/assay1_processeddata/"
                "Combined uptake data 0-60 min.xlsx",
                CLASS_PROCESSED_DATA,
            ),
            (
                "assay_04_TRactivation/EDCs/assay4_EDCs_raw data/"
                "2024-10-30 SK sily n3 Raw data.eds",
                CLASS_RAW_DATA,
            ),
            (
                "assay_04_TRactivation/EDCs/assay4_EDCs_processed data/"
                "Silychristin SK redo combined.xlsx",
                CLASS_PROCESSED_DATA,
            ),
            # The folder names neither tier — the file's own columns decide, and
            # this one is the deposit's 1048-row tidy analysis output.
            (
                "assay_01_TH_uptake/EDCs/Combined uptake data EDCs_tidy.csv",
                CLASS_PROCESSED_DATA,
            ),
        ],
    )
    def test_a_file_in_this_deposit_resolves(self, relpath: str, expected: str) -> None:
        path = SVHPS22 / relpath
        assert path.is_file(), f"fixture is missing {relpath}"
        assert _classify_at(path, SVHPS22) == expected

    def test_a_folder_naming_both_tiers_does_not_decide_for_either(self) -> None:
        """The trap: S-VHPS21 deposits every run into one shared folder.

        ``Raw data + individual processed data/`` holds each run's raw ``.xls``
        beside its processed ``.prism``, so reading the folder for either word
        would mislabel one of them. The pair splits on its own evidence.
        """
        run = (
            SVHPS21
            / "Assay_MCT8-MDCK1"
            / "Raw data + individual processed data"
            / _S21_RUN
        )
        assert _classify_at(run / f"{_S21_RUN}.xls", SVHPS21) == CLASS_RAW_DATA
        assert _classify_at(run / f"{_S21_RUN}.prism", SVHPS21) == CLASS_PROCESSED_DATA


class TestStudyWideProtocols:
    """S-VHPS22 carries protocols at the STUDY level, not only per assay.

    ``cell_line_protocols/`` holds one culture protocol per cell line, shared by
    all four assays — the distinction ARC draws between
    ``studies/<study>/protocols/`` and ``assays/<assay>/protocols/``
    (AGENTS.md §9). Neither S-VHPS26 nor S-VHPS21 has a study-level protocol
    layer at all, so nothing else in the suite exercises it.
    """

    #: Each shared protocol and the cell line it governs, as its body names it.
    _PROTOCOLS = (
        ("20251114_cell culture protocol SK-N-AS.docx", "sk-n-as"),
        ("20251114_cell culture protocol H4.docx", "h4"),
        ("20251114_cell culture protocol MO3.13.docx", "mo3.13"),
    )

    def test_a_shared_protocol_exists_for_every_cell_line(self) -> None:
        folder = SVHPS22 / "cell_line_protocols"
        assert folder.is_dir(), "the study-level protocol folder must be committed"
        committed = {p.name for p in folder.glob("*.docx")}
        assert committed == {name for name, _ in self._PROTOCOLS}, committed

    @pytest.mark.parametrize(("filename", "cell_line"), _PROTOCOLS)
    def test_each_shared_protocol_has_extractable_body_text(
        self, filename: str, cell_line: str
    ) -> None:
        """Guards against the wrapper case seen elsewhere in this deposit.

        ``assay_02_deiodinase/4.1 Deiodinase activity assay.docx`` is 260 KB of
        Word wrapping a single embedded image and yields zero characters through
        python-docx. A protocol that extracts to nothing is indistinguishable
        from one that was never read, so each of these is pinned as genuinely
        readable and as naming the cell line it governs.
        """
        text = read_docx(str(SVHPS22 / "cell_line_protocols" / filename))
        assert text and text.strip(), f"{filename} yielded no body text"
        assert cell_line in text.lower(), f"{filename} never names {cell_line}"


class TestEveryAssayHasBothDataRoles:
    """Each S-VHPS22 assay must carry a raw AND a processed exemplar (#pairing).

    An assay represented by only one half is a shape the exporter never sees:
    the raw/processed split is what ``arc_writer`` projects onto the crate's
    ``dataset/raw_data`` and ``dataset/processed_data`` trees.
    """

    @pytest.mark.parametrize("assay", _S22_ASSAYS)
    def test_assay_carries_both_roles(self, assay: str) -> None:
        classes = {
            _classify_at(p, SVHPS22)
            for p in (SVHPS22 / assay).rglob("*")
            if p.is_file() and p.suffix.lower() in _DATA_SUFFIXES
        }
        assert {CLASS_RAW_DATA, CLASS_PROCESSED_DATA} <= classes, (
            f"{assay} carries only {classes or 'no data files'}"
        )
