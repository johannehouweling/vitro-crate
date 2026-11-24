# D16: ISA-Tox Specialization via Ontology Terms, Not `@type` Arrays

Every ISA-Tox specialization is an existing ontology term on the bare base `@type`, never a
`@type` array and never a label of the profile's own:

- A test-system source is `@type: "Sample"` with a `sampleType` DefinedTerm: NCIT:C16403 cell
  line or EFO:0002660 primary cell (`profiles/shapes/tox/1_cell_line_sample.ttl`). Both are one
  `CellLineSample` state type, told apart by `source_kind`. A Sample a preparation consumes is a
  source whatever it states, so one with no `sampleType` is reported.
- A step is `@type: "LabProcess"`, typed by the `intendedUse` of the protocol it executes (D17).
  ISA backbone nodes keep `@type: "Dataset"` + `additionalType: "Investigation"|"Study"|"Assay"`.
- Legacy `additionalType` labels (`"TestSystemPreparation"`, `"CellCulture"`, `"Exposure"`,
  `"EndpointReadout"`, `"DataAnalysis"`, `"CellLine"`, `"PrimaryCell"`) are read as aliases by the
  tox rules and `assessment_graph.with_kinds`, and migrated on resume. The build writes none.

Why: the profile **mints no classes and no terms**, so a generic RO-Crate consumer and the ISA-Tox
validator see the same types. Tox shapes select focus nodes from the data (`sh:SPARQLTarget`, an
asserted class, or a used property), never by a class only rocrate-validator infers, so the shape
files alone under any SHACL-AF engine report what the ISA-Tox pass reports; `tests/test_shape_constraint_coverage.py` holds every constraint to both.
A term outside a closed `sh:in` list is a warning. `tests/test_profile_crate.py` fails if anything
but a shape is declared under the profile's namespace.

One conceptual entity is one node: a separate `Sample` + `CellLineSample` sharing an `entity_id`
warns at `CrateState.add_entity` (#366).

A node may still carry more than one type when it genuinely is two things, the domain type first:

- **Its published schema.org supertype**, because RO-Crate's check for a schema.org type is
  syntactic and our types are Bioschemas IRIs. `add_schema_org_types` appends it, read from the
  vendored `profiles/vocabulary/type_supertypes.json`, never decided in code (see its docstring).
- **Cross-vocabulary co-typing**: a deposited procedure document is `File` + `LabProtocol` (#646),
  a script `File` + `SoftwareSourceCode` + `LabProtocol` (#786), the condition table
  `File` + `csvw:Table`.
