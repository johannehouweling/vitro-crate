# The Agent Toolbox

> Every LLM-callable tool, grouped by purpose. Guarded by tests/test_agents_doc_toolbox.py: a tool named in a code fence here must exist, and every specced tool must be named here.

> **Tool-registration contract:** every new LLM tool is registered in **four** lockstep places —
> `TOOL_REGISTRY`, `TOOL_SPECS`, the system-prompt "## Your Tools" catalogue, and this doc
> (`tests/test_agents_doc_toolbox.py`: no phantom tool, no omitted one). The registry ⇄ specs half
> is enforced by `assert_tool_spec_parity()` (`tools_spec.py`, #327) when the ReAct loop builds its
> tools, and in CI by `tests/test_tools_spec.py`: `expected_tool_spec_names()` is the registry plus
> the engine-routed tools declared once in `_LLM_TOOLS_OUTSIDE_REGISTRY`. A tool with no schema, or
> a schema for a tool the engine cannot run, fails fast. That assert is also the reachability
> guard: an advertised tool is one the model can always reach.

### File Tools
*The scanner/sampler triad and the archive tools are engine-routed (in `TOOL_SPECS`, not in
`TOOL_REGISTRY`); the full readers are registry tools.*
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20, mode: str = "content") → str | None
  mode: "content" (first `lines` lines), "summary" (file-type-aware), "overview" (metadata + summary)
  a directory returns an actionable "use list_scanned_files" message, never a silent None (#240)
read_multiple_files(paths: list[str], lines: int = 50, mode: str = "content") → dict
read_file(path: str) → str | None             # full read by extension; text/JSON complete up to 64 KiB, else the "[truncated … do not re-read]" marker; a directory gets the list_scanned_files guidance
read_excel(path: str) → str | None            # .xlsx → pipe-delimited text
read_docx(path: str) → str | None             # .docx → plain text
extract_pdf_text(path: str) → str             # [Page N] text, tables, image metadata
preview_archive(path: str) → ArchivePreview   # list zip/tar(.gz) members without extracting
unzip_file(path: str, output_dir: str | None = None) → {extracted_to, entry_count, message} | {error, message}
```
`scan_files`, `read_file_sample` and `read_multiple_files` also run at initialization. `unzip_file`
refuses an unsafe archive with an error dict rather than raising — a member escaping the
destination (Zip-Slip) or a total uncompressed size over `_MAX_UNCOMPRESSED_BYTES` (zip-bomb) —
and the agent calls `present_to_human` before extracting a large archive.

### Entity Drafting Tools
```
scaffold_isa_backbone(investigation=None, study=None, assay=None, validate_base=False) → dict  # linked Investigation→Study→Assay in one call; a reused layer's EMPTY fields are filled from hints (fill, don't clobber)
resolve_aop(aop_id: str, study_id: str | None = None) → {aop_id, aop_entity_id, events, events_detail, wired_to_study} | {ok: False, error}  # one AOP-Wiki id → ONE AdverseOutcomePathway entity
link_assay_to_key_event(assay_id: str, event_name: str | list[str]) → {ok, assay_id, key_event_ids, matched_names, unmatched?, unlisted_pathways?} | {ok: False, error, candidates, unlisted_pathways?}  # Assay → the Key Event(s) it MEASURES
resolve_compound(name: str, hints: dict | None = None, verify=None) → {entity_id, name, identifiers, verifications, verified, source}  # name → lookup → draft → verify (+ DTXSID)
resolve_cell_line(name: str, hints: dict | None = None, catalog_name: str | None = None, verify=None, made_from: str | None = None) → {entity_id, name, accession, match, query, verifications, verified, source}  # name → Cellosaurus; a miss still mints
resolve_characteristic_terms(entity_id: str | None = None) → {found, not_found, unavailable}  # stated organ / tissue / cell_type → its UBERON / CL term
resolve_publication(title: str, verify=None) → {ok, doi, entity_id, title, score} | {ok: False, reason, title}  # title → Crossref → confidence gate → draft_publication_with_authors
draft_publication_with_authors(doi: str) → {publication_id, doi, authors:[{name, person_id, orcid, resolution}], hitl}  # engine-routed, HITL-capable
draft_investigation(hints: dict) → Entity
draft_study(investigation_id: str, hints: dict) → Entity
draft_assay(study_id: str, hints: dict) → Entity
draft_molecular_entity(name: str, hints: dict) → Entity
draft_cell_line_sample(name: str, hints: dict) → Entity
draft_sample(hints: dict) → Entity                       # material input/output in the derivation chain
draft_process(assay_id: str, process_type: str, hints: dict) → Entity
draft_protocol(hints: dict) → Entity                     # what the lab states about a step's protocol (D17)
draft_person(name: str, hints: dict) → Entity            # see below: name split, ORCID
draft_organization(name: str, hints: dict) → Entity      # ROR id only as the deposit states it, once ROR returns it (D5)
draft_publication(doi: str, hints: dict) → Entity
draft_defined_term(name: str, hints: dict) → Entity
draft_property_value(name: str, hints: dict) → Entity
draft_file(name: str, path=None, role=None, encoding_format=None, additional_types=None, programming_language=None) → Entity
```
**Hints are typed per entity type** (#90): each `draft_*` advertises
`_crate_mapping.draft_hints_schema(type)`, built from `ENTITY_DRAFT_SCHEMA`; its reference keys are
a strict subset of `_REF_FIELDS` (asserted by test). The schema is open
(`additionalProperties: true`).

`draft_person` derives `givenName`/`familyName` from `name` when neither is given ("Last, First"
inverted; a lone token kept as a family-name candidate, never placed in `givenName`), so every
Person is ISA-conformant. Without an `orcid` hint it takes the ORCID the deposit states for exactly
that name, else a single strong name-search hit, once ORCID confirms it (D5). The tools the model
calls, `draft_person_tool` and `draft_organization_tool`, give an `orcid` or `ror` the model hands in
the same check before drafting: it is kept only once its registry returns the record. Other callers
pass identifiers a lookup returned.

`draft_defined_term` persists a looked-up term as a `schema:DefinedTerm`; pass the looked-up IRI as
the `url`/`@id` hint so it is a referenceable `mentions` / `measurementMethod` / `sampleType`
target. `draft_property_value` creates a `schema:PropertyValue` (`value`, optional `propertyID`,
`unitText`/`unitCode`); for one named `DOI` or `PubMedID` it defaults and `@id`-wraps the
`propertyID` (`OBI_0002110` / `OBI_0001617`), as the tox `{10,11}_*_property_value.ttl` shapes
require — a bare string is a Violation.

**AOP (`resolve_aop`, `link_assay_to_key_event`).** The crate carries the pathway a Study addresses
and the key event an Assay measures, **and nothing else of a pathway**: other key events,
`KeyEventRelationship`s and link properties stay on AOP-Wiki. The writer enforces this, so a
session saved with a whole pathway builds the same crate. Each node is keyed by its AOP-Wiki IRI
and carries only what the lookup gave (D5); both live in `aop_entities` and build as
`ContextEntity` nodes typed by their AOP class (MIE/KE/AO share `@type KeyEvent`, told apart by
`eventType`).
- `resolve_aop` wires the pathway onto the Study named (by state id or crate id), or the sole
  Study; with several Studies and no name it wires nothing, and `verify_isa_reachability` reports
  the island (#738). `events_detail` lists the events for `link_assay_to_key_event`.
- `link_assay_to_key_event` matches each name independently against the events `lookup_aop` lists
  for the pathway(s) in state and the KeyEvents already there; it stores the ONE event a name
  matches, with its AOP-Wiki id, and writes **nothing** for zero or several matches — which event an
  assay measures is a scientific claim (D5). Links accumulate deduplicated; removing one is
  `set_fields`' job. A pathway AOP-Wiki gave no events for is named under `unlisted_pathways`, and
  `lookup_aop` caches no transient failure.

**`resolve_compound`** fuses `lookup_compound` (PubChem → Compound Wiki → ChEBI) →
`draft_molecular_entity` → `verify_identifier`. A lookup miss returns `{ok: False, error}` and
mints nothing. `verify_identifier` clears any value that does not resolve (D5); `verified` is the
AND of `verifications`. Looked-up fields win over same-named hints. A best-effort CompTox
`lookup_dtxsid` (by `cas` → `inchikey` → name) adds the DTXSID; it is non-fatal, and no other
composite produces one. **Dedup is by chemical identity, not name**: `pubchem_cid` → `inchikey` →
`cas` → `chebiId`, so two names for one molecule are one node (the synonym an `alternateName`) and
never two entities minting one `@id`, which ro-crate-py silently overwrites; without an identity
field it reuses by name. Two chemicals that still mint one `@id` at build are one node
(`_crate_mapping._add_leaves`): the first describes it, later ones add only an `alternateName`.
A `pubchem_cid` equal to the lookup's own CID is verified against that resolution
(`_verify_compound_identifier`; see [lookup services](lookups.md)).

**`resolve_cell_line`** is the name→Cellosaurus path; `draft_cell_line_sample` directly skips the
lookup and leaves no accession. (1) `lookup_cell_line_by_name` on the full name, then on
`catalog_name`, each under that lookup's unmodified exact+unique rule (tier reported as `match`);
(2) `lookup_cell_line` on the accession **is** the verification, status set directly. A transient
step-2 failure keeps the accession unverified; a definitive miss clears it. A primary-cell source
(`hints.source_kind="primary cells"`, or a reused entity recording it) skips both: Cellosaurus holds
no record for primary cells, so any hit would be another entity.
- **A miss is not a failure** (unlike `resolve_compound`): a name-only `CellLineSample` is a valid
  Sample, and refusing would drop the line and everything that references it. Always mint; read
  `accession` / `match` (no `ok` key). `name` stays as the documents word it; the Cellosaurus label
  goes to `alternateName`.
- A `catalog_name` shaped like an accession is refused, as is any identifier-bearing hint, so every
  id committed came from Cellosaurus in the call. Reuse: by accession first (it drives the `@id`,
  so two names for one line would collide), then by name-derived id. Only `accession` /
  `alternateName` / `url` / `sameAs` / `taxonomicRange` are persisted (organism as an NCBITaxon
  `DefinedTerm`); `_CELL_LINE_DROPPED_FIELDS` records what else the record offers and why each
  would fail.
- **`made_from`**: cells differentiated from a line are a `Sample` that `derives_from` the line and
  states its own `cell_type` / `tissue` / `organ`; the result gives the Sample as `entity_id` and the
  line as `line_id`. The population is never looked up; an accession passed as `made_from` is
  ignored (D5). Each population is the `result` of its own TestSystemPreparation. Without
  `made_from`, two names resolving to one accession are one line **unless they state different
  cells**: then nothing is merged, the second is kept without the accession, and `fix` says to use
  `made_from`. Both get their characteristic terms in the same call.

**`resolve_characteristic_terms`**: the stated word stays the characteristic's `value`; the term is
its `valueReference` (`DefinedTerm`: IRI, label, CURIE). There is a term only when the word names
exactly one current term of the field's ontology (`_crate_mapping.CHARACTERISTIC_ONTOLOGIES`: UBERON
for `organ`/`tissue`, CL for `cell_type`) by its label (case and spacing aside) or an **exact**
synonym; a plural is retried once as its singular. Nothing is matched loosely — a near match beside
a stated value is an invented claim (D5); `lookups.bao.lookup_exact_term` re-checks the service's
answer. The term is held in `<field>_term` with `verified`/`lookup` status and `confirmedFor`, and
`_crate_mapping.confirmed_term` is the one reader: a term a model or person wrote, or one for an
earlier wording, is never written and is removed. These fields are `field_kinds.IDENTIFIER_FIELDS`
and refused as `resolve_cell_line` hints. A stated species is resolved the same way in NCBITaxon
(`STATED_TERM_ONTOLOGIES`) into `taxonomicRange`. An unreachable service is no answer: the word is
`unavailable` and retried later.

**`resolve_publication`**: a title alone cannot satisfy ISA's identifier or the root `citation`
`@id`, and no DOI is guessed from it. It runs a Crossref title search
(`lookups.crossref.search_works_by_title`) and commits a candidate only when it clears **both** the
score floor and a near-exact normalized-title match; otherwise `{ok: False, reason: "no confident
DOI match", title}` and no entity. A match delegates to `draft_publication_with_authors`.
Idempotent by DOI.

**`draft_publication_with_authors`** is the only HITL-capable drafter (`takes_human=True`; the
engine injects `human_interface`). For each author it creates/reuses a `Person` wired as `author`,
with its `@id` harmonized to an ORCID by a cascade that stops at the first success: (a) the Crossref
ORCID; (b) an in-crate Person with a verified ORCID matching family name + given/initial; (c) the
ORCID the deposit states for exactly that name (as `draft_person`), else a public ORCID search
(`lookups.orcid.lookup_orcid_by_name`; unreachable is no answer); (d) a synthesized
`#CitationAuthor_<Given>_<Family>`.
- The search auto-accepts only a **single strong** match (family + full given name) that passes
  verification; anything else escalates to HITL (ranked candidates + none/skip, optional pasted
  ORCID). HITL fires only on genuine ambiguity; with no `human_interface` an ambiguous author falls
  back to (d).
- D5: an ORCID from (a), (c) or HITL is attached only after `lookup_orcid` resolves it and the
  family name matches. A Person already holding that ORCID is the author, so no two people share an
  address.
- `affiliation` is an `Organization` reference, never a string (ISA Violation): find-or-drafted from
  the ORCID record, preferring its ROR. One without a ROR takes the ROR record that registers
  exactly that name (`registered_organization` over `lookups.ror.find_registered`; no acronyms, and
  only when one record does); otherwise it keeps its name and no ROR. Authors sharing an affiliation
  share one Organization.
- `jobTitle` is likewise a `DefinedTerm` reference (#837): `_wire_job_title` writes one code-less
  term per distinct title; a `jobTitle` already naming a term is wired as is.

**Process and sample hints.** `LabProcess` hints advertise `units` (each `ParameterValue` carries
its `unitText`) and subtype parameters (`assay_kit`/`substrate` for EndpointReadout,
`acceptance_criteria`/`evaluation_criteria` for DataAnalysis). `LabProcess.parameters` is a list of
`{name, value, unit}` (`NAMED_PARAMETERS_SCHEMA`, read by `named_parameters`) for any parameter the
deposit names: written as a `ParameterValue` under the stated name with no `propertyID`, never
twice for a name a typed field wrote, and omitted when empty. A `LabProcess`'s
`additionalProperty` resolves only to PropertyValues already in state or bare IRIs — a score is
never computed (D5).

A source of cells' `passage` / `growth` / `organ` / `tissue` / `cell_type` hints, the `supplier` /
`catalogue_number` / `lot_number` of commercial cells, and the `donor_code` / `number_of_donors` /
`donor_age` / `donor_health_status` of primary cells become ISA Sample Characteristics
(`additionalProperty` PropertyValues with the property's IRI when known; `cell_type` carries
EFO:0000324). The builder writes exactly the characteristics the specification lists, and a test
holds the two lists together. They apply to sources of cells only; donor `sex` is the BioSample's
own property.

**Identifier PropertyValues** (deterministic build path). `_crate_mapping._identifier_pv` mints a
stable `#param_<slug(name)>_<sha1("name|value")[:10]>` with `propertyID` as an `{"@id"}` node: a
Person's `orcid` → `ORCID` (`https://orcid.org`); a MolecularEntity's `cas`/`casrn`/`cas_number`,
`pubchem_cid`, `dtxsid` → `[CAS, PubChem CID, DTXSID]` in that order (CAS has no `propertyID`), and
`compoundwiki_id` → `Compound Wiki`. `resolve_compound` finds the wiki id by an exact InChIKey
statement search and keeps none when two items state one InChIKey (D5). A caller-supplied
`compoundwiki_id` — or the wiki's answer for a caller-supplied InChIKey — is recorded as the
caller's; only the answer for the lookup's own InChIKey is looked up, and only that becomes the
chemical's `@id` ([lookup services](lookups.md)). `Person.affiliation` and
`ScholarlyArticle.author` are `{"@id"}` references (`_wire_reference` / `_resolve_many`). Only
values already in state are wired (D5).

**`draft_file`** derives `encodingFormat` from the extension (`name`, then `path`) via the
scanner's scientific-format MIME registry when omitted; an explicit value wins, an extensionless
name leaves it unset. `additional_types` co-types beyond `File` (always first, duplicates dropped).
A source-code extension (`document_discovery.SCRIPT_SUFFIXES`) is always built as
`[File, SoftwareSourceCode, LabProtocol]`; `programming_language` adds `schema:programmingLanguage`.

### Entity Management Tools
```
set_fields(entity_id: str, fields: dict, source="llm") → Entity
set_crate_metadata(title=None, description=None, accession=None, release_date=None, date_modified=None, publisher=None, creator=None, contact=None, license=None) → {title, description, accession, release_date, date_modified, publisher, creator, contact, license, note?}  # Root Data Entity scalars
remove_entity(entity_id: str, cascade: bool = False) → {removed, entity_id, detached, discarded_fields, warning}
list_entities(entity_type: str | None) → [Entity]
list_scanned_files(name_contains=None, mime_contains=None, offset=0, limit=200) → {total_scanned, matched, files:[{path, filename, size, mime_type}]}
```
`set_fields` is the single mutation tool (#90); `update_entity` / `bulk_set_fields` /
`set_entity_field` remain as deprecated library aliases, not exposed to the LLM. It refuses an
`orcid` or `ror` from the model: each becomes the entity's `@id`, so it goes through `draft_person`
/ `draft_organization`, which confirm it with the registry (D5).

`set_crate_metadata` writes `CrateState.metadata` (root, `./`): `title` / `description` /
`accession` → `name` / `description` / `identifier`; `release_date` / `date_modified` →
`schema:releaseDate` / `schema:dateModified`. Only non-empty arguments are written — a date is
never fabricated (D5) — and ro-crate-py's auto-set `datePublished` is untouched. `publisher` /
`creator` / `contact` must be a Person/Organization in state or a verified ORCID/ROR IRI; a bare
name is refused (D5). `license` yields to one read from the deposit (`license_from_deposit`, #535),
reported under `note`. A call with every field empty is refused; `get_status` reads metadata.

`list_scanned_files` is the full inventory from `CrateState.scanned_files` (the `scan_files` output
is a sample and is pruned from history, D12): paginated, filterable, no `first_rows`.

`remove_entity` preserves referential integrity: it refuses while any `_REF_FIELDS` reference
points at the target, naming the referrers, unless `cascade=True` clears them. `entity_id` is the
stable key; renaming changes `name` only. It returns a report because `cascade=True` can be quietly
destructive — clearing an Assay off its processes detaches them, which no shape catches and only
the ISA reachability check ([the decision records](../adr/README.md)) reports — so `detached` and
`discarded_fields` let the caller re-point the children.

### Derivation Chain Tools
```
draft_process_chain(assay_id: str, chain: [{process_type, hints?, object?, result?}], validate_after=None) → {assay_id, process_ids, steps, synthesized, skipped?}  # whole TestSystemPreparation→Exposure→EndpointReadout→DataAnalysis chain, idempotent
link(from_id: str, relation: str, to_id: str) → {from_id, relation, to_id}
attach_files(to: str, name_contains=None, mime_contains=None, paths=None, role=None) → {attached, file_ids, to}
check_provenance() → {ok, issues:[{entity_id, property, message, fix, severity, profile}]}
```
**`draft_process_chain`** drafts and links any subset of the four steps, always wired in canonical
order whatever the input order, each producer's output threaded into the next step's input.
Explicit `object`/`result` win.
- **Outputs come from the deposit** ([build-path wiring
  contracts](building.md#build-path-wiring-contracts-buildertools_crate_mappingpy)).
  EndpointReadout/DataAnalysis have no build-time output fallback, so `_deposited_outputs` wires
  the scanned `raw_data_file`s as the readout's `result` and `processed_data_file`s as the
  analysis's (#589); the folder cannot decide the tier. With no such file the step keeps **no
  result** and the tox Violation reports the gap (#592) — nothing is manufactured to pass the shape.
  Only a TestSystemPreparation's output `Sample` is synthesized (modelled material), carrying
  structural metadata only, never measurement values or identifiers (D5).
- **Whether a step exists** is asked of the whole deposit (`_deposit_evidences`): any data file or
  any procedure document. With neither the step is not drafted and is reported under `skipped`.
- **Scoped to the assay**: with several Assays only what is already attached counts, so no assay
  takes its neighbour's data. Because the agent often drafts the chain before attaching files,
  `attach_files` re-asks for any unwired step (`composites.wire_deposited_outputs`).
- **The Exposure gets no synthesized result** (#285, #650): its build-time fallback, the exposed
  `Sample` deriving from what it consumed, is the correct output, and a placeholder would pre-empt
  it. Its compounds are **reagents of the protocol it executes** (D17), since `schema:object` is
  restricted to File/Sample/BioSample; the condition table states which well got what dose.
- A readout's chemical **substrate** is resolved with `resolve_compound` and named under
  `reagents` (a `reagent` of the protocol the readout executes; `substrate` stays the stated
  name). `link(readout, input, compound)` lands there (`provenance._PROCESS_LINK_HOMES`), and the
  export does not then wire it onto the Exposure.
- A TestSystemPreparation grows **one** cell line: several lines become one step per line, each with
  its own cultured `Sample`, so no `Sample` derives from more than one line. The Exposure consumes
  every cultured `Sample` no later preparation consumed (plus any material it names outside the
  chain) and emits one exposed `Sample` per `Sample` consumed. A co-culture is asserted by the step,
  typed `NCIT:C93168`, never inferred.
- Idempotent: ids derive from the step; files are found-or-created by
  `provenance.find_or_create_file`, deduped on destination and source, so `attach_files` and the
  chain converge. A step belongs to one assay and an assay to one study (`drafters._owned_id`):
  where a name's id is held by another owner's entity, the new one is named after its owner too.

`attach_files` (#177) places a group of scanned files (by `name_contains` / `mime_contains` /
`paths`, optional `role`) on a Study or Assay's `hasPart`, finding-or-creating one `File` per
source, which also takes it out of the root fallback (#175). Process inputs/outputs stay with
`link`.

`link` adds one provenance edge; `relation` is from `PROVENANCE_RELATIONS` (`object`/`input`/
`samples` consumed, `result`/`output` produced, `derives_from` lineage), a strict subset of
`_REF_FIELDS` (asserted by test).

`check_provenance` is a **report-only** lint (no auto-chaining; branching assays make a fixed
order wrong): EndpointReadout/DataAnalysis with no output, Files no process produces, and a
consumed `Sample` no process produces that is not a TestSystemPreparation seed (#140; false-flag
guards in its docstring). Issues use the `build_and_validate` shape (#87).

### Lookup Tools
```
lookup_compound(name: str) → {found, data, error}   # PubChem → Compound Wiki → ChEBI
lookup_dtxsid(query: str) → {found, data, error}       # EPA CompTox (DTXSID)
lookup_cell_line(accession: str) → {found, data, error}  # Cellosaurus (accession CVCL_*)
lookup_cell_line_by_name(name: str) → {found, data, error}  # Cellosaurus; found: False on ambiguous/partial (D5)
lookup_aop(aop_id: str) → {found, data, error}            # AOP-Wiki
lookup_bao_term(query: str) → {found, data, error}       # OLS/BAO
lookup_ontology_term(query: str, ontology: str) → {found, data, error}  # OLS (any ontology)
lookup_unit(unit_string: str) → {found, data, error}     # OLS/UO (units)
lookup_orcid(orcid_id: str) → {found, data, error}     # ORCID
lookup_ror(name: str) → {found, data, error}              # ROR
lookup_doi(doi: str) → {found, data, error}       # Crossref
```
Every lookup returns that dict and never `None` ([lookup services](lookups.md)). A failure adds
`transient: True` for a timeout / 429 / 5xx — so a caller keeps the value and retries rather than
clearing it — and a `fix` when it is definitive.

### Verification Tools
```
verify_identifier(entity_id: str, field: str) → VerificationResult
verify_all_identifiers() → [VerificationResult]
```

### Crate Assembly & Validation Tools
```
build_and_validate(severity="required", profile="all") → {ok, conformance, issues}
set_validation_preference(recommended: bool | None = None, optional: bool | None = None) → {validation_preferences, tiers_that_will_run}
fix_required_issues(severity="required", profile="all") → {ok, fixed:[{issue, rule, action}], remaining:[{issue, reason}]}
export_crate(output_path: str) → CrateBuildResult
build_crate(output_path: str) → CrateBuildResult     # back-compat alias of export_crate
validate(crate_path: str) → ValidationReport
validate_table(file: str, table_schema: dict, foreign_keys: dict | None = None, entity_id: str | None = None) → {ok, issues}
populate_condition_table(exposure_id: str, rows_or_csv_path: list[dict] | str, output_dir: str | None = None) → {ok, path, rows}
```
**`build_and_validate`** assembles in memory and validates the JSON-LD via
`rocrate_validator.services.validate_metadata_as_dict` — nothing is written or re-read — returning
issues keyed to the failing entity/property:

```python
{
  "ok": bool,                                  # no issues at the gate severity
  "conformance": {"base": bool, "isa": bool, "tox": bool},  # only the scoped key(s) when profile != "all"
  "issues": [
    {"entity_id", "property", "message", "fix", "severity", "profile"}, ...
  ],
}
```

`conformance` covers the layers the crate claims for `profile="all"` (D18), or the one scoped
layer. When the ISA pass runs it also carries `verify_isa_reachability`'s findings
(`property: None`; [the decision records](../adr/README.md)). `severity` gates which checks run
(`required` fastest); `profile` (`all`|`base`|`isa`|`tox`) scopes the passes. Which pass costs most
is crate-dependent — on a large crate it is BASE, not tox (measured in
`builder/tools/validation.py`) — so scoping to `tox` to save time is backwards.

**`fix_required_issues`** runs `build_and_validate`, dispatches each issue through the ordered
`RepairRule` table (`builder/tools/repair.py`), and re-validates, trusting the validator rather
than the rule. A repair runs **only when state already determines the value**:
`missing_process_output` wires the one un-wired `File` as a readout's/analysis's `result`;
`missing_process_input` wires the one free-floating `Sample`/`File` as a DataAnalysis's `object`.
New content, new entities, fabricated identifiers or 2+ candidates are out of scope (D5) and go to
`remaining`. Idempotent: with nothing fixable it mutates nothing. Focus-node `@id`s map back to
state by inverting `_crate_mapping._mint_id`.

**`export_crate`** is the only tool that materialises the crate directory. The loop calls it after
every base-passing build and from the exit backstop ([the build
entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy)); `state.export_fingerprint()`
keeps repeats idempotent. Other writers: `populate_condition_table` (its CSV), `unzip_file`,
`save_session`. The in-memory path (`assemble_crate(..., materialize_payload=False)`) writes no
condition-table placeholder, so validation stays zero-disk.
- **Metadata-only** (`state.metadata.metadata_only`, `--metadata-only`): the session is built and
  judged with its files; only the export differs. Each payload file is described as deliberately
  not present — a local `#` id with a `localPath` (`_leave_files_out`) — keeping its description and
  references; a web-address file stays as is. The verdict and report in the crate are those of the
  document as written (`validation.validate_document` over `_without_files`), and data checks pass
  such files over (`assessment_graph.is_payload_file`). The export is refused unless the folder
  holds only what it writes, and no condition table is written.
- **Auto-included scanned files** (#175): `include_all_scanned=True` (export's default) attaches
  every scanned file not already a `File` entity to the root `hasPart`, so no file is silently
  dropped. Inclusion only: placed files are deduped by resolved source path, and reserved RO-Crate
  filenames are skipped. `build_and_validate` passes `False`, since plain leaves don't change the
  verdict.

**`validate_table`** is the payload layer (#95): a CSV's rows against a Frictionless `tableSchema`,
issues in the routable shape with `profile="data"` ([validation
layers](validation.md#validation-layers)).

**`populate_condition_table`** (#144) writes an Exposure's per-well rows (row dicts keyed by column
title, or a plate-map CSV) to the exact path the build wires (`_crate_mapping._condition_table_rel`),
so its CSVW `tableSchema` stays attached. The schema has 10 columns — `well_id`, `assay`,
`cell_line`, `compound`, `concentration_value`, `concentration_unit`, `exposure_duration`,
`experiment`, `technical_replicate`, `control` — each with `datatype` and `propertyUrl`, and a
`valueUrl` resolving cell-line/compound to their in-crate ids; missing cells are empty.
`_CONDITION_TABLE_HEADER` derives from `_CONDITION_TABLE_COLUMNS`, so header and schema cannot
drift. A `valueUrl` claims every row, so a column with ≥2 distinct values drops it
(`data_content.condition_table_multivalued_columns`; D5, #408). `propertyUrl`/`valueUrl` are
`{@id}` references, because the base profile flags an IRI used as a string when it is also a
described entity. Unlike a step's results, the condition table is declared before rows land: it
states this crate's expected compounds and doses, whereas a file for data nobody deposited would
be a claim about data that does not exist.

### Assessment Tools
```
assess_mit_coverage()   → MITReport
assess_fair_maturity()  → FAIRReport
assess_air_readiness()  → AIRReport
```
All three share one shape: a tri-state verdict per item with its evidence
(`builder/tools/assessment_graph.Verdict`), read from the assembled `@graph`, not `CrateState`.
MIT coverage reads through `mit_assessment.slot_matcher` (#377); a value the build synthesizes in
the user's absence (placeholder root name/description, the "licence not stated" entity) never
counts as filled — those values are imported from the build's constants, not duplicated. An item
that cannot be assessed is *not assessed*, never failed, and leaves the assessed denominator. Where
a published formula has no such state, both numbers are reported: `pct` over assessed,
`published_pct` over all (`AIRReport`, `fair_assessment.dsm_grid`).

`AIRReport` has **no aggregate score**: the Bridge2AI authors state AI-readiness is not scored
overall, so it is seven per-dimension percentages, each with published and local denominators.

The crate's build `CreateAction` (whose `result` is the root and every protocol record, D17) is
packaging provenance, not a data step: RDA-R1.2-01M credits it when the root is among its results,
and Bridge2AI 1.a–1.c never count it or its instrument.

### Session & HITL Tools
```
present_to_human(context: str, options: [str]) → HumanResponse
present_to_human(context: str, questions: [{question: str, options: [str]}]) → {action: "answered", answers: [{question, answer}]}
request_input(prompt: str, field_type: str | None = None) → InputResponse
save_session(label: str) → SessionInfo
list_sessions() → [SessionInfo]
load_session(session_id: str) → SessionStatus
get_status() → SessionStatus
get_hint() → str
```
`present_to_human` asks one decision; the first option is pre-selected. The console appends a
"Something else — let me type an answer" row to every prompt except a scan-root escalation; it
returns `action: "edited"` with the text in `comments` and `edits.value`. With `questions` it asks
each in turn (free text when no `options`), records every answered exchange in
`state.user_answers` (a skip is reported, not recorded); a malformed entry is an error and nothing
is asked. `request_input` asks for one free-form value. `list_sessions`/`load_session` drive resume
([session persistence](sessions-and-hitl.md#session-persistence--resume)).
`present_to_human`/`request_input` are engine-routed (not in `TOOL_REGISTRY`).

### Profiling
`ProfilingLogger` ([docs/profiling.md](../profiling.md)) times every tool call and graph node into
`sessions/<session_id>/profile.ndjson` (`tool_call`, `node_start`, `node_end`, `hitl_wait`).
`determine_agent_status(records)` (`builder/tools/dashboard.py`, #193) derives the ▶ driving /
⏸ awaiting input / ⏹ idle badge. A `tool_call` is logged only after the tool returns, so the engine
emits `hitl_wait` just before either HITL tool (`run_tool` in `builder/engine.py`); without it a
pending question is invisible.
