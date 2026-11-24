# D13: ISA hasPart Hierarchy — Investigation is the Root

The Investigation **is** the Root Data Entity (`./`): the ISA RO-Crate profile mandates it, and a
Study MUST be `hasPart` of the root (`StudyMustBeReferencedFromInvestigation`), so the root cannot
be a Study. What the deposit itself declares about the root is read, never guessed (D5).

**A declared licence is read (#535)** by `extract_deposit_licence`, from a named field in either
convention deposits use: the BioStudies attribute, or the field other records use (RO-Crate
`license`, CodeMeta, Frictionless `licenses[].path`, DataCite `rightsList[].rightsUri`). Also read:
`SPDX-License-Identifier:` in any text, and a URI in a file *named* `LICENSE` / `LICENCE` /
`COPYING`. Formats: `.json`, `.jsonld`, `.yaml`, `.yml`, `.cff`, `.xml`.

An IRI wins wherever it sits; otherwise the value is kept **verbatim** ("CC-BY" is not 4.0). XML
is matched by local name and parsed with `defusedxml`, never stdlib `ElementTree` (deposits are
untrusted). Prose is never a source: only a document whose top level is a **mapping** is a record,
and a `LICENSE` holding only legal text names no identifier. The shallowest file wins, then one
with an IRI, then by path. `license_from_deposit` marks the value; `set_crate_metadata` cannot
overwrite it.

**An unstated licence claims nothing (#540).** `license` is a base MUST, so a crate with no
declared terms carries `LICENCE_NOT_STATED_ID` (`#licence-not-stated`), a `CreativeWork` that is
neither grant nor restriction; consumers branch on the `@id`, and the MIT and FAIR scorers discount
it. A stated licence always wins, described when `describe_license` recognises it, never renamed.

**Declared funding** (`extract_deposit_funding`: BioStudies `Funding`, DataCite
`fundingReferences`) is kept verbatim: the root's `funder` is an Organization (the crate's own of
that name, else name-only) and a grant number a `Grant` on its `funding`. Since ISA-Tox requires a
ROR ID on every Organization, `composites.look_up_declared_funders` looks it up once before the
loop: a grant CORDIS holds under the named programme (`composites.cordis_grant`) goes to
`metadata.grants` and is written at its DOI, funded by the European Commission; otherwise the ROR
record registering exactly that name gives its ID. A funder neither answers keeps only its name.
A grant CORDIS could not be asked about is no answer: its funder goes on to the name match, and
`run_build` returns the number under `cordis_unreachable`, which the `--unattended` summary names.

**Declared people and organisations** (`extract_deposit_people`: BioStudies `author` /
`organisation`, DataCite `creators` / `contributors`) are read for their ORCID / ROR id alone; the
reader drafts nobody. `draft_person` (and a publication's author) takes the ORCID stated for exactly
its name once the record resolves with the same family name; `draft_organization` takes the ROR id
once `fetch_ror_by_id` returns it. A name given two ids gets neither; an entity already holding the
id is amended first, so no two share an address. No ROR id comes from a name alone, except a
declared funder's exact registered name.

**`_add_structural`** (`builder/tools/_crate_mapping.py`):

- **Folds a single Investigation onto the root**, indexed to `./` (0 or 2+ stay separate).
- Keeps **Study and Assay as `Dataset` + `additionalType` nodes**; an Assay MAY be `hasPart` of its
  Study or the Investigation (`AssayMustBeReferencedFromInvestigation`).
- **Identifies a Study or Assay by its own folder** (#852) via `dataset_folders`: the accepted
  `deposit_structure` labelling, else the deepest folder common to its attached files (not for an
  Assay holding every file of the deposit). A folder counts only if scanned, not the root, not
  holding another Assay's file, not taken by or nested with another level. It is then the directory
  Dataset there (`@id` the percent-encoded path ending `/`, `@type` plain `Dataset`); an Assay
  folder under a folderless Study is also listed by the root, since ro-crate-py reaches directories
  only through directories. Without a folder the entity keeps its `#Study_…` / `#Assay_…` id; no
  folder is invented. Metadata-only crates describe the folder at `#<folder>/` with a `localPath`.
- **Gives each level a distinct `identifier`**: the folder path, else `_isa_identifier`'s
  hierarchical descriptor (nested under the accession when the root is identified by a DOI). It is
  a Study's or Assay's only `schema:identifier` (`_identifier_terms` fields are dropped; the shapes
  cap it at one).
- **Attaches each result `File` to its Assay's `hasPart` in addition to the root's**, never instead
  (#532, `_append_unique`): the root's reference keeps it in the file tree ro-crate-py walks
  through directory Datasets, which no SHACL profile checks.
- Re-emits the Assay's `dataFiles` / `resources` aliases as File references nested under its
  `hasPart` (`_wire_dataset_aliases`).

**There is no crate reader** (#711): nothing reads a crate back into a `CrateState`. A future
reader starts from the crate a third party opens.
