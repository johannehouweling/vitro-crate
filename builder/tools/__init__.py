"""
Tool implementations for the ISA-Tox RO-Crate Builder.

Each tool is a standalone function in its own module file. Tools provide
the action primitives that the LLM agent can call to inspect, modify, and
validate the CrateState.

Tool categories:
    - scanner.py: File scanning and classification
    - drafters.py: Entity creation from hints
    - management.py: Entity CRUD operations
    - lookups.py: External API lookups (PubChem, Cellosaurus, etc.)
    - verification.py: Identifier verification against sources
    - hitl.py: Human-in-the-loop interaction
    - builder.py: ROCrate assembly
    - validation.py: SHACL validation wrapper
    - mit_assessment.py: MIT coverage scoring
    - fair_assessment.py: FAIR maturity scoring
    - session.py: Session persistence and resume
"""

from __future__ import annotations

from builder.tools.builder import build_crate, export_crate
from builder.tools.data_content import populate_condition_table, validate_table
from builder.tools.drafters import (
    draft_assay,
    draft_cell_line_sample,
    draft_defined_term,
    draft_investigation,
    draft_molecular_entity,
    draft_organization,
    draft_person,
    draft_process,
    draft_property_value,
    draft_publication,
    draft_study,
)
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.file_readers import read_docx, read_excel, read_file
from builder.tools.hitl import present_to_human, request_input
from builder.tools.lookups import (
    lookup_aop,
    lookup_bao_term,
    lookup_cell_line,
    lookup_cell_line_by_name,
    lookup_compound,
    lookup_doi,
    lookup_orcid,
    lookup_ror,
)
from builder.tools.management import (
    bulk_set_fields,
    list_entities,
    remove_entity,
    set_crate_metadata,
    set_entity_field,
    set_fields,
    update_entity,
)
from builder.tools.mit_assessment import assess_mit_coverage
from builder.tools.provenance import check_provenance, draft_file, link
from builder.tools.repair import fix_required_issues
from builder.tools.scanner import (
    extract_pdf_text,
    preview_archive,
    read_file_sample,
    read_multiple_files,
    scan_files,
    unzip_file,
)
from builder.tools.session import (
    get_hint,
    get_status,
    list_sessions,
    load_session,
    save_session,
)
from builder.tools.validation import build_and_validate, validate
from builder.tools.verification import verify_all_identifiers, verify_identifier

__all__ = [
    "assess_fair_maturity",
    "assess_mit_coverage",
    "build_and_validate",
    "build_crate",
    "bulk_set_fields",
    "check_provenance",
    "draft_assay",
    "draft_cell_line_sample",
    "draft_defined_term",
    "draft_file",
    "draft_investigation",
    "draft_molecular_entity",
    "draft_organization",
    "draft_person",
    "draft_process",
    "draft_property_value",
    "draft_publication",
    "draft_study",
    "export_crate",
    "extract_pdf_text",
    "fix_required_issues",
    "get_hint",
    "get_status",
    "link",
    "list_entities",
    "list_sessions",
    "load_session",
    "lookup_aop",
    "lookup_bao_term",
    "lookup_cell_line",
    "lookup_cell_line_by_name",
    "lookup_compound",
    "lookup_doi",
    "lookup_orcid",
    "lookup_ror",
    "populate_condition_table",
    "present_to_human",
    "preview_archive",
    "read_docx",
    "read_excel",
    "read_file",
    "read_file_sample",
    "read_multiple_files",
    "remove_entity",
    "request_input",
    "save_session",
    "scan_files",
    "set_crate_metadata",
    "set_entity_field",
    "set_fields",
    "unzip_file",
    "update_entity",
    "validate",
    "validate_table",
    "verify_all_identifiers",
    "verify_identifier",
]
