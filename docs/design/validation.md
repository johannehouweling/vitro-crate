# Validation

> The SHACL passes, the data-content layer, identifier verification and namespaces, and the published profile crate.

## Validator (`builder/tools/validation.py`, `profiles/validator.py`)
Runs three-pass SHACL validation via `profiles/validator.py`, which wraps
[`rocrate_validator`](https://github.com/ResearchObject/rocrate-validator).
Returns issues by severity: REQUIRED (blocking), SHOULD (recommended), MAY
(informational). Two entry points:
- `validate_crate(crate_dir)` validates a crate **on disk** and returns
  prose `ValidationResult`s (used by the `validate(crate_path)` tool).
- `validate_crate_dict(metadata_doc, severity, profile)` validates an
  **in-memory** metadata document (the dict from `crate.metadata.generate()`)
  via `services.validate_metadata_as_dict`, returning `DictValidationResult`s
  whose `RoutableIssue`s carry the focus-node `entity_id`, failing `property`
  IRI, `check_id`, `severity`, and `profile`. This backs `build_and_validate`
  and is the no-disk fast path.

**The in-memory path must not walk the working directory.** On a path where no
crate exists on disk, a recursive search of the current working directory for a
metadata descriptor costs most of a validation sweep (#115). roc-validator 0.13
resolves the descriptor id without that search (crs4/rocrate-validator#192), and
`tests/test_validator_perf.py` pins it.

**Messages on the in-memory path keep their text.** `rocrate_validator`
relativises URIs in every result message by substring-replacing the crate path
with `./`; on the dict path that path stringifies to `.`, so every dot in every
message would become `./`. `_patch_make_uris_relative()` leaves a message
untouched when the base is the bare current directory (#827). The constraint
coverage test asserts each shape's `sh:message` verbatim through this path, so a
regression here reddens it.

**Cost levers.** The residual cost is profile *composition*, not inference:
`rocrate_validator` recomposes the shapes/ontology graph and re-resolves check
overrides on every `validate()` call with no reuse hook, so a long-lived worker
does not amortize it. Profiling detail is in
[docs/validator-profiling-115.md](../validator-profiling-115.md). The supported
levers are to gate the inner loop at `required` severity (`validate_crate_dict`'s
default) and to scope `profile` to a single pass when the full sweep isn't needed;
the full 3-pass sweep is run only as a final gate. Switching the disk path to
`required` and reserving `OPTIONAL` for the final report is a further modest win,
not taken here because it changes which issues the loop sees. The only
order-of-magnitude path is an upstream injectable pre-composed shapes/ontology
graph.

Two things are settled; do not re-open them. **Caching the parsed shapes was
explored and deliberately abandoned** — the `.ttl` parse is negligible (~10–130 ms)
and `rocrate_validator` exposes no hook to reuse the compiled graph without a
fragile internals monkeypatch. **Pass-folding — one `tox` pass reporting all
layers, attributing issues by originating profile id — is the only large lever and
is NOT result-equivalent:** the bundled `tox → isa → ro-crate` chain is
RO-Crate **1.1** lineage while the dedicated base pass validates against **1.2**
(#110), so folding would downgrade the base layer and change the issue set. That
split is by design, not drift — `profiles/shapes/tox/profile.ttl` bumps to 1.2 only
if/when the upstream `isa-ro-crate` profile does, guarded by
`tests/test_profile_lineage.py`. Folding becomes safe only after that upstream bump
*and* a byte-identical issue-set test.

**Offline-safe validation — bundled RO-Crate context, no network on the base pass (#117).**
Every crate's `@context` points at the *remote* IRI
`https://w3id.org/ro/crate/1.2/context`, and the base pass must dereference it to
expand the data graph. `rocrate_validator` resolves that IRI over HTTP through two
paths — rdflib's JSON-LD document loader (feeding check `ro-crate-1.1_2.1`) and the
`FileDescriptorJsonLdFormat` check, which calls `HttpRequester().get(context_uri)`
directly (check `ro-crate-1.1_2.2`). On PR #116 CI that fetch flaked
(`RemoteDisconnected`) and the base pass emitted **spurious REQUIRED issues**,
turning a transient blip into red CI and violating #59's "runs offline" criterion.
`profiles/validator.py` makes validation offline-safe:

- **Pinned local contexts, deny-by-default for everything else (SSRF guard, #168).**
  `profiles/contexts/ro-crate-1.1-context.jsonld` and
  `ro-crate-1.2-context.jsonld` are committed copies of the RO-Crate JSON-LD
  contexts. The copy of the context a built crate declares (ro-crate-py's default
  version) is also the builder's term authority: `profiles.context.ro_crate_context_terms()`
  unioned with the ISA-Tox context is `_crate_mapping._context_terms()`, and
  `_scalar_props` emits a key only when that set defines it — whatever the key's
  shape — so no build ships a key the validator's own context rejects (#737).
  `_install_offline_context_loader()` (run at import) intercepts the
  `HttpRequester` GET/HEAD proxy (and `fetch_fresh`) and serves these well-known
  context URLs from disk, so both resolution paths get the bundled copy and never
  touch the wire. Any **other** outbound dereference — a crafted `@context` (or any
  crate-controlled IRI) in an *untrusted* crate pointing at e.g. cloud metadata
  `169.254.169.254` or an internal host — is **refused, not fetched**:
  `_blocked_remote_response()` returns a benign synthetic-200 empty JSON-LD document
  (`{"@context": {}}`) for every non-allowlisted URL. Failing closed with a valid
  200 (rather than raising) is deliberate: rocrate_validator's JSON-LD document
  loader (`_patched_source_to_json`) catches a fetch *exception* and falls back to
  rdflib's own `urllib` opener — which would perform the very request we are
  blocking — so serving an empty context keeps resolution on our intercept (no
  urllib fallback, no network), injects no term mappings from the crafted context,
  and raises no spurious REQUIRED content issue. It also sets
  `ROCRATE_VALIDATOR_AUTO_WARM=0` to suppress rocrate_validator's best-effort cache
  warm-up (pure network traffic we don't need, since the context is bundled and the
  warm-up's other artifact — the spec HTML page — is unused by any check). Refresh
  the bundled files only when the pinned RO-Crate context version changes.
- **Transport failure ≠ content violation.** If a remote resource genuinely can't
  be dereferenced, rocrate_validator swallows the connection error inside the
  check and re-emits it as a REQUIRED *content* issue. `validate_crate` and
  `validate_crate_dict` detect those (a connection-error message on a
  remote-resolving check) and raise `ValidationTransportError` instead, so a
  network failure surfaces as a clear error — never a spurious REQUIRED issue and
  never a false negative in `build_and_validate` (which maps the exception to
  `{"ok": False, "error": ...}`). The regression test
  `tests/test_offline_validation.py` runs validation with the HTTP transport hard-
  blocked and asserts green + no spurious REQUIRED issue;
  `tests/test_validation_ssrf.py` validates a crate whose `@context` points at an
  attacker URL and asserts **no** outbound request reaches it (deny-by-default);
  the #59 e2e harness also runs with the network disabled to prove the path is
  offline-safe.

## Validation Layers

| Layer | Severity | Meaning | Agent Action |
|-------|----------|---------|--------------|
| Base RO-Crate 1.2 | REQUIRED | Structural validity | MUST fix before proceeding |
| ISA Profile | REQUIRED | ISA conformance | MUST fix |
| ISA Profile | SHOULD | Recommended metadata | Fix if data available |
| ISA-Tox Profile | REQUIRED | Toxicology conformance | MUST fix |
| ISA-Tox Profile | SHOULD | Recommended tox fields | Fix if data available |
| Data content (Frictionless) | REQUIRED | Payload conformance (CSV rows vs CSVW/Frictionless `tableSchema`) | MUST fix offending cell |
| MIT Coverage | Score | % of the checklist parameters that carry a crate slot | Improvement suggestions |
| FAIR Indicators | Score | FAIR maturity | Guidance for improvement |

### Data-Content Layer (Frictionless, Issue #95)

The three SHACL passes (base/ISA/ISA-Tox) validate the **metadata descriptor** —
the structure and semantics of `ro-crate-metadata.json`. They do **not** check
the **payload**: whether the rows of a referenced CSV actually match its declared
schema. The Frictionless layer (`builder/tools/data_content.py`,
`validate_table`) closes that gap, mirroring the metadata/data split in the
BioHackEU25 report by Chadwick et al. (biohackrxiv `zah28`).

`validate_table(file, table_schema, foreign_keys=None, entity_id=None)` validates
a CSV's content against a Frictionless `tableSchema` descriptor (column types and
constraints) and, optionally, that designated columns reference only known
in-crate ids (`MolecularEntity` / `Sample`) via `foreign_keys`
(`column -> [allowed_id, ...]`). The obvious payload is the CSVW condition table
emitted by #94 and any raw-measurement tables. Issues come back in the **same
routable shape as #87** (`{entity_id, property, message, fix, severity,
profile}`), with `profile == "data"` so this layer stays cleanly distinct from
the SHACL layers — SHACL = metadata, Frictionless = payload, never entangled. It
never raises into the agent loop: setup errors (missing file, malformed schema)
return `{"ok": False, "issues": [], "error": ...}`.

**Invocation (Issue #409).** The layer cannot be reached through
`build_and_validate`: that call takes `profile="all"`, `profiles/validator.py`
accepts only `all|base|isa|tox`, and `DATA_CONTENT_PROFILE = "data"` sits
deliberately outside that set. So it is a tool of its own, `validate_table`, which
the agent calls on a table that holds rows (the header-only placeholder #94
materialises is valid by construction). Its verdict is **never** folded into
`build_and_validate`'s `ok`: a cell contradicting its `tableSchema` is a different
defect from a SHACL conformance failure, and merging them would make `success` in
the eval harness mean two different things. It stays out of `fix_required_issues`
for the same reason — that loop is keyed on SHACL rules and cannot repair a data
cell.

### Verification Layer
Checks that identifiers resolve at their source. Verification failures are REQUIRED — the identifier must be corrected or removed. Leaving a field empty is acceptable (shows up in MIT/FAIR scores but does not block).

**Derivation (Issue #64).** `_VERIFIERS` maps each `(entity_type, field_name)` pair to the
lookup that serves it, and `_VERIFIABLE_FIELDS` is that table's keys — so a pair cannot be
declared verifiable without wiring the verifier that answers for it.
`verify_all_identifiers` decides what to queue from those keys and `verify_identifier`
dispatches through the same table, so the two cannot drift apart. Fields like
`casrn`/`cas_number`/`inchikey` on `MolecularEntity` are included, while `ror` on
`Organization` — which has no verifier — is excluded and so is never queued. That exclusion
is the point: a pair declared verifiable with no verifier behind it comes back
`No verifier configured`, which [validation layers](#validation-layers) treats as a REQUIRED blocking failure on a perfectly
valid identifier. `tests/test_tools_verification.py` pins the invariant and fails on
injected drift.

### Identifier Namespaces (the Bioregistry's forms)

Verification asks a registry whether an identifier exists; the ISA-Tox shapes check that it has
the form its namespace gives it, offline. Every Person MUST be identified by an ORCID iD and every
Organization by a ROR ID, except one the crate names only as a funder, which SHOULD be: a funding
programme has no ROR record to give. Everything else is checked only where the crate claims the
namespace (an `@id` under a checked address, the `value` of an identifier PropertyValue, an
`inChIKey`). The profile document's Identifier Namespaces section is the table. What must hold:

- **A form is read from the registry, never typed.** `profiles/vocabulary/identifier_namespaces.json`
  vendors the Bioregistry's records (pattern, URI format, example, banana), with the package
  version, licence and date; `scripts/refresh_identifier_namespaces.py` writes it from the pinned
  `bioregistry` package, run by hand, and keeps the `decisions` section (the Compound Wiki, which the
  registry does not hold). `profiles/identifier_namespaces.py` derives the patterns.
  `profiles/ontology_iris.PREFIXES` is held to the same records.
- **The shapes are generated, one per question.** `profiles/shapes/tox/14_identifier_namespaces.ttl`
  is written by `scripts/gen_identifier_shapes.py` from two tables in `identifier_namespaces`
  (`NAMESPACE_CHECKS`, `VALUE_CHECKS`) and never edited by hand: whether a Person's and an
  Organization's `@id` is in the ORCID and ROR namespace at all (a Warning, not a Violation, for an
  Organization nothing refers to but a `funder`), whether an `@id` in any checked
  namespace has its form (one `sh:or` with a pattern per namespace), whether an identifier
  PropertyValue's `value` has its scheme's form, and an InChIKey's. Every `sh:pattern` in the tox
  shapes names the one record it came from with `rdfs:seeAlso`, and
  `tests/test_identifier_namespace_shapes.py` fails on a pattern that is not a derivation of it, on a
  vendored prefix no shape uses, on a record nothing vendors, and on a generated file that differs from
  the generator's output. Adding a namespace is a line in the table and a run of the generator.
- **A constraint is plain SHACL, not SPARQL.** A SPARQL constraint is re-parsed and run once per
  focus node; with a branch per namespace it cost 113 s against 3 s on a 365-entity crate. Only the
  target is SPARQL, and it runs once.
- **The record is a pointer on a shape, never an identity.** A crate keeps `https://orcid.org/…`;
  `https://bioregistry.io/registry/orcid` appears only as a shape's `rdfs:seeAlso` (`ontology_iris`).
- **A node-level constraint carries its `sh:message` and `sh:severity` on the NodeShape.** Both
  engines ignore a severity inside a constraint, and a Warning-level node shape is not loaded under
  the REQUIRED gate, so every check is a Violation but the funder's, a recommendation by design. The
  funder target excludes the node itself and `schema:sponsor` as referrers, because an engine that
  infers adds both. The namespace target matches the whole address
  (`identifier_namespaces.namespace_start`), so an ontology's own IRI (`…/obo/ncit.owl`) and the
  scheme nodes identifier PropertyValues name (`https://orcid.org`) are never selected.
- **One message, read by the `@id`.** The form check speaks one message for every namespace, and the
  words the tooling keys on are constants in `identifier_namespaces` (`MALFORMED`,
  `PERSON_NEEDS_ORCID`, `ORGANIZATION_NEEDS_ROR`, `FUNDER_SHOULD_HAVE_ROR`), never a second copy.
  The fix hint
  (`validation._identifier_fix`) and the report's instruction (`remediation._instruction`) name the
  namespace through `namespace_of(@id)`, and a finding on a generated identifier PropertyValue names
  the entity and field it is written from.
- **The build is never the cause of a finding.** `_crate_mapping._mint_id` writes the registry's
  address for an ORCID, ROR ID, Cellosaurus accession or DOI stated in another form
  (`_registry_iri`), and leaves anything that is not one as given, for the shape to report. The form
  it was stated in still reaches the node (`_stated_addresses`), two entities that now mint one `@id`
  are written as one node (`_written_as_one`), and a DOI PropertyValue's value is stored bare.
- **A finding is answered, not gamed.** No identifier is invented (D5). A REQUIRED finding is never
  filed under citations (`validation._partition_citations`). `verify_identifier` clears an ORCID iD
  whose record states another family name. The ReAct loop stops continuing by itself when the only
  REQUIRED findings are missing ORCID iDs and ROR IDs (`agent_loop._agent_is_done`), without calling
  the crate complete: its hint says how many still need one, and the session ends naming who
  (`agent_loop.format_unidentified_agents`). Its instructions forbid deleting a person to clear one.

### The Profile Crate

The published profile is an RO-Crate 1.2 Profile Crate generated by
`scripts/gen_profile_crate.py` from `profiles/docs/isa_tox.md` and
`profiles/shapes/tox/*.ttl`. It is never committed here and never edited by hand;
`tests/test_profile_crate.py` holds it to the three checks the RO-Crate Profile
Portal runs. It is served from the `vitro-crate/isa-tox-ro-crate-profile` repo,
which carries only licence, citation and Zenodo metadata. The spec text is
CC BY 4.0, the shapes MIT, declared per data entity in the crate.
