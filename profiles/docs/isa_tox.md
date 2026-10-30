# ISA-Tox RO-Crate Profile

* Version: 1.0.0
* Permalink: <https://w3id.org/ro/crate/isa-tox/1.0>
* Published at: <https://vitro-crate.github.io/isa-tox-ro-crate-profile/1.0/>
* Licence: this text [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the SHACL shapes MIT
* Profile of: [ISA RO-Crate Profile](isa.md) (<https://github.com/nfdi4plants/isa-ro-crate-profile>)
* Authors
  * Jente Houweling - https://orcid.org/0009-0005-3680-0645
* **Table of contents**
  * [Overview](#overview)
  * [Conformance](#conformance)
  * [Requirements](#requirements)
    * [Study - Adverse Outcome Pathway](#study---adverse-outcome-pathway)
    * [Assay - Key Event](#assay---key-event)
    * [MolecularEntity - Test and Control Chemicals](#molecularentity---test-and-control-chemicals)
    * [Sample - Source Cells](#sample---source-cells)
      * [Source Cells - Cell Line](#source-cells---cell-line)
      * [Source Cells - Primary Cells](#source-cells---primary-cells)
    * [Sample - Test System](#sample---test-system)
      * [Test System - Co-Culture](#test-system---co-culture)
    * [Sample - Exposed Test System](#sample---exposed-test-system)
    * [LabProcess - Test System Preparation](#labprocess---test-system-preparation)
    * [LabProcess - Exposure](#labprocess---exposure)
    * [LabProcess - Endpoint Readout](#labprocess---endpoint-readout)
    * [LabProcess - Data Analysis](#labprocess---data-analysis)
    * [LabProtocol - Protocol record](#labprotocol---protocol-record)
  * [Example ro-crate-metadata.json](#example-ro-crate-metadatajson)

## Overview

This profile extends the [ISA](https://isa-tools.org/index.html) [RO-Crate](https://www.researchobject.org/ro-crate/) profile
([ISA RO-Crate Profile](isa.md), originally produced by [NFDI4Plants](https://github.com/nfdi4plants/isa-ro-crate-profile))
with the entity types needed to describe experimental *in vitro* toxicology studies and New Approach Methodologies (NAMs).

It is the domain layer of a three-layer architecture: the base [RO-Crate](https://www.researchobject.org/ro-crate/)
layer provides a portable, self-describing data package on [Schema.org](https://schema.org/); the
[ISA RO-Crate Profile](isa.md) provides the structural Investigation → Study → Assay hierarchy with experimental steps
modelled as [Bioschemas](https://bioschemas.org/) `LabProcess` / `LabProtocol`; and this profile adds the
toxicology-specific, ontology-backed requirements on top.

The extension is additive and reuse-first: each concept is expressed through the most specific applicable
[Schema.org](https://schema.org/) or [Bioschemas](https://bioschemas.org/) type, refined where necessary by an existing
ontology term: a step by the protocol type of the record it executes, a test-system source by its `sampleType`. Test/control chemicals are
[bioschemas.org/MolecularEntity](https://bioschemas.org/MolecularEntity) instances; the cell-based test system's source is a
[Sample](isa.md#sample) carrying `sampleType`, either a cell line with a Cellosaurus `identifier` or primary cells
described by species and cell type; and the experimental workflow is a chain of
[LabProcess](isa.md#labprocess) steps discriminated as `TestSystemPreparation`, `Exposure`, `EndpointReadout`, and `DataAnalysis`.
No new RDF types are introduced, and no change to the base RO-Crate specification or the ISA profile is required.

Requirement levels use [RFC 2119](https://tools.ietf.org/html/rfc2119) keywords, as recommended by the RO-Crate 1.2
profile conventions: MUST (required for conformance), SHOULD (recommended; absence is reported as a warning), and
MAY (optional). The shapes are open-world: a property this profile does not name is neither checked nor
forbidden (no shape declares `sh:closed`), so a crate may carry further metadata without affecting conformance.
The figure below is the model of an *in vitro* NAM study in terms of this profile's entity types: box colour gives
the type, line style the relationship. Each material is a Bioschemas `Sample`, each step a `LabProcess` following a
`LabProtocol`, and the chemicals a `MolecularEntity`.

![The ISA-Tox model of an in vitro NAM study: source cells are prepared into a test system, exposed, read out and
analysed, each step following a protocol record and producing the next material or
file.](figures/isa-tox-model.svg)

> **How the reference builder encodes this.** The tables below give the canonical
> property names and expected types (what the SHACL shapes check). The
> `rocrate-wizard` builder emits them through compact JSON-LD aliases defined in
> the crate's `@context`, all RDF-identical to the canonical predicates:
>
> - `@type` is the bare token (`Sample`, `LabProcess`, `LabProtocol`,
>   `PropertyValue`, …); the `@context` expands it to the Bioschemas/schema.org
>   IRI named in the tables.
> - process parameters appear under `parameter` (→ `schema:additionalProperty`),
>   inputs under `input` (→ `schema:object`), outputs under `output`
>   (→ `schema:result`), and `derivesFrom` expands to `schema:isBasedOn`.
> - each `parameter` is a `PropertyValue` **node** with a deterministic `@id`, a
>   `propertyID` IRI and optional `unitText` — never an inline literal (the
>   LabProcess shapes use `sh:class schema:PropertyValue`).
> - a derived `Sample`'s `derivesFrom` links the Sample it was made from; only cultured
>   material is constrained: at most one cell line, or two or more for a co-culture (#678).
>
> The four LabProcess shapes are **selected by the `intendedUse` of the protocol record a
> step executes**: a term of the test system preparation types set, treatment
> protocol EFO:0003969, assay protocol BAO:0000523, data transformation OBI:0200000. A
> `LabProcess` whose record carries none of these is targeted by no tox shape and is
> checked only by the base + ISA layers, and a type outside the list is a warning. The
> `additionalType` labels older crates carry (`TestSystemPreparation`, `CellCulture`,
> `Exposure`, `EndpointReadout`, `DataAnalysis`) are read as aliases, as are growth
> protocol and transfection stated without the set. The ISA-Tox pass runs at `OPTIONAL` severity, so a missing tox MUST
> is reported but does not fail the CLI exit code (which tracks the base pass).

## Conformance

A crate following this profile declares conformance using the **RO-Crate 1.2** convention: the
*RO-Crate Metadata Descriptor*'s `conformsTo` carries only the single base-specification URI, while the
profile URIs the crate targets are declared on the **Root Data Entity** (`./`). Each referenced profile is
also declared as a `Profile` contextual entity.

```json
{
  "@id": "ro-crate-metadata.json",
  "@type": "CreativeWork",
  "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
  "about": {"@id": "./"}
}
```

The Root Data Entity declares the targeted profiles:

```json
{
  "@id": "./",
  "@type": "Dataset",
  "conformsTo": [
    {"@id": "https://github.com/nfdi4plants/isa-ro-crate-profile"},
    {"@id": "https://w3id.org/ro/crate/isa-tox/1.0"}
  ]
}
```

The ISA layer is declared with the IRI the profile actually extends (see
`profiles/shapes/tox/profile.ttl`, `prof:isProfileOf`); a stable `w3id.org/ro/crate/isa/…`
permalink is not yet registered. The referenced profiles are declared as contextual entities:

```json
{
  "@id": "https://github.com/nfdi4plants/isa-ro-crate-profile",
  "@type": ["CreativeWork", "Profile"],
  "name": "ISA RO-Crate Profile"
},
{
  "@id": "https://w3id.org/ro/crate/isa-tox/1.0",
  "@type": ["CreativeWork", "Profile"],
  "name": "ISA-Tox RO-Crate Profile",
  "version": "0.1.0-draft.1"
}
```

> **Note — RO-Crate 1.2 placement.** RO-Crate **1.2** recommends declaring the profiles a crate targets on the
> *Root Data Entity* (`./`) and reserving the *Metadata Descriptor*'s `conformsTo` for a single base-specification
> value — which this profile now follows (Issue #110). The earlier 1.1 placement (all URIs on the descriptor) was a
> temporary measure while the toolchain lagged: it was lifted once `rocrate-validator` 0.11.0 shipped a `ro-crate-1.2`
> base profile (crs4/rocrate-validator#164), so the base pass validates against 1.2.

A machine-readable [Profile Crate](https://www.researchobject.org/ro-crate/specification/1.2/profiles.html#profile-crate) bundling this
description with the SHACL shapes MAY additionally be published at the profile URI; this is planned but not yet provided.

## Requirements

A crate MUST include at least one Exposure that one of its Studies or Assays is about: it
is not toxicology if nothing is exposed to a chemical (#781). The check targets the Root
Data Entity, so it holds even for a crate that contains nothing else. Each Exposure's
protocol then names its compounds (see [LabProcess - Exposure](#labprocess---exposure)).

### Study - Adverse Outcome Pathway

Is based on the [ISA Study](isa.md#study). A study addresses one Adverse Outcome Pathway, referenced via
`schema:mentions` at the Study level; each of its assays annotates the one Key Event it measures
([Assay - Key Event](#assay---key-event)). The referenced node is the AOP-Wiki entity itself: `@id` is its resolvable
AOP-Wiki IRI (`https://aopwiki.org/aops/<id>`), typed `AdverseOutcomePathway`
([AOPO](http://aopkb.org/aop_ontology#AdverseOutcomePathway), the ontology AOP-Wiki RDF itself uses) and
[DefinedTerm](isa.md#definedterm), with AOP-Wiki as its term set. A crate MAY carry the surrounding AOP-Wiki subgraph
— Key Events and their `KeyEventRelationship` nodes, cross-linked by `upstream_event` / `downstream_event` — so the
pathway reads without a network call.

The adverse outcome the study is ultimately about is reached through the pathway, via its `has_adverse_outcome`,
rather than named again on the Study: an adverse outcome occurs in more than one pathway, so the pathway is the more
specific statement and the one the assays' Key Events are steps of.

The rows below are the [ISA Study](isa.md#study)'s own, repeated so this profile's requirement levels can be read
in one place; `mentions` is this profile's addition. `additionalType` MUST carry 'Study', as the ISA profile requires.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Should be a subdirectory corresponding to this study.|
|@type|MUST|Text|MUST be '[schema.org/Dataset](https://schema.org/Dataset)'|
|identifier|MUST|Text or URL|Identifying descriptor of the study.|
|name|MUST|Text|A title of the study.|
|mentions|SHOULD|[DefinedTerm](isa.md#definedterm)|The Adverse Outcome Pathway the study addresses, as its AOP-Wiki node.|
|about|SHOULD|[bioschemas.org/LabProcess](isa.md#labprocess)|The experimental processes performed in this study.|
|creator|SHOULD|[schema.org/Person](isa.md#person)|The performer of the study.|
|dateCreated|SHOULD|DateTime|When the Study was created.|
|datePublished|SHOULD|DateTime|When the Study was published.|
|description|SHOULD|Text|A short description of the study (e.g. an abstract).|
|hasPart|SHOULD|[schema.org/Dataset](https://schema.org/Dataset) ([Assay](isa.md#assay)) or [File](isa.md#file)|Assays contained in this study or actual data files resulting from the process sequence.|

### Assay - Key Event

Is based on the [ISA Assay](isa.md#assay). An assay measures one endpoint, annotated with the AOP-Wiki Key Event it
reads out via `schema:mentions` — the level below the Study's AOP, so a reader can tell which step of the pathway
each assay covers. The referenced node is the AOP-Wiki Key Event itself: `@id` is its resolvable IRI
(`https://aopwiki.org/events/<id>`), typed `KeyEvent` ([AOPO](http://aopkb.org/aop_ontology#KeyEvent)) and
[DefinedTerm](isa.md#definedterm).

The rows below are the [ISA Assay](isa.md#assay)'s own, repeated so this profile's requirement levels can be read
in one place; `mentions` is this profile's addition. `additionalType` MUST carry 'Assay', as the ISA profile requires.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Should be a subdirectory corresponding to this assay.|
|@type|MUST|Text|MUST be '[schema.org/Dataset](https://schema.org/Dataset)'|
|identifier|MUST|Text or URL|Identifying descriptor of the assay.|
|mentions|SHOULD|[DefinedTerm](isa.md#definedterm)|The Key Event this assay's endpoint reads out, as its AOP-Wiki node.|
|name|SHOULD|Text|A title of the assay.|
|description|SHOULD|Text|A short description of the assay (e.g. an abstract).|
|about|SHOULD|[bioschemas.org/LabProcess](isa.md#labprocess)|The experimental processes performed in this assay.|
|creator|SHOULD|[schema.org/Person](isa.md#person)|The performer of the experiments.|
|hasPart|SHOULD|[File](isa.md#file)|The data files resulting from the process sequence. MUST not be used to directly point to data fragments.|
|measurementMethod|SHOULD|URL or [schema.org/DefinedTerm](isa.md#definedterm)|Describes the type measurement e.g Complexomics or Transcriptomics as an ontology term.|
|measurementTechnique|SHOULD|URL or [schema.org/DefinedTerm](isa.md#definedterm)|Describes the type of technology used to take the measurement, e.g mass spectrometry or deep sequencing.|

### MolecularEntity - Test and Control Chemicals

The test and control chemicals are modelled as [bioschemas.org/MolecularEntity](https://bioschemas.org/MolecularEntity)
instances (rather than the broader `ChemicalSubstance`), so a resolvable identifier and structure make the compound
machine-actionable. Concentration is **not** a property of the compound — it is carried per well by the
[Exposure](#labprocess---exposure)'s condition table (CSVW), so there is no per-concentration chemical Sample.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|A resolvable compound IRI where possible, e.g. a [PubChem](https://pubchem.ncbi.nlm.nih.gov/) or CompoundWiki entry.|
|@type|MUST|Text|MUST be '[bioschemas.org/MolecularEntity](https://bioschemas.org/MolecularEntity)'|
|name|MUST|Text|The compound name.|
|identifier|SHOULD|Text, URL or [schema.org/PropertyValue](isa.md#propertyvalue)|A compound identifier (e.g. InChIKey, CAS, PubChem CID), ideally a PropertyValue carrying its scheme, resolving to an authoritative resource.|
|inChIKey|SHOULD|Text|The hashed InChI for unambiguous structure lookup.|
|smiles|SHOULD|Text|The compound structure as SMILES.|
|inChI|MAY|Text|The compound structure as InChI.|
|molecularFormula|MAY|Text|The molecular formula.|
|url|MAY|URL|Link to an external record, e.g. PubChem, ChEBI or CompoundWiki.|

### Sample - Source Cells

Is based on the Bioschemas [bioschemas.org/Sample](https://bioschemas.org/Sample) type ([ISA Sample](isa.md#sample)).
The cells a [Test System Preparation](#labprocess---test-system-preparation) starts from — the test system in the
sense IUCLID OHT 201 gives it. One Sample carries a categorical annotation via `sampleType`
(a [DefinedTerm](isa.md#definedterm)) saying which kind it is, and each kind adds requirements of its own below. A
Sample a preparation consumes is source cells whatever it states, so one with no `sampleType` is still reported. The
labels `"CellLine"` / `"PrimaryCell"` older crates carry in `additionalType` are aliases.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|Text|MUST be '[bioschemas.org/Sample](https://bioschemas.org/Sample)'|
|sampleType|MUST|[schema.org/DefinedTerm](isa.md#definedterm)|A categorical annotation of the material as a fixed, resolvable ontology term; see the expected terms below.|
|name|MUST|Text|A name identifying the sample, e.g. the cell-line name.|
|additionalProperty|SHOULD|[schema.org/PropertyValue](isa.md#propertyvalue) ([Characteristic](isa.md#propertyvalue---characteristic))|Characteristics of the sample, e.g. passage number or growth conditions.|

**Expected `sampleType` terms.** Each is a [DefinedTerm](isa.md#definedterm) whose `@id` dereferences to the term.
The term is what each section below keys on, so it decides which requirements apply to a Sample.

| term | @id | Applies to |
|------|-----|------------|
|cell line|`http://purl.obolibrary.org/obo/NCIT_C16403`|[Source Cells - Cell Line](#source-cells---cell-line)|
|primary cell|`http://www.ebi.ac.uk/efo/EFO_0002660`|[Source Cells - Primary Cells](#source-cells---primary-cells)|
|cell culture|`http://purl.obolibrary.org/obo/OBI_0001876`|[Sample - Test System](#sample---test-system) and [Sample - Exposed Test System](#sample---exposed-test-system)|
|co-culture|`http://purl.obolibrary.org/obo/NCIT_C93168`|[Test System - Co-Culture](#test-system---co-culture)|

#### Source Cells - Cell Line

`sampleType` "cell line" → [NCIT:C16403](http://purl.obolibrary.org/obo/NCIT_C16403): an immortalized line,
identified by its Cellosaurus accession.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|Text|MUST be '[bioschemas.org/Sample](https://bioschemas.org/Sample)'|
|sampleType|MUST|[schema.org/DefinedTerm](isa.md#definedterm)|"cell line" → [NCIT:C16403](http://purl.obolibrary.org/obo/NCIT_C16403).|
|name|MUST|Text|A name identifying the sample, e.g. the cell-line name.|
|identifier|SHOULD|Text, URL or [schema.org/PropertyValue](isa.md#propertyvalue)|The cell-line identity resolved to a [Cellosaurus](https://www.cellosaurus.org/) accession (e.g. `CVCL_0027`).|
|additionalProperty|SHOULD|[schema.org/PropertyValue](isa.md#propertyvalue) ([Characteristic](isa.md#propertyvalue---characteristic))|Characteristics of the sample, e.g. passage number or growth conditions.|
|url|MAY|URL|Link to the [Cellosaurus](https://www.cellosaurus.org/) entry.|

#### Source Cells - Primary Cells

`sampleType` "primary cell" → [EFO:0002660](http://www.ebi.ac.uk/efo/EFO_0002660). Primary cells are taken directly
from donor tissue and are not immortalized. Cellosaurus holds no record for them and assigns them no RRID
([FAQ Q19](https://www.cellosaurus.org/faq)), so the source is described by the species and the cell type it was
isolated as instead.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|Text|MUST be '[bioschemas.org/Sample](https://bioschemas.org/Sample)'|
|sampleType|MUST|[schema.org/DefinedTerm](isa.md#definedterm)|"primary cell" → [EFO:0002660](http://www.ebi.ac.uk/efo/EFO_0002660); its `termCode` also carries the IUCLID OHT 201 test-system type `IUCLID:108175` ("primary cells").|
|name|MUST|Text|A name identifying the sample, e.g. "kidney tubuloids, donor T19".|
|taxonomicRange|SHOULD|Text, URL or [schema.org/DefinedTerm](isa.md#definedterm)|The species the cells were taken from, e.g. NCBITaxon:9606.|
|additionalProperty|SHOULD|[schema.org/PropertyValue](isa.md#propertyvalue) ([Characteristic](isa.md#propertyvalue---characteristic))|Characteristics of the source; see expected values below. The cell type SHOULD be one.|
|identifier|MAY|Text, URL or [schema.org/PropertyValue](isa.md#propertyvalue)|A BioSamples accession, if the material was deposited. There is no Cellosaurus entry or RRID.|

**Expected `additionalProperty` items.** Each is a Characteristic [PropertyValue](isa.md#propertyvalue---characteristic)
(`additionalType` `"CharacteristicValue"`). Only the cell type is checked by a shape.

| name | Required | propertyID | Notes |
|------|----------|------------|-------|
|cell type|SHOULD|`http://www.ebi.ac.uk/efo/EFO_0000324`|Value e.g. a CL term. The only item a shape checks.|
|Organ|SHOULD|`http://purl.obolibrary.org/obo/UBERON_0000062`|Value e.g. an UBERON term.|
|Tissue|SHOULD|`http://purl.obolibrary.org/obo/UBERON_0000479`|Value e.g. an UBERON term.|
|supplier|SHOULD if commercial|`http://purl.obolibrary.org/obo/NCIT_C90473`||
|catalogue number|SHOULD if commercial|`http://purl.obolibrary.org/obo/NCIT_C99286`||
|lot number|SHOULD if commercial|`http://purl.obolibrary.org/obo/NCIT_C70848`|The lot, not the catalogue number, fixes the donor.|
|donor code|SHOULD|—||
|number of donors|SHOULD|—||
|donor sex|MAY|`http://purl.obolibrary.org/obo/PATO_0000047`||
|donor age|MAY|`http://www.ebi.ac.uk/efo/EFO_0000246`|A reported band, never a birth date.|
|donor health status|MAY|—||

Donor consent or ethics approval MAY be stated at [Study](isa.md#study) level, not on the Sample.

**Modelling rules.**

- A donor pool is **one** primary-cell Sample carrying the number of donors.
- Donors cultured separately are one Sample each.

### Sample - Test System

The material a [Test System Preparation](#labprocess---test-system-preparation) produces: cultured cells, derived
from the [Source Cells](#sample---source-cells) they were grown from. Its `sampleType` is "cell culture"
→ [OBI:0001876](http://purl.obolibrary.org/obo/OBI_0001876).

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|Text|MUST be '[bioschemas.org/Sample](https://bioschemas.org/Sample)'|
|name|MUST|Text|A name identifying the material, e.g. "SK-N-AS, 48 h culture".|
|additionalProperty|SHOULD|[schema.org/PropertyValue](isa.md#propertyvalue) ([Characteristic](isa.md#propertyvalue---characteristic))|Characteristics of the material, e.g. passage number or growth conditions.|
|sampleType|MAY|[schema.org/DefinedTerm](isa.md#definedterm)|"cell culture" → [OBI:0001876](http://purl.obolibrary.org/obo/OBI_0001876). The term is what makes the Sample a test system: without it these requirements do not apply to it.|
|isBasedOn|MAY|[Sample](isa.md#sample)|The material this one was grown from; see the modelling rule below.|

**Modelling rules.**

- A test system derives from **at most one** parent Sample. Culture each source separately, or type material grown
  from several sources as a [co-culture](#test-system---co-culture).

#### Test System - Co-Culture

`sampleType` "co-culture" → [NCIT:C93168](http://purl.obolibrary.org/obo/NCIT_C93168): a test system grown from two
or more source cells. One source grown alone is an ordinary test system, not a co-culture.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|Text|MUST be '[bioschemas.org/Sample](https://bioschemas.org/Sample)'|
|name|MUST|Text|A name identifying the material, e.g. "hepatocyte / Kupffer co-culture".|
|isBasedOn|MUST|[Sample](isa.md#sample)|The two or more Samples this material was grown from.|
|additionalProperty|SHOULD|[schema.org/PropertyValue](isa.md#propertyvalue) ([Characteristic](isa.md#propertyvalue---characteristic))|Characteristics of the material, e.g. passage number or growth conditions.|
|sampleType|MAY|[schema.org/DefinedTerm](isa.md#definedterm)|"co-culture" → [NCIT:C93168](http://purl.obolibrary.org/obo/NCIT_C93168). The term is what makes the Sample a co-culture: without it these requirements do not apply to it.|

### Sample - Exposed Test System

The material an [Exposure](#labprocess---exposure) produces: the test system after the chemical was applied. It
carries the same requirements as the [Test System](#sample---test-system) it derives from — same type, same
`sampleType` "cell culture", `isBasedOn` the test system it was exposed from — so it has no table of its own. What
makes it the exposed material is stated on the step, not on the Sample: an Exposure MUST have the test system as
`schema:object` and the exposed material as `schema:result`. Following `isBasedOn` back from it therefore walks the
whole chain: exposed test system → test system → source cells.

### LabProcess - Test System Preparation

Is based on the Bioschemas DRAFT [bioschemas.org/LabProcess](https://bioschemas.org/LabProcess) type
([ISA LabProcess](isa.md#labprocess)), typed by its protocol record's `intendedUse`, a term of the test system preparation types set, to represent
preparation of the biological model from its source cells, such as its maintenance in culture. A test system may be
prepared in several steps, each typed by what it does. A stably modified line is the source itself, not a step.

The set is a [DefinedTermSet](https://schema.org/DefinedTermSet), `https://w3id.org/ro/crate/isa-tox/1.0/preparation-types`, published with this
profile; the term states it with `inDefinedTermSet`:

| Preparation type | Term |
|------------------|------|
|Culture, plating|[EFO:0003789](http://www.ebi.ac.uk/efo/EFO_0003789) *growth protocol*|
|Transient transfection|[OBI:0001152](http://purl.obolibrary.org/obo/OBI_0001152) *transfection*|
|Differentiation|[GO:0030154](http://purl.obolibrary.org/obo/GO_0030154) *cell differentiation*|

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|Text|MUST be '[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)'|
|name|MUST|Text|The name of the process, e.g. "Cell Culture".|
|object|MUST|[bioschemas.org/Sample](isa.md#sample)|The input source sample(s): a cell line or primary cells. At least one.|
|result|MUST|[bioschemas.org/Sample](isa.md#sample)|The output (cultured) sample(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](isa.md#propertyvalue) ([Parameter](isa.md#propertyvalue---parameter))|Process parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD).|

**Expected `parameterValue` items.** Each is a Parameter [PropertyValue](isa.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). The profile requires at least one `parameterValue`; the following are recommended
for this step:

| name | Required | propertyID |
|------|----------|------------|
|Culture Medium|SHOULD|`http://www.bioassayontology.org/bao#BAO_0000114`|

### LabProcess - Exposure

Is based on the Bioschemas DRAFT [bioschemas.org/LabProcess](https://bioschemas.org/LabProcess) type
([ISA LabProcess](isa.md#labprocess)), typed by its protocol record's `intendedUse`, treatment protocol (EFO:0003969), to represent exposing the cell-based test system to
the chemical(s). It takes the cultured cell [Sample](isa.md#sample) as its `object`, executes a protocol record naming
the compounds it applied, and emits the exposed cell [Sample](isa.md#sample)(s) as its `result`. The experimental design
is a run-specific condition table (CSVW): a data file `about` those exposed Samples, in which each row records a single
well (cell line, compound, concentration, exposure duration).

> **Where the compound goes.** The [MolecularEntity](#molecularentity---chemical) compound is **not** a process
> `object`: the inherited ISA [LabProcess](isa.md#labprocess) shape restricts `schema:object` (and `schema:result`)
> to `File`/`Sample`/`BioSample`, so a `MolecularEntity` there fails validation. The compound is a `reagent` of the
> Exposure's [protocol record](#labprotocol---protocol-record), which MUST name at least one. The procedural SOPs name no
> compound and are what the record `isBasedOn`, so the assay stays executable with other chemicals: a re-run needs a new
> record and condition table and the same SOPs.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|Text|MUST be '[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)'|
|name|MUST|Text|The name of the process, e.g. "Exposure".|
|object|MUST|[bioschemas.org/Sample](isa.md#sample) or [File](https://schema.org/MediaObject)|The input cell sample(s) being exposed. At least one. (A `MolecularEntity` is **not** allowed here — see note above.)|
|parameterValue|MUST|[schema.org/PropertyValue](isa.md#propertyvalue) ([Parameter](isa.md#propertyvalue---parameter))|Exposure parameter(s); see expected values below. At least one.|
|result|MUST|[bioschemas.org/Sample](isa.md#sample) or [File](https://schema.org/MediaObject)|The exposed sample(s) the step produces. At least one. The condition table is **not** a result: it is a `File` + `csvw:Table` `about` the exposed Samples, and each exposure SHOULD have one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD), whose `reagent`s MUST include at least one test or control compound (a MolecularEntity).|

**Expected `parameterValue` items.** Each is a Parameter [PropertyValue](isa.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). The profile requires at least one `parameterValue`; the following are recommended
for this step:

| name | Required | propertyID |
|------|----------|------------|
|Exposure Duration|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C83280`|
|Cell Seeding Density|SHOULD|`http://purl.obolibrary.org/obo/MSIO_0000062`|
|Microplate|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C43377`|

### LabProcess - Endpoint Readout

Is based on the Bioschemas DRAFT [bioschemas.org/LabProcess](https://bioschemas.org/LabProcess) type
([ISA LabProcess](isa.md#labprocess)), typed by its protocol record's `intendedUse`, assay protocol (BAO:0000523), to represent measurement of the assay endpoint.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|Text|MUST be '[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)'|
|name|MUST|Text|The name of the process, e.g. "Endpoint Readout".|
|object|MUST|[bioschemas.org/Sample](isa.md#sample) or [File](https://schema.org/MediaObject)|What the step measured: the exposed Sample(s) (the cultured Sample in an assay with no exposure), or the File(s) a re-analysis measures. At least one.|
|result|MUST|[File](https://schema.org/MediaObject)|The output data file(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](isa.md#propertyvalue) ([Parameter](isa.md#propertyvalue---parameter))|Measurement parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD).|

**Expected `parameterValue` items.** Each is a Parameter [PropertyValue](isa.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). The profile requires at least one `parameterValue`; the following are recommended
for this step:

| name | Required | propertyID |
|------|----------|------------|
|Detection Instrument|SHOULD|`http://www.bioassayontology.org/bao#BAO_0000697`|
|Instrument Manufacturer|SHOULD|`http://www.bioassayontology.org/bao#BAO_0002628`|
|Measured Entity|SHOULD|`http://www.bioassayontology.org/bao#BAO_0002001`|
|Technical replicate|SHOULD|`http://www.ebi.ac.uk/efo/EFO_0002090`|
|Endpoint|SHOULD|`http://www.bioassayontology.org/bao#BAO_0000179`|
|Assay Kit|MAY|`http://www.bioassayontology.org/bao#BAO_0000248`|
|Substrate|MAY|`http://www.bioassayontology.org/bao#BAO_0003063`|

> **Note.** The `propertyID` IRIs above record exactly what the reference builder emits and are not yet normalised to a
> single resolver; their curation and the mechanism that scores these parameters against the OECD / community reporting
> standards are described in [`../mit/docs/MIT_METADATA_WIRING.md`](../mit/docs/MIT_METADATA_WIRING.md).

### LabProcess - Data Analysis

Is based on the Bioschemas DRAFT [bioschemas.org/LabProcess](https://bioschemas.org/LabProcess) type
([ISA LabProcess](isa.md#labprocess)), typed by its protocol record's `intendedUse`, data transformation (OBI:0200000), to represent the processing pipeline that turns the
raw endpoint measurements into the reported results. It consumes the [Endpoint Readout](#labprocess---endpoint-readout)'s
raw-data File(s) and emits the processed-data File(s) along the derivation graph. It corresponds to the *Analysis and
Statistics* module of the Minimal Information Table.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|Text|MUST be '[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)'|
|name|MUST|Text|The name of the process, e.g. "Data Analysis".|
|object|MUST|[File](https://schema.org/MediaObject) or [bioschemas.org/Sample](isa.md#sample)|The raw-data input(s) being analysed. At least one.|
|result|MUST|[File](https://schema.org/MediaObject)|The processed-data output file(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](isa.md#propertyvalue) ([Parameter](isa.md#propertyvalue---parameter))|Analysis parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD), which SHOULD be `isBasedOn` the deposited analysis script (a File co-typed `SoftwareSourceCode` and `LabProtocol`).|

**Expected `parameterValue` items.** Each is a Parameter [PropertyValue](isa.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). The profile requires at least one `parameterValue`; the following are recommended
for this step (drawn from OHT 201's *Data & Analysis* module):

| name | Required |
|------|----------|
|Data Calculation and Statistics|SHOULD|
|Computational Tool|SHOULD|
|Acceptance Criteria|MAY|
|Evaluation Criteria|MAY|

### LabProtocol - Protocol record

Is based on the Bioschemas [bioschemas.org/LabProtocol](https://bioschemas.org/LabProtocol) type
([ISA LabProtocol](isa.md#labprotocol)). Each LabProcess above executes exactly one: a record of the protocol its step
follows, present whether or not the lab deposited a document for it, because its `intendedUse` says what kind of step
it is. A deposited SOP or script stays a `File` co-typed `LabProtocol` (the RO-Crate 1.2 multi-type idiom) and is what
the record is `isBasedOn`, so one document can serve several steps, each record carrying its own type. ISA-JSON has no
`isBasedOn` on a protocol, so that link is lost on conversion to ISA-JSON.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|One record per step, e.g. the process `@id` followed by `_protocol`.|
|@type|MUST|Text|MUST be '[bioschemas.org/LabProtocol](https://bioschemas.org/LabProtocol)'|
|intendedUse|SHOULD|[schema.org/DefinedTerm](isa.md#definedterm)|The protocol type of its step, from the table below, written as `{"@id": …}` and described once in the graph.|
|name|SHOULD|Text|The name of the protocol, e.g. "Exposure protocol".|
|description|SHOULD|Text|What the lab stated about the procedure; a builder writes none of its own.|
|url|MAY|URL|Where the lab published the procedure, when it stated one.|
|reagent|MAY|[bioschemas.org/MolecularEntity](#molecularentity---chemical)|The compounds the protocol applied. On the Exposure's record this is a MUST: the test and control compounds, at least one, checked on the Exposure step.|
|isBasedOn|MAY|[File](https://schema.org/MediaObject)|The deposited documents the step follows.|

| Step | intendedUse |
|------|-------------|
|Test System Preparation|a term of the [test system preparation types](#labprocess---test-system-preparation) set|
|Exposure|[EFO:0003969](http://www.ebi.ac.uk/efo/EFO_0003969) *treatment protocol*|
|Endpoint Readout|[BAO:0000523](http://www.bioassayontology.org/bao#BAO_0000523) *assay protocol*|
|Data Analysis|[OBI:0200000](http://purl.obolibrary.org/obo/OBI_0200000) *data transformation*: no protocol-type term exists, so the OBI process class stands in, as ISA's reference dataset BII-I-1 does for extraction|

## Example ro-crate-metadata.json

Crates conforming to this profile are published at <https://github.com/johannehouweling/crates>. Each types
every step by its protocol record's `intendedUse` and every test-system source by its `sampleType`, as
described above, and is validated in three passes (base RO-Crate 1.2 → ISA → ISA-Tox).
