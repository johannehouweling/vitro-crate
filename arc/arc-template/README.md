# VHP4Safety ARC template

Annotated Research Context (ARC) folder structure for a VHP4Safety study.
ARC organization principle: https://arc-rdm.org/details/organization-principle/

## How to use
1. Copy this folder; rename it to your accession (e.g. `S-VHPS27_arc`).
2. Fill `S-VHPSxx.pagetab.json` — the BioStudies submission record (the
   study/investigation metadata). Replace every `[DRAFT]`; delete any subsection
   you don't need. Field conventions: VHP4Safety `biostudies_metadata` repo.
3. For each assay (measurement), copy `assays/assay_1`, rename it, fill its
   `ToxTemp_<assay>.md` (test-method description) and drop data into `dataset/`.
4. Shared protocols/resources go at STUDY level; assay-specific protocols at
   ASSAY level.

## Layout (authoritative metadata vs generic ARC tables)
    S-VHPSxx.pagetab.json     BioStudies PageTab record — study/investigation metadata  [authoritative]
    isa.investigation.xlsx    generic ARC investigation table                            [optional]
    studies/<study>/
        isa.study.xlsx        generic ARC study table                                    [optional]
        protocols/            SHARED protocols: starting material/data -> samples
        resources/            external data the study references (papers, reference sets)
    assays/<assay>/
        ToxTemp_<assay>.md    test-method description (ToxTemp)                           [authoritative per assay]
        isa.assay.xlsx        generic ARC assay table                                     [optional]
        dataset/raw_data/         raw instrument output
        dataset/processed_data/   analysed results
        protocols/            assay-specific protocols: samples -> measurement
    workflows/<wf>/           reusable analysis scripts/tools + their environment
    runs/<run>/               parameters + inputs for one execution of a workflow

## From ARC to RO-Crate
    uv run rocrate-wizard pagetab S-VHPSxx.pagetab.json --data-root <this folder> --out-dir out/
projects this onto an ISA-Tox RO-Crate and validates it.
