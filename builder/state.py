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
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal

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
        classification: What the file IS — one of
            :data:`~builder.tools.document_discovery.FILE_CLASSES` — stamped
            once, from a real preview, by
            :func:`~builder.tools.document_discovery.classify_scanned_files`
            (#591). ``None`` until then, and on a session saved before it
            existed; :func:`~builder.tools.document_discovery.classification_of`
            derives an answer for those from this record alone.
    """

    path: str
    filename: str
    size: int
    mime_type: str
    first_rows: list[str] | None = None
    reviewed_by_user: bool = False
    classification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "filename": self.filename,
            "size": self.size,
            "mime_type": self.mime_type,
            "first_rows": self.first_rows,
            "reviewed_by_user": self.reviewed_by_user,
            "classification": self.classification,
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
            classification=data.get("classification"),
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


def _app_version() -> str:
    """The running vitro-crate version, for the crate's generator record.

    A default rather than something only ``capture()`` fills in: the version is
    a constant of the running program, and leaving it empty until export made
    ``build_and_validate`` validate a DIFFERENT crate from the one written —
    one whose ``SoftwareApplication`` had no ``version``, which trips a BASE
    check the agent then cannot fix, because the value only ever appears after
    the export it is trying to reach.

    Prefers the installed distribution metadata so a wheel reports its real
    version, falling back to the source ``__version__``.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("vitro-crate")
        except PackageNotFoundError:
            pass
    except Exception:  # pragma: no cover - metadata is optional, never fatal
        logger.debug("distribution version unavailable", exc_info=True)
    try:
        from builder import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - version is cosmetic, never fatal
        return ""


@dataclass
class GeneratorInfo:
    """What produced this crate: the application, and the model(s) it drove.

    A receiving lab cannot judge LLM-assisted metadata without knowing what
    assisted it. This records the tool, its version, and the model behind it so
    the crate says how it was made rather than presenting itself as hand-curated.

    **Secrets never enter this record.** Only the allowlisted fields below are
    captured — never an API key, never a raw ``base_url`` (which can carry a
    token in its path or query), never arbitrary environment. ``api_host`` keeps
    the hostname alone, which is what identifies a deployment; anything else is
    dropped. The crate is a shareable artifact, so a leak here is a leak to
    everyone the crate reaches.

    Attributes:
        name: Application name.
        version: Application version.
        url: Application homepage / source repository.
        provider: The API family driving the run ("openai" / "anthropic" / …).
        model: Effective orchestrator model name.
        drafter_model: Effective drafter model, when it differs.
        api_host: HOSTNAME of a custom API base, or None for the vendor default.
        architecture: Which build path ran ("react" / "pipeline").
        settings: Extra allowlisted, stringified run settings.
    """

    name: str = "vitro-crate"
    version: str = field(default_factory=lambda: _app_version())
    url: str = "https://github.com/johannehouweling/vitro-crate"
    provider: str | None = None
    model: str | None = None
    drafter_model: str | None = None
    api_host: str | None = None
    architecture: str | None = None
    settings: dict[str, str] = field(default_factory=dict)
    # Run cost/effort. Baked into every export so a crate carries the price and
    # wall-clock of producing it — the numbers to optimise against, and the ones
    # nobody can reconstruct after the session is gone.
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    cost_usd: float | None = None
    # Seconds spent INSIDE model calls. ``duration_seconds`` is wall clock from
    # session start to export, so it counts the user reading, thinking, going to
    # lunch — one real session recorded 54,589s (15.2h) for ~30 min of work.
    # This is the machine's effort, and the number worth optimising against.
    model_seconds: float = 0.0

    # Run settings safe to publish. Anything not named here is dropped rather
    # than filtered by pattern — an allowlist cannot be defeated by a new secret
    # whose name nobody thought to blocklist.
    ALLOWED_SETTINGS: ClassVar[frozenset[str]] = frozenset(
        {"temperature", "max_iterations", "seed", "reasoning_effort", "max_history_tokens"}
    )

    @classmethod
    def capture(
        cls,
        *,
        architecture: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> GeneratorInfo:
        """Snapshot the running application and its resolved model configuration."""
        import builder.config as _cfg

        # `str(...)` rather than importing straight into the name: `__version__`
        # is a string LITERAL, so binding the fallback "" to the same name is a
        # type error against its narrowed type.
        app_version: str
        try:
            from builder import __version__

            app_version = str(__version__)
        except Exception:  # pragma: no cover - version is cosmetic, never fatal
            app_version = ""

        def _host(raw: str | None) -> str | None:
            if not raw:
                return None
            host = str(raw).split("://", 1)[-1].split("/", 1)[0]
            return host or None

        safe: dict[str, str] = {}
        for key, value in (settings or {}).items():
            if key in cls.ALLOWED_SETTINGS and value is not None:
                safe[key] = str(value)

        def _quiet(fn: Any) -> Any:
            try:
                return fn()
            except Exception:  # pragma: no cover - config must never fail an export
                return None

        model = _quiet(_cfg.get_active_model)
        drafter = _quiet(_cfg.get_drafter_model)
        return cls(
            version=str(app_version or ""),
            provider=_quiet(_cfg.get_provider),
            model=model,
            drafter_model=drafter if drafter and drafter != model else None,
            api_host=_host(os.environ.get("VITRO_OPENAI_API_BASE")
                           or os.environ.get("OPENAI_API_BASE")),
            architecture=architecture,
            settings=safe,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "version": self.version, "url": self.url}
        for key in (
            "provider", "model", "drafter_model", "api_host", "architecture",
            "started_at", "ended_at",
        ):
            value = getattr(self, key)
            if value:
                d[key] = value
        for key in ("input_tokens", "output_tokens", "llm_calls", "model_seconds"):
            if getattr(self, key):
                d[key] = getattr(self, key)
        if self.duration_seconds is not None:
            d["duration_seconds"] = self.duration_seconds
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        if self.settings:
            d["settings"] = dict(self.settings)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeneratorInfo:
        return cls(
            name=data.get("name", "vitro-crate"),
            # A session saved before the version was recorded (or by an older
            # build) restores as empty; fill it from the running app rather than
            # resuming into a crate that fails BASE for a value we know.
            version=data.get("version") or _app_version(),
            url=data.get("url", "https://github.com/johannehouweling/vitro-crate"),
            provider=data.get("provider"),
            model=data.get("model"),
            drafter_model=data.get("drafter_model"),
            api_host=data.get("api_host"),
            architecture=data.get("architecture"),
            settings={k: str(v) for k, v in (data.get("settings") or {}).items()},
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            duration_seconds=data.get("duration_seconds"),
            input_tokens=int(data.get("input_tokens") or 0),
            model_seconds=float(data.get("model_seconds") or 0.0),
            output_tokens=int(data.get("output_tokens") or 0),
            llm_calls=int(data.get("llm_calls") or 0),
            cost_usd=data.get("cost_usd"),
        )


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
    # Crate-level attribution. Distinct from the publication's authors, which
    # describe the PAPER: these say who is responsible for this dataset. Each
    # holds an entity id (a drafted Person/Organization) or a resolvable IRI
    # (ORCID / ROR). Without them a crate credits nobody a registry can resolve.
    publisher: str | None = None
    creator: str | None = None
    contact: str | None = None
    license: str | None = None
    # True when `license` was READ from the deposit rather than drafted (#535).
    # A depositor's statement is a fact and a drafter's is a guess, so the guess
    # does not get to overwrite it — a wrong licence is wrong in the one
    # direction that suppresses reuse of openly-licensed data.
    license_from_deposit: bool = False
    input_type: InputType = "directory"
    input_path: str | None = None
    output_path: str | None = None
    # When the crate was last written to ``output_path`` (ISO-8601, local tz).
    # A session that has never exported leaves this None — distinguishable from
    # "exported but the path is unknown", which the dashboard reports differently.
    exported_at: str | None = None

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
        for key in ("publisher", "creator", "contact", "license"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        if self.license_from_deposit:
            d["license_from_deposit"] = True
        if self.input_path is not None:
            d["input_path"] = self.input_path
        if self.output_path is not None:
            d["output_path"] = self.output_path
        if self.exported_at is not None:
            d["exported_at"] = self.exported_at
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
            publisher=data.get("publisher"),
            creator=data.get("creator"),
            contact=data.get("contact"),
            license=data.get("license"),
            license_from_deposit=bool(data.get("license_from_deposit", False)),
            input_type=data.get("input_type", "directory"),  # type: ignore[arg-type]
            input_path=data.get("input_path"),
            output_path=data.get("output_path"),
            exported_at=data.get("exported_at"),
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
        issue_records: The same findings with their structure intact — one
            ``{"profile", "severity", "entity_id", "message"}`` dict per finding
            (``profile`` is ``base``/``isa``/``tox``, ``severity`` a tier name,
            ``entity_id`` empty when the producer had none). The three flat
            lists above are the display projection of these records and stay
            byte-stable because the ReAct loop parses them; the records exist so
            renderers (the maturity report's per-profile fold-outs, #510) never
            have to re-parse that display format. Empty on a verdict recorded
            before the field existed — consumers must fall back, not infer.
        assessed_tiers: The tiers whose findings this verdict CURRENTLY answers
            for. A gate is a floor, so a "recommended" sweep marks both
            ``required`` and ``recommended``; a tier the crate has moved on from
            is retired here rather than left describing an older crate.
        stale_tier_counts: How many findings a RETIRED tier held when it was
            last swept. Retirement is about freshness, but the status footer read
            it as ignorance and printed "rec/opt locked" — the very same thing it
            shows for a tier nobody ever ran, so a session that had been
            reporting "4 rec 11 opt" appeared to lock them again after one
            REQUIRED-gated run over an edited crate. Keeping the last count lets
            the footer say what it knew and mark it unverified, instead of
            choosing between claiming ignorance and printing a 0 that reads as
            clean. Not serialized: a resumed session restores its own verdict,
            and this only ever describes findings THIS process computed and then
            retired.
        payload_checked: Whether anything actually looked at the crate's files.
            The in-memory gate validates a document, so checks that need a
            payload — "is every declared Data Entity present?" — emit nothing
            there, and their silence must not be read as a pass (#530). False on
            a verdict that only ever saw the metadata.
        isa_reachability_checked: Whether anything asked which structural
            entities the ISA backbone actually reaches. The profile's own rules
            cannot ask — they target a class that is only minted once the
            reference exists, so a detached entity is skipped rather than
            failed, and the silence reads as a pass (#537). False on a verdict
            that never looked.
        input_fingerprint: :meth:`CrateState.validation_fingerprint` as it was
            when this verdict was recorded — the answer to "does this verdict
            still describe the crate?". Empty means unknown (a report restored
            from an older checkpoint, or one built by hand).
    """

    base_passed: bool = False
    isa_passed: bool = False
    tox_passed: bool = False
    required_issues: list[str] = field(default_factory=list)
    should_issues: list[str] = field(default_factory=list)
    may_issues: list[str] = field(default_factory=list)
    assessed_tiers: set[str] = field(default_factory=set)
    stale_tier_counts: dict[str, int] = field(default_factory=dict, repr=False)
    issue_records: list[dict[str, str]] = field(default_factory=list)
    payload_checked: bool = False
    isa_reachability_checked: bool = False
    input_fingerprint: str = ""

    def is_stale_for(self, state: CrateState) -> bool:
        """True when *state* has changed since this verdict was recorded.

        A verdict with no fingerprint is NOT reported stale: it predates the
        stamp, and downgrading every restored checkpoint to "stale" would be a
        false alarm. Freshness is only ever asserted on a positive match.
        """
        return bool(self.input_fingerprint) and (
            self.input_fingerprint != state.validation_fingerprint()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_passed": self.base_passed,
            "isa_passed": self.isa_passed,
            "tox_passed": self.tox_passed,
            "required_issues": list(self.required_issues),
            "should_issues": list(self.should_issues),
            "may_issues": list(self.may_issues),
            "assessed_tiers": sorted(self.assessed_tiers),
            "issue_records": [dict(r) for r in self.issue_records],
            "payload_checked": self.payload_checked,
            "isa_reachability_checked": self.isa_reachability_checked,
            "input_fingerprint": self.input_fingerprint,
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
            issue_records=data.get("issue_records", []),
            payload_checked=data.get("payload_checked", False),
            isa_reachability_checked=data.get("isa_reachability_checked", False),
            input_fingerprint=data.get("input_fingerprint", ""),
        )


@dataclass
class MITReport:
    """Coverage report from the MIT assessment.

    Attributes:
        module_scores: Per-module scores keyed by module name,
            each containing {"completed": int, "total": int}.
        overall_score: Overall coverage as a fraction (0.0 - 1.0).
        standard_scores: Per-guidance-document scores keyed by the checklist's
            ``standards`` keys (e.g. ``oecd_gd211``), same bucket shape.
            Documents overlap — one parameter can be required by several — so
            buckets do not sum to the checklist total.
        standard_module_scores: Each document's bucket split by module —
            ``{document_key: {module_name: {"completed", "total"}}}``. A
            document's module buckets partition its ``standard_scores`` bucket
            (they sum to it); a module that contributes no parameter to a
            document has no key under it. This is what lets the maturity report
            draw a document's bar as a stack of modules.
    """

    module_scores: dict[str, dict[str, int]] = field(default_factory=dict)
    overall_score: float = 0.0
    standard_scores: dict[str, dict[str, int]] = field(default_factory=dict)
    standard_module_scores: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_scores": dict(self.module_scores),
            "overall_score": self.overall_score,
            "standard_scores": dict(self.standard_scores),
            "standard_module_scores": {
                key: dict(by_module) for key, by_module in self.standard_module_scores.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MITReport:
        return cls(
            module_scores=data.get("module_scores", {}),
            overall_score=data.get("overall_score", 0.0),
            standard_scores=data.get("standard_scores", {}),
            standard_module_scores=data.get("standard_module_scores", {}),
        )


@dataclass
class FAIRReport:
    """Maturity report from the FAIR indicators assessment.

    Attributes:
        indicator_results: List of individual indicator results as dicts.
        dsm_level: FAIRplus Dataset Maturity (DSM) level (0-5).
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
    generator: GeneratorInfo = field(default_factory=GeneratorInfo)
    entities: EntityStore = field(default_factory=EntityStore)

    scanned_files: list[FileClassification] = field(default_factory=list)
    approved_scan_roots: set[str] = field(default_factory=set)

    # Discovered, ranked scientific documentation (SOPs, protocols, publications,
    # metadata files, data dictionaries, etc.) — populated by the engine after
    # file scanning, consumed by both the ReAct brief and the pipeline context.
    # Stored as a list of dicts to keep CrateState JSON-serializable without
    # importing the document_discovery module at load time.
    documents: list[dict[str, Any]] = field(default_factory=list)
    # Bounded content captured from successfully read main documents. This is
    # session evidence, not a general-purpose result cache.
    document_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    validation: ValidationReport = field(default_factory=ValidationReport)
    mit_assessment: MITReport = field(default_factory=MITReport)
    fair_assessment: FAIRReport = field(default_factory=FAIRReport)
    checkpoint: ReasoningLog = field(default_factory=ReasoningLog)

    # Standing answers to "run the broader validation tiers?", keyed
    # ``"recommended"`` / ``"optional"``. Absent means "not asked yet"; a
    # recorded bool means the user has decided and must not be asked again.
    # Persisted deliberately: an answer given before a --resume is still the
    # user's answer afterwards.
    validation_preferences: dict[str, bool] = field(default_factory=dict)

    # What the user has already told us, in order. A HITL answer arrives as a
    # tool result and therefore lives ONLY in the graph checkpoint — so a turn
    # that ends mid-flight (a loop guard, a timeout) rotates the thread and the
    # answer is gone, and the agent asks the same question again. One session
    # asked who owns the dataset three times and was answered twice. State is
    # the durable half of the session, so answers belong here.
    user_answers: list[dict[str, str]] = field(default_factory=list)

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

    # ------------------------------------------------------------------
    # Generator provenance & run cost
    # ------------------------------------------------------------------

    def record_llm_usage(
        self,
        usage: dict[str, Any] | None,
        *,
        calls: int = 1,
        seconds: float = 0.0,
    ) -> None:
        """Accumulate one LLM call's token usage onto the generator record.

        Accepts either naming convention the codebase sees
        (``input_tokens``/``output_tokens`` or ``prompt_tokens``/``completion_tokens``).
        A missing or unparseable usage dict is ignored rather than raising — cost
        accounting must never take down a build.
        """
        if not usage:
            return
        try:
            inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            return
        if not (inp or out):
            return
        self.generator.input_tokens += inp
        self.generator.output_tokens += out
        self.generator.llm_calls += calls
        if seconds > 0:
            self.generator.model_seconds = round(self.generator.model_seconds + seconds, 3)

    def record_user_answer(self, question: str, answer: str, *, limit: int = 20) -> None:
        """Remember something the user told us, durably and in order.

        Bounded to the most recent *limit* exchanges: this is fed back into the
        model's context every turn, so it must stay small, and the recent
        answers are the ones still in play. A repeated question overwrites its
        earlier entry rather than accumulating, so re-asking (which happens) does
        not push the other answers out.
        """
        q = " ".join(str(question).split())[:300]
        a = " ".join(str(answer).split())[:300]
        if not q or not a:
            return
        self.user_answers = [item for item in self.user_answers if item.get("question") != q]
        self.user_answers.append({"question": q, "answer": a})
        del self.user_answers[:-limit]

    def stamp_generator(self, *, architecture: str | None = None) -> GeneratorInfo:
        """Finalise the generator record for export: identity, timing, cost.

        Called by ``export_crate`` so every written crate carries how it was made.
        Preserves already-recorded token counts (the accumulator above) and fills
        in what can only be known at the end: the end time, the elapsed wall-clock
        since ``created_at``, and the money the run cost.

        Never raises: a pricing lookup needs network on first use, and a crate
        must still export when it is unavailable.
        """
        prior = self.generator
        info = GeneratorInfo.capture(
            architecture=architecture or prior.architecture,
            settings={"max_iterations": self.max_iterations}
            if getattr(self, "max_iterations", None)
            else None,
        )
        info.input_tokens = prior.input_tokens
        info.model_seconds = prior.model_seconds
        info.output_tokens = prior.output_tokens
        info.llm_calls = prior.llm_calls
        info.started_at = prior.started_at or self.created_at or ""
        info.ended_at = _config.now().isoformat()
        if info.started_at:
            try:
                from datetime import datetime

                start = datetime.fromisoformat(info.started_at)
                end = datetime.fromisoformat(info.ended_at)
                info.duration_seconds = round((end - start).total_seconds(), 3)
            except (TypeError, ValueError):
                info.duration_seconds = None
        if info.input_tokens or info.output_tokens:
            try:
                from builder.pricing import compute_cost

                priced = compute_cost(
                    info.input_tokens,
                    info.output_tokens,
                    info.model or "",
                    info.provider,
                )
                total = priced.get("total_cost")
                info.cost_usd = round(float(total), 6) if total is not None else None
            except Exception:  # noqa: BLE001 - pricing is best effort, never fatal
                info.cost_usd = None
        self.generator = info
        return info

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
        metadata = self.metadata.to_dict() if hasattr(self.metadata, "to_dict") else None
        if metadata is not None:
            # WHERE the crate was written and WHEN — neither is anything the
            # validator can observe. Two exports of an identical crate to
            # different directories, or at different times, produce the same
            # verdict. Leaving them in made `export_crate` (which sets both) look
            # like a content change, so the recorded verdict read as stale and
            # `ensure_validated` re-ran a full 3-pass SHACL sweep on every export.
            metadata = {
                k: v for k, v in metadata.items() if k not in ("output_path", "exported_at")
            }
        content = {
            "entities": self.entities.to_dict()
            if hasattr(self.entities, "to_dict")
            else str(self.entities),
            "metadata": metadata if metadata is not None else str(self.metadata),
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


def _migrate_documents(documents: list[Any]) -> list[Any]:
    """Read a pre-#591 session's discovered documents under the current key.

    Document discovery labelled each candidate ``role``; it now answers with the
    one file ``classification``. ``--resume`` bypasses ``initialize()``, so
    discovery never re-runs and the saved dicts are what the readers see for the
    rest of the session — and one reader is not cosmetic: the ReAct gap engine
    counts assay folders by the documents classified ``metadata`` or
    ``protocol``, so an old session produced none and the "N assay folders, M
    Assays" item stopped appearing.

    Renamed once here rather than by teaching four readers two spellings. The
    current key wins where a session somehow carries both, and anything that is
    not a dict is passed through: ``documents`` is free-form JSON on disk and a
    hand-edited file must not break the load.
    """
    migrated: list[Any] = []
    for document in documents:
        if isinstance(document, dict) and "role" in document:
            document = {
                k: v for k, v in document.items() if k != "role"
            } | {"classification": document.get("classification") or document["role"]}
        migrated.append(document)
    return migrated


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
            "generator": cls._encode(state.generator),
            "entities": cls._encode(state.entities),
            "approved_scan_roots": list(state.approved_scan_roots),
            "scanned_files": [cls._encode(f) for f in state.scanned_files],
            "documents": state.documents,
            "document_evidence": state.document_evidence,
            "validation": cls._encode(state.validation),
            "mit_assessment": cls._encode(state.mit_assessment),
            "fair_assessment": cls._encode(state.fair_assessment),
            "checkpoint": cls._encode(state.checkpoint),
            "validation_preferences": dict(state.validation_preferences),
            "user_answers": [dict(a) for a in state.user_answers],
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
            generator=GeneratorInfo.from_dict(data.get("generator", {})),
            entities=EntityStore.from_dict(data.get("entities", {})),
            approved_scan_roots=set(data.get("approved_scan_roots", [])),
            scanned_files=[FileClassification.from_dict(f) for f in data.get("scanned_files", [])],
            documents=_migrate_documents(data.get("documents") or []),
            document_evidence=dict(data.get("document_evidence") or {}),
            validation=ValidationReport.from_dict(data.get("validation", {})),
            mit_assessment=MITReport.from_dict(data.get("mit_assessment", {})),
            fair_assessment=FAIRReport.from_dict(data.get("fair_assessment", {})),
            checkpoint=checkpoint,
            # Read back, not just written: without this the standing "don't ask
            # me again" answers reset to {} on every --resume, which is the one
            # thing persisting them was meant to prevent. Coerced so a
            # hand-edited session file cannot inject non-bool values.
            validation_preferences={
                str(k): bool(v) for k, v in (data.get("validation_preferences") or {}).items()
            },
            user_answers=[
                {"question": str(a.get("question", "")), "answer": str(a.get("answer", ""))}
                for a in (data.get("user_answers") or [])
                if isinstance(a, dict)
            ],
        )

    @classmethod
    def from_json(cls, data: str) -> CrateState:
        """Deserialize a CrateState from a JSON string."""
        return cls.from_dict(json.loads(data))
