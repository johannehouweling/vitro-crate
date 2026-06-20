# Slice 3c: Identity Lookup Tools (ORCID, ROR, Crossref)

## What to build

Wrapper tools around the existing `lookups/orcid.py`, `lookups/ror.py`, and `lookups/crossref.py` modules:

- **ORCID**: Given an ORCID iD, return person name, affiliation name, and affiliation ROR
- **ROR**: Given an organization name, return ROR ID and website URL
- **Crossref**: Given a DOI, return title, authors, journal, publication year

Each returns `{found: bool, data: dict, error: str | None}`. LRU cached, rate-limited.

## Acceptance criteria

- [ ] `lookup_orcid(orcid_id: str) -> PersonData` — ORCID iD -> name, affiliation, affiliation ROR
- [ ] `lookup_ror(name: str) -> OrgData` — organization name -> ROR ID, website URL
- [ ] `lookup_doi(doi: str) -> PublicationData` — DOI -> title, authors, journal, year
- [ ] All three follow `{found, data, error}` return shape
- [ ] LRU cache and rate limiting per service
- [ ] Existing lookup module tests pass (or are adapted)

## Blocked by

None — can start immediately. The `lookups/orcid.py`, `ror.py`, and `crossref.py` modules already exist.