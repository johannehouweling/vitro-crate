# Lookup services

> The external registries, the one result shape every lookup returns, and how lookups stay polite and fast.


All lookups follow a consistent pattern: return `{found: bool, data: dict, error: str | None}`, never throw, LRU cached, with rate limiting.

### Available Services
- **PubChem**: Name/CAS/CID → SMILES, InChI, formula, mass
- **CompTox (EPA)**: Name/CAS/InChIKey → DTXSID (the DSSTox anchor identifier)
- **Cellosaurus**: Accession (CVCL_xxxx) → name, species, disease, site, sex
- **AOP-Wiki**: AOP ID → full pathway graph (AOP, events, relationships)
- **OLS4 (generic)**: Free-text query + ontology short name → best-matching
  term IRI with a relevance score. Backs `lookup_bao_term` (BAO),
  `lookup_unit` (UO units), and `lookup_ontology_term` for any OLS-hosted
  vocabulary (EFO/OBI/NCIT/UBERON/ChEBI/…). `lookup_exact_term` asks the other
  question, the one term a given word names by its label or an exact synonym, for
  a stated organ, tissue or cell type ([the toolbox](toolbox.md)); it is not a tool of its own.
- **ORCID**: ORCID iD → name, affiliation, affiliation ROR
- **ROR**: Organization name → ROR ID, website URL, country. The build writes the
  country as a `PostalAddress` the Organization's `address` references, one per country
- **Crossref**: DOI → title, authors, journal, volume, ISSN, work type, year. The build
  writes the journal as a `Periodical` and the volume as a `PublicationVolume`, which
  the article is `isPartOf`
- **CORDIS**: EU grant agreement number → project acronym, title, framework programme,
  grant DOI and the European Commission's contribution (`lookups/cordis.py`). It is
  called for a declared funder's grant number, never chosen by the model

### Multi-Strategy Lookups
For chemicals, `lookup_compound` tries by name, then CAS, then the
**VHP4Safety Compound Wiki**, then **ChEBI** (via OLS4), rather than a hard
not-found. The Compound Wiki is a Wikibase instance curated for the compounds
these studies test: a hit carries the structure and a dereferenceable entity
IRI, where a ChEBI hit yields an ontology term and no structure, so it is
consulted first of the two. Its property numbers are instance-local and only
`tests/test_lookups_live.py` can catch a renumbering. If all fail, ask the user
for SMILES/InChI. The ChEBI fallback's
identity rides on **context-declared keys** so the `MolecularEntity` compacts
cleanly under RO-Crate 1.2 (Issue #243): the ChEBI CURIE on `chebiId`
(`schema:identifier`, mirroring `cas`/`pubchemCid`) and the dereferenceable
ontology IRI on `sameAs` as an `{"@id": …}` node. The legacy bare
`chebi_id`/`chebi_iri` keys were absent from the `@context` and failed the base
pass (and leaked into the HITL loop as unanswerable gaps), so they are gone.
A ChEBI-only `MolecularEntity` (no `pubchem_cid`) takes that resolvable ChEBI
**PURL** as its `@id` (`http://purl.obolibrary.org/obo/CHEBI_<n>`, derived from
the `sameAs` IRI or the `chebiId` CURIE by `_crate_mapping._chebi_purl`) rather
than a `#MolecularEntity_<eid>` fragment — preferring an externally resolvable
identifier the lookup actually produced over a local fragment (Issue #179, D5:
never fabricated; a compound with no `pubchem_cid` and no ChEBI identity still
falls back to the fragment). `pubchem_cid` (the PubChem compound URL) still wins
when both are present.

**A chemical is identified by its Compound Wiki address.** Where `resolve_compound`
found the chemical's item (the one item that states the InChIKey the lookup
returned, [the toolbox](toolbox.md)), the `MolecularEntity`'s `@id` is that item's address,
`https://compoundcloud.wikibase.cloud/entity/Q<n>`. The PubChem compound URL is the
`@id` only of a chemical the wiki has no item for, and the ChEBI PURL and the
fragment follow as above. The id must be one a lookup wrote, which its completion
record says (`_crate_mapping._compound_wiki_iri`): a `compoundwiki_id` a model or a
person wrote is still written as an identifier value and never becomes the `@id`
(D5). Nor does an address on the wiki that the chemical was given as its own id: the
chemical then keeps a local id, and a reference holding that id still reaches it.
The item a name search hits in `lookup_compound`'s fallback does not count
either; it stays the chemical's `sameAs`, and is the `@id` instead only when the
InChIKey search finds it too. The identifier values (CAS, PubChem CID, DTXSID,
Compound Wiki) are the same whichever address the chemical has. An unreachable wiki
reads as no item, so the chemical then keeps its PubChem address. A reference that
holds one of a chemical's addresses rather than its state id, such as the PubChem
address it had before the wiki named it, resolves to the chemical's node
(`_crate_mapping._compound_addresses`).

### Anti-Hallucination
The agent **never fabricates identifiers**. Every identifier is verified against its source. If verification fails, the field is cleared and the agent tries alternatives or asks the user.

**PubChem CID — verify against the authority's own answer (Issue #261).** A PubChem
CID is the *primary key of the PubChem record itself*. PubChem's
`/compound/name` endpoint (which `verify_identifier` re-queries) resolves *names*
and CAS synonyms but **not** a bare numeric CID, so routing a CID back through it
always missed — and D5 then cleared the very CID the authoritative name→CID lookup
had just returned, on *every* compound. `resolve_compound` therefore confirms a
`pubchem_cid` that equals the CID its primary `lookup_compound` returned directly
against that resolution (`composites._verify_compound_identifier`), marking it
`verified` rather than re-querying the wrong endpoint. A CID that is **not** the
lookup's own answer (a hint-supplied / stale value) — and every non-CID field such
as `cas` — still goes through the normal `verify_identifier`, which confirms
against source and clears an unconfirmable value (D5 preserved, CAS unchanged).
This is correct independent of the #252 warm cache, which previously only *masked*
the false negative on the single-call happy path.

### Concurrency & Per-Host Rate Limiting (Issue #62)
Independent lookups with no data dependency are issued **concurrently** via a
bounded `concurrent.futures.ThreadPoolExecutor` (max ~6 workers), so cold paths
no longer pay strictly-serial latency:

- `lookups/aopwiki.py:lookup_aop` builds the key-event entities deterministically
  (pathway order: MIE → KE → AO), then fetches each event's `_event_details`
  concurrently and merges them back **by identifier** — the assembled result is
  byte-identical to the serial path and order-independent.
- `builder/tools/verification.py:verify_all_identifiers` collects its work plan
  (entity, field) deterministically, runs the per-field verifications
  concurrently, and returns results in that fixed order. Each task mutates only
  its own field's status, so there is no cross-task contention.

Politeness is preserved by a **single per-host throttle** in `lookups/_http.py`
(`_HostRateLimiter` / `throttle_for_url`), applied inside `http_get_json`. It
enforces a minimum spacing (`_HOST_MIN_INTERVAL`, default 0.1s) between requests
to the *same* host — replacing the old per-client `time.sleep(0.1)` calls — and
honours that cap even when many workers fire at once. Different hosts are
throttled independently, so parallel lookups across services are not serialised.
Both functions remain `lru_cache`d, so this only benefits cold paths.

### Compound Resolution Performance (Issue #252)
Left to itself, `resolve_compound` fans a single compound out to up to **six**
PubChem round-trips — name→JSON + synonyms for the lookup, then a *fresh*
re-resolution of the same compound for each of the CAS and PubChem-CID
verifications — so a concurrent burst multiplies 429 retry/backoff across all of
them. Three in-process levers in `builder/tools/_resolve_cache.py` bound that cost
without weakening D5 (identifiers still come from the authority and are verified):

- **Shared in-process cache** keyed by *normalized* name (strip + collapse
  whitespace + casefold), warmed with the resolved CAS / `CID <cid>` alias keys.
  `lookup_compound` consults it before any network work, so the two verify
  re-resolutions read the already-fetched authoritative record (6 round-trips →
  ~2) and a repeated compound is instant (0 round-trips). The alias keys hold the
  exact record PubChem returned, so verification still confirms against the
  authority's own answer.
- **Bounded concurrency gate** (`_ResolveConcurrency`, default 4) admits only a
  few resolves at once, so a burst does not all storm PubChem and trip its rate
  limiter — complementing the per-host throttle, which spaces each request.

> **Status: the shipped default no longer matches the bound stated below.**
> `DEFAULT_RESOLVE_TIMEOUT` (`builder/tools/_resolve_cache.py`) is currently
> `240.0` seconds — four times the stall the bound exists to cut short — so the
> 20s figure is the intended budget, not the one in force.

- **Per-compound timeout** (`run_with_timeout`, default 20s) bounds the lookup; on
  expiry `resolve_compound` returns a graceful `{"ok": False, …timeout…}` partial
  result and creates no entity, rather than hanging ~60s on a stuck round-trip.
