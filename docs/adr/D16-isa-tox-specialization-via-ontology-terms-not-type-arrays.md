# D16: ISA-Tox Specialization via Ontology Terms, Not `@type` Arrays

Every ISA-Tox specialization is expressed by an existing ontology term on the bare base
`@type` — **not** a JSON-LD `@type` array, and not a label of the profile's own:

- A test-system source is `@type: "Sample"` (`bioschemas:Sample`) typed by its
  `sampleType` DefinedTerm, NCIT:C16403 cell line or EFO:0002660 primary cell
  (`profiles/shapes/tox/1_cell_line_sample.ttl`, isa_tox.md §Sample - Cell-based Test
  System, §Sample - Primary Cells). Both are one `CellLineSample` state type, told apart
  by `source_kind` as `LabProcess` is by `process_type`. A Sample a test system
  preparation consumes is a source whatever it states, so one with no `sampleType` is
  still reported.
- A LabProcess step is `@type: "LabProcess"` typed by the `intendedUse` of the protocol
  it executes (D17): a preparation term (growth
  protocol EFO:0003789, transfection OBI:0001152, cell differentiation GO:0030154),
  treatment protocol EFO:0003969, assay protocol BAO:0000523, data transformation
  OBI:0200000. ISA backbone nodes keep the ISA profile's own convention, `@type:
  "Dataset"` + `additionalType: "Investigation"|"Study"|"Assay"`.
- The `additionalType` labels older crates carry on steps and sources
  (`"TestSystemPreparation"`, `"CellCulture"`, `"Exposure"`, `"EndpointReadout"`,
  `"DataAnalysis"`, `"CellLine"`, `"PrimaryCell"`) are aliases: the tox rules and the
  readers (`assessment_graph.with_kinds`) still read them, and a resumed session is
  migrated on load. The build writes none.

The profile **mints no classes and no terms**: every type and term it names is an
existing Schema.org, Bioschemas or ontology IRI. Where the `@type` alone does not say
what a node is, a tox shape selects its focus nodes from the data with a SHACL
`sh:SPARQLTarget` — a Sample by its `sampleType` term, a step by its executed protocol's
`intendedUse`, either one by its legacy `additionalType` alias, a Study or an Assay by the
ISA `additionalType`, the root as the Dataset the metadata descriptor is about. The rest
target a class the crate itself asserts (`MolecularEntity`, `csvw:Table`, `csvw:Column`)
or the nodes that use a property (`sampleType`, `executesLabProtocol`) —
so a generic RO-Crate consumer and the ISA-Tox validator see the same types, which is
why `@type` stays the bare base token. No target is a class only rocrate-validator
infers, so the shape files alone, run over a crate's metadata with any SHACL-AF engine,
report what the ISA-Tox pass reports; `tests/test_shape_constraint_coverage.py` holds
every constraint to both. A term outside the profile's closed lists
(`sh:in`) is a warning. `tests/test_profile_crate.py` fails if anything but a shape is
declared under the profile's namespace.

Consequences the builder respects: `_crate_mapping` emits the bare base `@type` plus the
term (never a `@type` array), and a single conceptual entity is ONE node — a
`CellLineSample` already IS a Sample, so modelling it as a *separate* `Sample` +
`CellLineSample` (two entities sharing a bare `entity_id`) is discouraged by RO-Crate 1.2
(§Contextual entities) and warns at `CrateState.add_entity` (#366).

**A node may still carry more than one type — for reasons that are not
discriminators.** D16 forbids expressing an ISA-Tox *specialization* as a `@type`
array; it does not forbid a node being genuinely two things:

- **Its published schema.org supertype.** RO-Crate RECOMMENDS a type in the
  schema.org namespace, and the shape checking it is syntactic — it looks for an
  IRI beginning `http(s)://schema.org/`. Our domain types resolve to
  `https://bioschemas.org/…`, so a well-typed entity would fail it. `add_schema_org_types`
  therefore appends the supertype: `LabProcess → schema:Action`, `LabProtocol →
  schema:HowTo`, `Sample → schema:Thing`, `MolecularEntity → schema:BioChemEntity`.
  Every one is READ from the vendored `profiles/vocabulary/type_supertypes.json`,
  never decided in code — a published alignment, not our claim. See that function's
  docstring for the rules; a type absent from the vocabulary simply gains nothing.
- **Cross-vocabulary co-typing**, where one artefact really is two kinds of thing:
  a deposited procedure document is `File` + `LabProtocol` (#646) and a deposited
  script `File` + `SoftwareSourceCode` + `LabProtocol` (#786): documents a step
  executes or bases its record on (D17). The generated per-well condition table is
  `File` + `csvw:Table`: a CSV, and a typed table.

The domain type stays FIRST in every case: it is the specific, meaningful one, and
the rest are what a generic consumer can follow.
