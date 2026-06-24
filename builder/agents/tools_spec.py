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
                "investigation_id": {"type": "string", "description": "entity_id of the parent Investigation."},
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
        "name": "draft_molecular_entity",
        "description": "Create a MolecularEntity from a compound name. Look the compound up first (lookup_compound) so you can pass a verified pubchem_cid. Example: draft_molecular_entity(name='Silychristin A', hints={'pubchem_cid': '443515', 'identifier': '33889-69-9'}).",
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
        "description": "Create a CellLineSample from a cell-line name. Look it up first (lookup_cell_line) to get a verified Cellosaurus accession. Example: draft_cell_line_sample(name='HepG2', hints={'accession': 'CVCL_0027'}).",
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
        "description": "Create a File data entity (raw measurements, processed results, figures). Use this so a process can take the file as input/output via `link` — the agent had no other way to create a File. Returns the File entity.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "File name, e.g. 'raw_measurements.csv'"},
                "path": {"type": "string", "description": "Crate-relative path (dest_path), e.g. 'data/raw.csv' (optional)"},
                "role": {"type": "string", "description": "Role label, e.g. 'raw_data' or 'figure' (optional)"},
                "encoding_format": {"type": "string", "description": "IANA media type, e.g. 'text/csv' (optional)"},
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
                "from_id": {"type": "string", "description": "entity_id of the source (usually the LabProcess)"},
                "relation": {
                    "type": "string",
                    "enum": ["object", "input", "samples", "result", "output", "derives_from"],
                    "description": "Edge verb: object/input/samples = consumed; result/output = produced; derives_from = sample lineage.",
                },
                "to_id": {"type": "string", "description": "entity_id of the target (a Sample, File, etc.)"},
            },
            "required": ["from_id", "relation", "to_id"],
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
        "description": "Create a Person entity. Look the person up first (lookup_orcid) to pass a verified orcid. Example: draft_person(name='Jane Doe', hints={'orcid': '0000-0002-1825-0097'}).",
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
        "description": "Create a schema:DefinedTerm contextual entity to PERSIST a looked-up ontology / AOP / Key-Event term so it round-trips into the crate and can be referenced (via set_fields/link) as a mentions / measurementMethod / sampleType target. Pass the looked-up IRI as the 'url' hint so the node gets a dereferenceable @id. Example: draft_defined_term(name='cell viability assay', hints={'term_code': 'BAO:0002993', 'url': 'http://www.bioassayontology.org/bao#BAO_0002993'}).",
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
        "description": "Set one or more fields on an existing entity (the single mutation tool — pass one key or many). Use it to fill or correct an entity's metadata, e.g. after build_and_validate names a field to fix. Example: set_fields(entity_id='chem_silychristin_a', fields={'identifier': '33889-69-9', 'smiles': 'C[C@H]1...'}).",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID of the entity to update."},
                "fields": {
                    "type": "object",
                    "description": "Dictionary of field names to values (one or many), e.g. {\"name\": \"new name\", \"description\": \"new desc\"}.",
                },
            },
            "required": ["entity_id", "fields"],
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
        "description": "Look up cell line via Cellosaurus",
        "parameters": {
            "type": "object",
            "properties": {"accession": {"type": "string"}},
            "required": ["accession"],
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
                "ontology": {"type": "string", "description": "OLS ontology short name, e.g. 'efo', 'obi', 'chebi', 'uberon'."},
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
        "name": "export_crate",
        "description": "Write the finished RO-Crate to disk (the only step that touches disk). Use build_and_validate while iterating; call export_crate once the crate is conformant. Returns crate_path. Auto-embeds the browsable preview and the entity-graph diagram (ro-crate-graph.mmd, a CreativeWork about ./) so the crate is self-describing. When output_path is omitted, defaults to sessions/<session_id>/working_crate/",
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
                    "description": "Frictionless table schema descriptor: {\"fields\": [{\"name\", \"type\", \"constraints\"?}, ...]}. Adapt CSVW datatype columns to this shape.",
                },
                "foreign_keys": {
                    "type": "object",
                    "description": "Optional map of column_name -> [allowed_id, ...]; each named column's cells must be one of the allowed in-crate ids (e.g. MolecularEntity/Sample). Example: {\"compound\": [\"chem_aspirin\"]}.",
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
        "description": "Write per-well rows into an Exposure's CSVW condition table (replacing the header-only placeholder). Pass rows_or_csv_path as a list of row dicts keyed by the columns cell_line/compound/concentration/unit/duration, or a path to a user-supplied plate-map CSV. The table's CSVW typing (tableSchema) is preserved. Returns {ok, path, rows}. Validate the result with validate_table using the inferred schema.",
        "parameters": {
            "type": "object",
            "properties": {
                "exposure_id": {"type": "string", "description": "entity_id of the Exposure LabProcess."},
                "rows_or_csv_path": {
                    "description": "A list of row dicts (keys: cell_line/compound/concentration/unit/duration) OR a path to a plate-map CSV.",
                    "anyOf": [
                        {"type": "array", "items": {"type": "object"}},
                        {"type": "string"},
                    ],
                },
                "output_dir": {"type": "string", "description": "Crate root to write the CSV under (optional; defaults to the session output path)."},
            },
            "required": ["exposure_id", "rows_or_csv_path"],
        },
    },
    {
        "name": "assess_mit_coverage",
        "description": "Score MIT coverage from entity fields",
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
                "context": {"type": "string", "description": "Context or information to present to the user"},
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
        "description": "Read a sample from a file. Use mode 'content' (first N lines), 'summary' (file-type-aware overview like columns for CSV, keys for JSON, page count for PDF), or 'overview' (file metadata + summary). Use this instead of read_multiple_files when inspecting a single file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
                "lines": {"type": "integer", "description": "Number of lines to read in 'content' mode (default 20)"},
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
                "paths": {"type": "array", "items": {"type": "string"}, "description": "List of file paths to read"},
                "lines": {"type": "integer", "description": "Max lines per file in 'content' mode (default 50)"},
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
        "description": "Read a supported file in full by extension (txt, csv, json, xlsx, docx, md, pdf) and return its text content. Use read_file_sample instead when you only need a preview of a large file.",
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
        "description": "Read an Excel .xlsx file and return its content as pipe-delimited text.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .xlsx file to read"},
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
                "prompt": {"type": "string", "description": "The prompt describing what input is needed"},
                "field_type": {"type": "string", "description": "Type of input expected (e.g. text, number, identifier)"},
            },
            "required": ["prompt"],
        },
    },
]

__all__ = ["TOOL_SPECS"]
