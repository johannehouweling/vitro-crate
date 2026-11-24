# Validation

> The SHACL passes, the data-content layer, identifier verification and namespaces, and the published profile crate.

## Validator (`builder/tools/validation.py`, `profiles/validator.py`)
Three-pass SHACL validation (base / ISA / ISA-Tox) wrapping
[`rocrate_validator`](https://github.com/ResearchObject/rocrate-validator), returning issues as
REQUIRED (blocking), SHOULD or MAY. Two entry points:
- `validate_crate(crate_dir)` validates a crate **on disk** and returns prose `ValidationResult`s
  (the `validate(crate_path)` tool).
- `validate_crate_dict(metadata_doc, severity, profile)` validates an **in-memory** metadata
  document via `services.validate_metadata_as_dict`; its `RoutableIssue`s carry `entity_id`,
  `property` IRI, `check_id`, `severity` and `profile`. It backs `build_and_validate`.

**The in-memory path must not walk the working directory** for a descriptor; that search costs
most of a sweep (#115). Pinned by `tests/test_validator_perf.py`.

**Messages on the in-memory path keep their text.** There the crate path stringifies to `.`, and
upstream's URI relativising would turn every dot into `./`; `_patch_make_uris_relative()` skips a
bare-current-directory base (#827). The constraint coverage test asserts each `sh:message`
verbatim through this path.

**Cost levers.** The cost is profile *composition*, recomposed on every `validate()` with no reuse
hook ([profiling notes](../validator-profiling-115.md)). Gate the inner loop at `required`
(`validate_crate_dict`'s default), scope `profile` to one pass, and run the full sweep only as a
final gate. The only order-of-magnitude path is an upstream injectable pre-composed graph.

Two things are settled; do not re-open them. **Caching the parsed shapes** was abandoned: the parse
is negligible and reusing the compiled graph needs a fragile internals monkeypatch.
**Pass-folding** (one `tox` pass reporting all layers) is not result-equivalent: the bundled
`tox → isa → ro-crate` chain is RO-Crate 1.1 lineage while the base pass is 1.2 (#110), guarded by
`tests/test_profile_lineage.py`. It becomes safe only after upstream bumps to 1.2 *and* a
byte-identical issue-set test.

**Validation never reaches the network (#117).** Every crate's `@context` is the remote
`https://w3id.org/ro/crate/1.2/context`, which `rocrate_validator` would fetch through two paths
(rdflib's JSON-LD loader, and `HttpRequester().get` in `FileDescriptorJsonLdFormat`); a flaky fetch
turns into spurious REQUIRED issues.

- **Pinned local contexts, deny-by-default for everything else (SSRF guard, #168).**
  `profiles/contexts/ro-crate-1.{1,2}-context.jsonld` are committed copies;
  `_install_offline_context_loader()` (run at import) intercepts `HttpRequester` GET/HEAD and
  `fetch_fresh` and serves them from disk. Any other URL — a crafted `@context` in an untrusted
  crate pointing at e.g. `169.254.169.254` — gets `_blocked_remote_response()`'s synthetic-200
  `{"@context": {}}`. It must not raise: upstream catches a fetch exception and falls back to
  rdflib's `urllib`, which would make the blocked request. `ROCRATE_VALIDATOR_AUTO_WARM=0` stops the
  cache warm-up. Refresh the bundled files only when the pinned context version changes.
  The context a built crate declares is also the builder's term authority:
  `_crate_mapping._context_terms()` (`profiles.context.ro_crate_context_terms()` ∪ the ISA-Tox
  context), and `_scalar_props` emits only keys it defines (#737).
- **Transport failure ≠ content violation.** Upstream re-emits a connection error as a REQUIRED
  content issue; `validate_crate` / `validate_crate_dict` detect it and raise
  `ValidationTransportError` (which `build_and_validate` maps to `{"ok": False, "error": ...}`).
  Pinned by `tests/test_offline_validation.py` (transport hard-blocked) and
  `tests/test_validation_ssrf.py` (no request reaches an attacker `@context`).

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

SHACL validates the metadata descriptor, never the payload. `validate_table(file, table_schema,
foreign_keys=None, entity_id=None)` (`builder/tools/data_content.py`) validates a CSV's rows
against a Frictionless `tableSchema` and, optionally, that columns reference only known in-crate
ids (`foreign_keys`: `column -> [allowed_id, ...]`). Issues use the routable shape
(`{entity_id, property, message, fix, severity, profile}`) with `profile == "data"`. It never
raises into the loop: setup errors return `{"ok": False, "issues": [], "error": ...}`.

**A tool of its own, never folded into the SHACL verdict (#409).** `profiles/validator.py` accepts
only `all|base|isa|tox`, and `DATA_CONTENT_PROFILE = "data"` is deliberately outside it. The agent
calls `validate_table` on a table that holds rows. Its verdict never feeds `build_and_validate`'s
`ok` (that would make eval `success` mean two things) nor `fix_required_issues` (keyed on SHACL
rules).

### Verification Layer
Checks that identifiers resolve at their source. A verification failure is REQUIRED — correct or
remove the identifier; an empty field does not block.

**One table (#64).** `_VERIFIERS` maps `(entity_type, field_name)` to its lookup and
`_VERIFIABLE_FIELDS` is its keys; `verify_all_identifiers` and `verify_identifier` both use it, so a
pair cannot be verifiable without a verifier (which would report `No verifier configured`, a
blocking failure on a valid id — why `Organization.ror` is excluded). Pinned by
`tests/test_tools_verification.py`.

### Identifier Namespaces (the Bioregistry's forms)

The ISA-Tox shapes check offline that an identifier has its namespace's form. Every Person MUST
have an ORCID iD and every Organization a ROR ID, except one named only as a funder, which SHOULD
(a programme has no ROR record). Anything else is checked only where the crate claims the namespace
(an `@id` under a checked address, an identifier PropertyValue's `value`, an `inChIKey`); the table
is the profile document's Identifier Namespaces section.

- **A form is read from the registry, never typed.** `profiles/vocabulary/identifier_namespaces.json`
  vendors the Bioregistry records, written by hand-run `scripts/refresh_identifier_namespaces.py`
  from the pinned `bioregistry` package (keeping the `decisions` section, e.g. the Compound Wiki).
  `profiles/identifier_namespaces.py` derives the patterns; `profiles/ontology_iris.PREFIXES` is held
  to the same records.
- **The shapes are generated, never hand-edited.** `profiles/shapes/tox/14_identifier_namespaces.ttl`
  comes from `scripts/gen_identifier_shapes.py` over `NAMESPACE_CHECKS` and `VALUE_CHECKS`. Every
  `sh:pattern` names its record with `rdfs:seeAlso`; `tests/test_identifier_namespace_shapes.py`
  fails on an underived pattern, an unused prefix, an unvendored record or a stale generated file.
- **A constraint is plain SHACL, not SPARQL** — a per-node SPARQL constraint cost 113 s against 3 s.
  Only the target is SPARQL.
- **The record is a pointer on a shape, never an identity**: crates keep `https://orcid.org/…`.
- **`sh:message` and `sh:severity` sit on the NodeShape**: engines ignore a severity inside a
  constraint, and a Warning node shape is not loaded under the REQUIRED gate. The funder target
  excludes the node itself and `schema:sponsor` as referrers (an inferring engine adds both); the
  namespace target matches the whole address (`identifier_namespaces.namespace_start`).
- **One message, read by the `@id`.** The keyed-on words are constants in `identifier_namespaces`
  (`MALFORMED`, `PERSON_NEEDS_ORCID`, `ORGANIZATION_NEEDS_ROR`, `FUNDER_SHOULD_HAVE_ROR`), never a
  copy; `validation._identifier_fix` and `remediation._instruction` name the namespace via
  `namespace_of(@id)`.
- **The build is never the cause of a finding.** `_crate_mapping._mint_id` writes the registry
  address for an ORCID, ROR ID, Cellosaurus accession or DOI stated otherwise (`_registry_iri`) and
  leaves anything else for the shape to report (`_stated_addresses`, `_written_as_one`).
- **A finding is answered, not gamed.** No identifier is invented (D5); a REQUIRED finding is never
  filed under citations (`validation._partition_citations`); `verify_identifier` clears an ORCID iD
  whose record states another family name. The loop stops by itself when only missing ORCID/ROR
  findings remain (`agent_loop._agent_is_done`) without calling the crate complete, and ends naming
  who (`agent_loop.format_unidentified_agents`); it must never delete a person to clear one.

### The Profile Crate

An RO-Crate 1.2 Profile Crate generated by `scripts/gen_profile_crate.py` from
`profiles/docs/isa_tox.md` and `profiles/shapes/tox/*.ttl`; never committed here or hand-edited.
`tests/test_profile_crate.py` holds it to the RO-Crate Profile Portal's three checks. It states
the shapes' requirement levels whatever the page's column says, and refuses to build when a shape
requires a property the page has no row for. `vitro-crate/isa-tox-ro-crate-profile` holds only
its output and regenerates it from `main` nightly, or at once when `publish-profile.yml` here
dispatches; spec text CC BY 4.0, shapes MIT, declared per data entity.
