# Slice 3a: Chemical Lookup Tools (PubChem)

## What to build

A wrapper tool around the existing `lookups/pubchem.py` module. Given a compound name, CAS RN, or PubChem CID, lookup and return SMILES, InChIKey, molecular formula, and molecular mass. Multi-strategy: try name first, then CAS, then CID.

Return shape: `{found: bool, data: dict, error: str | None}`. Never throw exceptions. LRU cached with configurable max size. Rate-limited to avoid API throttling.

## Acceptance criteria

- [ ] `lookup_compound(name: str) -> CompoundData` returns found/data/error
- [ ] Multi-strategy fallback: name -> CAS -> CID
- [ ] Returns SMILES, InChIKey, formula, mass when found
- [ ] Returns `{found: False, error: "not found"}` when all strategies fail
- [ ] LRU cache (configurable, default 100 entries)
- [ ] Rate limiting (configurable, default 5 req/s)
- [ ] All existing `lookups/pubchem.py` tests pass (or are adapted)
- [ ] No fabricated data — if not found, return not found

## Blocked by

None — can start immediately. The `lookups/pubchem.py` module already exists.