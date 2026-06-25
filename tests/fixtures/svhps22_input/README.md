# S-VHPS22 — TPO inhibition dose-response screen

A cell-based in vitro assay screening a reference chemical for its capacity to
inhibit thyroid peroxidase (TPO) activity, using a TPO-overexpressing follicular
cell model. This folder is a richer structured-metadata input than S-VHPS21: it
names a clear compound, a clear cell line, a protocol, and ships both raw and
processed data so an agent must draft several distinct domain entities.

- Accession: S-VHPS22
- DOI: 10.6019/S-VHPS22
- Author: Marije Vonk (ORCID 0000-0002-1825-0097), Universiteit Utrecht
- Organization: Universiteit Utrecht
- Assay: TPO inhibition dose-response assay
- Cell line: FRTL-5 TPO-overexpressing rat thyroid follicular cells
- Compound: Methimazole (reference TPO inhibitor)
- Protocol: Amplex Red fluorometric TPO activity readout

## Files

- `raw_data/dose_response_raw.csv` — per-well fluorescence at each dose.
- `processed_data/ic50_results.csv` — fitted IC50 for the compound.

This is a minimal, synthetic research folder used as a deterministic, offline
input fixture for the A/B evaluation harness (Issue #179). No real experimental
data is committed.
