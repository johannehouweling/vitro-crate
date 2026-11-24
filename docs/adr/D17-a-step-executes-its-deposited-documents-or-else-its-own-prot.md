# D17: A step executes its deposited documents, or else its own protocol record

A step **executes the documents it follows** — those the deposit places on it, else those the
drafter named (#789) — one `executesLabProtocol` value per `File`. Such a document is `File` +
`LabProtocol` (a script also `SoftwareSourceCode`) in the validated graph and the written crate
alike. `_crate_mapping._execute_documents` writes the step's protocol statements onto it:

- `intendedUse`, a `DefinedTerm`: TestSystemPreparation one of
  `profiles.models.tox.PREPARATION_TYPES` (its `preparation_type`, else EFO:0003789, #785);
  Exposure EFO:0003969; EndpointReadout BAO:0000523; DataAnalysis OBI:0200000.
- `reagent`: on the Exposure, the compounds applied (at least one; a tox Violation), and on any
  step the compounds under its `reagents` (a readout's substrate).
- the `description` and `url` the lab stated (`draft_protocol`, no crate node of its own) where the
  document has none; a differing stated name is an `alternateName`.

Steps share a document only where they agree on `intendedUse`, reagents, description and url. A
union of reagents is never written: it would let an Exposure naming no compound pass. A step
executes all its documents or its record, never a mix.

Every other step — no document, a non-File document, or disagreement — executes its own
**protocol record** (`_crate_mapping._protocol_record`): the same `intendedUse` and `reagent`,
`isBasedOn` its documents, named and described only from what the lab stated (else named after the
step with no description: a builder sentence would game the checks, #670). A document only a record
is based on keeps its own types. Records are a `result` of the build `CreateAction`; executed
deposited files never are. A record sits at `<process @id>_protocol`, or the next free `_<n>`.
Nothing is minted for a document the deposit does not hold.

Following a document takes two independent pieces of evidence: the scan classified it as a
protocol **by content** (`document_discovery.classification_of`; source code by `SCRIPT_SUFFIXES`),
and its path is about **that kind of step** and names **exactly one** subject.

Ambiguity resolves to nothing (D5): two candidates, or one naming two subjects, is evidence for
neither. A `SoftwareSourceCode` protocol in an assay's `hasPart` is the DataAnalysis's; the tox
profile SHOULD-checks that each DataAnalysis executes one or a record based on one (#786),
answered by `labprotocol` (`validation.answering_field`). No cue routes a document to the
Exposure. Culture documents are keyed on the **cell line**, not the assay, so every assay growing a
line follows one entity and no composite is invented; each is also `hasPart` of its Study.
