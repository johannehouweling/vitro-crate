# Building the crate

> Entity drafting, crate assembly, the build-path wiring contracts the profiles impose, and the on-disk output.

## Entity Drafters (`builder/tools/drafters.py`)

Each drafter is a **pure state mutation** over hints its caller supplies; the model's part is
recorded as provenance (`created_by="llm"`, `source="llm"`). **Identifiers come from lookups,
never fabricated** (D5): `_resolve_person_orcid` takes the ORCID the deposit states for that
name, else a single full-name search hit, only once the record endpoint confirms it, and
otherwise keeps the deterministic local id. `draft_organization` takes the ROR id the deposit
states for that name once ROR returns the record, and none for a name alone.

Entity types: Investigation, Study, Assay, MolecularEntity, CellLineSample
(CellLine/PrimaryCell), LabProcess (TestSystemPreparation / Exposure / EndpointReadout /
DataAnalysis), Person, Organization, Publication.

## Crate Builder (`builder/tools/builder.py`)

Assembles the crate with [`ro-crate-py`](https://github.com/ResearchObject/ro-crate-py)
(`profiles/models/isa.py`, `profiles/models/tox.py`, `profiles/context.py`); a partial crate
can be built at any point.

- Every `@context` term resolves to its vocabulary's own canonical IRI; a test holds every
  context namespace to a declared allow-list, so an invented one (as the AOP terms once were,
  under an `aopwiki.org` path AOP-Wiki does not serve, #644) fails CI.
- The context declares no AOP term, and the profile needs none: its Study and Assay shapes read
  the AOP-Wiki IRI mentioned (`https://aopwiki.org/aops/<id>`, `.../events/<id>`). The crate
  describes only the pathway a Study mentions and the key event an Assay mentions, each as one
  `DefinedTerm` at that IRI with its name; the address says which of the two it is, to the shapes
  and to every reader of the graph (`provenance_dag._types`). The writer leaves the rest of a
  pathway out whatever the state holds ([the toolbox](toolbox.md)).
- A single-valued property is written as the value, never a one-item array
  (`_write_single_values`, run after assembly and again before the write, since the preview and
  report are added later).

## Build-Path Wiring Contracts (`builder/tools/_crate_mapping.py`)

`scaffold_isa_backbone(...)` alone passes base and ISA with zero issues and fails ISA-Tox on one
finding: the Investigation MUST include an Exposure its root reaches
(`tox:InvestigationMustIncludeAnExposure`, #781). Only deposit
evidence can supply one, and the finding names no property. Three **conditional Violation
traps** fire only when the relevant entities exist:

1. A `PropertyValue` named `DOI` / `PubMedID` MUST carry `propertyID` as an **`@id` node**
   (`OBI_0002110` / `OBI_0001617`; `tox:DOIPropertyValueMustHaveOBIPropertyID`,
   `tox:PubMedIDPropertyValueMustHaveOBIPropertyID`); `draft_property_value` defaults and wraps it. A string
   literal is a Violation.
2. `EndpointReadout` / `DataAnalysis` have **no `result` build-time fallback**
   (`tox:EndpointReadoutRequirements`, `tox:DataAnalysisRequirements`). A readout MUST
   name what it measured (`tox:EndpointReadoutConsumesExposedMaterial`), which
   `_floor_readout_objects` supplies when nothing upstream does; a DataAnalysis's `object` has
   no fallback (`fix_required_issues` wires a sole unambiguous candidate).
   `draft_process_chain` wires outputs from the DEPOSIT (raw → readout, processed → analysis)
   and `attach_files` completes any step still unwired. With no such file the Violation is
   **left to fire** (#592) — never a manufactured file. A step nothing evidences (no data, no
   procedure document) is not drafted.
3. What an `Exposure` executes MUST name a test or control compound as a `reagent`
   (`tox:ExposureRequirements`, D17). The
   build writes reagents from the step's `chemicals`, so the fix hint names `chemicals`
   (`validation.answering_field`), not `executesLabProtocol`. A compound the deposit does not
   name cannot be supplied, so the finding is **left to fire**.

The first two are code-fixable; the third is the user's to answer.

**Fidelity beyond validity** is deterministic `_crate_mapping` wiring, not new LLM tools (D5):

- **Identifier PropertyValues** — Person `orcid` → ORCID PV; MolecularEntity `cas` /
  `pubchem_cid` → `[CAS, PubChem CID]`; DOI/PubMed `propertyID` as OBI `@id`s;
  `Person.affiliation` / `Publication.author` as `{@id}` references.
- **References** — root `funder` → Organization, root `about` → the DataAnalysis, Assay
  `measurementMethod` → BAO `DefinedTerm`, hasPart-family aliases as File refs under the assay.
- **CSVW** — the Exposure condition table. Nothing is appended to an EndpointReadout: its
  result is the deposit's raw tier (#589).
- **Characteristics** — CellLineSample `organ` / `tissue` and LabProcess `additionalProperty`
  as PropertyValues; source code co-typed `[File, SoftwareSourceCode, LabProtocol]` by
  extension, with `schema:programmingLanguage` when named.

Root `releaseDate` / `dateModified` come only from `set_crate_metadata`, never guessed;
`datePublished` is set by ro-crate-py and never by the builder.

## Input & Output Formats

### Input Formats

| Format | Curation level | Description |
|--------|---------------|-------------|
| **Directory with metadata files** | Medium | Metadata files (README, `.json`, `.yaml`, `.csv`, …) beside the data; each is a generic metadata source **whatever its format or schema**. |
| **Unstructured directory** | Low | Data only, the common case; everything is drafted from conversation, scanning, lookups and HITL. |

Meet the input where it is: reuse every field any metadata file holds, and never discard
curated metadata.

### Output Format

One self-describing RO-Crate directory. `export_crate` is the only step that materialises it;
`build_and_validate` works in memory.

```
<output_dir>/
├── ro-crate-metadata.json           the metadata descriptor
├── ro-crate-preview.html            ro-crate-html (`rochtml`) else ro-crate-py's template; strings
│                                    are escaped first, and the embedded JSON-LD is that escaped
│                                    copy (#86, #169, #849)
├── ro-crate-metadata-maturity.html  maturity report + entity explorer (#85)
└── <payload>                        the described files
```

- A metadata-only export ([the toolbox](toolbox.md)) writes the three generated files and no
  payload, describing each payload file as not present.
- Payload lands at its path **relative to the input tree**; an absolute or traversing
  `dest_path` becomes `data/<slug>`, so nothing is written outside the crate (#167). Generated
  tables go under `data/` (`data/<exposure>_condition_table.csv`).
- Every scanned file the agent did not place is still a root `hasPart` `File` leaf, so an
  export never silently drops a file (#175).
