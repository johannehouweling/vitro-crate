"""
CrateState — The Central Data Model for the ISA-Tox RO-Crate Builder.

CrateState is the single source of truth for the builder. It is serializable
to JSON and persists to disk for session resume. All entities, validation
results, assessment scores, and agent reasoning are tracked in this state.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

EntityType = Literal[
    "Investigation",
    "Study",
    "Assay",
    "LabProcess",
    "LabProtocol",
    "Sample",
    "MolecularEntity",
    "CellLineSample",
    "Person",
    "Organization",
    "Publication",
    "DefinedTerm",
    "PropertyValue",
    "File",
]

CompletionStatus = Literal["missing", "filled", "verified"]
CompletionSource = Literal["scanner", "llm", "user", "lookup"]
InputType = Literal["directory", "conversation"]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EntityStatus(str, Enum):
    """Provenance status for an entity in the builder workflow.

    Attributes:
        DRAFT: Entity has been created but not yet enriched.
        ENRICHED: Entity has been enriched with lookups / additional data.
        REVIEWED: Entity has been reviewed by a human.
        VERIFIED: Entity identifiers have been verified against their sources.
    """

    DRAFT = "draft"
    ENRICHED = "enriched"
    REVIEWED = "reviewed"
    VERIFIED = "verified"


# ---------------------------------------------------------------------------
# Core dataclasses — FieldCompletion & EntityProvenance
# ---------------------------------------------------------------------------


@dataclass
class FieldCompletion:
    """Tracks the completion status and provenance of a single field.

    Attributes:
        status: Whether the field value is missing, filled, or verified.
        source: How the field value was obtained.
    """

    status: CompletionStatus
    source: CompletionSource

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> FieldCompletion:
        return cls(status=data["status"], source=data["source"])  # ty: ignore[invalid-argument-type]


@dataclass
class EntityProvenance:
    """Provenance metadata for an entity.

    Attributes:
        created_by: How the entity was created.
        reviewed_by: Optional user who reviewed this entity.
        lookups_used: List of lookup services that contributed data.
    """

    created_by: CompletionSource
    reviewed_by: str | None = None
    lookups_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"created_by": self.created_by}
        if self.reviewed_by is not None:
            d["reviewed_by"] = self.reviewed_by
        d["lookups_used"] = self.lookups_used
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityProvenance:
        return cls(
            created_by=data["created_by"],  # type: ignore[arg-type]
            reviewed_by=data.get("reviewed_by"),
            lookups_used=data.get("lookups_used", []),
        )

# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """A metadata entity in the RO-Crate builder state.

    Entities are the core building blocks of the ISA-Tox RO-Crate. Each entity
    carries its field values plus completion and provenance metadata tracked
    via private ``_completion`` and ``_provenance`` attributes.

    Attributes:
        entity_id: Unique identifier for this entity within the session.
        type: The RO-Crate entity type (e.g. "Investigation").
        fields: Key-value pairs of schema.org / profile properties.
        _completion: Per-field completion tracking by "{type}:{field}".
        _provenance: Provenance metadata for the entity.
    """

    entity_id: str
    type: EntityType
    fields: dict[str, Any] = field(default_factory=dict)
    _completion: dict[str, FieldCompletion] = field(default_factory=dict, repr=False)
    _provenance: EntityProvenance = field(
        default_factory=lambda: EntityProvenance(created_by="llm"), repr=False
    )

    def _completion_key(self, field: str) -> str:
        return f"{self.type}:{field}"

    def set_field_status(
        self, field: str, status: CompletionStatus, source: CompletionSource
    ) -> None:
        """Set the completion status for a single field."""
        key = self._completion_key(field)
        self._completion[key] = FieldCompletion(status=status, source=source)

    def get_field_status(self, field: str) -> FieldCompletion | None:
        """Return the completion status for a field, or None if unset."""
        return self._completion.get(self._completion_key(field))

    def set_fields_from_dict(
        self, values: dict[str, Any], source: CompletionSource = "llm"
    ) -> None:
        """Set multiple fields at once, marking each as filled."""
        for field_name, value in values.items():
            self.fields[field_name] = value
            self.set_field_status(field_name, "filled", source)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this entity to a JSON-compatible dictionary."""
        return {
            "entity_id": self.entity_id,
            "type": self.type,
            "fields": dict(self.fields),
            "_completion": {k: v.to_dict() for k, v in self._completion.items()},
            "_provenance": self._provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """Deserialize an entity from a dictionary."""
        completion = {
            k: FieldCompletion.from_dict(v)
            for k, v in data.get("_completion", {}).items()
        }
        provenance = EntityProvenance.from_dict(
            data.get("_provenance", {"created_by": "llm"})
        )
        return cls(
            entity_id=data["entity_id"],
            type=data["type"],  # type: ignore[arg-type]
            fields=data.get("fields", {}),
            _completion=completion,
            _provenance=provenance,
        )

# ---------------------------------------------------------------------------
# FileClassification
# ---------------------------------------------------------------------------


@dataclass
class FileClassification:
    """Raw file inventory record produced by the scanner.

    Attributes:
        path: Absolute or relative path to the file.
        filename: Base name of the file.
        size: File size in bytes.
        mime_type: Detected MIME type.
        first_rows: Preview of the first rows for CSV/TSV/XLSX files (None for
            other formats or when the preview could not be read).
        reviewed_by_user: Whether a human has reviewed this classification.
    """

    path: str
    filename: str
    size: int
    mime_type: str
    first_rows: list[str] | None = None
    reviewed_by_user: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "size": self.size,
            "mime_type": self.mime_type,
            "first_rows": self.first_rows,
            "reviewed_by_user": self.reviewed_by_user,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileClassification:
        return cls(
            path=data["path"],
            filename=data["filename"],
            size=data["size"],
            mime_type=data["mime_type"],
            first_rows=data.get("first_rows"),
            reviewed_by_user=data.get("reviewed_by_user", False),
        )

@dataclass
class ArchivePreview:
    """Preview metadata for a zip archive without extracting it.

    Attributes:
        path: Absolute path to the archive file.
        filename: Base name of the archive file.
        size_bytes: Size of the archive in bytes.
        size_mb: Size of the archive in megabytes.
        entry_count: Number of entries inside the archive.
        entries: List of dicts with keys ``path``, ``size``, ``is_dir``.
        message: Human-readable summary message.
        error: Error message if the archive could not be read (None on success).
    """

    path: str
    filename: str
    size_bytes: int
    size_mb: float
    entry_count: int
    entries: list[dict[str, Any]]
    message: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "size_mb": self.size_mb,
            "entry_count": self.entry_count,
            "entries": self.entries,
            "message": self.message,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchivePreview:
        return cls(
            path=data["path"],
            filename=data["filename"],
            size_bytes=data["size_bytes"],
            size_mb=data["size_mb"],
            entry_count=data["entry_count"],
            entries=data.get("entries", []),
            message=data["message"],
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CrateMetadata:
    """Top-level metadata describing the RO-Crate itself.

    Attributes:
        title: Human-readable title for the crate.
        description: Free-text description.
        accession: Optional accession or identifier.
        input_type: Whether input was a directory or conversation.
        input_path: Path to the input directory (if applicable).
        output_path: Path where the crate will be written.
    """

    title: str | None = None
    description: str | None = None
    accession: str | None = None
    input_type: InputType = "directory"
    input_path: str | None = None
    output_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"input_type": self.input_type}
        if self.title is not None:
            d["title"] = self.title
        if self.description is not None:
            d["description"] = self.description
        if self.accession is not None:
            d["accession"] = self.accession
        if self.input_path is not None:
            d["input_path"] = self.input_path
        if self.output_path is not None:
            d["output_path"] = self.output_path
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrateMetadata:
        return cls(
            title=data.get("title"),
            description=data.get("description"),
            accession=data.get("accession"),
            input_type=data.get("input_type", "directory"),  # type: ignore[arg-type]
            input_path=data.get("input_path"),
            output_path=data.get("output_path"),
        )


@dataclass
class ValidationReport:
    """Results from the three-pass SHACL validation.

    Attributes:
        base_passed: Base RO-Crate 1.1 profile passed.
        isa_passed: ISA profile passed (no REQUIRED issues).
        tox_passed: ISA-Tox profile passed (no REQUIRED issues).
        required_issues: REQUIRED-severity issue descriptions.
        should_issues: SHOULD-severity issue descriptions.
        may_issues: MAY-severity (informational) issue descriptions.
    """

    base_passed: bool = False
    isa_passed: bool = False
    tox_passed: bool = False
    required_issues: list[str] = field(default_factory=list)
    should_issues: list[str] = field(default_factory=list)
    may_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_passed": self.base_passed,
            "isa_passed": self.isa_passed,
            "tox_passed": self.tox_passed,
            "required_issues": list(self.required_issues),
            "should_issues": list(self.should_issues),
            "may_issues": list(self.may_issues),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationReport:
        return cls(
            base_passed=data.get("base_passed", False),
            isa_passed=data.get("isa_passed", False),
            tox_passed=data.get("tox_passed", False),
            required_issues=data.get("required_issues", []),
            should_issues=data.get("should_issues", []),
            may_issues=data.get("may_issues", []),
        )

@dataclass
class MITReport:
    """Coverage report from the MIT assessment.

    Attributes:
        module_scores: Per-module scores keyed by module name,
            each containing {"completed": int, "total": int}.
        overall_score: Overall coverage as a fraction (0.0 - 1.0).
    """

    module_scores: dict[str, dict[str, int]] = field(default_factory=dict)
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_scores": dict(self.module_scores),
            "overall_score": self.overall_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MITReport:
        return cls(
            module_scores=data.get("module_scores", {}),
            overall_score=data.get("overall_score", 0.0),
        )


@dataclass
class FAIRReport:
    """Maturity report from the FAIR indicators assessment.

    Attributes:
        indicator_results: List of individual indicator results as dicts.
        dsm_level: Data Stewardship Maturity level (0-5).
    """

    indicator_results: list[dict[str, Any]] = field(default_factory=list)
    dsm_level: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_results": list(self.indicator_results),
            "dsm_level": self.dsm_level,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FAIRReport:
        return cls(
            indicator_results=data.get("indicator_results", []),
            dsm_level=data.get("dsm_level", 0),
        )

# ---------------------------------------------------------------------------
# Reasoning & Checkpoint
# ---------------------------------------------------------------------------


@dataclass
class ReasoningStep:
    """A single reasoning step recorded in the agent's reasoning log.

    Attributes:
        step: Sequential step number.
        action: Human-readable description of the action taken.
        tool: The tool function that was called.
        result: Summary of the result or outcome.
        timestamp: ISO-8601 formatted datetime string.
    """

    step: int
    action: str
    tool: str
    result: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "tool": self.tool,
            "result": self.result,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReasoningStep:
        return cls(
            step=data["step"],
            action=data["action"],
            tool=data["tool"],
            result=data["result"],
            timestamp=data["timestamp"],
        )


@dataclass
class ReasoningLog:
    """Agent progress tracking, reasoning log, and iteration state.

    Attributes:
        next_actions: List of suggested or pending next actions.
        completed_checkpoints: List of completed checkpoint identifiers.
        reasoning_log: Chronological log of all reasoning steps.
        iteration_count: Number of tool-calling iterations completed.
        max_iterations: Maximum tool-calling iterations allowed.
        stuck: Whether the agent is currently marked as stuck.
    """

    next_actions: list[str] = field(default_factory=list)
    completed_checkpoints: list[str] = field(default_factory=list)
    reasoning_log: list[ReasoningStep] = field(default_factory=list)
    iteration_count: int = 0
    max_iterations: int = 50
    stuck: bool = False

    def log_reasoning(self, action: str, tool: str, result: str) -> ReasoningStep:
        """Append a reasoning step to the log and return it."""
        step = len(self.reasoning_log) + 1
        ts = datetime.now(timezone.utc).isoformat()
        entry = ReasoningStep(
            step=step, action=action, tool=tool, result=result, timestamp=ts
        )
        self.reasoning_log.append(entry)
        return entry

    def add_step(self, action: str, tool: str, result: str) -> ReasoningStep:
        """Backward-compatible alias; new code should use log_reasoning()."""
        return self.log_reasoning(action, tool, result)

    def mark_stuck(self, reason: str) -> ReasoningStep:
        """Mark the agent as stuck and record the reason."""
        self.stuck = True
        return self.log_reasoning("mark_stuck", "system", reason)

    def is_stuck(self) -> bool:
        """Return whether the agent is stuck or has exhausted iterations."""
        return self.stuck or self.iteration_count >= self.max_iterations

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_actions": list(self.next_actions),
            "completed_checkpoints": list(self.completed_checkpoints),
            "reasoning_log": [s.to_dict() for s in self.reasoning_log],
            "iteration_count": self.iteration_count,
            "max_iterations": self.max_iterations,
            "stuck": self.stuck,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReasoningLog:
        reason_log = [
            ReasoningStep.from_dict(s) for s in data.get("reasoning_log", [])
        ]
        return cls(
            next_actions=data.get("next_actions", []),
            completed_checkpoints=data.get("completed_checkpoints", []),
            reasoning_log=reason_log,
            iteration_count=data.get("iteration_count", 0),
            max_iterations=data.get("max_iterations", 50),
            stuck=data.get("stuck", False),
        )

# Permanent backward-compatible alias for existing Checkpoint imports.
Checkpoint = ReasoningLog

# Entity type → collection name mapping (module-level constant)
ENTITY_TYPE_MAP: dict[str, str] = {
    "Investigation": "investigations",
    "Study": "studies",
    "Assay": "assays",
    "LabProcess": "lab_processes",
    "LabProtocol": "protocols",
    "Sample": "samples",
    "MolecularEntity": "molecular_entities",
    "CellLineSample": "samples",
    "Person": "people",
    "Organization": "organizations",
    "Publication": "publications",
    "DefinedTerm": "defined_terms",
    "PropertyValue": "property_values",
    "File": "files",
}

# ENTITY_TYPE_MAP is a module-level constant; derive shared collection
# information once at import time so list_entities() can filter by
# concrete type when needed.
COLLECTION_NAME_COUNTS = Counter(ENTITY_TYPE_MAP.values())
SHARED_COLLECTION_ENTITY_TYPES: frozenset[str] = frozenset(
    entity_type
    for entity_type, collection_name in ENTITY_TYPE_MAP.items()
    if COLLECTION_NAME_COUNTS[collection_name] > 1
)

# CellLineSample is modeled as a subtype of Sample and stored in the same
# samples collection, so deduplicate collection names while preserving order.
ENTITY_COLLECTION_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(ENTITY_TYPE_MAP.values())
)


@dataclass
class EntityStore:
    """Store entities grouped by collection and provide CRUD helpers."""

    investigations: dict[str, Entity] = field(default_factory=dict)
    studies: dict[str, Entity] = field(default_factory=dict)
    assays: dict[str, Entity] = field(default_factory=dict)
    lab_processes: dict[str, Entity] = field(default_factory=dict)
    protocols: dict[str, Entity] = field(default_factory=dict)
    samples: dict[str, Entity] = field(default_factory=dict)
    molecular_entities: dict[str, Entity] = field(default_factory=dict)
    people: dict[str, Entity] = field(default_factory=dict)
    organizations: dict[str, Entity] = field(default_factory=dict)
    publications: dict[str, Entity] = field(default_factory=dict)
    defined_terms: dict[str, Entity] = field(default_factory=dict)
    property_values: dict[str, Entity] = field(default_factory=dict)
    files: dict[str, Entity] = field(default_factory=dict)

    def _collection_for_type(self, entity_type: str) -> dict[str, Entity]:
        """Return the entity collection dict for a given entity type."""
        coll_name = ENTITY_TYPE_MAP.get(entity_type)
        if coll_name is None:
            raise ValueError(f"Unknown entity type: {entity_type!r}")
        return getattr(self, coll_name)

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the correct collection based on its type."""
        coll = self._collection_for_type(entity.type)
        coll[entity.entity_id] = entity
        logger.debug("Added entity %s (%s)", entity.entity_id, entity.type)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Look up an entity by entity_id across all collections."""
        for coll_name in ENTITY_COLLECTION_NAMES:
            coll: dict[str, Entity] = getattr(self, coll_name)
            if entity_id in coll:
                return coll[entity_id]
        return None

    def remove_entity(self, entity_id: str) -> bool:
        """Remove an entity by entity_id from whichever collection holds it."""
        for coll_name in ENTITY_COLLECTION_NAMES:
            coll: dict[str, Entity] = getattr(self, coll_name)
            if entity_id in coll:
                del coll[entity_id]
                logger.debug("Removed entity %s", entity_id)
                return True
        return False

    def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        """Return all entities, optionally filtered by type."""
        if entity_type is not None:
            coll = self._collection_for_type(entity_type)
            if entity_type in SHARED_COLLECTION_ENTITY_TYPES:
                # Entity types that share a collection require filtering on the
                # concrete type when one of those types is requested.
                return [
                    entity for entity in coll.values() if entity.type == entity_type
                ]
            return list(coll.values())

        result: list[Entity] = []
        for coll_name in ENTITY_COLLECTION_NAMES:
            coll: dict[str, Entity] = getattr(self, coll_name)
            result.extend(coll.values())
        return result

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Serialize entity collections to a JSON-compatible dictionary."""
        return {
            coll_name: [e.to_dict() for e in getattr(self, coll_name).values()]
            for coll_name in ENTITY_COLLECTION_NAMES
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityStore:
        """Deserialize an EntityStore from a dictionary."""

        def _entities_from_list(items: list[dict[str, Any]]) -> dict[str, Entity]:
            return {item["entity_id"]: Entity.from_dict(item) for item in items}

        return cls(
            **{
                coll_name: _entities_from_list(data.get(coll_name, []))
                for coll_name in ENTITY_COLLECTION_NAMES
            }
        )


# ===================================================================
# CrateState - the top-level state container
# ===================================================================


@dataclass
class CrateState:
    """The single source of truth for the ISA-Tox RO-Crate Builder.

    CrateState tracks all entities, scanned files, validation results,
    assessment scores, and agent reasoning. It is serializable to/from
    JSON for session persistence and resume.

    Entity collections live in an EntityStore keyed by entity_id for fast
    lookup. The add_entity / get_entity / remove_entity / list_entities
    methods provide the primary API for entity management.
    """

    session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: CrateMetadata = field(default_factory=CrateMetadata)
    entities: EntityStore = field(default_factory=EntityStore)

    scanned_files: list[FileClassification] = field(default_factory=list)
    approved_scan_roots: set[str] = field(default_factory=set)
    validation: ValidationReport = field(default_factory=ValidationReport)
    mit_assessment: MITReport = field(default_factory=MITReport)
    fair_assessment: FAIRReport = field(default_factory=FAIRReport)
    checkpoint: ReasoningLog = field(default_factory=ReasoningLog)

    # ------------------------------------------------------------------
    # Entity management
    # ------------------------------------------------------------------

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the correct collection based on its type."""
        self.entities.add_entity(entity)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Look up an entity by entity_id across all collections."""
        return self.entities.get_entity(entity_id)

    def remove_entity(self, entity_id: str) -> bool:
        """Remove an entity by entity_id from whichever collection holds it."""
        return self.entities.remove_entity(entity_id)

    def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        """Return all entities, optionally filtered by type."""
        return self.entities.list_entities(entity_type=entity_type)
    # ------------------------------------------------------------------
    # Completion & reasoning helpers
    # ------------------------------------------------------------------

    def _update_completion(
        self,
        entity_id: str,
        field: str,
        status: CompletionStatus,
        source: CompletionSource,
    ) -> None:
        """Update the completion status for a single field on an entity."""
        entity = self.get_entity(entity_id)
        if entity is None:
            raise ValueError(f"Entity not found: {entity_id}")
        entity.set_field_status(field, status, source)

    def log_reasoning(self, action: str, tool: str, result: str) -> ReasoningStep:
        """Append a reasoning step to the checkpoint log."""
        return self.checkpoint.log_reasoning(action, tool, result)

    def mark_stuck(self, reason: str) -> ReasoningStep:
        """Mark the current state as stuck and record the reason."""
        return self.checkpoint.mark_stuck(reason)

    def is_stuck(self) -> bool:
        """Return whether the state is stuck or has exhausted iterations."""
        return self.checkpoint.is_stuck()

    @property
    def iteration_count(self) -> int:
        """Return the delegated iteration count for existing callers."""
        return self.checkpoint.iteration_count

    @iteration_count.setter
    def iteration_count(self, value: int) -> None:
        self.checkpoint.iteration_count = value

    @property
    def max_iterations(self) -> int:
        """Return the delegated maximum iteration count for existing callers."""
        return self.checkpoint.max_iterations

    @max_iterations.setter
    def max_iterations(self, value: int) -> None:
        self.checkpoint.max_iterations = value

    @property
    def stuck(self) -> bool:
        """Return the delegated stuck flag for existing callers."""
        return self.checkpoint.stuck

    @stuck.setter
    def stuck(self, value: bool) -> None:
        self.checkpoint.stuck = value
    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize this CrateState to a JSON-compatible dictionary."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata.to_dict(),
            "entities": self.entities.to_dict(),
            "approved_scan_roots": list(self.approved_scan_roots),
            "scanned_files": [f.to_dict() for f in self.scanned_files],
            "validation": self.validation.to_dict(),
            "mit_assessment": self.mit_assessment.to_dict(),
            "fair_assessment": self.fair_assessment.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "iteration_count": self.iteration_count,
            "max_iterations": self.max_iterations,
            "stuck": self.stuck,
        }

    def to_json(self) -> str:
        """Serialize this CrateState to a JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrateState:
        """Deserialize a CrateState from a dictionary."""
        checkpoint_data = data.get("checkpoint", {})
        checkpoint = ReasoningLog.from_dict(checkpoint_data)

        def _get_reasoning_field_with_fallback(
            field_name: str, default: Any
        ) -> Any:
            if field_name in checkpoint_data:
                return checkpoint_data[field_name]
            return data.get(field_name, default)

        checkpoint.iteration_count = _get_reasoning_field_with_fallback(
            "iteration_count", checkpoint.iteration_count
        )
        checkpoint.max_iterations = _get_reasoning_field_with_fallback(
            "max_iterations", checkpoint.max_iterations
        )
        checkpoint.stuck = _get_reasoning_field_with_fallback(
            "stuck", checkpoint.stuck
        )
        return cls(
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            metadata=CrateMetadata.from_dict(data.get("metadata", {})),
            entities=EntityStore.from_dict(data.get("entities", {})),
            approved_scan_roots=set(data.get("approved_scan_roots", [])),
            scanned_files=[
                FileClassification.from_dict(f)
                for f in data.get("scanned_files", [])
            ],
            validation=ValidationReport.from_dict(data.get("validation", {})),
            mit_assessment=MITReport.from_dict(data.get("mit_assessment", {})),
            fair_assessment=FAIRReport.from_dict(data.get("fair_assessment", {})),
            checkpoint=checkpoint,
        )

    @classmethod
    def from_json(cls, data: str) -> CrateState:
        """Deserialize a CrateState from a JSON string."""
        return cls.from_dict(json.loads(data))
