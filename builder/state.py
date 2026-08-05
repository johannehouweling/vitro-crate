"""
CrateState — The Central Data Model for the ISA-Tox RO-Crate Builder.

CrateState is the single source of truth for the builder. It is serializable
to JSON and persists to disk for session resume. All entities, validation
results, assessment scores, and agent reasoning are tracked in this state.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import builder.config as _config

logger = logging.getLogger(__name__)


def _default_max_iterations() -> int:
    """Return the default max iterations from config (env / config file / built-in)."""
    from builder.config import get_max_iterations

    return get_max_iterations()


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
    # AOP-Wiki subgraph contextual entities (Issue #180). MIE/KE/AO all share
    # @type KeyEvent and are discriminated only by their eventType string.
    "AdverseOutcomePathway",
    "KeyEvent",
    "KeyEventRelationship",
]

CompletionStatus = Literal["missing", "filled", "verified"]
CompletionSource = Literal["scanner", "llm", "user", "lookup"]
InputType = Literal["directory", "conversation"]

# Internal @id / @type handles that live on the Entity itself (the ``entity_id``
# and ``type`` attributes), NOT as schema.org properties. They must never be
# stored in ``Entity.fields``: a field named ``entity_id`` / ``@id`` / ``type`` /
# ``@type`` serializes as a bare JSON-LD key absent from the RO-Crate @context
# and fails base conformance (Issue #286). ``set_fields_from_dict`` drops them.
_RESERVED_INTERNAL_KEYS: frozenset[str] = frozenset({"entity_id", "@id", "type", "@type"})


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
        """Set multiple fields at once, marking each as filled.

        Reserved internal handles (``entity_id`` / ``@id`` / ``type`` /
        ``@type``) are silently dropped rather than stored as fields: they are
        the entity's own ``entity_id`` / ``type`` attributes, not schema.org
        properties, and would otherwise serialize as bare JSON-LD keys absent
        from the RO-Crate @context, failing base validation (Issue #286).
        """
        for field_name, value in values.items():
            if field_name in _RESERVED_INTERNAL_KEYS:
                logger.debug(
                    "Dropping reserved internal key %r from fields of entity %s; "
                    "it is an @id/@type handle, not a property.",
                    field_name,
                    self.entity_id,
                )
                continue
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
            k: FieldCompletion.from_dict(v) for k, v in data.get("_completion", {}).items()
        }
        provenance = EntityProvenance.from_dict(data.get("_provenance", {"created_by": "llm"}))
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
        release_date: ISO-8601 date the crate/dataset was released
            (schema:releaseDate on the Root Data Entity). ``None`` when unset —
            ro-crate-py still auto-sets ``datePublished`` independently.
        date_modified: ISO-8601 date/datetime the crate was last modified
            (schema:dateModified on the Root Data Entity). ``None`` when unset.
        input_type: Whether input was a directory or conversation.
        input_path: Path to the input directory (if applicable).
        output_path: Path where the crate will be written.
    """

    title: str | None = None
    description: str | None = None
    accession: str | None = None
    release_date: str | None = None
    date_modified: str | None = None
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
        if self.release_date is not None:
            d["release_date"] = self.release_date
        if self.date_modified is not None:
            d["date_modified"] = self.date_modified
        if self.input_path is not None:
            d["input_path"] = self.input_path
        if self.output_path is not None:
            d["output_path"] = self.output_path
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrateMetadata:
        # ``release_date`` / ``date_modified`` default to None so sessions saved
        # before these fields existed still load (back-compat, #180).
        return cls(
            title=data.get("title"),
            description=data.get("description"),
            accession=data.get("accession"),
            release_date=data.get("release_date"),
            date_modified=data.get("date_modified"),
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
    assessed_tiers: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_passed": self.base_passed,
            "isa_passed": self.isa_passed,
            "tox_passed": self.tox_passed,
            "required_issues": list(self.required_issues),
            "should_issues": list(self.should_issues),
            "may_issues": list(self.may_issues),
            "assessed_tiers": sorted(self.assessed_tiers),
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
            assessed_tiers=set(data.get("assessed_tiers", [])),
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
    max_iterations: int = field(default_factory=_default_max_iterations)
    stuck: bool = False

    def log_reasoning(self, action: str, tool: str, result: str) -> ReasoningStep:
        """Append a reasoning step to the log and return it."""
        step = len(self.reasoning_log) + 1
        ts = _config.now().isoformat()
        entry = ReasoningStep(step=step, action=action, tool=tool, result=result, timestamp=ts)
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
        reason_log = [ReasoningStep.from_dict(s) for s in data.get("reasoning_log", [])]
        return cls(
            next_actions=data.get("next_actions", []),
            completed_checkpoints=data.get("completed_checkpoints", []),
            reasoning_log=reason_log,
            iteration_count=data.get("iteration_count", 0),
            max_iterations=data.get("max_iterations", _default_max_iterations()),
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
    # The AOP-Wiki subgraph (AOP + KeyEvent + KeyEventRelationship) shares one
    # collection; all three are AOP contextual entities (Issue #180).
    "AdverseOutcomePathway": "aop_entities",
    "KeyEvent": "aop_entities",
    "KeyEventRelationship": "aop_entities",
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
ENTITY_COLLECTION_NAMES: tuple[str, ...] = tuple(dict.fromkeys(ENTITY_TYPE_MAP.values()))

# Collection names that are shared by multiple entity types (e.g. "samples"
# holds both Sample and CellLineSample) — used by EntityStore to do
# type-qualified key lookups when the bare entity_id is not found (Issue #57).
_SHARED_COLLECTION_NAMES: frozenset[str] = frozenset(
    coll_name for coll_name, count in COLLECTION_NAME_COUNTS.items() if count > 1
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
    aop_entities: dict[str, Entity] = field(default_factory=dict)

    def _collection_for_type(self, entity_type: str) -> dict[str, Entity]:
        """Return the entity collection dict for a given entity type."""
        coll_name = ENTITY_TYPE_MAP.get(entity_type)
        if coll_name is None:
            raise ValueError(f"Unknown entity type: {entity_type!r}")
        return getattr(self, coll_name)

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the correct collection based on its type.

        For collections shared by multiple entity types (e.g. ``samples``
        holds both ``Sample`` and ``CellLineSample``), the storage key is
        type-qualified (``{type}:{entity_id}``) to prevent silent overwrite
        when two types happen to share an ``entity_id`` (Issue #57).
        """
        coll = self._collection_for_type(entity.type)
        if entity.type in SHARED_COLLECTION_ENTITY_TYPES:
            coll[f"{entity.type}:{entity.entity_id}"] = entity
        else:
            coll[entity.entity_id] = entity
        logger.debug("Added entity %s (%s)", entity.entity_id, entity.type)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Look up an entity by entity_id across all collections.

        For shared collections (e.g. ``samples`` holds both ``Sample`` and
        ``CellLineSample``), also checks type-qualified storage keys
        (``{type}:{entity_id}``) — see Issue #57.
        """
        for coll_name in ENTITY_COLLECTION_NAMES:
            coll: dict[str, Entity] = getattr(self, coll_name)
            if entity_id in coll:
                return coll[entity_id]
            # For shared collections the key may be type-qualified
            if coll_name in _SHARED_COLLECTION_NAMES:
                for key, entity in coll.items():
                    if key.endswith(f":{entity_id}"):
                        return entity
        return None

    def remove_entity(self, entity_id: str) -> bool:
        """Remove an entity by entity_id from whichever collection holds it."""
        for coll_name in ENTITY_COLLECTION_NAMES:
            coll: dict[str, Entity] = getattr(self, coll_name)
            if entity_id in coll:
                del coll[entity_id]
                logger.debug("Removed entity %s", entity_id)
                return True
            if coll_name in _SHARED_COLLECTION_NAMES:
                matching = [k for k in coll if k.endswith(f":{entity_id}")]
                if matching:
                    for k in matching:
                        del coll[k]
                    logger.debug("Removed entity %s (%d keys)", entity_id, len(matching))
                    return True
        return False

    def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        """Return all entities, optionally filtered by type."""
        if entity_type is not None:
            coll = self._collection_for_type(entity_type)
            if entity_type in SHARED_COLLECTION_ENTITY_TYPES:
                # Entity types that share a collection require filtering on the
                # concrete type when one of those types is requested.
                return [entity for entity in coll.values() if entity.type == entity_type]
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
        """Deserialize an EntityStore from a dictionary.

        For shared collections, uses type-qualified keys (``{type}:{entity_id}``)
        to match the format ``add_entity`` uses — see Issue #57.
        """

        def _entities_from_list(items: list[dict[str, Any]], is_shared: bool) -> dict[str, Entity]:
            if is_shared:
                return {
                    f"{item['type']}:{item['entity_id']}": Entity.from_dict(item) for item in items
                }
            return {item["entity_id"]: Entity.from_dict(item) for item in items}

        return cls(
            **{
                coll_name: _entities_from_list(
                    data.get(coll_name, []),
                    is_shared=(coll_name in _SHARED_COLLECTION_NAMES),
                )
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

    # Discovered, ranked scientific documentation (SOPs, protocols, publications,
    # metadata files, data dictionaries, etc.) — populated by the engine after
    # file scanning, consumed by both the ReAct brief and the pipeline context.
    # Stored as a list of dicts to keep CrateState JSON-serializable without
    # importing the document_discovery module at load time.
    documents: list[dict[str, Any]] = field(default_factory=list)

    validation: ValidationReport = field(default_factory=ValidationReport)
    mit_assessment: MITReport = field(default_factory=MITReport)
    fair_assessment: FAIRReport = field(default_factory=FAIRReport)
    checkpoint: ReasoningLog = field(default_factory=ReasoningLog)

    # ------------------------------------------------------------------
    # Entity management
    # ------------------------------------------------------------------

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the correct collection based on its type.

        Warns (#366) when a bare ``entity_id`` is shared across DIFFERENT entity types.
        RO-Crate 1.2 discourages two conceptually-different entities sharing an
        identifier (§Contextual entities); the mapper still de-collides via
        type-qualified @ids (#57) so the crate stays @id-unique, but the collision
        usually signals mis-modelling — e.g. one cell-line sample expressed as a
        separate ``Sample`` + ``CellLineSample`` rather than a single ``CellLineSample``
        (which already IS a ``bioschemas:Sample`` discriminated by ``additionalType
        "CellLine"``; see AGENTS.md D16).
        """
        existing = self.entities.get_entity(entity.entity_id)
        if existing is not None and existing.type != entity.type:
            logger.warning(
                "entity_id %r is shared across types (%s + %s). RO-Crate 1.2 discourages "
                "two conceptually-different entities sharing an identifier; if these are "
                "the same thing, model it as ONE entity (a CellLineSample already is a "
                "Sample). See #366.",
                entity.entity_id,
                existing.type,
                entity.type,
            )
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
    # Content fingerprints (change detection for caching / export gating)
    # ------------------------------------------------------------------

    def validation_fingerprint(self) -> str:
        """Hash of what ``build_and_validate`` consumes: entities + metadata.

        Deliberately EXCLUDES ``validation`` / assessments / checkpoint. Those
        are *outputs* the #153 write-back mutates after every validation, so
        including them would change the hash on every call and defeat the #155
        debounce. ``assemble_crate`` reads only entities + metadata, so this is a
        safe superset of the validation inputs: a change to anything the
        validator could observe busts the cache, while a change to a pure output
        (a verdict, a score) does not.
        """
        content = {
            "entities": self.entities.to_dict()
            if hasattr(self.entities, "to_dict")
            else str(self.entities),
            "metadata": self.metadata.to_dict()
            if hasattr(self.metadata, "to_dict")
            else str(self.metadata),
        }
        return hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def export_fingerprint(self) -> str:
        """Hash of what ``export_crate`` writes: the validation inputs + payload.

        Strictly wider than :meth:`validation_fingerprint` because the exporter
        packages every scanned file that has not been drafted as a ``File``
        entity (``assemble_crate(..., include_all_scanned=True)``), which the
        validation path never sees (it passes ``include_all_scanned=False``). A
        second ``scan_files`` therefore changes the exported payload without
        changing the validation hash, so the scan inventory is folded in here and
        deliberately kept out of the narrower hash (#380).

        Used to gate re-export: an entity COUNT is invariant under every
        field-level mutation the toolbox exposes (``set_fields``,
        ``set_crate_metadata``, ``fix_required_issues``, ``link``), so counting
        entities silently dropped all of that work from the crate on disk.
        """
        content = {
            "validation": self.validation_fingerprint(),
            "scanned": sorted(fc.path for fc in self.scanned_files),
        }
        return hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

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
        return StateSerializer.to_dict(self)

    def to_json(self) -> str:
        """Serialize this CrateState to a JSON string."""
        return StateSerializer.to_json(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrateState:
        """Deserialize a CrateState from a dictionary."""
        return StateSerializer.from_dict(data)

    @classmethod
    def from_json(cls, data: str) -> CrateState:
        """Deserialize a CrateState from a JSON string."""
        return StateSerializer.from_json(data)


# ===================================================================
# StateSerializer - JSON (de)serialization for CrateState
# ===================================================================


class StateSerializer:
    """(De)serialize :class:`CrateState` to/from JSON-compatible structures.

    Serialization of component values is driven by a type registry: any value
    whose ``type()`` is registered is encoded by the registered callable;
    otherwise a value's own ``to_dict()`` is used. New component types can be
    supported by calling :meth:`register_serializer` rather than editing this
    class. ``CrateState``'s ``to_dict``/``from_dict``/``to_json``/``from_json``
    delegate here, so the extraction is transparent to existing callers.
    """

    _encoders: dict[type, Callable[[Any], Any]] = {}

    @classmethod
    def register_serializer(cls, type_: type, fn: Callable[[Any], Any]) -> None:
        """Register ``fn`` to encode instances of ``type_`` into JSON data."""
        cls._encoders[type_] = fn

    @classmethod
    def _encode(cls, value: Any) -> Any:
        """Encode a component value via the registry, else its ``to_dict()``."""
        encoder = cls._encoders.get(type(value))
        if encoder is not None:
            return encoder(value)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return value

    @classmethod
    def to_dict(cls, state: CrateState) -> dict[str, Any]:
        """Serialize a CrateState to a JSON-compatible dictionary."""
        return {
            "session_id": state.session_id,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "metadata": cls._encode(state.metadata),
            "entities": cls._encode(state.entities),
            "approved_scan_roots": list(state.approved_scan_roots),
            "scanned_files": [cls._encode(f) for f in state.scanned_files],
            "documents": state.documents,
            "validation": cls._encode(state.validation),
            "mit_assessment": cls._encode(state.mit_assessment),
            "fair_assessment": cls._encode(state.fair_assessment),
            "checkpoint": cls._encode(state.checkpoint),
            "iteration_count": state.iteration_count,
            "max_iterations": state.max_iterations,
            "stuck": state.stuck,
        }

    @classmethod
    def to_json(cls, state: CrateState) -> str:
        """Serialize a CrateState to a JSON string."""
        return json.dumps(cls.to_dict(state), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrateState:
        """Deserialize a CrateState from a dictionary."""
        checkpoint_data = data.get("checkpoint", {})
        checkpoint = ReasoningLog.from_dict(checkpoint_data)

        def _reasoning_field_with_fallback(field_name: str, default: Any) -> Any:
            """Prefer the value nested in checkpoint, else the legacy top-level."""
            if field_name in checkpoint_data:
                return checkpoint_data[field_name]
            return data.get(field_name, default)

        checkpoint.iteration_count = _reasoning_field_with_fallback(
            "iteration_count", checkpoint.iteration_count
        )
        checkpoint.max_iterations = _reasoning_field_with_fallback(
            "max_iterations", checkpoint.max_iterations
        )
        checkpoint.stuck = _reasoning_field_with_fallback("stuck", checkpoint.stuck)

        return CrateState(
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            metadata=CrateMetadata.from_dict(data.get("metadata", {})),
            entities=EntityStore.from_dict(data.get("entities", {})),
            approved_scan_roots=set(data.get("approved_scan_roots", [])),
            scanned_files=[FileClassification.from_dict(f) for f in data.get("scanned_files", [])],
            documents=list(data.get("documents", [])),
            validation=ValidationReport.from_dict(data.get("validation", {})),
            mit_assessment=MITReport.from_dict(data.get("mit_assessment", {})),
            fair_assessment=FAIRReport.from_dict(data.get("fair_assessment", {})),
            checkpoint=checkpoint,
        )

    @classmethod
    def from_json(cls, data: str) -> CrateState:
        """Deserialize a CrateState from a JSON string."""
        return cls.from_dict(json.loads(data))
