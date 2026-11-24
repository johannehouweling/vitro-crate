# D17: A step executes its deposited documents, or else its own protocol record

A step **executes the documents it follows**, those the deposit places on it or, where it
places none, those the drafter named (#789): `executesLabProtocol` names each `File` by
its path in the crate, one value per document (a readout that follows RNA isolation,
DNase and cDNA/qPCR executes all three). A document a step executes is `File` +
`LabProtocol` (a script `File` + `SoftwareSourceCode` + `LabProtocol`) in the graph that
is validated and in the crate that is written alike. What the step states about its
protocol is written on each document it executes (`_crate_mapping._execute_documents`):

- `intendedUse`, the step's protocol type as an ontology term (a `DefinedTerm`):
  TestSystemPreparation one of the three preparation terms
  (`profiles.models.tox.PREPARATION_TYPES`: EFO:0003789 *growth protocol*, OBI:0001152
  *transfection*, GO:0030154 *cell differentiation*), the step's stated
  `preparation_type` or else EFO:0003789 (#785); Exposure EFO:0003969 *treatment protocol*,
  EndpointReadout BAO:0000523 *assay protocol*, DataAnalysis OBI:0200000 *data
  transformation*;
- on the Exposure, `reagent`: the compounds it applied, at least one (the tox
  profile's Violation);
- on any step, `reagent` also lists the compounds it names under `reagents`: a
  readout's substrate;
- the `description` and `url` the lab stated about the step's protocol — the protocol
  the drafter recorded (`draft_protocol` with a name and description), which is no
  crate node of its own (#789) — where the document has
  none, the rule of `draft_protocol(source_file=...)`. The document keeps its own
  `name`; a protocol name the lab stated that differs from it is an `alternateName`. A
  description or url the document already has is kept, so a different one the lab
  stated is not in the crate.

A document carries one protocol type and one set of reagents, so steps share one only
where they agree: every step that would execute it states the same `intendedUse` term (no
term counts as a kind of its own), the same reagents, and no differing description or
url. The union of two steps' reagents is never written on a document: it would let an
Exposure that names no compound pass the profile's Violation. Where steps disagree — one
SOP named by an exposure and by a readout, two exposures with different compounds on one
SOP, the culture and the differentiation of one line on one culture document — every step
involved keeps its record, based on its documents. A step executes all of its documents
or its record, never a mix.

Every other step executes its own **protocol record** (`_crate_mapping._protocol_record`):
a step with no document, one whose document is not a File, and one that disagrees with
another step on a document. The record carries the same `intendedUse` and `reagent`, is
`isBasedOn` the step's documents, and takes its `name`, `description` and `url` from
what the lab stated. A document that only a record is based on keeps the types it had,
since the bundled ISA shape asks `LabProtocol` only of what a step executes: a full
export can write an undrafted scanned document as a plain `File` where the graph that is
validated has it `File` + `LabProtocol`. Without a statement the record is named after
its step and carries no description: a builder sentence would satisfy the checks that
ask for one (#670). One based on nothing is the honest gap. Every record is a `result`
of the crate's build `CreateAction`, beside the root; a deposited file a step executes
never is. A record sits at `<process @id>_protocol` unless another entity holds that
id, in which case it takes the next free `_<n>` suffix rather than replacing it.

ISA-JSON gives a process one protocol and has no `isBasedOn` on a protocol, so a
conversion to ISA-JSON can carry neither all of a step's documents nor a record's link
to them. Nothing is minted for a document the deposit does not hold.

Following a document requires two independent pieces of evidence, because it asserts the
document explains how that step turns its input into its output:

1. The scan classified it as a protocol **by content**
   (`document_discovery.classification_of`), not by filename — except source code,
   which its extension classifies (`SCRIPT_SUFFIXES`).
2. Its path is about **that kind of step** and names **exactly one** subject.

A script's kind is its step evidence: a `SoftwareSourceCode` protocol in an assay's
`hasPart` is the DataAnalysis's, never the readout's, whatever it is called. The tox
profile then SHOULD-checks that each DataAnalysis executes one, or a record based on one
(#786): an analysis done by hand in Excel or Prism deposits no re-runnable step, and says
so. The finding is answered by the step's `labprotocol` (`validation.answering_field`).
No cue routes a document to the Exposure, so it follows what was named.

Ambiguity resolves to nothing: two candidate documents, or one naming two subjects, is
evidence for neither (D5). Culture documents are keyed on the **cell line**, not the
assay — culturing is study-level, and a deposit ships one document per line — so every
assay growing SK-N-AS follows one entity, the culture of each line its own, and no
composite is invented. Each is also linked to its Study: `hasPart` for a deposited
document, which is real payload.
