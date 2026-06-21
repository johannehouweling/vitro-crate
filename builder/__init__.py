"""
ISA-Tox RO-Crate Builder — core package.

The builder package contains the state model, tool implementations, input
readers, output writers, and agent orchestration for the LLM-assisted
RO-Crate builder.
"""

from __future__ import annotations

from builder.state import (
    CrateState,
    Entity,
    EntityStatus,
    EntityProvenance,
    FieldCompletion,
    FileClassification,
    ReasoningStep,
    ReasoningLog,
    Checkpoint,
    CrateMetadata,
    ValidationReport,
    MITReport,
    FAIRReport,
    EntityType,
    CompletionStatus,
    CompletionSource,
    InputType,
)

__version__ = "0.1.0"

__all__ = [
    "CrateState",
    "Entity",
    "EntityStatus",
    "EntityProvenance",
    "FieldCompletion",
    "FileClassification",
    "ReasoningStep",
    "ReasoningLog",
    "Checkpoint",
    "CrateMetadata",
    "ValidationReport",
    "MITReport",
    "FAIRReport",
    "EntityType",
    "CompletionStatus",
    "CompletionSource",
    "InputType",
]