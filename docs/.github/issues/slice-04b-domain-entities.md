# Slice 4b: Domain Entity Drafting (MolecularEntity, CellLineSample, Process subtypes)

## What to build

Drafting tools for the domain (ISA-Tox) layer of entities:

- `draft_molecular_entity(name, hints)` — Compound metadata, enriched via PubChem lookup (SMILES, InChIKey, CAS, formula)
- `draft_cell_line_sample(name, hints)` — Cell line metadata, enriched via Cellosaurus lookup (species, organ, disease, sex)

These are the toxicology-specific entities that require domain lookups to fill correctly.

## Acceptance criteria

- [ ] `draft_molecular_entity(name, hints)` creates MolecularEntity in CrateState, calls PubChem lookup for enrichment
- [ ] SMILES, InChIKey, formula, CAS auto-populated from PubChem when found
- [ ] If PubChem lookup fails, fields left empty — no fabricated data
- [ ] `draft_cell_line_sample(name, hints)` creates CellLineSample, calls Cellosaurus lookup for enrichment
- [ ] Species, organ, disease, sex auto-populated from Cellosaurus when found
- [ ] If Cellosaurus lookup fails, fields left empty — no fabricated data
- [ ] Each entity has `_completion` and `_provenance` tracking
- [ ] Multi-strategy for chemicals: name -> CAS -> CID
- [ ] Tests: draft with valid name (lookup succeeds), draft with unknown name (lookup fails, fields empty)

## Blocked by

- Slice 1 (CrateState — entities stored here)
- Slice 3a (PubChem lookup for MolecularEntity)
- Slice 3b (Cellosaurus lookup for CellLineSample)