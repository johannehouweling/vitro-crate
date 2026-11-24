# Building the crate

> Entity drafting, crate assembly, the build-path wiring contracts the profiles impose, and the on-disk output.

## Entity Drafters (`builder/tools/drafters.py`)
Generate metadata entities from files, conversation, or existing metadata.
Each drafter is a **pure state mutation** over hints its caller supplies — the
model's contribution is recorded as provenance (`created_by="llm"`,
`source="llm"`) rather than made from inside the drafter — and **identifiers come
from lookups, never fabricated**: `_resolve_person_orcid` verifies the ORCID the
deposit states for a person of that name, and failing it a single full-name ORCID
search hit, through the record endpoint before using its URL as an entity id, and
leaves the deterministic local id in place for anything ambiguous, unavailable,
unconfirmed or weak. `draft_organization` takes the ROR id the deposit states for
an organisation of that name once ROR returns that record, and none for a name alone.

**Entity types:** Investigation, Study, Assay, MolecularEntity, CellLineSample
(CellLine/PrimaryCell), LabProcess (TestSystemPreparation/Exposure/EndpointReadout/DataAnalysis), Person,
Organization, Publication.

## Crate Builder (`builder/tools/builder.py`)
Assembles the RO-Crate using [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py)
(`profiles/models/isa.py`, `profiles/models/tox.py`, `profiles/context.py`).
Can produce partial crates at any point.

Every term the crate's `@context` declares resolves to a real vocabulary's own canonical IRI — the
policy `profiles/ontology_iris` states for lookup terms, applied to the context too. AOP terms are
**AOPO**'s (`http://aopkb.org/aop_ontology#`), the ontology AOP-Wiki RDF itself annotates with; they
spent months under an `aopwiki.org` path AOP-Wiki does not serve, so a whole vocabulary resolved to
nothing while looking familiar (#644). A test holds every namespace in the context to a declared
allow-list, so an invented one fails CI instead of shipping inside crates. The profile itself needs
no AOP class: its Study and Assay shapes read the AOP-Wiki IRI a crate mentions
(`https://aopwiki.org/aops/<id>`, `https://aopwiki.org/events/<id>`), so a bare link satisfies them
and a node merely typed `KeyEvent` does not. The crate describes each of the two as one entity at
that IRI, with its name: the pathway a Study mentions and the key event an Assay mentions. The rest
of a pathway stays on AOP-Wiki, and the writer leaves it out whatever the state holds ([the toolbox](toolbox.md)).

A property holding one value is written as that value, never as a one-item array: the compact
JSON-LD form RO-Crate 1.2 recommends (`_write_single_values`, run after assembly and again before
the write, since the preview and the report are added after assembly).

## Build-Path Wiring Contracts (`builder/tools/_crate_mapping.py`)

A single `scaffold_isa_backbone(...)` call yields a crate that passes base and ISA
with zero issues. It fails ISA-Tox on one finding: the Investigation MUST include an
Exposure its root reaches (#781), and only the deposit's evidence can supply one. The
finding names no property, so no field answers it.
A richer build must additionally honour three **conditional Violation traps** — the
build-path *wiring contract* — which fire only when the relevant entities exist
(document for callers):

1. A `PropertyValue` named `DOI`/`PubMedID` is SHACL-duck-typed and MUST carry
   `propertyID` as an **`@id` IRI node** (the OBI IRI: `DOI`→`OBI_0002110`,
   `PubMedID`→`OBI_0001617`). `draft_property_value` defaults the IRI by name and
   `@id`-wraps it — a bare string literal is a Violation.
2. `EndpointReadout`/`DataAnalysis` have **no `result` build-time fallback**
   (unlike TestSystemPreparation/Exposure); a process with no explicit output fires a Violation.
   Their inputs differ: a readout MUST name what it measured
   (`tox:EndpointReadoutConsumesExposedMaterial`) and the build names it when nothing
   upstream does (`_floor_readout_objects`), while a DataAnalysis's `object` is the
   data analysed and has no fallback (`fix_required_issues` wires it only when one
   candidate is unambiguous).
   `draft_process_chain` closes it from the DEPOSIT (raw files → the readout,
   processed → the analysis), and `attach_files` completes any step still unwired.
   When the deposit holds no such file the Violation is **left to fire** (#592):
   this trap is closed with evidence or reported, never with a manufactured file.
   A step nothing evidences at all — neither data nor a procedure document — is
   not drafted: a crate with no evidenced Exposure fails ISA-Tox (#781) rather
   than asserting an experiment it cannot show.
3. What an `Exposure` executes MUST name at least one test or control compound
   (`MolecularEntity`) as a `reagent` (D17). The build writes the reagents from the
   step's `chemicals`, so the finding is answered there: its fix hint names
   `chemicals` (`validation.answering_field`), never the `executesLabProtocol`
   SHACL reports. No code can supply a compound the deposit does not name, so an
   Exposure whose chemicals were never recorded is **left to fire**.

The first two are code-fixable; the third is the user's to answer.

**Fidelity beyond validity — deterministic build-path wiring, not new LLM tools.**
Reproducing the richer structure of a real gold crate is done through `_crate_mapping`
wiring rather than LLM tools (D5 — identifiers come from lookups or the value is
dropped, never fabricated):

- **Identifier PropertyValues** — Person `orcid` → ORCID PV; MolecularEntity
  `cas`/`pubchem_cid` → `[CAS, PubChem CID]`; DOI/PubMed `propertyID` as OBI `@id`
  IRIs; `Person.affiliation` / `Publication.author` resolved as `{@id}` references.
- **Reference wiring** — root `funder` → Organization, root `about` → the DataAnalysis
  LabProcess, Assay `measurementMethod` → BAO `DefinedTerm`, and the hasPart-family
  aliases re-emitted as resolved File refs nested under the assay.
- **CSVW payload** — the full Exposure condition table. Nothing is appended to an
  EndpointReadout: its result is the deposit's raw tier (#589), never a manufactured
  table standing beside it.
- **Characteristics/properties** — CellLineSample `organ`/`tissue` and LabProcess
  `additionalProperty` as PropertyValue characteristics, and source-code co-typing
  (`@type:[File, SoftwareSourceCode, LabProtocol]`, from the extension;
  `schema:programmingLanguage` when the caller names it).

AOP pathways and publications-with-authors are drafted by their own composites
(`resolve_aop`, `draft_publication_with_authors`). Root
`releaseDate` / `dateModified` are set through `set_crate_metadata` and emitted
**only when explicitly supplied** — never guessed (D5); `datePublished` is auto-set
by ro-crate-py at crate construction, so the builder never sets or fabricates it.

## Input & Output Formats

### Input Formats

Input comes in tiers of readiness. The agent should prefer the most structured form available:

| Format | Curation level | Description |
|--------|---------------|-------------|
| **Directory with metadata files** | Medium — partial structure | A research folder that contains some metadata files (README, `.json`, `.yaml`, `.csv`, or other records) alongside raw data. The scanner identifies these by role and the agent drafts entities from whatever structured content they hold — **regardless of the metadata file's format or schema**. Any such file is treated as a generic metadata source, not a special-cased input type. |
| **Unstructured directory** | Low — raw data only | The worst case: a folder of research data with no accompanying metadata. All entities must be drafted from scratch through conversation with the user (file scanning, lookups, and HITL checkpoints). This is the most common real-world scenario. |

**Guiding principle:** Meet the input where it is. Read every metadata file present and reuse every field it can, whatever its structure; if nothing is present, build everything from conversation and lookups. Never discard curated metadata.

### Output Format

The output is a single self-describing RO-Crate directory: `ro-crate-metadata.json`
at its root, describing everything beside it. `export_crate`
(`builder/tools/builder.py`) is the only step that materialises the crate
directory — `build_and_validate` assembles and validates entirely in memory.

```
<output_dir>/                        RO-Crate root directory
├── ro-crate-metadata.json           the RO-Crate metadata descriptor
├── ro-crate-preview.html            browsable preview: the community generator (ro-crate-html,
│                                    `rochtml` installed) else ro-crate-py's template; strings are
│                                    escaped before either renders, and the page's embedded JSON-LD
│                                    is that escaped copy (#86, #169, #849)
├── ro-crate-metadata-maturity.html  maturity report + entity explorer (#85)
└── <payload>                        the described files, copied in
```

A metadata-only export ([the toolbox](toolbox.md)) writes the three generated files and no payload; its
metadata describes each payload file as not present.

Payload files land at their path **relative to the input tree**, so an exported
crate mirrors the folder the depositor handed over. A `File` entity's
`dest_path` is contained to the crate root — an absolute or traversing path is
refused and replaced with a `data/<slug>` fallback, so no payload byte is ever
written outside the output directory (#167). Generated tables land under `data/`
(an exposure's condition table at `data/<exposure>_condition_table.csv`). Every
scanned file the agent did not explicitly place is still attached to the root
`hasPart` as a plain `File` leaf, so an export never silently drops a file
(#175).
