# Lookup services

> The external registries, the one result shape every lookup returns, and how lookups stay polite and fast.

Every lookup returns `{found: bool, data: dict, error: str | None}`, never throws and never returns
`None`, and is LRU cached and rate limited.

### Available Services
- **PubChem**: Name/CAS/CID → SMILES, InChI, formula, mass
- **CompTox (EPA)**: Name/CAS/InChIKey → DTXSID
- **Cellosaurus**: Accession (CVCL_xxxx) → name, species, disease, site, sex
- **AOP-Wiki**: AOP ID → pathway, events, relationships
- **OLS4**: query + ontology → best-matching term IRI and score; backs `lookup_bao_term`,
  `lookup_unit` and `lookup_ontology_term`. `lookup_exact_term` (not a tool) finds the one term a
  word names by label or exact synonym ([the toolbox](toolbox.md)).
- **ORCID**: ORCID iD → name, affiliation, affiliation ROR
- **ROR**: name → ROR ID, website, country (built as one `PostalAddress` per country)
- **Crossref**: DOI → title, authors, journal, volume, ISSN, type, year (journal and volume built
  as `Periodical` / `PublicationVolume`, which the article `isPartOf`)
- **CORDIS**: EU grant number → acronym, title, programme, grant DOI, EC contribution
  (`lookups/cordis.py`); called for a declared funder's grant, never chosen by the model

### Multi-Strategy Lookups
`lookup_compound` tries name, then CAS, then the **VHP4Safety Compound Wiki** (carries structure
and an entity IRI), then **ChEBI** via OLS4; if all fail, ask the user for SMILES/InChI. The wiki's
property numbers are instance-local; only `tests/test_lookups_live.py` catches a renumbering.
ChEBI identity rides on context-declared keys (#243): the CURIE on `chebiId`, the IRI on `sameAs`
as `{"@id": …}` — never bare `chebi_id`/`chebi_iri`, which fail the base pass.

**A chemical's `@id`**, in order: its Compound Wiki item
(`https://compoundcloud.wikibase.cloud/entity/Q<n>`) where `resolve_compound` found the one item
stating the lookup's InChIKey ([the toolbox](toolbox.md)); else the PubChem compound URL; else the
ChEBI PURL (`_crate_mapping._chebi_purl`); else the `#MolecularEntity_<eid>` fragment. The wiki id
counts only if a lookup wrote it, per its completion record (`_crate_mapping._compound_wiki_iri`,
D5): a model- or person-written `compoundwiki_id`, an address given as the chemical's own id, or a
name-search hit (kept as `sameAs`) never becomes the `@id`. An unreachable wiki reads as no item.
Identifier values are the same whatever the address, and a reference holding any of a chemical's
addresses resolves to its node (`_crate_mapping._compound_addresses`).

### Anti-Hallucination
The agent **never fabricates identifiers**. Every identifier is verified against its source; on
failure the field is cleared and the agent tries alternatives or asks the user.

**PubChem CID — verify against the authority's own answer (#261).** PubChem's `/compound/name`
endpoint does not resolve a bare CID, so re-verifying through it would clear every CID.
`resolve_compound` confirms a `pubchem_cid` equal to its own `lookup_compound` answer directly
(`composites._verify_compound_identifier`); any other CID, and every other field, goes through
`verify_identifier`.

### Concurrency & Per-Host Rate Limiting (Issue #62)
Independent lookups run on a bounded `ThreadPoolExecutor` (~6 workers), with results identical to
the serial path: `lookups/aopwiki.py:lookup_aop` merges event details by identifier, and
`verify_all_identifiers` returns results in its deterministic plan order, each task touching only
its own field. Politeness is one per-host throttle in `lookups/_http.py` (`_HostRateLimiter` /
`throttle_for_url`, inside `http_get_json`): `_HOST_MIN_INTERVAL` (0.1s) between requests to the
same host, however many workers; hosts are throttled independently.

### Compound Resolution Performance (Issue #252)
`builder/tools/_resolve_cache.py` bounds `resolve_compound`'s PubChem round-trips without weakening
D5:

- **Shared in-process cache** keyed by normalized name and warmed with CAS / `CID <cid>` aliases
  holding PubChem's exact record, so verification reads the authority's own answer.
- **Bounded concurrency gate** (`_ResolveConcurrency`, default 4), so a burst does not trip
  PubChem's rate limiter.

- **Per-compound timeout** (`run_with_timeout`, `DEFAULT_RESOLVE_TIMEOUT`, 240 s): on expiry
  `resolve_compound` returns `{"ok": False, …timeout…}` and creates no entity.
