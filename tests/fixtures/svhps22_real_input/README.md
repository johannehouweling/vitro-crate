# `svhps22_real_input/` — real S-VHPS22 deposit (curated subset)

A **byte-verbatim** subset of the genuine S-VHPS22 deposit. Unlike
`svhps21_real_input/` and `svhps26_real_input/`, which each hold exactly one
assay, this deposit is **one study containing four assays** plus a shared
cell-line protocol folder. That is the whole point of the fixture.

| | |
|---|---|
| Accession | `S-VHPS22` |
| Title | Neural cell screening models for endocrine disruption of thyroid hormone signaling |
| DOI | `10.6019/S-VHPS22` |
| Licence | CC-BY |
| Release date | 2026-05-27 |
| Source archive | `input/raw/S-VHPS22.zip` (Git LFS; **not** fetched by CI) |

## Why this fixture exists

The real deposit is 1,580 files (3,199 zip entries once macOS resource forks are
counted) across four assays:

| Assay | Measures |
|---|---|
| `assay_01_TH_uptake` | Thyroid-hormone uptake (radioactive T3/T4 transporter assay) |
| `assay_02_deiodinase` | Deiodinase activity |
| `assay_03_metabolism` | Metabolism in neural culture (UPLC) |
| `assay_04_TRactivation` | TR activation (T3-responsive genes, qPCR) |

plus `cell_line_protocols/` for three lines (H4, MO3.13, SK-N-AS).

**The ingestion path currently models exactly one Assay.** This fixture is the
first thing in the repo that feeds it a genuinely multi-assay deposit, so it
exists to characterize and pin that limitation, not to pass quietly.

## Subsetting rule

Byte-identical members only, verified by CRC-32 against the source zip. Real
nesting and real filenames preserved, including spaces and the zero-byte files.
One smallest real exemplar of each structural shape; nothing that is only bulk.

Per assay the subset keeps: its `README*.txt`, its `*_assay_metadata.xlsx`, one
protocol document, and (for assays 1 and 4) one real data exemplar — so both the
`raw_data` and `processed_data` roles are exercised.

Zero-byte files are kept on purpose: `assay_02_deiodinase/ToxTemp_placeholder.txt`
and `assay_01_TH_uptake/characterisation uptake/assay1_rawdata/README.txt` are
both genuinely empty in the deposit, and an empty file is a real edge case.

## Deliberately excluded

- **`assay_01_TH_uptake/Input files for toxtemp (temporary)/` in full** — it
  contains an unpublished manuscript draft. Not redistributed. It also
  duplicates files already present elsewhere in the subset.
- All `.eds` instrument files (~4.7 MB each) and the ~2,450 chromatogram PDFs —
  bulk only.
- The 875 KB RNeasy PDF and the 260 KB / 228 KB protocol documents — a smaller
  protocol document from the same assay stands in for each.
- All `__MACOSX/` entries and `.DS_Store` files. The scanner already prunes
  both (`_should_prune`: a directory is skipped when it starts with `.` or is
  named `__MACOSX`), and `.DS_Store` is in the repo's `.gitignore`, so macOS
  junk should be **synthesised in `tmp_path` at test time** rather than
  committed here.

## Measured behaviour at time of writing

Scanning this fixture through the real guard (`AgentEngine.initialize`):

- 23 files scanned; roles `{raw_data: 22, processed_data: 1}`.
- All four `assay_0*` folders and `cell_line_protocols/` are inventoried.
- `_gather_context` returns **33,694 characters** against a declared
  `_MAX_CONTEXT_CHARS` of 16,000. The per-file scaffolding is not charged
  against the budget, so the string the leaf receives grows with file count:
  16,795 chars for the 5-file `svhps26` fixture, 19,457 for the 6-file
  `svhps21` fixture, 33,694 here. Pin this as a known limitation; do not trim
  the fixture until it passes.
