# The Agent Toolbox

> Every LLM-callable tool, grouped by purpose. Guarded by tests/test_agents_doc_toolbox.py: a tool named in a code fence here must exist, and every specced tool must be named here.


> **Tool-registration contract:** every new LLM tool must be registered in **four**
> lockstep places — `TOOL_REGISTRY`, `TOOL_SPECS`, the system-prompt "## Your Tools"
> catalogue, and this doc (guarded by `tests/test_agents_doc_toolbox.py`). The
> `TOOL_REGISTRY` ⇄ `TOOL_SPECS` half is enforced automatically (#327): the parity
> contract in `tools_spec.py` — `expected_tool_spec_names()`, the shared registry plus
> a small, *documented* set of engine-routed tools (`_LLM_TOOLS_OUTSIDE_REGISTRY`) —
> is checked at runtime by `assert_tool_spec_parity()` when the ReAct loop builds its
> tools, and in CI by `tests/test_tools_spec.py`. A registered tool with no schema, or
> a schema advertising a tool the engine cannot run, fails fast in either direction.
> A genuinely engine-routed tool (present to the LLM but not in the registry) is
> declared once, in `_LLM_TOOLS_OUTSIDE_REGISTRY`. That parity assert is also the
> toolbox's *reachability* guard — an advertised tool is one the model can always
> reach.

### File Tools
*The scanner/sampler triad and the archive tools below are engine-routed — the
engine dispatches them itself, so they are advertised in `TOOL_SPECS` but are not
in `TOOL_REGISTRY`; the full readers are registry tools. All of them are
LLM-callable during the agent loop.*
```
scan_files(path: str) → [FileClassification]
read_file_sample(path: str, lines: int = 20, mode: str = "content") → str | None
  mode: "content" (first `lines` lines — `lines` controls how much is returned), "summary" (file-type-aware), "overview" (metadata + summary)
  a directory path returns an actionable "use list_scanned_files" message, never a silent None (#240)
read_multiple_files(paths: list[str], lines: int = 50, mode: str = "content") → dict
  mode: same options as read_file_sample
read_file(path: str) → str | None             # full read by extension (txt, csv, json, xlsx, docx, md, pdf); text/JSON returned COMPLETE up to a 64 KiB budget, over-budget files carry an explicit "[truncated … do not re-read]" marker; a directory returns the list_scanned_files guidance (#240)
read_excel(path: str) → str | None            # .xlsx → pipe-delimited text
read_docx(path: str) → str | None             # .docx → plain text
extract_pdf_text(path: str) → str             # structured PDF: [Page N] text, tables, image metadata
preview_archive(path: str) → ArchivePreview   # list a zip/tar(.gz) archive's members + metadata without extracting
unzip_file(path: str, output_dir: str | None = None) → {extracted_to, entry_count, message} | {error, message}   # extract a zip/tar(.gz)
```
`scan_files`, `read_file_sample`, and `read_multiple_files` run during session
initialization to classify inputs and feed the state brief. The full readers
(`read_file`/`read_excel`/`read_docx`/`extract_pdf_text`) and the archive tools
(`preview_archive`/`unzip_file`) are dispatchable so the agent can pull a file's
full contents on demand. `unzip_file` refuses an unsafe archive with an error dict
rather than raising — a member escaping the destination (Zip-Slip), or a total
uncompressed size over `_MAX_UNCOMPRESSED_BYTES` (zip-bomb) — and the agent calls
`present_to_human` before extracting a large archive so the user can confirm.
Every tool named in this section exists in the code —
**never document a tool that does not exist**: the parity guard in
`tests/test_agents_doc_toolbox.py` fails the build on a phantom tool name in
this section (Issue #145).

### Entity Drafting Tools
```
scaffold_isa_backbone(investigation=None, study=None, assay=None, validate_base=False) → dict  # composite: linked Investigation→Study→Assay in one call (idempotent-WITH-merge: a reused layer's EMPTY fields are filled from the supplied hints, fill-don't-clobber), the fast path to a BASE-passing crate
resolve_aop(aop_id: str, study_id: str | None = None) → {aop_id, aop_entity_id, events, events_detail, wired_to_study} | {ok: False, error}  # composite: one AOP-Wiki id → the ONE AdverseOutcomePathway entity, at its AOP-Wiki address with its name; nothing else of the pathway is stored, and `events_detail` lists its key events for `link_assay_to_key_event`; wired onto the Study named (by its state id or by the id the crate gives it, as a finding does), or the sole Study when none is named — with several Studies and no name it wires nothing and the ISA reachability check names the island
link_assay_to_key_event(assay_id: str, event_name: str | list[str]) → {ok, assay_id, key_event_ids, matched_names, unmatched?, unlisted_pathways?} | {ok: False, error, candidates, unlisted_pathways?}  # composite: link an Assay (named by its state id or by the id the crate gives it, as a finding does) to the AOP Key Event(s) it MEASURES (schema:mentions via keyEvent), each name matched INDEPENDENTLY against the events AOP-Wiki lists for the pathway(s) in state and the KeyEvents already there (a stated AOP-Wiki address or number is matched against the same events), and names under `unlisted_pathways` a pathway AOP-Wiki gave no events for; stores the ONE event a name matches and commits its AOP-Wiki id, never one built from the name, and writes NOTHING for a name matching zero or several Key Events because which Key Event an assay measures is a scientific claim (D5); links ACCUMULATE deduplicated rather than replace — removing one is `set_fields`' job (`key_event_id`/`matched_name` are kept as the first match for callers written against the one-event form)
resolve_compound(name: str, hints: dict | None = None, verify=None) → {entity_id, name, identifiers, verifications, verified, source}  # composite: chemical name → lookup_compound → draft_molecular_entity → verify_identifier (+ best-effort CompTox DTXSID), in one idempotent call; carries the looked-up CAS + PubChem CID + EPA DTXSID and never keeps an unverified id (D5)
resolve_cell_line(name: str, hints: dict | None = None, catalog_name: str | None = None, verify=None, made_from: str | None = None) → {entity_id, name, accession, match, query, verifications, verified, source}  # composite: cell-line name → lookup_cell_line_by_name (full name, then catalog_name) → draft_cell_line_sample → lookup_cell_line (which IS the verification), in one idempotent call; a miss is NOT a failure (no `ok` key) — the Sample is always minted and the accession is enrichment; `hints.source_kind="primary cells"` skips Cellosaurus
resolve_characteristic_terms(entity_id: str | None = None) → {found, not_found, unavailable}  # composite: each stated organ / tissue / cell_type of a CellLineSample or Sample → the UBERON or CL term that word names (its label or an exact synonym), kept beside the word and written as the characteristic's valueReference; a word that names no term stays alone (D5); idempotent, and resolve_cell_line runs it for its own hints
resolve_publication(title: str, verify=None) → {ok, doi, entity_id, title, score} | {ok: False, reason, title}  # composite: publication title → Crossref title-search → confidence gate → draft_publication_with_authors(doi=…), in one idempotent call; commits a DOI only on a high-confidence match (score floor AND near-exact title) and never fabricates one (D5)
draft_publication_with_authors(doi: str) → {publication_id, doi, authors:[{name, person_id, orcid, resolution}], hitl}  # composite (engine-routed, HITL-capable): publication + every author wired as a Person, each author's @id harmonized to their ORCID via a verify-first cascade
draft_investigation(hints: dict) → Entity
draft_study(investigation_id: str, hints: dict) → Entity
draft_assay(study_id: str, hints: dict) → Entity
draft_molecular_entity(name: str, hints: dict) → Entity
draft_cell_line_sample(name: str, hints: dict) → Entity
draft_sample(hints: dict) → Entity                       # material input/output in the derivation chain
draft_process(assay_id: str, process_type: str, hints: dict) → Entity
draft_protocol(hints: dict) → Entity                     # what the lab states about a step's protocol: written on the documents the step executes, or on its protocol record (D17)
draft_person(name: str, hints: dict) → Entity            # hints accept givenName/familyName; when neither is given they are derived by a deterministic split of `name` (comma-form "Last, First" inverted; a lone token kept as a family-name candidate, never mis-placed into givenName) so EVERY Person path is ISA-conformant (non-empty schema:givenName). Without an orcid hint it takes the ORCID the deposit states for exactly that name, else a single strong name-search hit, once ORCID confirms it (D5)
draft_organization(name: str, hints: dict) → Entity      # without a ror hint it takes the ROR id the deposit states for exactly that name, once ROR returns that record (D5)
draft_publication(doi: str, hints: dict) → Entity
draft_defined_term(name: str, hints: dict) → Entity
draft_property_value(name: str, hints: dict) → Entity
draft_file(name: str, path=None, role=None, encoding_format=None, additional_types=None, programming_language=None) → Entity
```
`draft_defined_term` persists a looked-up ontology / AOP / Key-Event term as a
`schema:DefinedTerm` contextual entity (Issue #141): pass the looked-up IRI as the
`url`/`@id` hint so the node gets a dereferenceable `@id`, and it then round-trips
into the `@graph` and is referenceable (via `set_fields`/`link`) as a `mentions` /
`measurementMethod` / `sampleType` target. `draft_property_value` creates a typed
`schema:PropertyValue` node (`value` + optional `propertyID` + `unitText`/`unitCode`).
Both back the previously-unfilled `defined_terms` / `property_values` CrateState
collections the mapping already rendered.
`resolve_aop` (Issue #180) is the AOP counterpart of
`scaffold_isa_backbone`: from the single model-supplied numeric `aop_id` it calls
`lookup_aop` and stores the pathway as ONE typed contextual entity, an
`aopo:AdverseOutcomePathway` with the name, identifier and url the lookup gave.
**The crate carries the pathway a Study addresses and the key event an Assay
measures, and nothing else of a pathway**: its other key events, its
`KeyEventRelationship`s and the properties that link them (`has_key_event`,
`upstream_event`, …) stay on AOP-Wiki, at the entity's address, and the Study and
the Assay carry the only connection between the two. `link_assay_to_key_event`
stores the measured event: it matches what the deposit states against the events
`lookup_aop` lists for the pathway(s) in state (any entity at a pathway's AOP-Wiki
address, a term drafted there included), and against the KeyEvents already
there, and stores the one it matches as an `aopo:KeyEvent` (MIE/KE/AO all share
`@type KeyEvent`, discriminated only by the `eventType` string). Since the match
asks AOP-Wiki when the question is put, `lookup_aop` keeps no transient failure in
its cache, and while AOP-Wiki gives no events for a pathway in the crate the
answer says so rather than that the name matched nothing. Each node is
keyed by its resolvable AOP-Wiki IRI and takes the facts the lookup gave for it,
so all wiring is deterministic and idempotent and no id is ever fabricated (D5).
Both types live in the shared `aop_entities` CrateState collection and
build via `_crate_mapping` as `ContextEntity` nodes typed by their own AOP class.
The writer is where the rule holds: it writes every pathway and the key events an
Assay names, and never a relationship or a link property, so a session saved with
a whole pathway builds the same crate and is itself left as it is.
The AOP is wired onto a Study via the `aop` reference (an alias of
`schema:mentions`) — the one `study_id` names, or the sole Study in the crate
when none is named; with several Studies and no name it refuses to guess and
wires nothing, and the pathway is then an island `verify_isa_reachability`
reports (#738). The wiring closes the largest gold-crate fidelity gap.

`resolve_compound` (Issue #179, task 3) is the chemistry counterpart of
`scaffold_isa_backbone`: from the single model-supplied compound `name` it fuses
the recurring `lookup_compound` → `draft_molecular_entity` → `verify_identifier`
chain into ONE deterministic call. (1) `lookup_compound` resolves the chemical
(PubChem, then the Compound Wiki, then ChEBI) — a miss returns `{ok: False, error}` and creates
no entity; (2) `draft_molecular_entity` mints (or, idempotently, reuses) the
`MolecularEntity` carrying the looked-up `cas` / `pubchem_cid` (and `smiles` /
`inchikey` / …) — the build's shared `_identifier_pv` path turns `cas` +
`pubchem_cid` into the `[CAS, PubChem CID]` identifier PropertyValues, so this
composite never hand-rolls that wiring; (3) `verify_identifier` confirms each
minted identifier against source. **D5:** `verify_identifier` *clears* any value
that does not resolve, so a failed identifier never lingers as a fabricated id —
the per-field verdicts are surfaced in `verifications` and `verified` is the AND of
them. **DTXSID enrichment (Issue #179):** after resolution it also runs a
best-effort CompTox `lookup_dtxsid` — querying by the strongest EXACT key
available (`cas` → `inchikey` → name) — and stores the EPA **DTXSID** on the
entity when found, so the build appends it as a third `DTXSID` identifier
PropertyValue after `[CAS, PubChem CID]`. It is D5-safe (the value comes straight
from CompTox, never fabricated) and **non-fatal** — a CompTox miss or outage never
sinks an already-resolved compound. No other composite produces a DTXSID, so
dropping the CompTox call drops the identifier.
Looked-up identifier fields win over same-named caller `hints`. **Dedup is
by resolved chemical IDENTITY, not by name** (Issue #179): after a successful
lookup it computes an identity key in priority order `pubchem_cid` → `inchikey` →
`cas` → `chebiId` and reuses any existing `MolecularEntity` whose same identity
field matches — so two DIFFERENT names for one molecule (e.g. `Indocyanine green`
and `ICG`, same CID/InChIKey) collapse to ONE node (the synonym is recorded as a
`schema:alternateName`) instead of minting a second `chem_<name>` node, and two
names resolving to the same `pubchem_cid` can no longer mint the same `@id` (which
ro-crate-py silently overwrites — data loss). When the record carries no identity
field, it falls back to name-keyed reuse so re-running the same name still reuses
the entity rather than duplicating it.
Two chemicals that still mint one `@id` at build time, as two names do when the
Compound Wiki holds them as one item, are written as one node
(`_crate_mapping._add_leaves`): the first in state describes it, with its name and
identifiers; of each later one only the name is written, as an `alternateName`;
and a reference to either reaches it.

`resolve_cell_line` (Issue #372) is the cell-line counterpart of
`resolve_compound`, and the composite name→Cellosaurus path.
Minting a cell line straight through `draft_cell_line_sample(name=…, hints={})`
skips the lookup and leaves the crate with no accession at all. Two steps, both
Cellosaurus: (1)
`lookup_cell_line_by_name` on the full normalized name and then on
`catalog_name`, each gated by that lookup's **unmodified** exact+unique D5 rule —
first unique-exact hit wins, and the tier is reported as `match`; (2)
`lookup_cell_line` on the accession, which **is** the verification —
`_select_verifier("CellLineSample", "accession")` already resolves to exactly
that function, so a following `verify_identifier` would re-issue the same
`lru_cache`d call, and the status is set directly (mirroring
`_verify_compound_identifier`). A *transient* step-2 failure keeps the accession
unverified; a *definitive* step-2 miss clears it. A primary-cell source
(`hints.source_kind="primary cells"`, or a reused entity that records it) skips
both steps and keeps no accession: Cellosaurus holds no record for primary cells
(FAQ Q19), so any name hit would be a different entity.

**Cells made from a line are a sample of it** (`made_from`). Two cell types
differentiated from one iPSC line share that line's accession, and a name that
reaches a record only says which line, not that the cells are the line.
`resolve_cell_line(name, made_from="<catalogue name>", hints=…)` resolves the line
as above (it is the `CellLineSample` and carries the accession) and records the
named cells as a `Sample` that `derives_from` it and states its own `cell_type`,
`tissue` and `organ`; the result gives the Sample as `entity_id` and the line as
`line_id`. The population is never looked up, and an accession handed in as
`made_from` is ignored (D5). Each population is the `result` of a
`TestSystemPreparation` step of its own from that line: `draft_process_chain`
lets preparation steps that each name their `cell_line` run side by side and
hands what all of them made to the next step, and the build writes a Sample's
characteristics like a cell line's. Without `made_from`, two names that resolve to one accession are still one line under two
names **unless they state different cells** (`cell_type`, `tissue`, `organ`):
then nothing is merged or overwritten, the second is kept as a source of its own
without the accession, and the result's `fix` says to resolve both with
`made_from`. Cells made from a line, and a name kept apart from its line, get
the term of the cell type, tissue and organ they state in the same call, as a
line does (`resolve_characteristic_terms`).

**A miss is NOT a failure** — the one deliberate divergence from
`resolve_compound`, which returns `{ok: False}` and mints nothing. A
`CellLineSample` with only a name is a valid ISA Sample, so returning
`{ok: False}` would delete the cell
line from every crate whose line is not catalogued, taking the
`TestSystemPreparation.cell_line` input and the Study's `cell_lines` mention with it.
**Always mint; the accession is enrichment** — hence no `ok` key: read
`accession` / `match`. `name` stays the name **as the source documents word it**;
the Cellosaurus label goes to `alternateName`. The short `catalog_name`
(a catalogue *name* is a name, so it is D5-clean) is what lets a descriptive phrase such as "FRTL-5 TPO-overexpressing
rat thyroid follicular cells" reach CVCL_0265 at all; one shaped like an
accession is **refused**, as is any identifier-bearing `hints` key, so every id
it commits came back from Cellosaurus inside the call. Idempotency lives in the
composite, not the drafter: reuse is tried by accession first — one line often
appears under two names in one submission, and once the accession drives the
`@id` (`_crate_mapping._mint_id`) two such entities would collide onto one node
— then by the deterministic name-derived id. Only `accession` /
`alternateName` / `url` / `sameAs` / `taxonomicRange` are persisted; the
organism is the NCBITaxon term Cellosaurus states, which the build writes as a
`DefinedTerm` entity the Sample references. `_CELL_LINE_DROPPED_FIELDS`
records what else the record offers and the failure each would cause (the
record's own `identifier` is a full URL that `verify_all_identifiers` would
re-query percent-encoded, miss, and pop; `disease`/`anatomicalSite` are
`DefinedTerm` node objects that `_scalar_props` emits un-flattened, failing
base conformance).

`resolve_characteristic_terms` gives a stated organ, tissue and cell type its
ontology term. The word the documents use stays the characteristic's `value`; the
term is added as its `valueReference`, a `DefinedTerm` entity with the term's IRI,
label and CURIE. One rule decides whether there is a term: the word names
exactly one term of the ontology the characteristic belongs to
(`_crate_mapping.CHARACTERISTIC_ONTOLOGIES`: UBERON for `organ` and `tissue`, CL
for `cell_type`), among that ontology's own, current terms. It names a term by
being its label, case and spacing aside, or else a synonym the ontology marks as
exact ("Bronchial epithelium" for *epithelium of bronchus*); a narrow, broad or
related synonym names something else. A plural that names nothing is asked about
once more as its singular ("Dopaminergic neurons"). `lookups.bao.lookup_exact_term`
asks the service for exact matches and checks the answer again on our side.
Nothing is matched loosely: a search finds some term for nearly any word, and a
near match beside a stated value would be an invented claim (D5). "Epithelial
ovary cell" names no term and stays a word. The term is held in `<field>_term`
with status `verified` / `lookup` and the word it was confirmed for
(`confirmedFor`), and `_crate_mapping.confirmed_term` is the one reader: it
returns the term only while that status holds and the field still states that
word. A term a model or a person wrote, or one for an earlier wording, is
therefore never written, and the composite removes it; the three fields are
identifier fields (`field_kinds.IDENTIFIER_FIELDS`) and refused as
`resolve_cell_line` hints. The species a source of cells states in words is asked
about the same way, in NCBITaxon (`_crate_mapping.STATED_TERM_ONTOLOGIES`). It is
no characteristic: `taxonomicRange` holds a term for a cell line whose record
names its organism, and the term confirmed for a stated species is written in
its place the same way, so primary cells state "Homo sapiens" as the term every
cell line states. Without a confirmed term the word stays.
An unreachable service is no answer: the word is
reported `unavailable` and asked about again on a later call. The composite runs
at the end of `resolve_cell_line`, for what that call was told, and as a tool of
its own for a value the agent sets later.

`resolve_publication` (Issue #179) is the citation counterpart of
`resolve_compound`: a source often names a publication by its *title* only
(D5 — no DOI is guessed from it), but ISA REQUIRES a `ScholarlyArticle` with
an identifier (and BASE requires the auto-wired root `citation` `@id` to be an
absolute URI) — both unreachable from a title alone. From the single
model-supplied `title` it (1) runs a Crossref `query.bibliographic` title-search
(`lookups.crossref.search_works_by_title`) for candidate works ranked by
Crossref's relevance `score`; (2) applies a **D5 confidence gate** — a candidate
is committed ONLY when it clears BOTH the Crossref score floor AND a
normalized-title near-exact match (token-overlap threshold), so a high score on a
*different* paper, a weak score on the right title, or no candidate all return
`{ok: False, reason: "no confident DOI match", title}` and create NO entity (a DOI
is never fabricated from a title); (3) on a confident match delegates to
`draft_publication_with_authors(doi=…)`, reusing its DOI→`ScholarlyArticle`+authors
path (the ORCID cascade is handled there). It is idempotent (keyed by the resolved
DOI). It is registered four-place like `resolve_compound`, so the agent calls it
with the title a source states.

`draft_publication_with_authors` (Issue #180, deferred item) is the citation
counterpart of the composites above and the **only engine-routed, HITL-capable
drafter** (registered `takes_human=True`; the engine injects the active
`HumanInterface` as a `human_interface` kwarg). It calls `lookup_doi`, ensures the
`ScholarlyArticle` exists in state, and for EACH author creates/reuses a `Person`
wired as the article's `author`, harmonizing the author's `@id` to their **ORCID**
via a verify-first cascade (stop at first success): **(a)** the Crossref ORCID on
the author; **(b)** an in-crate `Person` with a *verified* ORCID matching the
author's family name + given/initial (affiliation-preferred) — this resolves the
gold case where citation `Fabian Wagenaars` reuses root `F.M.A. Wagenaars`'s ORCID
`0000-0003-4766-7358`; **(c)** the ORCID the deposit's own record states for a
person of exactly the author's name, by the name rule and the confirmation
`draft_person` applies (case and spacing aside; the ORCID record resolves and
states the same family name), the author then attached as for (a), and failing
it a public ORCID search
(`lookups.orcid.lookup_orcid_by_name`, the `/v3.0/expanded-search` endpoint via the
shared rate-limited HTTP layer; a search that cannot be reached is no answer, so
the author falls through to (d)); **(d)** fallback to a synthesized
`#CitationAuthor_<Given>_<Family>` Person (the legacy behavior). **Confidence rule
for the search in (c):** auto-accept **only** a *single* candidate that is a STRONG match (family
+ full — not initial-only — given name) and that passes name verification;
*anything else* — multiple candidates, a weak / initial-only match, or a sole
strong match that fails verification — **escalates to HITL** (`present` the ranked
candidates plus a none/skip option, then optionally `request_input` for a pasted
ORCID). **D5:** an ORCID from (a) or (c), and an HITL-chosen one, is attached only
after `lookup_orcid` resolves it and the family name matches; (b) is already
verified; an ORCID-resolved Person also carries its ORCID `identifier`
PropertyValue at build via the shared `_identifier_pv` path. A Person the crate already
holds under that ORCID is the author, whatever id it was drafted under, so no two
people take one address. HITL fires **only** on
genuine ambiguity — never when an author is confidently resolved or confidently
absent — and when no `human_interface` is available an ambiguous author falls back
to a synthesized id rather than guessing. An ORCID-resolved author's
**`affiliation` becomes an `schema:Organization` reference, never a literal
string** (Issue #179 — the ISA shape flags a literal-string affiliation as a
Violation): the ORCID record's `affiliation_name` (+ `affiliation_ror`) is
find-or-drafted into an Organization (preferring the ROR so its `@id` resolves to
the ROR IRI; D5 — a ROR is set only when the lookup returns one, never fabricated)
and the Person's `affiliation` is wired to that Organization's `@id`. An
affiliation that comes without a ROR id — on the ORCID record or on a paper's
author line — takes the id and the displayed name of the ROR
record that registers exactly that name (`registered_organization`, over
`lookups.ror.find_registered`): its displayed name, a label or an alias, compared
without case, accents, punctuation or connectives, never an acronym, and only when
one record of the search does. That is the organization, not a guess; a name no
record or several register keeps its own name and no ROR. Authors
sharing an affiliation reuse ONE Organization (deduped by name), so the build's
`_wire_reference` resolves each `Person.affiliation` to the shared node.
A Person's **`jobTitle` is likewise a `DefinedTerm` reference, never a string**
(the ISA person shape flags a literal title as a Violation, #837): the state keeps
the role as ORCID states it, and the build (`_wire_job_title`) writes one term per
distinct title, named after it and carrying no code — none exists for a free-text
role, and D5 forbids inventing one. A `jobTitle` that already names a term is
wired as it is.
The `hints` parameter is **typed per entity type** (Issue #90). Each `draft_*`
tool advertises a JSON-Schema built by `_crate_mapping.draft_hints_schema(type)`
from the single source of truth `_crate_mapping.ENTITY_DRAFT_SCHEMA` — allowed
scalar keys plus reference keys, the latter a strict subset of `_REF_FIELDS`
(asserted by test) so the advertised reference vocabulary and the crate-mapping
resolver cannot drift. The schema is open (`additionalProperties: true`), so a
weak model sees the high-value keys without the long tail being forbidden.

`LabProcess` hints additionally advertise a `units` map plus the optional
subtype parameters (`assay_kit`/`substrate` for EndpointReadout,
`acceptance_criteria`/`evaluation_criteria` for DataAnalysis) (Issue #143):
`_build_process` threads `units=f.get('units')` into the Exposure /
EndpointReadout / DataAnalysis constructors so each `ParameterValue` carries its
`unitText`, and threads the optional params into the matching subtype. A
`CellLineSample`'s `passage` / `growth` / `organ` / `tissue` / `cell_type` hints are promoted to
ISA Sample Characteristics — `schema:additionalProperty` PropertyValue nodes
carrying the value and, when known, the property's ontology IRI (`organ` / `tissue`
mirror the gold crate's `Organ` / `Tissue` characteristics with the ISA-Tox
`param/{organ,tissue}` `propertyID`, Issue #180; `cell_type` carries EFO:0000324
"cell type", which the primary-cell shape asks for). So are the `supplier`,
`catalogue_number` and `lot_number` of commercially sourced cells and the `donor_code`,
`number_of_donors`, `donor_age` and `donor_health_status` of primary cells: the builder
writes exactly the characteristics the specification lists for source cells, under its
names and terms, and a test holds the two lists to each other. They are characteristics of
a source of cells only: the same field on a chemical stays that chemical's own. Donor
`sex` is the BioSample's own property. The hint schema names every one of them, which is
how the agent learns that a source can state it. A `LabProcess`'s
`additionalProperty` field is likewise resolved to its in-state PropertyValue
reference(s) at build time (gold `#report_analysis` → `[#pv_repro_score]`) — only
PropertyValues already present in state (or bare IRIs) are wired; a score is never
computed or fabricated here (D5).

**A step states any parameter the deposit names.** The typed fields cover the
parameters this tool has a name for; a deposit states more (replicates, a positive
control, a time point, the exposure type, a wavelength). `LabProcess.parameters` is
the place for those: a list of `{name, value, unit}` (`NAMED_PARAMETERS_SCHEMA`,
advertised in the `LabProcess` hints and read by `named_parameters`). The build
writes each as a `ParameterValue` of the step under the name stated, with its unit
and no `propertyID`, since no term was looked up for it. A name a typed field
already wrote is not written twice, and a value that says nothing is left out as
for every parameter.

**Looked-up identifiers round-trip as `schema:PropertyValue` nodes** (Issue #180,
deterministic build path — no new LLM tools). `_crate_mapping._identifier_pv(name,
value, property_id_url=None)` mints an identifier PropertyValue with a stable id
mirroring rocrate-wizard's `param_id` (`#param_<slug(name)>_<sha1("name|value")[:10]>`)
and emits `propertyID` as an `{"@id": …}` node when a url is given. At build time:
a Person carrying `orcid` gains an `ORCID` PropertyValue identifier
(`propertyID {"@id": https://orcid.org}`); a MolecularEntity carrying
`cas`/`casrn`/`cas_number` and/or `pubchem_cid` and/or `dtxsid` gains
`[CAS, PubChem CID, DTXSID]` identifiers in that order (CAS has no `propertyID`;
PubChem CID's is `{"@id": https://pubchem.ncbi.nlm.nih.gov/compound}`; DTXSID's is
`{"@id": https://comptox.epa.gov/dashboard/chemical/details}`), and one carrying
`compoundwiki_id` gains a fourth, `Compound Wiki` (`propertyID`
`{"@id": https://compoundcloud.wikibase.cloud/entity}`); `resolve_compound` finds that
id by the InChIKey the lookup returned, an exact statement search on the wiki, and
keeps none when two items state one InChIKey (D5). A `compoundwiki_id` a caller
hands in as a hint is written as the caller's, and the wiki is then not asked. The
id the wiki gives for an InChIKey the caller handed in, where the lookup returned
none, is the caller's as well. Only the wiki's answer for the lookup's own InChIKey
is recorded as looked up, and such an id is also the chemical's `@id` ([lookup services](lookups.md)); any
other is an identifier value and nothing more. These source fields
are consumed structurally (kept off the node as raw literals). A Person's
`affiliation` and a Publication's `author` are resolved to `{"@id"}` references
(via `_wire_reference` / `_resolve_many`) rather than emitted as literals, so
`Person.affiliation` points at its Organization and `ScholarlyArticle.author`
is an array of `Person` references. No identifier is ever fabricated — only values
already in state (from a lookup) are wired (D5). `draft_property_value` defaults
and `@id`-wraps the `propertyID` for a PropertyValue named `DOI`
(`OBI_0002110`) or `PubMedID` (`OBI_0001617`), the IRIs the tox
`{10,11}_*_property_value.ttl` shapes require as `sh:hasValue` nodes — so a
DOI/PubMedID PropertyValue passes the tox pass instead of silently failing.

`draft_file` auto-derives `encodingFormat` from the file extension (`name`, then
`path`) when the caller omits it (Issue #148), via the same scientific-format-aware
MIME registry the scanner uses — so `run.mzML` becomes `application/x-mzml` and
`acquisition.fcs` becomes `application/vnd.isac.fcs` rather than being left blank
or mislabeled `text/plain`. An explicit `encoding_format` always wins; an
extensionless name leaves the field unset. `additional_types` co-types the node
beyond plain `File` (Issue #180); it is consumed structurally (`File` is always the
leading type and duplicates are dropped), and a plain File keeps its scalar `@type`.
A file whose extension names source code (`document_discovery.SCRIPT_SUFFIXES`) is
built as `@type:[File, SoftwareSourceCode, LabProtocol]` whatever the caller passes;
`programming_language` adds `schema:programmingLanguage` (gold `plot.py`).

### Entity Management Tools
```
set_fields(entity_id: str, fields: dict, source="llm") → Entity
set_crate_metadata(title=None, description=None, accession=None, release_date=None, date_modified=None, publisher=None, creator=None, contact=None, license=None) → {title, description, accession, release_date, date_modified, publisher, creator, contact, license, note?}  # set Root Data Entity (crate-level) scalar metadata
remove_entity(entity_id: str, cascade: bool = False) → {removed, entity_id, detached, discarded_fields, warning}
list_entities(entity_type: str | None) → [Entity]
list_scanned_files(name_contains=None, mime_contains=None, offset=0, limit=200) → {total_scanned, matched, files:[{path, filename, size, mime_type}]}
```
`set_crate_metadata` (Issue #180) is the single setter for **crate-level**
metadata — the scalar properties of the Root Data Entity (`./`), as opposed to
`set_fields` which mutates a graph *entity*. It writes onto `CrateState.metadata`:
`title` / `description` / `accession` map to the root's `name` / `description` /
`identifier`, and `release_date` / `date_modified` add `release_date` /
`date_modified` to `CrateMetadata` (defaulting to `None`, so sessions saved
before these fields existed still load) and emit `schema:releaseDate` /
`schema:dateModified` on the root at build time (the gold S-VHPS21 root carries
both alongside the auto-set `datePublished`). Only the arguments actually passed
(non-empty) are written — a date is never fabricated (D5) — and ro-crate-py's
auto-set `datePublished` is left untouched unless explicitly overridden.
Attribution — `publisher` / `creator` / `contact` — must be a resolvable
reference: a Person/Organization already in state, or a verified ORCID/ROR IRI.
A bare name is REFUSED, because a name string credits nobody a registry can
resolve (D5). `license` yields to a licence READ from the deposit descriptor
(`license_from_deposit`, #535) — a depositor's statement is a fact and a drafted
guess does not replace it — and the call reports that under `note`. A call with
every field empty is REFUSED rather than answered with a success-shaped summary:
it can write nothing, so it is always a mistake; `get_status` reads the current
metadata.
`list_scanned_files` retrieves the **full** raw scan inventory from
`CrateState.scanned_files`. `scan_files` only surfaces a ~15-file sample and its
output is later pruned from history (D12), so this is how the agent re-reads the
complete file list to decide which files to place/annotate — paginated
(`offset`/`limit`) and filterable (`name_contains`/`mime_contains`) so it stays
token-bounded, and compact (no `first_rows` preview).
`set_fields` is the **single consolidated mutation tool** (Issue #90). It
replaced three redundant tools — `update_entity` and `bulk_set_fields` were
byte-identical, and `set_entity_field` was just the single-field (one-key dict)
case. Those names survive as thin deprecated aliases for library callers but are
no longer exposed to the LLM. Pass one field or many in the `fields` dict.
`remove_entity` preserves referential integrity: the builder rebuilds the crate
from state each iteration, so a reference left dangling in state surfaces as a
dangling `{"@id": ...}` in the built graph. It first scans every entity's
reference fields (`_REF_FIELDS`); if the target is still referenced it refuses
with an actionable error naming the referrers, unless `cascade=True` clears those
references first. `entity_id` is the stable key — "renaming" changes the `name`
field, never the `entity_id`, so referrers (which point at `entity_id`) are never
orphaned. It returns a REPORT, not a bare boolean: `cascade=True` is right for a
stray reference and quietly destructive for a parent link — clearing an Assay's
id off its processes detaches every experiment in the crate, and a parentless
process violates no shape: only the ISA reachability check ([the decision records](../adr/README.md)) reports it, one
REQUIRED finding per entity on the next verdict. `detached` and
`discarded_fields` name what came loose and what was thrown away, so the caller
can re-point the children in the call that detached them.

### Derivation Chain Tools
```
draft_process_chain(assay_id: str, chain: [{process_type, hints?, object?, result?}], validate_after=None) → {assay_id, process_ids, steps, synthesized, skipped?}  # composite: create + wire the whole TestSystemPreparation→Exposure→EndpointReadout→DataAnalysis chain in one idempotent call; EndpointReadout/DataAnalysis have NO build-time output fallback, so each step is wired to the output the deposit actually holds — a data producer with no deposited file keeps no `result` and the tox Violation reports the gap; only a TestSystemPreparation's `Sample` is synthesized
link(from_id: str, relation: str, to_id: str) → {from_id, relation, to_id}
attach_files(to: str, name_contains=None, mime_contains=None, paths=None, role=None) → {attached, file_ids, to}
check_provenance() → {ok, issues:[{entity_id, property, message, fix, severity, profile}]}
```
`draft_process_chain` (Issue #179, task 3) is the `link` composite — the
proactive counterpart of `fix_required_issues`' missing-output repair rule. It
fuses the recurring `draft_process` + `link` sequence that wires the gold
S-VHPS21 derivation chain into ONE idempotent call. `chain` is an ordered list of
step dicts (`process_type` + optional `hints` + optional explicit `object` /
`result` ids); any **subset** of the four subtypes is allowed (partial chains
work) and steps are always wired in the canonical order regardless of input order,
so a weak model cannot mis-sequence the provenance. Each producing step's output
is threaded into the next step's input, so the chain is fully connected and
referenceable. **Its load-bearing job ([build-path wiring contracts](building.md#build-path-wiring-contracts-buildertools_crate_mappingpy)):** `EndpointReadout`/`DataAnalysis`
have **no build-time output fallback**, so a process with no explicit `result`
(and, for DataAnalysis, no `object`) fires a tox REQUIRED Violation. The composite
closes that trap, but **asks the deposit first** (#589): the scanned files
classified `raw_data_file` are the `EndpointReadout`'s result and the
`processed_data_file`s the `DataAnalysis`'s, found by `_deposited_outputs` and
wired whole (`schema:result` takes "at least one"). Only when the deposit holds
no file of that class is the step left **without a result**, so the tox Violation
fires and the gap is reported (#592). Nothing is
manufactured to make the shape pass: an empty stand-in bought a green profile at
the cost of a 0-byte CSV a consumer reads as data, with nothing telling the
depositor what was missing. A material producer (TestSystemPreparation) still gets a
`Sample` via `draft_sample` — that is modelled material, not a stand-in for a
file nobody deposited.

**Whether the step exists at all is a separate question** (`_deposit_evidences`),
asked of the whole deposit: any data file, or any document describing the
procedure. Either makes it real; a deposit holding neither leaves nothing to
model, and drafting a step there would invent it exactly as an empty output file
used to invent its result — so it is skipped and reported under `skipped`. A
protocol counts on its own because the step carries more than its output: on
svhps26 the SOP yields `Detection Instrument = "gamma counter"`, which reaches
the crate only because an EndpointReadout exists to hold it. Not class-specific,
deliberately: a deposit with only processed data still evidences that a
measurement happened — its raw output was simply not submitted, and that gap is
the Violation's to report.

Scoped to the assay: one Assay owns the whole deposit, several means only what is
already attached counts, so no assay is given its neighbour's measurements. That
scope made the answer depend on call order — the real agent drafts the chain
before it attaches files — so `attach_files` re-asks the question for any step
still unwired (`composites.wire_deposited_outputs`). Evidence arriving is when
the answer can change; asking once at draft time silently answered "nothing
deposited" for 5 of 8 steps whose output was in the deposit all along.

Asking first is what keeps the chain pointing at evidence: a composite that
synthesizes regardless produces a crate whose every chain ends at an empty stub
while the deposit's measurements sit beside it, referenced by nothing — and a
crate that reports no problem, because the stub satisfied the shape.

**The Exposure is the deliberate exception (#285, #650):** it is
NOT given a generic placeholder result here, because its build-time fallback is the
*semantically-correct* output — the **exposed Sample**, the cells after treatment,
deriving from the sample it consumed. Synthesizing a generic result File
would populate `result` and pre-empt it, leaving the crate with no exposed-sample
entity at all and every downstream step hanging off the culture instead. So the
Exposure step is left output-less in state and the build emits the exposed sample;
the material flow still passes downstream via the step's inputs.

The compounds do not ride on that output. They are **reagents of the protocol the
Exposure executes** (D17): `schema:object` is restricted to File/Sample/BioSample at
Violation severity, so a compound cannot be the Exposure's input. The deposited SOP
the Exposure executes names this run's compounds, or its record does where it
executes none, and the per-well condition table — a data file `about` the exposed
`Sample`s — states which well got which compound at which dose.

A readout's **substrate** that is a chemical (the labelled hormone an uptake assay
measures) is a compound of its own too, and no test chemical. It is resolved with
`resolve_compound` like any compound, so its CAS and CID come from the lookup, and
the EndpointReadout names it under `reagents`: the build lists it as a `reagent` of
the protocol the readout executes, while `substrate` stays the name as stated, the
step's `Substrate` parameter. `link(readout, input, compound)` lands there
(`provenance._PROCESS_LINK_HOMES`), and a compound a readout holds is used, so the
export does not wire it onto the Exposure.
**Requires:** an existing `assay_id` + each step's
`process_type`. **Reads from the deposit (before synthesizing):** the raw /
processed files that are the step's real output. **Synthesizes:** only a
TestSystemPreparation's output `Sample`; the Exposure's output is the build's exposed
`Sample`. A TestSystemPreparation grows **one** cell line: a step naming several is built as
one step per line, each following that line's culture document and producing
its own cultured `Sample`, so no `Sample` derives
from more than one line and none
stands for a mixture the lab never made. The Exposure consumes where its assay's
preparation ended — every cultured `Sample` no later preparation step consumed, never an
intermediate one — unless its step names material outside that chain, which it
keeps. It emits **one exposed `Sample` per `Sample` consumed**, so the split is
not undone a hop later. A co-culture is the explicit exception — asserted
by the step, typed `NCIT:C93168` on the material it yields, and never inferred
from a step that merely names several lines. **Skips (and reports under
`skipped`):** a data producer nothing in the deposit evidences. **Reports rather than fills:** a data producer that IS
evidenced but whose output the deposit lacks keeps no `result`, and the tox
Violation carries that to the report.
**Respects:** any explicit `object`/`result` you pass — those win over both.
The `Sample` carries only structural metadata (name, crate path, role) and
**never fabricates measurement values or identifiers** (D5).
Idempotent: placeholder/process ids are derived deterministically from the step,
so re-running reuses them rather than duplicating; deposited files are found-or-
created by `provenance.find_or_create_file`, deduped on destination and source so
`attach_files` and the chain converge on one entity per file rather than two.
A step belongs to one assay, and an assay to one study (`drafters._owned_id`):
two assays may name a step alike ("Exposure"), and two studies an assay. Where
the id that name gives is already held by another assay's step, or another
study's assay, the entity is named after its owner as well, so drafting it never
takes the other one's.
`attach_files` is the bulk *placement* verb (#177): it associates a **group** of
scanned files with a Study or Assay in one call (select by `name_contains` /
`mime_contains` / explicit `paths`, stamp an optional `role`). For each match it
finds-or-creates a `File` entity (deduped by on-disk source, so it is never
duplicated and drops out of the #175 root fallback) and appends it to the
target's `hasPart`, which `_add_structural` resolves to nest the file under that
dataset. It is the scalable counterpart to per-file `draft_file` and complements
the #175 auto-include fallback (inclusion) with agent-driven placement
(association). Process inputs/outputs stay with `link`.
`link` adds one provenance edge — `relation` is drawn from `PROVENANCE_RELATIONS`
(`object`/`input`/`samples` = consumed, `result`/`output` = produced,
`derives_from` = sample lineage), a strict subset of the crate mapping's
`_REF_FIELDS` (asserted by test, so the edge vocabulary and the resolver cannot
drift). It is the explicit verb the agent uses to wire the
Sample →[TestSystemPreparation]→ Sample →[Exposure]→ Sample →[EndpointReadout]→ raw
→[DataAnalysis]→ figures chain (those reference keys are otherwise hidden behind
the schema-less `hints` param, so a weak model never sets them). `check_provenance`
is a **report-only** connectivity lint (no auto-chaining — branching assays make a
fixed process order wrong): it flags EndpointReadout/DataAnalysis processes with no
output (the build has no fallback for those), File entities produced by no
process, and broken derivation-chain continuity — a process consuming a `Sample`
that no process produces and that is not a TestSystemPreparation seed (#140; the guards
that keep primary-cell, data-only and multi-assay crates from being false-flagged
live in the `check_provenance` docstring). Issues come back in the same routable
shape as `build_and_validate` (#87).

### Lookup Tools
```
lookup_compound(name: str) → {found, data, error}   # PubChem → Compound Wiki → ChEBI
lookup_dtxsid(query: str) → {found, data, error}       # EPA CompTox (DTXSID)
lookup_cell_line(accession: str) → {found, data, error}  # Cellosaurus (accession CVCL_*)
lookup_cell_line_by_name(name: str) → {found, data, error}  # Cellosaurus name → accession (confidence-gated; found: False on ambiguous/partial, D5)
lookup_aop(aop_id: str) → {found, data, error}            # AOP-Wiki
lookup_bao_term(query: str) → {found, data, error}       # OLS/BAO
lookup_ontology_term(query: str, ontology: str) → {found, data, error}  # OLS (any ontology)
lookup_unit(unit_string: str) → {found, data, error}     # OLS/UO (units)
lookup_orcid(orcid_id: str) → {found, data, error}     # ORCID
lookup_ror(name: str) → {found, data, error}              # ROR
lookup_doi(doi: str) → {found, data, error}       # Crossref
```
Every lookup returns that one dict shape and NEVER `None` ([lookup services](lookups.md)), so a miss is a
truthy result to read, not a falsy one to branch on. A failure adds `transient:
True` for a timeout / 429 / 5xx — distinct from a definitive not-found, so a
caller keeps the user's value and retries instead of clearing it — and a `fix`
naming the next action when the failure is definitive.

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

`validate_table` is the **data-content (payload) layer** (#95): it validates a
CSV's rows against a Frictionless `tableSchema` — separate from the SHACL
metadata passes (see [validation layers](validation.md#validation-layers), Data-Content Layer). Issues use the same routable shape
with `profile="data"`.

`populate_condition_table` (Issue #144) writes the per-well rows into an
Exposure's CSVW condition table — replacing the header-only placeholder #94
materialises — either from a list of row dicts (keyed by the condition-table
column titles) or by attaching a user-supplied plate-map CSV. The condition
table's typed schema is the gold S-VHPS21 crate's full **10 columns** (#180,
Lane D): `well_id`, `assay`, `cell_line`, `compound`, `concentration_value`,
`concentration_unit`, `exposure_duration`, `experiment`, `technical_replicate`,
`control` — each with a `datatype` + ontology `propertyUrl` (and a `valueUrl`
resolving the cell-line/compound columns to their in-crate Sample /
MolecularEntity id). Population fills whatever columns the data provides; the
schema describes all 10 and missing cells are written empty.

A column's `valueUrl` is a claim about **every row in that column**, so it holds
only while the column carries at most one distinct value. Once rows exist,
`data_content.condition_table_multivalued_columns` reads the populated CSV back
and any column with ≥2 distinct values **drops** its `valueUrl` rather than assert
an unverified per-value mapping (D5, #408). The guard is per-column — a
single-compound plate keeps its claim; a multi-compound one loses only `compound`.
Per-value entity mapping is out of scope. It targets the
exact path the build wires (`_crate_mapping._condition_table_rel`), so the #94
CSVW typing (`tableSchema`) stays attached to the populated table.
`_CONDITION_TABLE_HEADER` is derived from the same column constant
(`_CONDITION_TABLE_COLUMNS`), so the placeholder header and the typed schema
cannot drift.

An `EndpointReadout`'s results are the files it measured, and nothing is appended
to them. **The deposit decides what a step produced** (#589): `raw_data_file`s
are the `EndpointReadout`'s `schema:result` and `processed_data_file`s the
`DataAnalysis`'s, read off the file classification
(`composites._deposited_outputs`). The folder a file sits in cannot decide its
tier: two of the three real deposits file both tiers under one `Raw data +
individual processed data/` (#591).

A step whose output the deposit does **not** contain gets nothing — no entity, no
file, no `result` — and the tox Violation reports it (#592). Writing a file for a
file nobody produced is a claim about data that does not exist; dressing it in a
column contract made that claim machine-readable and vacuously true over zero
rows (#473), and the columns came from a module constant identical in every
crate.
The condition table is the deliberate contrast: its schema resolves `valueUrl` to
this crate's own `Sample` / `MolecularEntity` ids, so it states which compounds at
which doses *this* experiment expected — worth declaring before a row lands.

The condition table emits `propertyUrl`/`valueUrl` as `{@id}` references
(not bare strings): RO-Crate 1.2's base profile flags an IRI value used as a
string when that IRI is also a described entity (e.g. the cell-line `NCIT_C16403`,
which a cell-line `CellLineSample` materialises as a `cell line` `DefinedTerm`).

`build_and_validate` is the agent's primary build/fix loop: it assembles the
crate from `CrateState` **in memory** and validates the generated JSON-LD
document directly via `rocrate_validator.services.validate_metadata_as_dict` —
**no crate is written to disk and nothing is re-read** (the old
`build_crate`→`validate` round-trip touched disk on every ReAct iteration). It
returns issues keyed to the entity/property that failed so the agent can route
a fix to a specific field:

```python
{
  "ok": bool,                                  # no issues at the gate severity
  "conformance": {"base": bool, "isa": bool, "tox": bool},  # only the scoped key(s) when profile != "all"
  "issues": [
    {"entity_id", "property", "message", "fix", "severity", "profile"}, ...
  ],
}
```

`conformance` is keyed to the passes actually run: every layer the crate claims for
`profile="all"` (D18), or just the scoped layer when a single `profile` is given.
Whenever the ISA pass runs, `issues` and `conformance["isa"]` also carry
`verify_isa_reachability`'s findings (`property: None`; [the decision records](../adr/README.md)).

`severity` (`required`|`recommended`|`optional`) is the gate that decides which
SHACL checks run — `required` (the default) is fastest. `profile`
(`all`|`base`|`isa`|`tox`) scopes the passes, so the inner loop can validate a
single profile at REQUIRED severity and run the full 3-pass sweep only as a gate.
Which pass costs most is crate-dependent and NOT tox by default: on a real
293-entity crate the BASE pass is 22.9s of a 36.9s optional sweep (62%) against
9.2s for tox — measured in `builder/tools/validation.py`. Scoping to `tox` to
save time is therefore backwards on a large crate. The three passes mirror
`profiles/validator.validate_crate`, fed the metadata dict instead of a path.

`fix_required_issues` is the **deterministic repair loop** — what maps a routed
issue back to a *repair*. It runs
`build_and_validate`, dispatches each issue through a
small, ordered table of issue-shape → repair rules (`builder/tools/repair.py`,
`RepairRule`), then **re-validates** to confirm what actually cleared (it trusts the
validator's verdict, not a rule's optimism). A repair runs **only when the correct
value is already determined by state**. Two symmetric rules are wired today:
`missing_process_output` — an `EndpointReadout`/`DataAnalysis` missing its `result`
where exactly **one** un-wired `File` already exists in state is auto-wired as its
`result` via `link` (the [build-path wiring contracts](building.md#build-path-wiring-contracts-buildertools_crate_mappingpy) "no output fallback" trap); and
`missing_process_input` — a `DataAnalysis` missing its required `schema:object`
(input) where exactly **one** free-floating `Sample`/`File` (one wired as no
process input or output) already exists is auto-wired as its `object`. Anything
needing **new content, a new entity, or a fabricated identifier — or a genuinely
ambiguous target (2+ candidates) — is out of scope (D5)** and returned under
`remaining` for the agent. It is **idempotent and side-effect-safe**: if nothing is
deterministically fixable it mutates nothing and returns every issue in `remaining`.
It maps a validation focus-node `@id` (e.g. `./#LabProcess_er1`) back to its state
entity by inverting `_crate_mapping._mint_id`. Each `fixed` item carries
`{issue, rule, action}`; each `remaining` item `{issue, reason}`.

`export_crate` is the **only** tool that materialises the on-disk RO-Crate
directory (payload included). The ReAct loop calls it **repeatedly** —
automatically after every base-passing build, and again from the exit backstop
([the build entrypoint](react-loop.md#the-build-entrypoint-builderagentsbuildpy)) — and
`state.export_fingerprint()` is what keeps the repeat idempotent. It is not
the only tool that writes at all: `populate_condition_table` writes the condition
table's CSV, `unzip_file` extracts an archive, and `save_session` persists the
session.
`build_crate` remains as a back-compat alias. The in-memory assembly path
(`assemble_crate(..., materialize_payload=False)`) skips writing the Exposure
condition-table placeholder CSV so validation stays a zero-disk operation.

**Metadata-only export.** With `state.metadata.metadata_only` set (`--metadata-only`),
`export_crate` writes the same crate without its payload, for a deposit whose data is not
public yet. Only the export differs: the session is built and judged with its files, because
they are what the metadata is read from. The crate is assembled whole in memory, then every
payload file is described as RO-Crate 1.2 describes a file that is deliberately not present:
under a local `#` identifier, with a `localPath` naming where it belongs (`_leave_files_out`).
It keeps its description and every reference to it, so a step still names its files and the
profiles find what they find in the full crate; a file at a web address stays as it is. The
verdict and the report inside the crate are those of the document as written
(`validation.validate_document`), on a view of the state the session never sees
(`_without_files`). A file that is described and not present is no data a reader receives, so
every check about the crate's data passes it over (`assessment_graph.is_payload_file`). The
folder must hold nothing the export does not write itself, or the export is refused, since
files an earlier export left there would travel with the metadata. No condition table is
written in such a session.

**Auto-included scanned files (#175).** `assemble_crate(..., include_all_scanned=True)`
(the default, used by `export_crate`) packages *every* scanned file that the agent
has **not** already drafted as a `File` entity, attaching it to the root `hasPart`
as a plain `File` leaf — an honest *fallback* so the exported crate never silently
drops a data file. It is **inclusion only, not placement**: files the agent has
explicitly placed (under a Study/Assay, wired as a process `result`/`object`, or
given a role) already have a `File` entity and are deduped out by resolved source
path, so the agent's semantic association always wins. Reserved RO-Crate filenames
are skipped. The hot `build_and_validate` path passes `include_all_scanned=False` —
plain leaves don't change the validation verdict, so skipping them keeps the ReAct
loop fast. Semantic placement of file *groups* (which assay, which role) stays an
agent task — the bulk `attach_files` verb (#177) documented above.

### Assessment Tools
```
assess_mit_coverage()   → MITReport
assess_fair_maturity()  → FAIRReport
assess_air_readiness()  → AIRReport
```
All three score published instruments and share one shape: a tri-state verdict per
item carrying the evidence behind it (`builder/tools/assessment_graph.Verdict`), read
from the assembled `@graph` rather than from `CrateState` — the domain content exists
only after assembly. MIT coverage reads it through one matcher,
`mit_assessment.slot_matcher` (#377), and a value the build synthesizes in the user's
absence — the placeholder root name and description, the "licence not stated"
entity — never counts as filled: the values are imported from the build's own
constants rather than duplicated. An item the tool cannot assess from a crate is reported *not
assessed*, never failed, and leaves the denominator of what was assessed — never the
instrument's own. Where a published formula has no *not assessed* state, the report carries
both numbers: `pct` over the criteria assessed, `published_pct` over every criterion
(`AIRReport`, `fair_assessment.dsm_grid`).

`AIRReport` carries **no aggregate score**. The Bridge2AI authors state that
AI-readiness is not scored pass/fail overall, so the axis is seven per-dimension
percentages; a single number would be an invented metric wearing a citation. Each
dimension reports the published denominator (every criterion) beside the local one
(criteria assessed), because the published formula has no "not assessed" state and
substituting ours silently would misstate the instrument.

The crate's own build record — the `CreateAction` whose `result` is the Root Data
Entity and every protocol record the build wrote (D17) — is provenance of the
packaging, not a data-transformation step: RDA-R1.2-01M credits it when the root is
among its results, and Bridge2AI 1.a–1.c never count it or its instrument as a step
or as analysis software.

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
`present_to_human` asks one decision: `context` says what was found and
`options` are the rows the user picks between, the first pre-selected. The
console appends a final "Something else — let me type an answer" row to every
prompt except a scan-root escalation, so an answer the caller did not foresee is
still possible; it comes back as `action: "edited"` with the text in `comments`
and `edits.value`. With `questions` the tool asks several in turn — each with its
own `options`, or as a free-text field when it has none — records every answered
exchange in `state.user_answers` (a skip is reported, not recorded), and returns
`{action: "answered", answers: [{question, answer}]}`; an entry not of that shape
is an error and nothing is asked. `request_input` asks the human for a single free-form value (e.g. a
compound name, CAS number, or cell line accession) when a lookup needs a missing
identifier. `list_sessions` and `load_session` drive the resume flow ([session persistence](sessions-and-hitl.md#session-persistence--resume));
`present_to_human`/`request_input` are engine-routed HITL tools (not in
`TOOL_REGISTRY`), the rest are specced.

### Profiling
Every tool call and graph node execution is automatically timed and recorded by `ProfilingLogger` (see [docs/profiling.md](../profiling.md)). Profile data is written to `sessions/<session_id>/profile.ndjson` as newline-delimited JSON with event types including `tool_call`, `node_start`, `node_end`, and `hitl_wait`. This file is the primary input for timing analysis, debugging, and live status in future web UIs.

The dashboard derives a live **▶ / ⏸ / ⏹** agent-status badge from these events via the pure helper `determine_agent_status(records)` (`builder/tools/dashboard.py`, issue #193): **▶ driving** when a node/tool is in flight, **⏸ awaiting input** when blocked on a human, **⏹ idle** otherwise. Because a `tool_call` event is only logged *after* the tool returns, a pending HITL call would be invisible to inference — the agent is blocked *inside* `present_to_human`/`request_input` before the event is written. The engine therefore emits an explicit `hitl_wait` event immediately before invoking either HITL tool (`run_tool` in `builder/engine.py`); the subsequent `tool_call` for that tool marks the human's response. The badge renders in the CrateState overview panel title across both the live (`run_dashboard`) and static (`run_static_dashboard`) paths.
