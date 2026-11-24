# D13: ISA hasPart Hierarchy — Investigation is the Root

The Investigation **is** the Root Data Entity (`./`); the ISA RO-Crate profile mandates this and
the SHACL shapes forbid alternatives (a Study carrying `additionalType "Study"` MUST be `hasPart`
of the root via `StudyMustBeReferencedFromInvestigation`, so the root cannot itself be a Study).
**A declared licence is read, in whatever convention the deposit states it (#535).** Nothing used
to read it: `set_crate_metadata` — an LLM-callable tool — was its only writer, so the licence was
whatever the model supplied, and on S-VHPS26 the guess inverted the depositor's (CC-BY-4.0 declared,
all-rights-reserved asserted) in the one direction that suppresses reuse. `extract_deposit_licence`
reads a NAMED field, which is not guessing, across the two conventions deposits actually use: the
BioStudies *attribute* (a node naming the field, usually qualified with a canonical URL) and the
*field* every other record uses — RO-Crate `license`, CodeMeta, Frictionless `licenses[].path`,
DataCite `rightsList[].rightsUri`. Gating on the BioStudies shape, as it first did, answered for one
repository's export and left every other deposit on the fabricated fallback. An IRI wins wherever it
sits; without one the declared value is returned **verbatim**, because mapping "CC-BY" onto a 4.0
URI states a version the depositor did not (D5).

Two conventions live outside a metadata record and are read as well. `SPDX-License-Identifier:` is a
formal declaration that can sit in any text, so it is honoured wherever it appears; and a file *named*
`LICENSE` / `LICENCE` / `COPYING` declares by its name that its whole content is the licence, which is
what permits reading a URI out of it — in any other file that would be a URL appearing in prose. The
document formats widen with them: `.json`, `.jsonld`, `.yaml`, `.yml`, `.cff` (CITATION.cff is YAML,
and `license` is one of its standard keys) and `.xml`.

XML carries the same field convention in element form — DataCite keeps the machine-actionable value
in an attribute (`<rights rightsURI="…">CC BY 4.0</rights>`) and the label in the text, Dublin Core
puts the whole thing in `<dc:rights>` — so tags and attributes are matched on their **local** name,
namespace stripped. It is parsed through `defusedxml`, not stdlib `ElementTree`: a deposit is
untrusted input, and one crafted file would otherwise expand a billion-laughs entity during a scan.
A refused or malformed document is simply not a declaration.

Prose is never a source, and the guard is load-bearing rather than defensive: YAML parses a README's
bullets into a list of mappings, so `- License: see the LICENSE file` would otherwise be filed as
legal terms, and every real deposit's README ships the unfilled placeholder `[Default CC-BY 4.0 for
data, CC0 for metadata unless specified otherwise]` — two licences named, neither declared. Only a
document whose top level is a **mapping** is a metadata record, and a `LICENSE` holding only legal
text names no identifier: reading "Creative Commons Attribution 4.0 International Public License" off
its first line would invent a machine-actionable claim out of a heading. Which file is the DEPOSIT's is
decided rather than left to directory order: shallowest first (a root descriptor describes the
deposit, a bundled `package.json` four levels down describes itself), then a machine-actionable IRI,
then the path. Depth outranks the IRI preference deliberately — a nested SPDX URI must not beat the
root descriptor's own label. `license_from_deposit` marks the value as read, and `set_crate_metadata`
cannot overwrite one that was.

**An unstated licence says so, and claims nothing (#540).** `license` is a base MUST, so the field
cannot be left empty — but answering it with `ALL RIGHTS RESERVED BY THE AUTHORS`, as it once did,
converts an unanswered question into the most restrictive claim available, asserted by machine over
someone else's data by a tool whose purpose is FAIR outputs. RO-Crate allows `license` to be a
`CreativeWork` rather than a URL, so a crate with no declared terms carries `LICENCE_NOT_STATED_ID`
(`#licence-not-stated`) — an entity whose name and description record that the depositor stated
nothing, which is neither a grant nor a restriction. It satisfies the requirement, keeps the crate
conformant, and is machine-readable: a consumer branches on the `@id` rather than string-matching
prose. A licence the depositor *did* state always wins, and is emitted as a described entity when
`describe_license` recognises it (never renamed when it does not). The MIT and FAIR scorers
discount the entity by id, so an unstated licence never reads as filled.

**Declared funding is read the same way, and the crate states it as a funder and a grant.**
`extract_deposit_funding` reads who paid and under which grant number from the two conventions that
give funding a place of its own: the BioStudies `Funding` section (`Agency`, `Grant_id`) and DataCite's
`fundingReferences` (`funderName`, `awardNumber`). Both values are kept verbatim, since "EU Horizon
2020" names a programme and the European Commission in its place is a claim the depositor did not make
(D5), and a sentence about funding is never a source. The build writes the funder as the root's
`funder`, an Organization: the crate's own when one carries that name, else one that states the name
and nothing more. A grant number is no property of an organization, so it is a `Grant` the root's
`funding` references, named after its funder and number, which names that funder. This is also what
fills the checklist's `Investigation:funder` slot. ISA-Tox requires every Organization to be identified by
a ROR ID ([validation layers](../design/validation.md#validation-layers), Identifier Namespaces), so the build looks the declared funding up once, before the loop
starts and behind the provider gate (`composites.look_up_declared_funders`). An EU grant comes first: a
grant number CORDIS holds under the programme the deposit names ("EU Horizon 2020" for an `H2020`
project, `composites.cordis_grant`) is recorded in `metadata.grants`, apart from what the deposit
stated, and the build writes it as a `Grant` at its DOI (else its CORDIS address) with its title and
acronym, funded by the European Commission, the Organization ROR registers under exactly that name;
CORDIS states the Commission's contribution to every grant it records. Otherwise ROR is asked for the
record that registers exactly the declared name, as an affiliation is matched. One that does gives the
ROR ID to the Organization the crate holds under that ID or under the declared name, or to a new one,
with the declared name as an `alternateName` where it goes by another; the build then writes the funder
as the Organization that holds the ROR ID. A funder neither answers, such as a national programme, is
written by name only, and the profile recommends a ROR ID for it rather than requiring one.

**The people and organisations a deposit identifies are read the same way, for their identifiers
alone.** `extract_deposit_people` reads the name and the ORCID of each person a record names and the
name and the ROR id of each organisation, from the same two conventions: the BioStudies `author` and
`organisation` sections (`Name`, `ORCID`, `RORID`), and the `nameIdentifiers` and `affiliation` of
DataCite's `creators` and `contributors`. Nothing else is kept, though the same sections state an
e-mail address, a role and a country. The reader drafts nobody and asks no registry: it is only the
source of an identifier for an entity someone drafts. `draft_person` tries the ORCID stated for a
person of exactly its name, case and spacing aside, before the name search, and uses it only once
the ORCID record resolves and states the same family name; only the iD is taken, and the name stays
as the deposit spells it. A publication's author whose name the record states takes that ORCID the
same way, after a person the crate holds and before the name search, and keeps that spelling too.
A name the record gives two ORCIDs is given neither, though an ORCID or a
ROR id written bare in one place and as its address in another is one identifier. A person the crate
already holds under that ORCID is amended before any namesake is, so no two people take one address,
and a namesake who holds another ORCID than the one confirmed is another person, at the confirmed
ORCID's address. `draft_organization` takes the ROR id stated for an organisation of exactly its
name once `fetch_ror_by_id` returns that record, with the website and country the record states, and
an Organization the crate already holds under that ROR id is amended instead; a name the record
gives two ROR ids is given neither. An author whose ORCID record gives that ROR id is affiliated to
the Organization that holds it, whatever name the record uses for it. No ROR id is taken for a name
alone, except the one ROR registers under exactly a declared funder's name (above).

`_add_structural` (`builder/tools/_crate_mapping.py`) therefore:

- **Folds a single Investigation entity onto the root** instead of emitting a duplicate
  `#Investigation_*` node, and indexes it to `./` so `investigation_id` references resolve there.
  (0 or 2+ Investigations keep separate nodes — out of scope, rare.)
- Keeps **Study and Assay as their own `Dataset` + `additionalType` nodes** (required by the ISA
  shapes); an Assay MAY be `hasPart` of either its Study or the Investigation (`2_assay.ttl`
  `AssayMustBeReferencedFromInvestigation`).
- Identifies **a Study or an Assay by its folder** where the deposit gives it one of its own (#852,
  "the folder tree is the depositor's own statement of what the studies and assays are"):
  `dataset_folders` reads the labelling `deposit_structure` holds when the checks accepted it, and
  otherwise the deepest folder common to the files `attach_files` placed on the entity, except for
  an Assay that holds every file of the deposit; a folder counts
  only if the scan saw it, never the deposit root, never one that holds a file placed on another
  Assay, and never one another Study or Assay takes or is nested with (an Assay before its Study).
  Such an entity is the directory Dataset at
  that folder: its `@id` is the folder's path percent-encoded as a file's is and ending in `/`, its
  `@type` the plain `Dataset` ro-crate-py walks. An Assay with a folder whose Study has none is
  listed by the root as well, because ro-crate-py reaches a directory only through directories and
  refuses a crate with one it does not reach. Without a folder (a flat deposit, a study at the
  deposit root, a shared or nested folder, no scan) the entity keeps its contextual `#Study_…` /
  `#Assay_…` id: no folder is invented and no directory is made for an id. A metadata-only crate
  describes such a folder as it describes its files, at `#<folder>/` with a `localPath`.
- Gives **each level a distinct identifier**: a Study or an Assay with a folder states the folder's
  path, and one without, or whose folder's path another level already states, the hierarchical
  descriptor `_isa_identifier` mints (`FAB-2026` → `FAB-2026/study-<id>` → `…/assay-<id>`). Under a
  root identified by its DOI the descriptor nests under the deposit's accession instead
  (`S-VHPS21/study-<id>`), because one built under a web address reads as an address that resolves
  to nothing. The `@id` is the true unique key; the `identifier` *property* is the ISA descriptor and must not
  collide across levels. It is also a Study's or Assay's **only** `schema:identifier`: a drafted
  field the context expands to `schema:identifier` (`accession`, `dsstoxId`, … —
  `_identifier_terms`) is not emitted on either, because their ISA shapes cap the property at one
  (`sh:maxCount 1`); the deposit's own identifier is the root's `identifier` (`CrateMetadata`'s
  DOI, else its accession).
- Attaches each **result `File` to its producing Assay's `hasPart`** (de-duped via `_append_unique`)
  **in addition to** the root's reference, never instead of it (#532) — raw/processed data are the
  data of an assay, and RO-Crate lets a data entity be `hasPart` of more than one `Dataset`. The
  root's reference is what keeps the file in the crate's **file tree**: that tree is walked from
  `./` through *directory* Datasets, and an ISA container without a folder of its own is a
  contextual `#Study_…` / `#Assay_…` node, not a directory. Re-parenting therefore stranded every
  payload file — ro-crate-py refuses to open such a crate, while all three SHACL profiles pass it,
  because none of them asks. A file under a container that is a folder keeps the root's reference
  too, so no file's place in the tree depends on which containers have one.
- Re-emits the Assay's **`dataFiles` / `resources` PageTab aliases** (both expand to `schema:hasPart`
  via `profiles/context.py`) as resolved File references *and* nests those Files under the Assay's
  `hasPart` — same move as result Files, so the gold-crate JSON keys round-trip (`_wire_dataset_aliases`,
  #180 Lane C).

**There is no crate reader.** This tool writes crates and scores them; nothing reads one back
into a `CrateState`. `builder/readers/` used to, with no production caller and a round trip that
recovered 180 of 329 node ids on a real crate, and it went with #711. A future reader — for an
index, or an MCP query server — starts from the crate a third party opens, not from that one.
