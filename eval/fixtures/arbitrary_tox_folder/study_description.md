# TPO inhibition screen — study notes

These are my working notes for the in-vitro thyroid peroxidase (TPO) inhibition
study. This folder is the raw research material as it sat on disk before any
RO-Crate was built: a description, a methods write-up, a compound list, and the
measurement files. It is deliberately *arbitrary* — there is no metadata file,
just the documents a researcher actually keeps.

## What we did

We ran a cell-based in-vitro assay measuring whether a reference chemical inhibits
thyroid peroxidase (TPO) activity in a TPO-overexpressing rat thyroid follicular
cell line. The goal was a dose-response IC50 for the reference inhibitor as a
positive control for the assay.

- Study: TPO inhibition dose-response screen
- Test system: FRTL-5 TPO-overexpressing rat thyroid follicular cells
- Endpoint: TPO enzymatic activity (Amplex Red fluorometric readout)
- Reference compound: Methimazole (a known TPO inhibitor)

## People

- Lead researcher: Marije Vonk (ORCID 0000-0002-1825-0097)
- Affiliation: Universiteit Utrecht

## Folder contents

- `study_description.md` — this file.
- `methods_protocol.txt` — culture, exposure, readout and analysis protocol.
- `compounds.csv` — the chemicals used, with identifiers.
- `measurements/dose_response_raw.csv` — per-well raw fluorescence at each dose.
- `analysis/ic50_results.csv` — the fitted IC50 result for the compound.

No real experimental data is committed — the numbers are small and synthetic. This
folder is a deterministic, offline input fixture for the A/B evaluation harness
(Issue #179): a realistic *arbitrary* research folder that exercises the full
scan -> extract -> materialize -> assess path, not a pre-seeded backbone.
