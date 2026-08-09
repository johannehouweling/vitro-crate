# `svhps21_real_input/` — real S-VHPS21 deposit (curated subset)

A **byte-verbatim** subset of the genuine S-VHPS21 deposit, committed so the
deterministic pipeline can be driven over real study material rather than a
hand-built fixture. Sibling of `svhps26_real_input/`.

| | |
|---|---|
| Accession | `S-VHPS21` |
| Title | Inhibition of MCT8-mediated cellular uptake of triiodothyronine in an overexpressing cell model |
| DOI | `10.6019/S-VHPS21` |
| Licence | CC-BY |
| Release date | 2025-11-10 |
| Source archive | `input/raw/S-VHPS21.zip` (Git LFS; **not** fetched by CI) |

## Subsetting rule

Every file here is **byte-identical** to its member in the source archive —
verified by CRC-32 against the zip. Nothing is truncated, re-saved or stubbed.
Real nesting and real filenames are preserved exactly, **including the spaces,
`+` signs and the deposit's own typos**, because those are the ingestion hazards
the fixture exists to exercise.

The rule is: *one smallest real exemplar of every distinct structural shape;
nothing whose only contribution is bulk.*

## What is here, and why

| Path | Why it is here |
|---|---|
| `S-VHPS21.json` | The BioStudies submission descriptor. |
| `Assay_MCT8-MDCK1/README.txt` | The **only** methods document in this deposit — there is no SOP (see below). |
| `Assay_MCT8-MDCK1/Assay-metadata-MCT8-MDCK1-v1.1.xlsx` | The assay-metadata workbook: cell line, instrument, exposure parameters, the named chemical panel. |
| `.../Raw data + individual processed data/220407_SK_MCT8_MDC1_P2_Bisphenol Z+Bisphenol AF/*.xls` | Real raw plate data. Legacy OLE2 `.xls` — openpyxl **cannot** read it, so it contributes filename only (`first_rows=None`) while still becoming a File entity. |
| `.../220407_.../*.prism` | The matching GraphPad processed file, so the raw/processed role split is exercised on a real pair. |
| `Assay_MCT8-MDCK1/Study wide processed data/Data for statistical analysis/Krebs et al (2018) ...(ALTEX).pdf` | Keeps the **study-wide** processed tier present — a level `svhps26_real_input/` has no equivalent of — and is a cited methods paper rather than data. |

The chosen experiment directory carries three hazards in one path: a space, a
`+`, and a directory name (`MDC1`, `Bisphenol Z+Bisphenol AF`) that **disagrees**
with the filenames inside it (`MDCK1`, `Bisphenol-Z+BPAF`).

## Deliberately excluded

- The study-wide GraphPad `.pzf` files (54 MB, 25 MB, 20 MB) — bulk only.
- The other 25 experiment directories — each is another copy of the same shape.
- The file/directory name collision under `221027_..._P1_BPA+THinhibitors/`
  (a `.prism` file sitting beside a directory of the same stem). It is the one
  genuinely unique structural signal left on the table; it costs **+3.58 MB**,
  so it was left out pending a call on repo weight.

## Notes for anyone writing assertions against this fixture

- **There is no SOP.** `README.txt` section 4 refers to a standard operating
  procedure that is not in the deposit. The only PDF is a third-party paper.
  Do not let the assay protocol be modelled from that PDF.
- **CAS numbers in the workbook use an en dash** (`33889-69-9` written with
  U+2013) in 10 of 15 rows, not a hyphen. This is a real ingestion hazard.
- The workbook contains its own spelling variants (`Sunitib Malate`,
  `Dastinib`, `desipramine` vs. the folder's `Desipramide`). Mirror them
  verbatim; do not "correct" them.
- Token gating for the extraction-leaf stub must be **re-measured** against this
  deposit. None of the `svhps26_real_input` tokens occur here.
