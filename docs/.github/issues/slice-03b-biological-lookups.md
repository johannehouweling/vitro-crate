# Slice 3b: Biological Lookup Tools (Cellosaurus, AOP-Wiki, BAO/OLS)

## What to build

Wrapper tools around the existing `lookups/cellosaurus.py`, `lookups/aopwiki.py`, and `lookups/bao.py` modules:

- **Cellosaurus**: Given a CVCL accession, return cell line name, species, organ/tissue, disease, sex
- **AOP-Wiki**: Given an AOP ID, return the full pathway graph (key events, relationships) as structured data
- **BAO/OLS**: Given a free-text query, return the best-matching ontology term with IRI

Each returns `{found: bool, data: dict, error: str | None}`. LRU cached, rate-limited.

## Acceptance criteria

- [ ] `lookup_cell_line(accession: str) -> CellLineData` — CVCL_xxxx -> name, species, organ, disease, sex
- [ ] `lookup_aop(aop_id: str) -> AOPData` — AOP ID -> events, relationships, structured graph
- [ ] `lookup_bao_term(query: str) -> TermData` — free text -> best-matching term + IRI
- [ ] All three follow `{found, data, error}` return shape
- [ ] LRU cache and rate limiting per service
- [ ] Existing lookup module tests pass (or are adapted)

## Blocked by

None — can start immediately. The `lookups/cellosaurus.py`, `aopwiki.py`, and `bao.py` modules already exist.