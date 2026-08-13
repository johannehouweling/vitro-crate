# ruff: noqa: E501
"""Tool specifications for LLM function calling.

The ``draft_*`` tools advertise a typed ``hints`` schema built from
:data:`builder.tools._crate_mapping.ENTITY_DRAFT_SCHEMA` via
:func:`draft_hints_schema`, the single source of truth shared with
``_crate_mapping._REF_FIELDS`` (Issue #90, sub-task 1). This replaces the old
schema-less ``hints: {type: object}`` so a weak model is told exactly which
scalar and reference keys an entity accepts.
"""

from builder.tools._crate_mapping import draft_hints_schema


def _cell_line_resolve_hints_schema() -> dict:
    """``draft_hints_schema("CellLineSample")`` minus the keys the composite refuses.

    The drafter's schema advertises ``accession`` — correct there, since
    ``draft_cell_line_sample`` is the primitive an agent uses when it already
    holds a looked-up id. It is exactly wrong for ``resolve_cell_line``, whose
    whole point is that the accession comes back from Cellosaurus inside the
    call: advertising a slot the composite silently drops invites the model to
    fill it with the nearest CVCL id in its context (#383). Derived from
    ``_CELL_LINE_REFUSED_HINTS`` rather than restated, so the advertised schema
    and the enforced one cannot drift.
    """
    from builder.tools.composites import _CELL_LINE_REFUSED_HINTS

    schema = draft_hints_schema("CellLineSample")
    schema["properties"] = {
        key: spec
        for key, spec in schema.get("properties", {}).items()
        if key not in _CELL_LINE_REFUSED_HINTS
    }
    return schema


TOOL_SPECS = [
    {
        "name": "draft_investigation",
        "description": "Create an Investigation entity (the top of the ISA hierarchy). Example: draft_investigation(hints={'name': 'Hepatotoxicity screen', 'description': 'In vitro liver tox study'}).",
        "parameters": {
            "type": "object",
            "properties": {"hints": draft_hints_schema("Investigation")},
            "required": ["hints"],
        },
    },
    {
        "name": "draft_study",
        "description": "Create a Study entity linked to an investigation. Example: draft_study(investigation_id='inv_hepatotoxicity_screen', hints={'name': 'Silychristin exposure', 'aop': 'Aop:144'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "entity_id of the parent Investigation.",
                },
                "hints": draft_hints_schema("Study"),
            },
            "required": ["investigation_id", "hints"],
        },
    },
    {
        "name": "draft_assay",
        "description": "Create an Assay entity linked to a study. Example: draft_assay(study_id='study_silychristin_exposure', hints={'name': 'Cell viability assay'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "study_id": {"type": "string", "description": "entity_id of the parent Study."},
                "hints": draft_hints_schema("Assay"),
            },
            "required": ["study_id", "hints"],
        },
    },
    {
        "name": "scaffold_isa_backbone",
        "description": "Create a linked Investigation -> Study -> Assay backbone in ONE call (idempotent: reuses an existing entity of each type instead of duplicating). The fastest path to a BASE-passing crate. Pass optional per-entity hints; set validate_base=true to also run a base-profile check. Creates no File entities. Example: scaffold_isa_backbone(investigation={'name': 'Hepatotoxicity screen'}, study={'name': 'Silychristin exposure'}, assay={'name': 'Cell viability assay'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "investigation": draft_hints_schema("Investigation"),
                "study": draft_hints_schema("Study"),
                "assay": draft_hints_schema("Assay"),
                "validate_base": {
                    "type": "boolean",
                    "description": "Also run build_and_validate(profile='base') and return it under 'validation'.",
                },
            },
        },
    },
    {
        "name": "draft_process_chain",
        "description": "Create and wire a whole LabProcess derivation chain in ONE idempotent call: Sample ->[CellCulture]-> Sample ->[Exposure]-> table ->[EndpointReadout]-> raw ->[DataAnalysis]-> figures. Pass the parent assay_id and an ordered chain of steps; each step is {process_type, hints?, object?, result?}. Steps are always wired in canonical order (CellCulture->Exposure->EndpointReadout->DataAnalysis) and a subset is fine (partial chains work). CRITICAL: it SYNTHESIZES the missing outputs that EndpointReadout/DataAnalysis require (they have no build-time fallback) so the chain never dangles into a validation error — placeholder File/Sample entities with NO fabricated data. Explicit object/result you pass win over synthesis. Set validate_after=true to also run build_and_validate. Prefer this over draft_process+link for the standard chain. ALWAYS fill each step's experimental parameters from the assay metadata workbook / SOP you have read — Exposure: duration, cell_seeding_density, microplate; EndpointReadout: detection_instrument, instrument_manufacturer, measured_entity, endpoint, technical_replicate; DataAnalysis: computational_tool, data_calculation_and_statistics. A parameter you leave out is OMITTED from the crate (never written as 'unknown'), and each process MUST end up with at least one, so an empty hints={} on a step whose values are sitting in the workbook is a validation failure you caused. Never invent a value — if the source is silent, leave it out. Example: draft_process_chain(assay_id='assay_cell_viability_assay', chain=[{'process_type':'CellCulture','hints':{'name':'Seed MDCK'}},{'process_type':'Exposure','hints':{'duration':'24h','microplate':'96-well'}},{'process_type':'EndpointReadout','hints':{'detection_instrument':'Multiskan FC','endpoint':'T4 uptake','measured_entity':'Radioactivity'}},{'process_type':'DataAnalysis','hints':{'computational_tool':'GraphPad Prism'}}]).",
        "parameters": {
            "type": "object",
            "properties": {
                "assay_id": {
                    "type": "string",
                    "description": "entity_id of the parent Assay every process belongs to.",
                },
                "chain": {
                    "type": "array",
                    "description": "Ordered list of process steps to create and wire.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "process_type": {
                                "type": "string",
                                "enum": [
                                    "CellCulture",
                                    "Exposure",
                                    "EndpointReadout",
                                    "DataAnalysis",
                                ],
                                "description": "Which domain LabProcess subtype this step is.",
                            },
                            "hints": draft_hints_schema("LabProcess"),
                            "object": {
                                "type": ["array", "string"],
                                "description": "Explicit input entity id(s) the process consumes (overrides the inherited upstream output).",
                            },
                            "result": {
                                "type": ["array", "string"],
                                "description": "Explicit output entity id(s) the process produces (overrides synthesis).",
                            },
                        },
                        "required": ["process_type"],
                    },
                },
                "validate_after": {
                    "type": "boolean",
                    "description": "Also run build_and_validate and return it under 'validation'.",
                },
            },
            "required": ["assay_id", "chain"],
        },
    },
    {
        "name": "resolve_compound",
        "description": "Resolve a chemical NAME to a verified MolecularEntity in ONE call (the chemistry counterpart of scaffold_isa_backbone): it looks the compound up (lookup_compound: PubChem -> ChEBI fallback), mints/reuses the MolecularEntity carrying the looked-up CAS + PubChem CID (which the build turns into [CAS, PubChem CID] identifier PropertyValues), then VERIFIES each minted identifier against source. D5: a value that does not resolve is cleared, never kept as a fabricated id — the per-field verdicts are returned. Idempotent (keyed by name, no duplicate). Prefer this over lookup_compound + draft_molecular_entity + verify_identifier for a single compound. On a lookup miss returns {ok:false, error}. Example: resolve_compound(name='Silychristin A', hints={'description': 'a flavonolignan'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Compound name to resolve, e.g. 'Silychristin A'.",
                },
                "hints": draft_hints_schema("MolecularEntity"),
                "verify": {
                    "type": "boolean",
                    "description": "Verify the minted identifiers against source (default true). Pass false only when you will verify later — never to attach an unverified id.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "resolve_cell_line",
        "description": "Resolve a cell-line NAME to a CellLineSample carrying its Cellosaurus accession in ONE call (the cell-line counterpart of resolve_compound). Two steps, both against Cellosaurus: lookup_cell_line_by_name on the name (and then on catalog_name) commits an accession ONLY on its exact+unique D5 gate, and lookup_cell_line on that accession IS the verification (a definitive miss clears the accession; a transient outage keeps it unverified). Prefer this over lookup_cell_line_by_name + draft_cell_line_sample + verify_identifier. UNLIKE resolve_compound a miss is NOT a failure and there is no 'ok' key: a name-only CellLineSample is a valid ISA Sample, so the entity is ALWAYS minted and the accession is enrichment — read 'accession'/'match' to see whether one was found. 'name' is kept exactly as the source documents word it; the Cellosaurus label lands on alternateName. Idempotent (reused by accession, then by name). NEVER pass an accession — not as a hint and not as catalog_name (a CVCL_*-shaped catalog_name is refused); every id must come back from the lookup. Example: resolve_cell_line(name='FRTL-5 TPO-overexpressing rat thyroid follicular cells', catalog_name='FRTL-5').",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Cell-line name as the source documents word it, e.g. 'FRTL-5 TPO-overexpressing rat thyroid follicular cells'.",
                },
                "hints": _cell_line_resolve_hints_schema(),
                "catalog_name": {
                    "type": "string",
                    "description": "Optional SHORT catalogue name for the same line, e.g. 'FRTL-5' or 'HepG2' — a name, never an accession. Tried after the full name so a descriptive phrase can still reach its record.",
                },
                "verify": {
                    "type": "boolean",
                    "description": "Confirm the accession against its Cellosaurus record (default true). Passing false also skips the record fetch, so no enrichment lands — use only when you will verify later.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "resolve_publication",
        "description": "Resolve a publication TITLE to a DOI-backed ScholarlyArticle in ONE call (the citation counterpart of resolve_compound): it searches Crossref by title (query.bibliographic), applies a STRICT confidence gate, and on a confident match builds the publication + authors via draft_publication_with_authors(doi=...). D5 confidence gate: a DOI is committed ONLY when the top candidate clears BOTH Crossref's relevance score floor AND a normalized-title near-exact match — a high score on a different paper, a weak score on the right title, or no candidate all return {ok:false, reason:'no confident DOI match', title} and create NO entity. A DOI is never fabricated from a title. Idempotent (keyed by the resolved DOI). Returns {ok, doi, entity_id, title, score}. Example: resolve_publication(title='Adverse outcome pathway-based assessment of TPO inhibition in vitro').",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Publication title to resolve to a DOI, e.g. 'Adverse outcome pathway-based assessment of TPO inhibition in vitro'.",
                },
                "verify": {
                    "type": "boolean",
                    "description": "Reserved for parity with resolve_compound; the DOI is implicitly verified by the Crossref resolution. Accepted and ignored.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "materialize_aop_subgraph",
        "description": "Turn ONE AOP-Wiki id into the full crate subgraph in one call: an AdverseOutcomePathway node plus every KeyEvent (MIE/KE/AO, discriminated by eventType) and KeyEventRelationship, all cross-linked deterministically from AOP-Wiki (never fabricated). Pass only the numeric aop_id; optionally pass study_id to wire the AOP onto that Study (schema:mentions). Idempotent (keyed by AOP-Wiki IRI). Prefer this over lookup_aop + manual drafting when you want the whole pathway in the crate. Example: materialize_aop_subgraph(aop_id='610', study_id='study_silychristin_exposure').",
        "parameters": {
            "type": "object",
            "properties": {
                "aop_id": {
                    "type": "string",
                    "description": "Numeric AOP-Wiki identifier, e.g. '610'.",
                },
                "study_id": {
                    "type": "string",
                    "description": "Optional entity_id of a Study to wire the AOP onto (via the aop/mentions reference).",
                },
            },
            "required": ["aop_id"],
        },
    },
    {
        "name": "link_assay_to_key_event",
        "description": "Link an Assay to the AOP Key Event it MEASURES, by the event's name (schema:mentions via the Assay's keyEvent field). Run materialize_aop_subgraph first so the KeyEvents exist in the crate — the reference committed is always the in-state AOP-Wiki id for the matched event, never one built from the name. Matching ignores case and punctuation. If the name matches zero or several events it writes NOTHING and returns the candidates for you to choose from, because which Key Event an assay measures is a scientific claim rather than a string match. Example: link_assay_to_key_event(assay_id='assay_oatp1c1_transport', event_name='Thyroperoxidase, Inhibition').",
        "parameters": {
            "type": "object",
            "properties": {
                "assay_id": {
                    "type": "string",
                    "description": "entity_id of the Assay that performs the measurement.",
                },
                "event_name": {
                    "type": "string",
                    "description": "Name of the Key Event as written in the source, e.g. 'mitochondrial dysfunction'.",
                },
            },
            "required": ["assay_id", "event_name"],
        },
    },
    {
        "name": "draft_publication_with_authors",
        "description": "Create a publication (from a DOI) AND wire every author as a Person in ONE call, harmonizing each author's @id to their ORCID when it can be determined. Resolution cascade per author (first hit wins): (1) the Crossref ORCID on the author (verified before use); (2) an in-crate Person with a verified ORCID matching the author's family + given/initial (e.g. citation 'Fabian Wagenaars' -> root 'F.M.A. Wagenaars'); (3) a public ORCID search — a single strong (family + full given) match is auto-used, anything ambiguous (multiple candidates or an initial-only match) asks YOU via present_to_human/request_input; (4) fallback to a synthesized #CitationAuthor_<Given>_<Family> Person. NEVER attaches an unverified or guessed ORCID (D5). Prefer this over draft_publication when you want the authors resolved too. Example: draft_publication_with_authors(doi='10.1234/example').",
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "description": "DOI of the publication (with or without a URL prefix).",
                },
            },
            "required": ["doi"],
        },
    },
    {
        "name": "draft_molecular_entity",
        "description": "Create a MolecularEntity from a compound name. Look the compound up first (lookup_compound) so you can pass a verified pubchem_cid. Example: draft_molecular_entity(name='Silychristin A', hints={'pubchem_cid': '<CID returned by lookup_compound>', 'identifier': '<CAS RN returned by lookup_compound>'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Compound name."},
                "hints": draft_hints_schema("MolecularEntity"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_cell_line_sample",
        "description": "Create a CellLineSample from a cell-line name. Look the cell line up first (lookup_cell_line_by_name) to get a verified Cellosaurus accession; lookup_cell_line takes an accession you already hold, so it cannot resolve a bare name. Never carry an accession over from another cell line — an accession that resolves to a different record is worse than none. Example: draft_cell_line_sample(name='HepG2', hints={'accession': '<CVCL_… returned by lookup_cell_line_by_name>'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Cell-line name."},
                "hints": draft_hints_schema("CellLineSample"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_process",
        "description": "Create a LabProcess (CellCulture/Exposure/EndpointReadout/DataAnalysis). Wire its inputs/outputs with `link` afterwards. Example: draft_process(assay_id='assay_cell_viability_assay', process_type='Exposure', hints={'duration': '24h', 'chemicals': 'chem_silychristin_a'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "assay_id": {"type": "string", "description": "entity_id of the parent Assay."},
                "process_type": {
                    "type": "string",
                    "enum": [
                        "CellCulture",
                        "Exposure",
                        "EndpointReadout",
                        "DataAnalysis",
                    ],
                    "description": "Which domain LabProcess subtype to create.",
                },
                "hints": draft_hints_schema("LabProcess"),
            },
            "required": ["assay_id", "process_type", "hints"],
        },
    },
    {
        "name": "draft_file",
        "description": "Create a File data entity (raw measurements, processed results, figures, analysis scripts). Use this so a process can take the file as input/output via `link` — the agent had no other way to create a File. For a source-code file pass additional_types=['SoftwareSourceCode'] and programming_language (e.g. 'Python') so it is typed @type:[File, SoftwareSourceCode]. Returns the File entity.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "File name, e.g. 'raw_measurements.csv'"},
                "path": {
                    "type": "string",
                    "description": "Crate-relative path (dest_path), e.g. 'data/raw.csv' (optional)",
                },
                "role": {
                    "type": "string",
                    "description": "Role label, e.g. 'raw_data' or 'figure' (optional)",
                },
                "encoding_format": {
                    "type": "string",
                    "description": "IANA media type, e.g. 'text/csv' (optional)",
                },
                "additional_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra @type term(s) alongside File, e.g. ['SoftwareSourceCode'] for an analysis script (optional)",
                },
                "programming_language": {
                    "type": "string",
                    "description": "schema:programmingLanguage for a source-code file, e.g. 'Python' (optional)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "link",
        "description": "Wire one provenance edge from_id --relation--> to_id to connect the derivation chain (e.g. a process's output to the next process's input). Both entities must already exist. Use to set a process's object/input (what it consumes) and result/output (what it produces). Calling it again with the same relation adds another target.",
        "parameters": {
            "type": "object",
            "properties": {
                "from_id": {
                    "type": "string",
                    "description": "entity_id of the source (usually the LabProcess)",
                },
                "relation": {
                    "type": "string",
                    "enum": ["object", "input", "samples", "result", "output", "derives_from"],
                    "description": "Edge verb: object/input/samples = consumed; result/output = produced; derives_from = sample lineage.",
                },
                "to_id": {
                    "type": "string",
                    "description": "entity_id of the target (a Sample, File, etc.)",
                },
            },
            "required": ["from_id", "relation", "to_id"],
        },
    },
    {
        "name": "attach_files",
        "description": "Bulk-place a GROUP of scanned files under a Study or Assay in one call — the scalable way to associate data with structure (e.g. all of an assay's raw CSVs). For each match it creates (or reuses) a File entity and adds it to the target's hasPart, so the build nests it under that dataset. Select with name_contains / mime_contains substrings or an explicit paths list; stamp an optional role (e.g. 'raw_data'). Files you don't place are still auto-included at the crate root on export, so this is for the files whose assay/study you DO know. Use link (not this) for a process's input/output. Returns {attached, file_ids, to}.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "entity_id of the target Study or Assay."},
                "name_contains": {
                    "type": "string",
                    "description": "Match files whose filename or path contains this substring (case-insensitive).",
                },
                "mime_contains": {
                    "type": "string",
                    "description": "Match files whose mime_type contains this substring, e.g. 'csv', 'image'.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit scanned paths/filenames to attach (with or instead of the substring filters).",
                },
                "role": {
                    "type": "string",
                    "description": "Optional role to stamp on each File, e.g. 'raw_data' or 'processed'.",
                },
            },
            "required": ["to"],
        },
    },
    {
        "name": "check_provenance",
        "description": "Lint the derivation chain (report-only, writes nothing). Returns {ok, issues:[{entity_id, property, message, fix, severity, profile}]} flagging EndpointReadout/DataAnalysis processes with no output and File entities produced by no process — each issue names the entity and the `link`/`draft_file` fix. Run it after wiring processes to confirm the Sample→CellCulture→Exposure→EndpointReadout→DataAnalysis chain is connected.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "draft_person",
        "description": "Create a Person entity. Pass an orcid only when you already hold one you can confirm with lookup_orcid, which takes an ORCID iD and not a name; when the person is an author of a DOI, draft_publication_with_authors resolves their ORCID for you. Example: draft_person(name='Jane Doe', hints={'orcid': '<ORCID iD confirmed with lookup_orcid>'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Person's name."},
                "hints": draft_hints_schema("Person"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_organization",
        "description": "Create an Organization entity. Look it up first (lookup_ror) to pass a verified ror. Example: draft_organization(name='Utrecht University', hints={'ror': '04pp8hn57'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Organization name."},
                "hints": draft_hints_schema("Organization"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_publication",
        "description": "Create a Publication entity from a DOI. Look it up first (lookup_doi) to fill the title and authors. Example: draft_publication(doi='10.1234/example', hints={'name': 'A paper title'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "DOI of the publication."},
                "hints": draft_hints_schema("Publication"),
            },
            "required": ["doi", "hints"],
        },
    },
    {
        "name": "draft_defined_term",
        "description": "Create a schema:DefinedTerm contextual entity to PERSIST a looked-up ontology / AOP / Key-Event term so it round-trips into the crate and can be referenced (via set_fields/link) as a mentions / measurementMethod / sampleType target. Pass the looked-up IRI as the 'url' hint so the node gets a dereferenceable @id. Example: draft_defined_term(name='cell viability assay', hints={'term_code': '<CURIE returned by lookup_bao_term / lookup_ontology_term>', 'url': '<IRI returned by the same lookup>'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Term label."},
                "hints": draft_hints_schema("DefinedTerm"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_property_value",
        "description": "Create a schema:PropertyValue contextual entity (a typed key/value with an optional ontology propertyID and unit). Use it for measured/asserted values that other entities reference. Example: draft_property_value(name='Passage Number', hints={'value': '12', 'unit_text': 'passages'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Property name."},
                "hints": draft_hints_schema("PropertyValue"),
            },
            "required": ["name", "hints"],
        },
    },
    {
        "name": "set_fields",
        "description": "Set one or more fields on an existing entity (the single mutation tool — pass one key or many). Use it to fill or correct an entity's metadata, e.g. after build_and_validate names a field to fix. Example: set_fields(entity_id='chem_silychristin_a', fields={'identifier': '<CAS RN from a verified lookup>', 'smiles': 'C[C@H]1...'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID of the entity to update."},
                "fields": {
                    "type": "object",
                    "description": 'Dictionary of field names to values (one or many), e.g. {"name": "new name", "description": "new desc"}.',
                },
            },
            "required": ["entity_id", "fields"],
        },
    },
    {
        "name": "set_crate_metadata",
        "description": "WRITE-ONLY setter for top-level crate metadata on the Root Data Entity (./): title/description/accession, the root dates release_date/date_modified, and crate-level ATTRIBUTION (publisher/creator/contact/license) — who is responsible for this dataset, which is NOT the same as the publication's authors. You MUST pass at least one value to write — a call with all fields null writes nothing, is REJECTED with an error, and does not read anything back; use get_status to READ the current crate metadata. Pass ISO-8601 strings for the dates, e.g. release_date='2025-11-10', date_modified='2026-06-14T19:37:30Z'. Only the fields you pass are written — never fabricate a date. datePublished is auto-set at build time and is not controlled here. Example: set_crate_metadata(accession='S-VHPS21', release_date='2025-11-10', date_modified='2026-06-14T19:37:30Z').",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Human-readable crate title (root name).",
                },
                "description": {
                    "type": "string",
                    "description": "Free-text crate description (root description).",
                },
                "accession": {
                    "type": "string",
                    "description": "Accession/identifier (root identifier).",
                },
                "release_date": {
                    "type": "string",
                    "description": "ISO-8601 release date for schema:releaseDate, e.g. '2025-11-10'.",
                },
                "date_modified": {
                    "type": "string",
                    "description": "ISO-8601 date/datetime for schema:dateModified, e.g. '2026-06-14T19:37:30Z'.",
                },
                "publisher": {
                    "type": "string",
                    "description": "WHO PUBLISHES this dataset: an entity id of a drafted Person/Organization, or a verified ORCID/ROR IRI. Rejected if it does not resolve — a bare name is not attribution.",
                },
                "creator": {
                    "type": "string",
                    "description": "WHO MADE this dataset (entity id or verified ORCID/ROR IRI). Distinct from the publication's authors, which describe the paper.",
                },
                "contact": {
                    "type": "string",
                    "description": "WHO TO CONTACT about this dataset (entity id or verified ORCID IRI) — typically the corresponding person named in the assay metadata.",
                },
                "license": {
                    "type": "string",
                    "description": "Licence for the dataset, ideally a URL (e.g. 'https://creativecommons.org/licenses/by/4.0/'). Ask the user; never guess.",
                },
            },
        },
    },
    {
        "name": "set_validation_preference",
        "description": "Record whether the user wants the broader RECOMMENDED / OPTIONAL validation tiers run from now on. The loop asks about each tier ONCE and then honours the answer silently — call this only when the user changes their mind mid-session, e.g. 'stop running the recommended checks' (recommended=false) or 'let's look at the optional findings too' (optional=true). The tiers are a hierarchy: turning recommended off turns optional off with it, and turning optional on turns recommended on. Never call this to re-ask a question the user has already answered.",
        "parameters": {
            "type": "object",
            "properties": {
                "recommended": {
                    "type": "boolean",
                    "description": "Run the RECOMMENDED (SHOULD) tier from now on. Omit to leave unchanged.",
                },
                "optional": {
                    "type": "boolean",
                    "description": "Run the OPTIONAL (MAY) tier from now on. Omit to leave unchanged.",
                },
            },
        },
    },
    {
        "name": "remove_entity",
        "description": "Remove an entity by id. Refuses (with an error naming the referrers) if other entities still reference it, so no dangling reference is left behind. Pass cascade=true to clear those references and remove anyway.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "cascade": {
                    "type": "boolean",
                    "description": "When true, clear references to this entity from all referrers instead of refusing. Default false.",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_entities",
        "description": "List entities, optionally filtered by type",
        "parameters": {
            "type": "object",
            "properties": {"entity_type": {"type": "string"}},
        },
    },
    {
        "name": "list_scanned_files",
        "description": "Retrieve the FULL scanned-file inventory from session state (path, filename, size, mime_type). scan_files only shows a ~15-file sample and its output is pruned from history, so use this to browse the inventory and decide which files to place/annotate (e.g. which group is an assay's raw data, which is a protocol). Paginated and filterable: pass name_contains / mime_contains to narrow, offset / limit to page (default limit 200). Returns {total_scanned, matched, offset, limit, returned, files:[...]}.",
        "parameters": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": "Only files whose filename or path contains this substring (case-insensitive).",
                },
                "mime_contains": {
                    "type": "string",
                    "description": "Only files whose mime_type contains this substring, e.g. 'csv', 'image'.",
                },
                "offset": {"type": "integer", "description": "Pagination start index (default 0)."},
                "limit": {"type": "integer", "description": "Max files to return (default 200)."},
            },
        },
    },
    {
        "name": "lookup_compound",
        "description": "Look up chemical compound via PubChem",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "lookup_cell_line",
        "description": "Fetch a Cellosaurus record from an accession you already hold (CVCL_*). It does NOT accept a cell-line name — from a bare name use lookup_cell_line_by_name.",
        "parameters": {
            "type": "object",
            "properties": {"accession": {"type": "string"}},
            "required": ["accession"],
        },
    },
    {
        "name": "lookup_cell_line_by_name",
        "description": "Resolve a cell-line NAME (e.g. 'HepG2', 'A549') to its Cellosaurus accession (CVCL_*) via a name search. Use this when you have a cell-line name but no accession; feed the returned accession to draft_cell_line_sample. Returns the accession ONLY on a confident exact match — an ambiguous or partial-only name returns not-found rather than guessing an id.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "lookup_aop",
        "description": "Look up AOP via AOP-Wiki",
        "parameters": {
            "type": "object",
            "properties": {"aop_id": {"type": "string"}},
            "required": ["aop_id"],
        },
    },
    {
        "name": "lookup_bao_term",
        "description": "Search BAO ontology via OLS",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "lookup_ontology_term",
        "description": "Search any OLS-hosted ontology (efo, obi, ncit, uberon, chebi, …) for the best-matching term IRI. Generalises lookup_bao_term to any vocabulary. Returns @id, name, termCode and a relevance score. Example: lookup_ontology_term(query='apoptosis', ontology='efo').",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ontology": {
                    "type": "string",
                    "description": "OLS ontology short name, e.g. 'efo', 'obi', 'chebi', 'uberon'.",
                },
            },
            "required": ["query", "ontology"],
        },
    },
    {
        "name": "lookup_unit",
        "description": "Resolve a unit string (e.g. 'micromolar', 'hour') to a Units of Measurement Ontology (UO) IRI via OLS. Returns @id (UO IRI), name, termCode.",
        "parameters": {
            "type": "object",
            "properties": {"unit_string": {"type": "string"}},
            "required": ["unit_string"],
        },
    },
    {
        "name": "lookup_dtxsid",
        "description": "Resolve a chemical (by name, CAS RN, or InChIKey) to its EPA DTXSID via the CompTox Dashboard. Returns dtxsid plus name/casrn/inchikey. Example: lookup_dtxsid(query='Bisphenol A').",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "lookup_orcid",
        "description": "Look up person via ORCID",
        "parameters": {
            "type": "object",
            "properties": {"orcid_id": {"type": "string"}},
            "required": ["orcid_id"],
        },
    },
    {
        "name": "lookup_ror",
        "description": "Search organization via ROR",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "lookup_doi",
        "description": "Look up publication via Crossref",
        "parameters": {
            "type": "object",
            "properties": {"doi": {"type": "string"}},
            "required": ["doi"],
        },
    },
    {
        "name": "verify_identifier",
        "description": "Check identifier resolves at source",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "field": {"type": "string"},
            },
            "required": ["entity_id", "field"],
        },
    },
    {
        "name": "build_and_validate",
        "description": "Build the crate from the current state in memory and validate it in one step (no files written). This is the fast build/fix loop: use it on every iteration. Returns {ok, conformance, issues:[{entity_id, property, message, fix, severity, profile}]} — each issue is keyed to the entity and property that failed, so route your fix there. conformance maps each layer that ran to its REQUIRED pass/fail: {base,isa,tox} for profile='all', or just the scoped layer when you pass a single profile. Fix REQUIRED issues bottom-up: base, then isa, then tox.",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["required", "recommended", "optional"],
                    "description": "Gate severity. 'required' (default) is fastest and surfaces only blocking issues; lower it to also see recommendations.",
                },
                "profile": {
                    "type": "string",
                    "enum": ["all", "base", "isa", "tox"],
                    "description": "Which layer(s) to check. 'all' (default) runs base+isa+tox; scope to one layer to go faster while iterating.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "fix_required_issues",
        "description": "Deterministically repair the routed issues from build_and_validate — no LLM, no network. It runs build_and_validate, and for each issue applies an automatic repair when the correct value is already determined by state (e.g. a process missing its output where exactly ONE un-wired File exists in state -> link it as the result), then re-validates. Issues needing NEW content, a NEW entity, or a fabricated identifier are left for you to fix and returned under 'remaining'. Idempotent and side-effect-safe: if nothing is deterministically fixable, state is unchanged. Returns {ok, fixed:[{issue, rule, action}], remaining:[{issue, reason}]}. Run it after wiring entities to auto-clear the mechanical REQUIRED issues, then handle the 'remaining' ones yourself.",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["required", "recommended", "optional"],
                    "description": "Gate severity forwarded to build_and_validate. 'required' (default) targets only blocking issues.",
                },
                "profile": {
                    "type": "string",
                    "enum": ["all", "base", "isa", "tox"],
                    "description": "Which layer(s) to check/repair. 'all' (default) runs base+isa+tox; scope to one layer to go faster.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "export_crate",
        "description": "Write the finished RO-Crate to disk (the only step that touches disk). Use build_and_validate while iterating; call export_crate once the crate is conformant. Returns crate_path. Auto-embeds the browsable preview and the entity-graph diagram (ro-crate-graph.mmd, a CreativeWork about ./) so the crate is self-describing. Before writing, it validates at the FULL gate (REQUIRED + RECOMMENDED + OPTIONAL) so the embedded maturity report covers every tier, and returns 'validation' with per-tier issue_counts; 'ok' there is REQUIRED conformance only, so RECOMMENDED/OPTIONAL counts are findings to report, not a failed export. When output_path is omitted, defaults to sessions/<session_id>/working_crate/",
        "parameters": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "Where to write the crate directory (optional, defaults to sessions/<session_id>/working_crate/)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "build_crate",
        "description": "Alias of export_crate: assemble and write the RO-Crate directory to disk. Returns crate_path. When output_path is omitted, defaults to sessions/<session_id>/working_crate/",
        "parameters": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "Where to write the crate directory (optional, defaults to sessions/<session_id>/working_crate/)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "validate",
        "description": "Run three-pass SHACL validation on a crate directory already written to disk. Prefer build_and_validate for the in-loop check; use this only to validate an existing crate_path from export_crate.",
        "parameters": {
            "type": "object",
            "properties": {
                "crate_path": {
                    "type": "string",
                    "description": "Path to the crate directory to validate (use the crate_path returned by export_crate)",
                }
            },
            "required": ["crate_path"],
        },
    },
    {
        "name": "validate_table",
        "description": "Validate a CSV's DATA CONTENT against its CSVW/Frictionless table schema (the payload layer, separate from SHACL metadata validation). Checks that each cell matches its declared column type/constraints and that foreign-key columns reference existing entity ids. Use it on the condition table or a raw-measurement table after export. Returns {ok, issues:[{entity_id, property, message, fix, severity, profile}]} with profile='data'; property names the offending column. Pass foreign_keys to check that compound/cell-line columns resolve to known MolecularEntity/Sample ids.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to the CSV file to validate."},
                "table_schema": {
                    "type": "object",
                    "description": 'Frictionless table schema descriptor: {"fields": [{"name", "type", "constraints"?}, ...]}. Adapt CSVW datatype columns to this shape.',
                },
                "foreign_keys": {
                    "type": "object",
                    "description": 'Optional map of column_name -> [allowed_id, ...]; each named column\'s cells must be one of the allowed in-crate ids (e.g. MolecularEntity/Sample). Example: {"compound": ["chem_aspirin"]}.',
                },
                "entity_id": {
                    "type": "string",
                    "description": "Optional id of the crate entity that owns the table (echoed on each issue for routing).",
                },
            },
            "required": ["file", "table_schema"],
        },
    },
    {
        "name": "populate_condition_table",
        "description": "Write per-well rows into an Exposure's CSVW condition table (replacing the header-only placeholder). Pass rows_or_csv_path as a list of row dicts, or a path to a user-supplied plate-map CSV. The canonical columns are well_id, assay, cell_line, compound, concentration_value, concentration_unit, exposure_duration, experiment, technical_replicate, control. Common synonyms are aliased for you (well->well_id, concentration/conc/dose->concentration_value, unit(s)->concentration_unit, duration/exposure_time->exposure_duration, cell->cell_line, chemical/substance/test_item->compound, replicate->technical_replicate), and a unit-suffixed dose header like concentration_uM fills concentration_value AND concentration_unit. Tidy per-well exports are aliased too (run_id->well_id, biosample_type->cell_line, test_substance_id->compound, exposure_concentration_value->concentration_value, exposure_concentration_unit->concentration_unit, assay_endpoint->assay, replicate_id->technical_replicate), and a split exposure_duration_value + exposure_duration_unit pair is composed into the single exposure_duration column ('24' + 'h' -> '24 h') rather than one half being dropped. Source columns with no canonical home (e.g. a measurement column) are reported in unmapped_source_columns, not written. The tool REFUSES and writes nothing if no canonical column maps or if no row has a well_id — a blank table is worse than the honest header. The table's CSVW typing (tableSchema) is preserved. Returns {ok, path, rows, unmapped_source_columns}. Validate the result with validate_table using the inferred schema.",
        "parameters": {
            "type": "object",
            "properties": {
                "exposure_id": {
                    "type": "string",
                    "description": "entity_id of the Exposure LabProcess.",
                },
                "rows_or_csv_path": {
                    "description": "A list of row dicts (canonical keys: well_id/assay/cell_line/compound/concentration_value/concentration_unit/exposure_duration/experiment/technical_replicate/control; common synonyms are aliased) OR a path to a plate-map CSV.",
                    "anyOf": [
                        {"type": "array", "items": {"type": "object"}},
                        {"type": "string"},
                    ],
                },
                "output_dir": {
                    "type": "string",
                    "description": "Crate root to write the CSV under (optional; defaults to the session output path).",
                },
            },
            "required": ["exposure_id", "rows_or_csv_path"],
        },
    },
    {
        "name": "assess_mit_coverage",
        "description": (
            "Score OECD MIT checklist coverage of the crate. Scores the ASSEMBLED "
            "crate, which it builds in memory when you do not hand it one — the "
            "checklist's slots name schema.org properties on assembled nodes, not "
            "CrateState fields, so a field scan cannot answer them."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "assess_fair_maturity",
        "description": "Score FAIR maturity from metadata",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "save_session",
        "description": "Save session to disk",
        "parameters": {"type": "object", "properties": {"label": {"type": "string"}}},
    },
    {
        "name": "get_status",
        "description": "Get current session status",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "scan_files",
        "description": "Scan a directory or zip archive for files. Archives are auto-extracted and scanned transparently. Results are stored in the session state.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "unzip_file",
        "description": "Extract a zip archive to a directory. Returns the extraction path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "output_dir": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "verify_all_identifiers",
        "description": "Verify all filled identifiers in state",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "extract_pdf_text",
        "description": "Extract structured content from a PDF file (text, tables, and image metadata). Returns a structured report with [Page N], [Text], [Table N], and [Image] markers, or None if the file cannot be read.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the PDF file"}},
            "required": ["path"],
        },
    },
    {
        "name": "draft_protocol",
        "description": "Create a LabProtocol entity that a LabProcess can follow. Example: draft_protocol(hints={'name': 'MTT viability protocol', 'url': 'https://protocols.io/...'}).",
        "parameters": {
            "type": "object",
            "properties": {"hints": draft_hints_schema("LabProtocol")},
            "required": ["hints"],
        },
    },
    {
        "name": "draft_sample",
        "description": "Create a Sample entity (a material input/output in the derivation chain). Example: draft_sample(hints={'name': 'Treated well A1', 'derives_from': 'sample_cultured_hepg2'}).",
        "parameters": {
            "type": "object",
            "properties": {"hints": draft_hints_schema("Sample")},
            "required": ["hints"],
        },
    },
    {
        "name": "get_hint",
        "description": "Get a hint for the next recommended action based on current state",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sessions",
        "description": "List all saved sessions",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "load_session",
        "description": "Load a previously saved session by session ID",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Session ID to load"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "present_to_human",
        "description": "Present information to the human user for review and get their response. Use this when you need user input, approval, or guidance.",
        "parameters": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "Context or information to present to the user",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of options for the user to choose from",
                },
            },
            "required": ["context"],
        },
    },
    {
        "name": "preview_archive",
        "description": "Preview the contents of a zip archive without extracting it. Returns a list of member file paths and metadata.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file_sample",
        "description": "Read a sample from a single FILE (not a directory). Use mode 'content' (first N lines, where N is the 'lines' argument — raise it to read more), 'summary' (file-type-aware overview like columns for CSV, keys for JSON, page count for PDF), or 'overview' (file metadata + summary). Use this instead of read_multiple_files when inspecting a single file. Passing a directory returns guidance to use list_scanned_files; to read a small file IN FULL prefer read_file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
                "lines": {
                    "type": "integer",
                    "description": "Number of lines to return in 'content' mode (default 20). This directly controls how much is returned — raise it (e.g. 1000) to read more of the file.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["content", "summary", "overview"],
                    "description": "Reading mode: 'content' (first N lines), 'summary' (file-type-aware summary), or 'overview' (file metadata + summary). Default 'content'.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_multiple_files",
        "description": "Read several files in one go. Each file is read with the same mode (content/summary/overview). Use this to inspect multiple files at once.",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to read",
                },
                "lines": {
                    "type": "integer",
                    "description": "Max lines per file in 'content' mode (default 50)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["content", "summary", "overview"],
                    "description": "Reading mode: 'content' (first N lines), 'summary' (file-type-aware summary), or 'overview' (file metadata + summary). Default 'content'.",
                },
            },
            "required": ["paths"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a supported FILE in full by extension (txt, csv, json, xlsx, docx, md, pdf) and return its text content. Text/JSON come back COMPLETE up to 64 KiB (a 32 KB JSON is returned whole — do NOT re-read it expecting more). A file larger than the budget is returned with an explicit '[truncated: showing first 64 KiB of N KiB; … do not re-read]' marker: re-reading the same way will NOT return more, so move on. A directory path returns guidance to use list_scanned_files. Use read_file_sample only when you want a small preview of a large file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_excel",
        "description": "Read an Excel .xlsx file and return its content as pipe-delimited text. Set compact=true on a depositor-filled metadata workbook to strip the repeated header row, the authoring-instructions Comments column and empty cells — same values, roughly 40% fewer characters, so more of the sheet fits in one read.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .xlsx file to read"},
                "compact": {
                    "type": "boolean",
                    "description": (
                        "Strip grid boilerplate (repeated header row, Comments column, "
                        "empty cells). No value is lost. Defaults to false."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_docx",
        "description": "Read a Word .docx file and return its text content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .docx file to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "request_input",
        "description": "Request a specific input value from the human user (e.g. a compound name, CAS number, or cell line accession). Use this when lookups fail and you need the user to provide information.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt describing what input is needed",
                },
                "field_type": {
                    "type": "string",
                    "description": "Type of input expected (e.g. text, number, identifier)",
                },
            },
            "required": ["prompt"],
        },
    },
]

# ---------------------------------------------------------------------------
# Provable tool-set parity with the shared registry (Issue #327)
# ---------------------------------------------------------------------------
#
# TOOL_SPECS advertises the ReAct tool set to the LLM. For the A/B comparison to
# be over the *same* toolbox (AGENTS.md §5, §12), it must stay in exact lockstep
# with the tools the shared engine can actually run. The authoritative set is:
#
#     builder.tools.registry.TOOL_REGISTRY.list()   # registered tools
#       | _LLM_TOOLS_OUTSIDE_REGISTRY               # engine-routed extras
#       - _REGISTRY_TOOLS_HIDDEN_FROM_LLM           # deliberately hidden
#
# Both divergences are small, intentional, and enumerated here — the one place
# they are declared — so ``expected_tool_spec_names()`` (used by the runtime
# assert and the parity tests) can never disagree with reality.

# LLM-callable tools the engine routes itself, so they are NOT in TOOL_REGISTRY:
#   - scanner / file-sample readers gated by AgentEngine before they touch disk
#     (Issue #167): scan_files, read_file_sample, read_multiple_files, unzip_file,
#     preview_archive;
#   - human-in-the-loop tools dispatched via the human interface: present_to_human,
#     request_input.
_LLM_TOOLS_OUTSIDE_REGISTRY: frozenset[str] = frozenset(
    {
        "scan_files",
        "read_file_sample",
        "read_multiple_files",
        "unzip_file",
        "preview_archive",
        "present_to_human",
        "request_input",
    }
)

# Registered tools deliberately withheld from the LLM. None today; a name added
# here (with a reason) is excluded from the advertised set without tripping parity.
_REGISTRY_TOOLS_HIDDEN_FROM_LLM: frozenset[str] = frozenset()

# Every module whose import registers tools into TOOL_REGISTRY. Enumerated so the
# registry is fully populated before it is read (registration is an import side
# effect), keeping this the single place that knows the tool-module set.
_TOOL_REGISTRY_MODULES: tuple[str, ...] = (
    "builder.tools.builder",
    "builder.tools.composites",
    "builder.tools.data_content",
    "builder.tools.drafters",
    "builder.tools.fair_assessment",
    "builder.tools.file_readers",
    "builder.tools.lookups",
    "builder.tools.management",
    "builder.tools.mit_assessment",
    "builder.tools.provenance",
    "builder.tools.repair",
    "builder.tools.scanner",
    "builder.tools.session",
    "builder.tools.validation",
    "builder.tools.verification",
)


class ToolSpecParityError(RuntimeError):
    """TOOL_SPECS has drifted from the tools the shared engine can run (#327)."""


def _registered_tool_names() -> set[str]:
    """Return every tool name in the shared registry (registry fully populated)."""
    import importlib

    for module in _TOOL_REGISTRY_MODULES:
        importlib.import_module(module)
    from builder.tools.registry import TOOL_REGISTRY

    return set(TOOL_REGISTRY.list())


def expected_tool_spec_names() -> set[str]:
    """The authoritative set of names TOOL_SPECS must advertise (Issue #327).

    ``registry ∪ engine-routed extras − deliberately-hidden`` — the single source
    of truth the runtime assert and the parity tests both measure against.
    """
    return (
        _registered_tool_names() | _LLM_TOOLS_OUTSIDE_REGISTRY
    ) - _REGISTRY_TOOLS_HIDDEN_FROM_LLM


def assert_tool_spec_parity() -> None:
    """Raise :class:`ToolSpecParityError` if TOOL_SPECS has drifted (Issue #327).

    Called when the ReAct arm builds its LangChain tools, so a mismatch fails fast
    at build time rather than silently advertising the wrong toolbox to the LLM.
    """
    # `str(...)`: a spec's values are heterogeneous (name, description, nested
    # parameter schemas), so the inferred element type is a wide union and
    # `sorted` has nothing to order it by. A tool name is always a string.
    spec_names = {str(spec["name"]) for spec in TOOL_SPECS}
    expected = expected_tool_spec_names()
    missing = expected - spec_names
    extra = spec_names - expected
    if missing or extra:
        raise ToolSpecParityError(
            "TOOL_SPECS is out of sync with the shared tool registry (#327): "
            f"callable tools with no schema: {sorted(missing)}; "
            f"schemas advertising uncallable tools: {sorted(extra)}."
        )


__all__ = [
    "TOOL_SPECS",
    "ToolSpecParityError",
    "assert_tool_spec_parity",
    "expected_tool_spec_names",
]
