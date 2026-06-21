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

from builder.tools.builder import build_crate
from builder.tools.drafters import (
    draft_assay,
    draft_cell_line_sample,
    draft_investigation,
    draft_molecular_entity,
    draft_organization,
    draft_person,
    draft_process,
    draft_publication,
    draft_study,
)
from builder.tools.fair_assessment import assess_fair_maturity
from builder.tools.hitl import present_to_human, request_input
from builder.tools.lookups import (
    lookup_aop,
    lookup_bao_term,
    lookup_cell_line,
    lookup_compound,
    lookup_doi,
    lookup_orcid,
    lookup_ror,
)
from builder.tools.management import (
    bulk_set_fields,
    list_entities,
    remove_entity,
    set_entity_field,
    update_entity,
)
from builder.tools.mit_assessment import assess_mit_coverage
from builder.tools.scanner import (
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
from builder.tools.validation import validate
from builder.tools.verification import verify_all_identifiers, verify_identifier

__all__ = [
    "assess_fair_maturity",
    "assess_mit_coverage",
    "build_crate",
    "bulk_set_fields",
    "draft_assay",
    "draft_cell_line_sample",
    "draft_investigation",
    "draft_molecular_entity",
    "draft_organization",
    "draft_person",
    "draft_process",
    "draft_publication",
    "draft_study",
    "get_hint",
    "get_status",
    "list_entities",
    "list_sessions",
    "load_session",
    "lookup_aop",
    "lookup_bao_term",
    "lookup_cell_line",
    "lookup_compound",
    "lookup_doi",
    "lookup_orcid",
    "lookup_ror",
    "present_to_human",
    "preview_archive",
    "read_file_sample",
    "read_multiple_files",
    "remove_entity",
    "request_input",
    "save_session",
    "scan_files",
    "set_entity_field",
    "unzip_file",
    "update_entity",
    "validate",
    "verify_all_identifiers",
    "verify_identifier",
]
