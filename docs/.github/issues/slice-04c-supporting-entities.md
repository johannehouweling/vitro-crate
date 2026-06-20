# Slice 4c: Supporting Entity Drafting (Person, Organization, Publication)

## What to build

Drafting tools for supporting entities:

- `draft_person(name, hints)` — Person metadata, enriched via ORCID lookup (name, affiliation)
- `draft_organization(name, hints)` — Organization metadata, enriched via ROR lookup (ROR ID, website)
- `draft_publication(doi, hints)` — Publication metadata, enriched via Crossref lookup (title, authors, journal, year)

These entities are referenced by Investigation and Study but don't contain domain-specific data.

## Acceptance criteria

- [ ] `draft_person(name, hints)` creates Person entity, calls ORCID lookup when ORCID iD provided
- [ ] `draft_organization(name, hints)` creates Organization entity, calls ROR lookup
- [ ] `draft_publication(doi, hints)` creates Publication entity, calls Crossref lookup
- [ ] Lookup enrichment is optional — entity can be drafted from hints alone
- [ ] No fabricated identifiers — if ORCID/ROR/Crossref fails, fields left empty
- [ ] Each entity has `_completion` and `_provenance` tracking
- [ ] Tests: draft with lookup enrichment, draft from hints only (no lookup)

## Blocked by

- Slice 1 (CrateState — entities stored here)
- Slice 3c (ORCID, ROR, Crossref lookups for enrichment)