# ruff: noqa: E501
"""Tool specifications for LLM function calling."""

TOOL_SPECS = [
    {
        "name": "draft_investigation",
        "description": "Create Investigation entity",
        "parameters": {
            "type": "object",
            "properties": {"hints": {"type": "object"}},
            "required": ["hints"],
        },
    },
    {
        "name": "draft_study",
        "description": "Create Study entity linked to an investigation",
        "parameters": {
            "type": "object",
            "properties": {
                "investigation_id": {"type": "string"},
                "hints": {"type": "object"},
            },
            "required": ["investigation_id", "hints"],
        },
    },
    {
        "name": "draft_assay",
        "description": "Create Assay entity linked to a study",
        "parameters": {
            "type": "object",
            "properties": {"study_id": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["study_id", "hints"],
        },
    },
    {
        "name": "draft_molecular_entity",
        "description": "Create MolecularEntity from compound name",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_cell_line_sample",
        "description": "Create CellLineSample from cell line name",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_process",
        "description": "Create LabProcess (CellCulture/Exposure/EndpointReadout/DataAnalysis)",
        "parameters": {
            "type": "object",
            "properties": {
                "assay_id": {"type": "string"},
                "process_type": {
                    "type": "string",
                    "enum": [
                        "CellCulture",
                        "Exposure",
                        "EndpointReadout",
                        "DataAnalysis",
                    ],
                },
                "hints": {"type": "object"},
            },
            "required": ["assay_id", "process_type", "hints"],
        },
    },
    {
        "name": "draft_person",
        "description": "Create Person entity",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_organization",
        "description": "Create Organization entity",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["name", "hints"],
        },
    },
    {
        "name": "draft_publication",
        "description": "Create Publication entity from DOI",
        "parameters": {
            "type": "object",
            "properties": {"doi": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["doi", "hints"],
        },
    },
    {
        "name": "update_entity",
        "description": "Update fields on an entity",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "patch": {"type": "object"},
            },
            "required": ["entity_id", "patch"],
        },
    },
    {
        "name": "remove_entity",
        "description": "Remove an entity by id",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
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
        "name": "build_crate",
        "description": "Assemble the RO-Crate directory. Returns crate_path which you must pass to validate(). When output_path is omitted, defaults to sessions/<session_id>/working_crate/",
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
        "description": "Run three-pass SHACL validation on a crate directory. Pass the crate_path returned by build_crate.",
        "parameters": {
            "type": "object",
            "properties": {
                "crate_path": {
                    "type": "string",
                    "description": "Path to the crate directory to validate (use the crate_path returned by build_crate)",
                }
            },
            "required": ["crate_path"],
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
        "name": "bulk_set_fields",
        "description": "Set multiple fields on an entity at once. Use this instead of calling update_entity or set_entity_field repeatedly.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID of the entity to update"},
                "fields": {
                    "type": "object",
                    "description": "Dictionary of field names to values (e.g. {\"name\": \"new name\", \"description\": \"new desc\"})",
                },
            },
            "required": ["entity_id", "fields"],
        },
    },
    {
        "name": "draft_protocol",
        "description": "Create a LabProtocol entity",
        "parameters": {
            "type": "object",
            "properties": {"hints": {"type": "object"}},
            "required": ["hints"],
        },
    },
    {
        "name": "draft_sample",
        "description": "Create a Sample entity",
        "parameters": {
            "type": "object",
            "properties": {"hints": {"type": "object"}},
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
    {
        "name": "set_entity_field",
        "description": "Set a single field on an entity. For setting multiple fields at once, use bulk_set_fields instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID of the entity to update"},
                "field": {"type": "string", "description": "Field name to set"},
                "value": {"type": "string", "description": "Value to set the field to"},
            },
            "required": ["entity_id", "field", "value"],
        },
    },
]

__all__ = ["TOOL_SPECS"]
