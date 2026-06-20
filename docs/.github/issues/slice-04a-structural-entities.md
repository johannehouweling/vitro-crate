# Slice 4a: Structural Entity Drafting (Investigation, Study, Assay, LabProcess, LabProtocol, Sample)

## What to build

Drafting tools for the structural (ISA) layer of entities:

- `draft_investigation(hints)` — Investigation metadata (title, description, identifier)
- `draft_study(investigation_id, hints)` — Study metadata linked to parent Investigation
- `draft_assay(study_id, hints)` — Assay metadata linked to parent Study
- `draft_process(assay_id, process_type, hints)` — LabProcess (CellCulture, Exposure, EndpointReadout, DataAnalysis) bound to an Assay
- `draft_protocol(hints)` — LabProtocol (procedure, parameters)
- `draft_sample(hints)` — Sample metadata

Each drafter takes hints (from conversation, scanned file names, or metadata files), calls the LLM if needed, and optionally enriches via lookups. Never fabricates identifiers — if a lookup fails, the field is left empty and noted. Each entity includes `_completion` and `_provenance` metadata.

## Acceptance criteria

- [ ] `draft_investigation(hints)` creates Investigation entity in CrateState with _completion tracking
- [ ] `draft_study(investigation_id, hints)` creates Study linked to existing Investigation
- [ ] `draft_assay(study_id, hints)` creates Assay linked to existing Study
- [ ] `draft_process(assay_id, process_type, hints)` creates LabProcess of the specified subtype linked to Assay
- [ ] `draft_protocol(hints)` creates LabProtocol entity
- [ ] `draft_sample(hints)` creates Sample entity
- [ ] Each entity has `_completion: {field_name: {status, source}}` — status is "filled", "missing", or "verified"
- [ ] Each entity has `_provenance: {created_by, reviewed_by, lookups_used}`
- [ ] No fabricated identifiers — empty field if lookup fails
- [ ] Tests: draft each entity type from hints, verify structure and completion metadata

## Blocked by

- Slice 1 (CrateState — entities stored here)
- Slices 3a, 3b, 3c (lookup tools for enrichment)