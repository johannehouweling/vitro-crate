"""
Tool implementations for the ISA-Tox RO-Crate Builder.

Each tool is a standalone function in its own module file. Tools provide
the action primitives that the LLM agent can call to inspect, modify, and
validate the CrateState.

Tool categories:
    - scanner.py: File scanning and classification
    - scaffolder.py: ARC folder structure scaffolding
    - drafters.py: Entity creation from hints
    - management.py: Entity CRUD operations
    - lookups.py: External API lookups (PubChem, Cellosaurus, etc.)
    - verification.py: Identifier verification against sources
    - validation.py: SHACL validation wrapper
    - mit_assessment.py: MIT coverage scoring
    - fair_assessment.py: FAIR maturity scoring
    - session.py: Session persistence and resume
"""

from __future__ import annotations

from builder.tools.scanner import read_file_sample, scan_files

__all__ = [
    "scan_files",
    "read_file_sample",
]