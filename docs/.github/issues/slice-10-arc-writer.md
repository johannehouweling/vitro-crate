# Slice 10: ARC/ROCrate Writer (Output)

## What to build

The output writer that projects completed CrateState entities onto the VHP4Safety ARC template (`arc/arc-template/`) and produces the final ARC directory.

The ARC directory *is* the RO-Crate — `ro-crate-metadata.json` lives at its root and describes every entity. The output structure follows:

```
<accession_arc>/
├── ro-crate-metadata.json     RO-Crate metadata
├── studies/<study>/
│   ├── protocols/
│   └── resources/
├── assays/<assay>/
│   ├── ToxTemp_<assay>.md     test-method description
│   ├── dataset/
│   │   ├── raw_data/
│   │   └── processed_data/
│   └── protocols/
├── workflows/<wf>/
└── runs/<run>/
```

The ToxTemp markdown files are derived from LabProcess metadata. Protocols are exported from LabProtocol entities. Raw and processed data files are placed under `dataset/` based on their binding to the process.

## Acceptance criteria

- [ ] `write_arc(crate_state, output_path)` produces the full ARC directory tree
- [ ] `ro-crate-metadata.json` at the root with all entities described
- [ ] Studies and assays mapped to ARC subdirectories
- [ ] ToxTemp_<assay>.md generated for each assay from LabProcess metadata
- [ ] Protocols exported from LabProtocol entities
- [ ] Data files placed in dataset/raw_data/ or dataset/processed_data/
- [ ] The ARC directory is a valid RO-Crate (can be validated by rocrate_validator)
- [ ] Tests: write ARC from populated CrateState, verify directory structure, validate output crate

## Blocked by

- Slice 1 (CrateState — source of all entity data)
- Slice 6a (crate assembly — entity relationships for ARC placement)