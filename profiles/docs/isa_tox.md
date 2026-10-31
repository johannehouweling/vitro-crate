# ISA-Tox RO-Crate Profile

* Version: 1.0.0
* Permalink: <https://w3id.org/ro/crate/isa-tox/1.0>
* Published at: <https://vitro-crate.github.io/isa-tox-ro-crate-profile/1.0/>
* Licence: this text [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the SHACL shapes MIT
* Profile of: [ISA RO-Crate Profile](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md) (<https://github.com/nfdi4plants/isa-ro-crate-profile>)
* Authors
  * Jente Houweling - <https://orcid.org/0009-0005-3680-0645>
* **Table of contents**
  * [Overview](#overview)
  * [Conformance](#conformance)
  * [Requirements](#requirements)
    * [Study - Adverse Outcome Pathway](#study---adverse-outcome-pathway)
    * [Assay - Key Event](#assay---key-event)
    * [MolecularEntity - Test and Control Chemicals](#molecularentity---test-and-control-chemicals)
    * [Sample - Source Cells](#sample---source-cells)
    * [Sample - Test System](#sample---test-system)
    * [Sample - Exposed Test System](#sample---exposed-test-system)
    * [LabProcess - Test System Preparation](#labprocess---test-system-preparation)
    * [LabProcess - Exposure](#labprocess---exposure)
    * [LabProcess - Endpoint Readout](#labprocess---endpoint-readout)
    * [LabProcess - Data Analysis](#labprocess---data-analysis)
    * [LabProtocol - Protocol record](#labprotocol---protocol-record)
  * [Example ro-crate-metadata.json](#example-ro-crate-metadatajson)

## Overview

This profile extends the [ISA](https://isa-tools.org/index.html) [RO-Crate](https://www.researchobject.org/ro-crate/) profile
([ISA RO-Crate Profile](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md), originally produced by [NFDI4Plants](https://github.com/nfdi4plants/isa-ro-crate-profile))
with the entity types needed to describe experimental *in vitro* toxicology studies and New Approach Methodologies (NAMs).

[RO-Crate](https://www.researchobject.org/ro-crate/) provides a portable data package expressed in
[Schema.org](https://schema.org/) terms. The [ISA profile](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md) defines the Investigation → Study → Assay
hierarchy and represents experimental steps as [Bioschemas](https://bioschemas.org/) `LabProcess` and
`LabProtocol` entities. This profile specifies the toxicological requirements and the ontology terms used to
express them.

Chemical entities are represented as [MolecularEntity](https://bioschemas.org/MolecularEntity) instances,
biological materials as [BioSample](https://bioschemas.org/BioSample) entities, a Bioschemas subtype of
[Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample), classified by `sampleType`, and experimental steps as
[LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess) entities classified by the `intendedUse` of the protocol record they execute:
`TestSystemPreparation`, `Exposure`, `EndpointReadout` or `DataAnalysis`. A step whose protocol record declares
none of these four terms is validated against the base and ISA layers only; a term outside this set is reported
as a warning.

The entity types align with the six modules of the Minimum Information Table for the Safety of Engineered
Nanomaterials ([MIT Nanosafety](https://fairsharing.org/5228), Elberskirch et al.): general information with the Investigation, Study and Assay;
material details with the MolecularEntity; biological model specifics with the Samples; and exposure conditions,
endpoint readouts, and analysis and statistics with the four step types.

Requirement levels follow [RFC 2119](https://tools.ietf.org/html/rfc2119).

![The ISA-Tox model of an in vitro NAM study: source cells are prepared into a test system, exposed, read out and
analysed, each step following a protocol record and producing the next material or
file.](figures/isa-tox-model.svg)

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

No `w3id.org/ro/crate/isa/…` permalink has been registered; the ISA layer is therefore identified by the IRI
the profile extends. Both referenced profiles are declared as contextual entities:

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

> **Note on RO-Crate 1.2 placement:** RO-Crate **1.2** recommends declaring the profiles a crate targets on the
> *Root Data Entity* (`./`) and reserving the *Metadata Descriptor*'s `conformsTo` for a single base-specification
> value — which this profile follows.

A machine-readable [Profile Crate](https://www.researchobject.org/ro-crate/specification/1.2/profiles.html#profile-crate) bundling this
description with the SHACL shapes MAY additionally be published at the profile URI; this is planned but not yet provided.

## Requirements

An Investigation MUST include at least one Exposure referenced by one of its Studies or Assays. A crate that
records no exposure of a test system to a chemical does not describe an *in vitro* toxicological experiment.

### Study - Adverse Outcome Pathway

Is based on the [ISA Study](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#study). A study should address one Adverse Outcome Pathway via `schema:mentions`;
its assays annotate the Key Events ([Assay - Key Event](#assay---key-event)). The referenced node is the AOP-Wiki
entity: `@id` is `https://identifiers.org/aop/<id>`, typed `AdverseOutcomePathway`
([AOPO](http://aopkb.org/aop_ontology#AdverseOutcomePathway)) and [DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm), with the wiki
page as `url`. A crate MAY carry the surrounding subgraph: Key Events (`aop.events/<id>`) and
`KeyEventRelationship` nodes (`aop.relationships/<id>`) cross-linked by `upstream_event` and `downstream_event`.
The adverse outcome is reached through the pathway's `has_adverse_outcome`, not named on the Study.

The following rows are those of the ISA Study, reproduced with the requirement levels this profile assigns.
`mentions` is introduced by this profile.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Should be a subdirectory corresponding to this study.|
|@type|MUST|[schema.org/Dataset](https://schema.org/Dataset)|—|
|additionalType|MUST|Text or URL|'Study' or ontology term to identify it as a Study. It is what selects the entity, so no shape can check it.|
|identifier|MUST|Text or URL|Identifying descriptor of the study.|
|name|MUST|Text|A title of the study.|
|mentions|SHOULD|[DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|The Adverse Outcome Pathway the study addresses, as its AOP-Wiki node.|
|about|SHOULD|[bioschemas.org/LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess)|The experimental processes performed in this study.|
|creator|SHOULD|[schema.org/Person](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#person)|The performer of the study.|
|dateCreated|SHOULD|DateTime|When the Study was created.|
|datePublished|SHOULD|DateTime|When the Study was published.|
|description|SHOULD|Text|A short description of the study (e.g. an abstract).|
|hasPart|SHOULD|[schema.org/Dataset](https://schema.org/Dataset) ([Assay](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#assay)) or [File](https://schema.org/MediaObject)|Assays contained in this study or actual data files resulting from the process sequence.|

**Expected identifier [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) items:** Where a study's `citation` carries its publication's
identifiers, each identifier is a PropertyValue whose `propertyID` declares the identifier scheme. The
`propertyID` of each is validated.

| name | Required | propertyID |
|------|----------|------------|
|DOI|MUST if stated|`http://purl.obolibrary.org/obo/OBI_0002110`|
|PubMedID|MUST if stated|`http://purl.obolibrary.org/obo/OBI_0001617`|

### Assay - Key Event

Is based on the [ISA Assay](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#assay). An assay measures one endpoint and names the KeyEvent it
reads out via `schema:mentions`: `@id` is `https://identifiers.org/aop.events/<id>`, typed `KeyEvent`
([AOPO](http://aopkb.org/aop_ontology#KeyEvent)) and [DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm). What it measures is named in
[BAO](https://bioassayontology.org/) terms through `measurementMethod` and `measurementTechnique`.

The following rows are those of the ISA Assay, reproduced with the requirement levels this profile assigns.
`mentions` is introduced by this profile.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Should be a subdirectory corresponding to this assay.|
|@type|MUST|[schema.org/Dataset](https://schema.org/Dataset)|—|
|additionalType|MUST|Text or URL|'Assay' or ontology term to identify it as an Assay. It is what selects the entity, so no shape can check it.|
|identifier|MUST|Text or URL|Identifying descriptor of the assay.|
|mentions|SHOULD|[DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|The Key Event this assay's endpoint reads out, as its AOP-Wiki node.|
|name|SHOULD|Text|A title of the assay.|
|description|SHOULD|Text|A short description of the assay (e.g. an abstract).|
|about|SHOULD|[bioschemas.org/LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess)|The experimental processes performed in this assay.|
|creator|SHOULD|[schema.org/Person](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#person)|The performer of the experiments.|
|hasPart|SHOULD|[File](https://schema.org/MediaObject)|The data files resulting from the process sequence. MUST not be used to directly point to data fragments.|
|measurementMethod|SHOULD|URL or [schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|What is measured, as an ontology term — for an in vitro assay a [BAO](https://bioassayontology.org/) term, e.g. the endpoint `BAO:0000179`.|
|measurementTechnique|SHOULD|URL or [schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|How it is measured, as an ontology term — for an in vitro assay a [BAO](https://bioassayontology.org/) term, e.g. the detection instrument `BAO:0000697`.|

### MolecularEntity - Test and Control Chemicals

Test and control chemicals are represented as [MolecularEntity](https://bioschemas.org/MolecularEntity)
instances, each denoting a single defined molecule and bearing the structural properties specified below.
Schema.org's [`ChemicalSubstance`](https://schema.org/ChemicalSubstance) defines no structural property and is
not used. Test items that are mixtures are outside the scope of this version.

Concentration is **not** a property of the compound. It is recorded per well in the
[Exposure](#labprocess---exposure)'s condition table.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|SHOULD be an absolute, resolvable compound IRI — a [PubChem](https://pubchem.ncbi.nlm.nih.gov/), [CompTox](https://comptox.epa.gov/dashboard/) or [CompoundWiki](https://compoundcloud.wikibase.cloud/) entry (`https://compoundcloud.wikibase.cloud/entity/Q<n>`). A crate-local identifier (`#iopanoic-acid`) is allowed, and is the only option for a coded or proprietary test item with no public record; the compound's identity is carried by `inChIKey` and `identifier` either way.|
|@type|MUST|[bioschemas.org/MolecularEntity](https://bioschemas.org/MolecularEntity)|—|
|name|MUST|Text|The compound name.|
|identifier|SHOULD|Text, URL or [schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue)|A compound identifier (e.g. InChIKey, CAS, PubChem CID, or a [CompoundWiki](https://compoundcloud.wikibase.cloud/) `Q` id), ideally a PropertyValue carrying its scheme, resolving to an authoritative resource.|
|inChIKey|SHOULD|Text|The hashed InChI for unambiguous structure lookup.|
|smiles|SHOULD|Text|The compound structure as SMILES.|
|inChI|MAY|Text|The compound structure as InChI.|
|molecularFormula|MAY|Text|The molecular formula.|
|url|MAY|URL|Link to an external record, e.g. PubChem, ChEBI or [CompoundWiki](https://compoundcloud.wikibase.cloud/).|

### Sample - Source Cells

Based on the Bioschemas [Sample](https://bioschemas.org/Sample) type ([ISA Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)). Source cells are
the material from which a [Test System Preparation](#labprocess---test-system-preparation) proceeds. The
`sampleType` property classifies the material. Cell lines and
primary cells carry the additional requirements specified below; materials of any other kind are governed by this
section alone. A Sample consumed by a preparation step is source cells irrespective of its declared `sampleType`.
The values `"CellLine"` and `"PrimaryCell"` in `additionalType` are retained as aliases.

Samples are typed as both `Sample` and [BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of
`Sample`. Validation is performed against `Sample`; a BioSample-aware consumer may read `cellType`,
`anatomicalStructure` and `sex` directly. These three properties express the same assertions as the corresponding
Characteristic [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic) items against which conformance is determined,
and are therefore optional.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|[BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of [bioschemas.org/Sample](https://bioschemas.org/Sample)|The array `["BioSample", "Sample"]`, most specific first. `Sample` MUST stay in it: every requirement here is matched on that type.|
|sampleType|MUST|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|A categorical annotation of the material as a fixed, resolvable ontology term; see the expected terms below.|
|cellType|MAY|Text, URL or [schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|BioSample: the cell line, lineage or biological classification of a cell. Also carried as a cell-type Characteristic (`EFO:0000324`), which is what the shapes check.|
|anatomicalStructure|MAY|[schema.org/AnatomicalStructure](https://schema.org/AnatomicalStructure), [DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm) or URL|BioSample: the organ or tissue the material was collected from. Also carried as Organ / Tissue Characteristics (`UBERON:0000062`, `UBERON:0000479`).|
|name|MUST|Text|A name identifying the sample, e.g. the cell-line name.|
|additionalProperty|SHOULD|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Characteristic](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic))|Characteristics of the sample, e.g. passage number or growth conditions.|

**Expected `sampleType` terms:** Each is a [DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm) whose `@id` dereferences to the term.
The sections below are selected by this term. A material whose kind has no corresponding term in this table is
reported, which also detects a malformed `sampleType`.

| term | @id | Applies to |
|------|-----|------------|
|cell line|`http://purl.obolibrary.org/obo/NCIT_C16403`|[Source Cells - Cell Line](#source-cells---cell-line)|
|primary cell|`http://www.ebi.ac.uk/efo/EFO_0002660`|[Source Cells - Primary Cells](#source-cells---primary-cells)|
|cell culture|`http://purl.obolibrary.org/obo/OBI_0001876`|[Sample - Test System](#sample---test-system) and [Sample - Exposed Test System](#sample---exposed-test-system)|
|co-culture|`http://purl.obolibrary.org/obo/NCIT_C93168`|[Test System - Co-Culture](#test-system---co-culture)|
|organoid|`http://purl.obolibrary.org/obo/NCIT_C172259`|This section; no further requirements.|
|tissue|`http://purl.obolibrary.org/obo/UBERON_0000479`|This section; no further requirements.|
|organ part|`http://purl.obolibrary.org/obo/UBERON_0000064`|This section; no further requirements.|
|multicellular organism|`http://purl.obolibrary.org/obo/UBERON_0000468`|This section; no further requirements.|

#### Source Cells - Cell Line

`sampleType` "cell line" → [NCIT:C16403](http://purl.obolibrary.org/obo/NCIT_C16403): an immortalized line,
identified by its Cellosaurus accession.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|[BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of [bioschemas.org/Sample](https://bioschemas.org/Sample)|The array `["BioSample", "Sample"]`, most specific first. `Sample` MUST stay in it: every requirement here is matched on that type.|
|sampleType|MUST|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|"cell line" → [NCIT:C16403](http://purl.obolibrary.org/obo/NCIT_C16403).|
|name|MUST|Text|A name identifying the sample, e.g. the cell-line name.|
|identifier|SHOULD|Text, URL or [schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue)|The cell-line identity resolved to a [Cellosaurus](https://www.cellosaurus.org/) accession (e.g. `CVCL_0027`).|
|additionalProperty|SHOULD|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Characteristic](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic))|Characteristics of the sample, e.g. passage number or growth conditions.|
|url|MAY|URL|Link to the [Cellosaurus](https://www.cellosaurus.org/) entry.|

#### Source Cells - Primary Cells

`sampleType` "primary cell" → [EFO:0002660](http://www.ebi.ac.uk/efo/EFO_0002660). Primary cells are taken directly
from donor tissue and are not immortalized. Cellosaurus holds no record for them and assigns them no RRID
([FAQ Q19](https://www.cellosaurus.org/faq)), so the source is described by the species and the cell type it was
isolated as instead.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|[BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of [bioschemas.org/Sample](https://bioschemas.org/Sample)|The array `["BioSample", "Sample"]`, most specific first. `Sample` MUST stay in it: every requirement here is matched on that type.|
|sampleType|MUST|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|"primary cell" → [EFO:0002660](http://www.ebi.ac.uk/efo/EFO_0002660); its `termCode` also carries the IUCLID OHT 201 test-system type `IUCLID:108175` ("primary cells").|
|name|MUST|Text|A name identifying the sample, e.g. "kidney tubuloids, donor T19".|
|taxonomicRange|SHOULD|Text, URL or [schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|The species the cells were taken from, e.g. NCBITaxon:9606.|
|sex|MAY|Text, URL or [schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|BioSample: the biological sex of the donor the cells were taken from. Also carried as a donor-sex Characteristic (`PATO:0000047`).|
|additionalProperty|SHOULD|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Characteristic](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic))|Characteristics of the source; see expected values below. The cell type SHOULD be one.|
|identifier|MAY|Text, URL or [schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue)|A BioSamples accession, if the material was deposited. There is no Cellosaurus entry or RRID.|

**Expected `additionalProperty` items:** Each is a Characteristic [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic)
(`additionalType` `"CharacteristicValue"`). Only the cell type is validated. Donor sex is expressed by the
BioSample `sex` property in the table above and not as a characteristic.

| name | Required | propertyID | Notes |
|------|----------|------------|-------|
|cell type|SHOULD|`http://www.ebi.ac.uk/efo/EFO_0000324`|A CL term. The only item in this table that is validated. Also expressible as `cellType`.|
|Organ|SHOULD|`http://purl.obolibrary.org/obo/UBERON_0000062`|An UBERON term. Also expressible as `anatomicalStructure`.|
|Tissue|SHOULD|`http://purl.obolibrary.org/obo/UBERON_0000479`|An UBERON term. Also expressible as `anatomicalStructure`.|
|passage|SHOULD|`http://www.ebi.ac.uk/efo/EFO_0007061`|The passage number the material was used at.|
|growth|SHOULD|`http://www.ebi.ac.uk/efo/EFO_0000523`|The growth or culture conditions.|
|supplier|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C90473`|For commercially sourced material.|
|catalogue number|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C99286`|For commercially sourced material.|
|lot number|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C70848`|For commercially sourced material. The lot, not the catalogue number, identifies the donor.|
|donor code|SHOULD|—||
|number of donors|SHOULD|—||
|donor age|MAY|`http://www.ebi.ac.uk/efo/EFO_0000246`|A reported band, never a birth date.|
|donor health status|MAY|—||

### Sample - Test System

The test system a [Test System Preparation](#labprocess---test-system-preparation) produces from its
[Source Cells](#sample---source-cells). Its `sampleType` is "cell culture"
→ [OBI:0001876](http://purl.obolibrary.org/obo/OBI_0001876).

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|[BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of [bioschemas.org/Sample](https://bioschemas.org/Sample)|The array `["BioSample", "Sample"]`, most specific first. `Sample` MUST stay in it: every requirement here is matched on that type.|
|name|MUST|Text|A name identifying the material, e.g. "SK-N-AS, 48 h culture".|
|additionalProperty|SHOULD|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Characteristic](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic))|Characteristics of the material, e.g. passage number or growth conditions.|
|sampleType|MAY|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|"cell culture" → [OBI:0001876](http://purl.obolibrary.org/obo/OBI_0001876). The term is what makes the Sample a test system: without it these requirements do not apply to it.|
|isBasedOn|SHOULD|[Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)|The material this one was grown from — the source cells for a test system, the test system for the exposed material. See the modelling rules below.|

**Modelling rules:**

- A test system derives from **at most one** parent Sample. Culture each source separately, or type material grown
  from several sources as a [co-culture](#test-system---co-culture).

#### Test System - Co-Culture

`sampleType` "co-culture" → [NCIT:C93168](http://purl.obolibrary.org/obo/NCIT_C93168): a test system grown from two
or more source cells. One source grown alone is an ordinary test system, not a co-culture.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could be the unique sample name.|
|@type|MUST|[BioSample](https://bioschemas.org/BioSample), a Bioschemas subtype of [bioschemas.org/Sample](https://bioschemas.org/Sample)|The array `["BioSample", "Sample"]`, most specific first. `Sample` MUST stay in it: every requirement here is matched on that type.|
|name|MUST|Text|A name identifying the material, e.g. "hepatocyte / Kupffer co-culture".|
|isBasedOn|MUST|[Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)|The two or more Samples this material was grown from.|
|additionalProperty|SHOULD|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Characteristic](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---characteristic))|Characteristics of the material, e.g. passage number or growth conditions.|
|sampleType|MAY|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|"co-culture" → [NCIT:C93168](http://purl.obolibrary.org/obo/NCIT_C93168). The term is what makes the Sample a co-culture: without it these requirements do not apply to it.|

### Sample - Exposed Test System

The test system following application of the chemical. It carries the requirements of the
[Test System](#sample---test-system) and declares `isBasedOn` the test system from which it was exposed, and so
has no table of its own. The exposed material is identified by the step rather than by the Sample: an Exposure
MUST declare the test system as `schema:object` and the exposed material as `schema:result`. Traversing
`isBasedOn` in reverse yields the derivation chain: exposed test system → test system → source cells.

### LabProcess - Test System Preparation

A [LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess) that prepares the biological model from its source cells, including its
maintenance in culture, typed by the `intendedUse` of its protocol record, one of the preparation types
below. A test system may be prepared in several steps. A stably modified line constitutes the source material
itself and is not represented as a preparation step.

A step is recognised as a test system preparation when the `intendedUse` of its protocol record is one of these
terms, reused from EFO, OBI and GO:

| Preparation type | Term |
|------------------|------|
|Culture, plating|[EFO:0003789](http://www.ebi.ac.uk/efo/EFO_0003789) *growth protocol*|
|Transient transfection|[OBI:0001152](http://purl.obolibrary.org/obo/OBI_0001152) *transfection*|
|Differentiation|[GO:0030154](http://purl.obolibrary.org/obo/GO_0030154) *cell differentiation*|

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)|—|
|name|MUST|Text|The name of the process, e.g. "Cell Culture".|
|object|MUST|[bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)|The input source sample(s): a cell line or primary cells. At least one.|
|result|MUST|[bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)|The output (cultured) sample(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Parameter](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter))|Process parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD).|

**Expected `parameterValue` items:** Each is a Parameter [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). At least one `parameterValue` is required. The following are recommended
for this step:

| name | Required | propertyID |
|------|----------|------------|
|Culture Medium|SHOULD|`http://www.bioassayontology.org/bao#BAO_0000114`|

### LabProcess - Exposure

A [LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess) that exposes the test system to one or more chemicals, typed by the
`intendedUse` of its protocol record, treatment protocol (EFO:0003969). It declares the cultured
[Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample) as `object`, executes a protocol record naming the compounds applied, and declares the
exposed [Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)(s) as `result`.

The experimental design is recorded in a condition table (CSVW): a data file `about` those exposed Samples, with
one row per well giving cell line, compound, concentration and exposure duration.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)|—|
|name|MUST|Text|The name of the process, e.g. "Exposure".|
|object|MUST|[bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample) or [File](https://schema.org/MediaObject)|The input cell sample(s) being exposed. At least one. (A `MolecularEntity` is **not** allowed here — see note above.)|
|parameterValue|MUST|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Parameter](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter))|Exposure parameter(s); see expected values below. At least one.|
|result|MUST|[bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample) or [File](https://schema.org/MediaObject)|The exposed sample(s) the step produces. At least one. The condition table is **not** a result: it is a `File` + `csvw:Table` `about` the exposed Samples, and each exposure SHOULD have one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD), whose `reagent`s MUST include at least one test or control compound (a MolecularEntity).|

**Expected `parameterValue` items:** Each is a Parameter [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). At least one `parameterValue` is required. The following are recommended
for this step:

| name | Required | propertyID |
|------|----------|------------|
|Exposure Duration|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C83280`|
|Cell Seeding Density|SHOULD|`http://purl.obolibrary.org/obo/MSIO_0000062`|
|Microplate|SHOULD|`http://purl.obolibrary.org/obo/NCIT_C43377`|

### LabProcess - Endpoint Readout

A [LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess) that measures the assay endpoint, typed by the `intendedUse` of its protocol
record, assay protocol (BAO:0000523).

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)|—|
|name|MUST|Text|The name of the process, e.g. "Endpoint Readout".|
|object|MUST|[bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample) or [File](https://schema.org/MediaObject)|What the step measured: the exposed Sample(s) (the cultured Sample in an assay with no exposure), or the File(s) a re-analysis measures. At least one.|
|result|MUST|[File](https://schema.org/MediaObject)|The output data file(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Parameter](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter))|Measurement parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD).|

**Expected `parameterValue` items:** Each is a Parameter [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). At least one `parameterValue` is required. The following are recommended
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

> **Note:** `propertyID` IRIs are given in each ontology's native canonical form (OBO Foundry PURLs, EFO under
> `www.ebi.ac.uk/efo/`, BAO under `www.bioassayontology.org/bao#`) rather than routed through a single resolver.

### LabProcess - Data Analysis

A [LabProcess](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprocess) that derives the reported results from the raw measurements, typed by the
`intendedUse` of its protocol record, data transformation (OBI:0200000). It consumes the raw-data File(s)
produced by the [Endpoint Readout](#labprocess---endpoint-readout) and declares the processed-data File(s) as its
result. It corresponds to the *analysis and statistics* module of [MIT Nanosafety](https://fairsharing.org/5228).

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|Could identify the process using the process name and protocol reference.|
|@type|MUST|[bioschemas.org/LabProcess](https://bioschemas.org/LabProcess)|—|
|name|MUST|Text|The name of the process, e.g. "Data Analysis".|
|object|MUST|[File](https://schema.org/MediaObject) or [bioschemas.org/Sample](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#sample)|The raw-data input(s) being analysed. At least one.|
|result|MUST|[File](https://schema.org/MediaObject)|The processed-data output file(s). At least one.|
|parameterValue|MUST|[schema.org/PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue) ([Parameter](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter))|Analysis parameter(s); see expected values below. At least one.|
|executesLabProtocol|MUST|[bioschemas.org/LabProtocol](#labprotocol---protocol-record)|The step's protocol record: exactly one (SHOULD), which SHOULD be `isBasedOn` the deposited analysis script (a File co-typed `SoftwareSourceCode` and `LabProtocol`).|

**Expected `parameterValue` items:** Each is a Parameter [PropertyValue](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#propertyvalue---parameter)
(`additionalType` `"ParameterValue"`). At least one `parameterValue` is required. The following, drawn from
OECD Harmonised Template 201's *Data & Analysis* module, are recommended for this step:

| name | Required |
|------|----------|
|Data Calculation and Statistics|SHOULD|
|Computational Tool|SHOULD|
|Acceptance Criteria|MAY|
|Evaluation Criteria|MAY|

### LabProtocol - Protocol record

Based on the Bioschemas [LabProtocol](https://bioschemas.org/LabProtocol) type
([ISA LabProtocol](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#labprotocol)). Each LabProcess executes exactly one protocol record. The record is present
whether or not a document was deposited, since its `intendedUse` determines the type of the step. A deposited
standard operating procedure is a `File` additionally typed `LabProtocol`, referenced by the record through
`isBasedOn`; one document may therefore serve several steps. ISA-JSON defines no `isBasedOn` on a protocol, and
this reference is consequently lost on conversion.

| Property | Required | Expected Type | Description |
|----------|----------|---------------|-------------|
|@id|MUST|Text or URL|One record per step, e.g. the process `@id` followed by `_protocol`.|
|@type|MUST|[bioschemas.org/LabProtocol](https://bioschemas.org/LabProtocol)|—|
|intendedUse|SHOULD|[schema.org/DefinedTerm](https://github.com/nfdi4plants/isa-ro-crate-profile/blob/1.0.0-draft.1/profile/isa_ro_crate.md#definedterm)|The protocol type of its step, from the table below, written as `{"@id": …}` and described once in the graph.|
|name|SHOULD|Text|The name of the protocol, e.g. "Exposure protocol".|
|description|SHOULD|Text|What the lab stated about the procedure; a builder writes none of its own.|
|url|MAY|URL|Where the lab published the procedure, when it stated one.|
|reagent|MAY|[bioschemas.org/MolecularEntity](#molecularentity---chemical)|The compounds the protocol applied. On the Exposure's record this is a MUST: the test and control compounds, at least one, checked on the Exposure step.|
|isBasedOn|MAY|[File](https://schema.org/MediaObject)|The deposited documents the step follows.|

| Step | intendedUse |
|------|-------------|
|Test System Preparation|one of the [test system preparation types](#labprocess---test-system-preparation)|
|Exposure|[EFO:0003969](http://www.ebi.ac.uk/efo/EFO_0003969) *treatment protocol*|
|Endpoint Readout|[BAO:0000523](http://www.bioassayontology.org/bao#BAO_0000523) *assay protocol*|
|Data Analysis|[OBI:0200000](http://purl.obolibrary.org/obo/OBI_0200000) *data transformation*|

## Example ro-crate-metadata.json

A conforming crate is one produced by the reference implementation,
[vitro-crate](https://github.com/johannehouweling/vitro-crate).
