"""
Thin wrapper around rocrate_validator that runs the three-pass ISA-Tox
validation and returns structured, plain-English results.

Passes:
  1. Base RO-Crate 1.2   -> bundled ``ro-crate`` profile
  2. ISA RO-Crate        -> bundled ``isa-ro-crate`` profile (roc-validator >=0.10)
  3. ISA-Tox RO-Crate    -> our ``tox-ro-crate`` profile, loaded from SHAPES_DIR
                            via ``extra_profiles_path`` and composed on top of the
                            bundled ``isa-ro-crate`` it ``isProfileOf``.

The tox pass passes ``profiles_path=DEFAULT_PROFILES_PATH`` (the bundled dir)
*and* ``extra_profiles_path=SHAPES_DIR`` so the inheritance chain
tox-ro-crate -> isa-ro-crate -> ro-crate resolves; ``extra_profiles_path``
composition is verified working as of roc-validator 0.10.0.
"""

from __future__ import annotations

import contextlib
import importlib.metadata as _metadata
import json
import logging
import multiprocessing
import os

# ---------------------------------------------------------------------------
# rocrate-validator compatibilty shim  (#57)
# ---------------------------------------------------------------------------
# The upstream get_config_path() resolves ../../pyproject.toml from the
# rocrate_validator/utils/ directory, which in a pip/uv install lands at
# site-packages/pyproject.toml (some other package's config).  That file
# has no [tool.poetry] key, so get_version() raises KeyError on import.
#
# We paper this over by seeding sys.modules with a patched versioning
# module *before* any rocrate_validator import triggers __init__.py's
# ``__version__ = get_version()`` call.  The patched get_version returns
# the version string from the package's own dist-info/METADATA so the
# [tool][poetry] lookup is never reached.
import sys as _sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_rv_ver = _metadata.version("roc-validator")


class _PatchedVersioning:
    """Drop-in stand-in for rocrate_validator.utils.versioning."""

    @staticmethod
    def get_version() -> str:
        return _rv_ver  # noqa: F821 — defined above, used before `del` on line 85

    @staticmethod
    def get_min_python_version():
        return (3, 9)

    @staticmethod
    def check_python_version() -> bool:
        return _sys.version_info >= (3, 9)  # noqa: F821 — same as above


# Pre-seed so rocrate_validator/__init__.py finds this instead of the real
# versioning module and never reaches the broken get_config() call.
_sys.modules["rocrate_validator.utils.versioning"] = _PatchedVersioning  # ty: ignore[invalid-assignment]

import rocrate_validator.utils.config as _rv_config  # noqa: E402 — intentional bootstrap
import rocrate_validator.utils.versioning as _rv_versioning  # noqa: E402

# Now that the package is loaded, patch get_config too so any later
# downstream call to get_version() (e.g. min-python-version checks) works.
# The real versioning module is now importable (it was deferred), so we
# read the dist-info version and inject it into the config cache.
_orig_get_config = _rv_config.get_config


def _patched_get_config() -> dict:
    cfg = _orig_get_config()
    if "tool" in cfg and "poetry" not in cfg["tool"]:
        cfg.setdefault("tool", {})["poetry"] = {"version": _rv_ver}  # noqa: F821
    return cfg


_rv_config.get_config = _patched_get_config  # ty: ignore[invalid-assignment]
_rv_config._config = None
# Restore the real versioning module for correct semantics downstream.
_sys.modules["rocrate_validator.utils.versioning"] = _rv_versioning

del _rv_config, _rv_versioning, _rv_ver, _sys, _metadata, _PatchedVersioning

from rocrate_validator import models, services  # noqa: E402 — see bootstrap above
from rocrate_validator.services import DEFAULT_PROFILES_PATH  # noqa: E402

# This file lives at <repo>/profiles/validator.py, so the repo root is two
# parents up and the SHACL shapes are the sibling ``shapes`` directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
SHAPES_DIR = Path(__file__).resolve().parent / "shapes"

logger = logging.getLogger(__name__)


def _patch_bundled_isa_ontology() -> None:
    """Work around an upstream syntax bug in roc-validator 0.10.0.

    The bundled ``isa-ro-crate/ontology.ttl`` is missing the terminating ``.``
    after ``isa-ro-crate:Data … rdfs:label "Data"@en``, which makes rdflib raise
    a ``BadSyntax`` when the ISA-Tox pass loads the inherited ISA ontology graph.
    We never hit it before because we used to ship our own ISA shapes; consuming
    the bundled profile exposes it. This idempotently inserts the missing period
    so a fresh ``uv sync`` / reinstall self-heals on first validate. Reported
    upstream: https://github.com/crs4/rocrate-validator/issues
    """
    try:
        onto = Path(DEFAULT_PROFILES_PATH) / "isa-ro-crate" / "ontology.ttl"
        text = onto.read_text(encoding="utf-8")
        bad = 'rdfs:label "Data"@en'
        if bad in text and f"{bad} ." not in text:
            onto.write_text(text.replace(bad, f"{bad} .", 1), encoding="utf-8")
    except OSError:
        # Read-only install or missing file: nothing we can do here; validation
        # will surface the underlying parse error if the bug is still present.
        pass


_patch_bundled_isa_ontology()


def _patch_in_memory_descriptor_id() -> None:
    """Skip the working-directory walk on the in-memory (dict) validation path (#115).

    ``validate_crate_dict`` validates a metadata *dict* via
    ``services.validate_metadata_as_dict``, which builds the RO-Crate through
    ``ROCrate.from_metadata_dict``. That factory hardcodes the crate URI to
    ``"./"`` (the current working directory) and dispatches to
    ``ROCrateLocalFolder``. The base-pass check ``ro-crate-1.2`` then resolves the
    metadata-descriptor id via ``ROCrateLocalFolder.metadata_descriptor_id``,
    which does ``base_path.rglob("*ro-crate-metadata.json")`` over that URI — a
    **recursive walk of the entire CWD tree** on every pass, every call.

    On any real run the CWD is a checkout (``.venv``, ``.git``, many git
    worktrees) or a large extracted dataset, so that walk dominated wall-clock:
    profiling pinned it at ~57s of a ~69s three-pass sweep (#115). It is pure
    waste on the dict path — there is *no* crate on disk, and the descriptor id is
    the fixed convention ``ro-crate-metadata.json`` (exactly what the upstream walk
    falls back to when it finds no candidate). So we wrap ``from_metadata_dict`` to
    pre-seed the instance's cached ``_metadata_descriptor_id`` with that canonical
    constant; the property then returns it immediately and never walks the disk.

    Correctness is preserved: the value is identical to the upstream no-candidate
    fallback, and the patch is scoped to ``from_metadata_dict`` — used *only* by
    the in-memory dict path — so the on-disk ``validate_crate`` (which legitimately
    discovers a descriptor in a real crate directory) is untouched. The patch is
    idempotent so repeated imports don't re-wrap it.

    **Delete this when upstream fixes it.** Reported as
    https://github.com/crs4/rocrate-validator/issues/192 — filed against the
    *symptom* (the walk finds nothing, ``get_file_content`` raises
    ``FileNotFoundError``, and the check's ``except Exception`` relabels it as a
    false "not UTF-8 encoded" finding), but the cause is the same walk this patch
    skips. Their fix is ours: in metadata-only mode there is no folder, so the
    descriptor id should be the canonical constant instead of something searched
    for. When a released roc-validator no longer walks the CWD on the dict path,
    this whole function goes — check by removing it and timing
    ``validate_crate_dict`` from a large checkout.
    """
    try:
        from rocrate_validator.rocrate.base import ROCrate as _RVROCrate
        from rocrate_validator.rocrate.metadata import ROCrateMetadata as _RVMetadata
    except ImportError as exc:  # pragma: no cover - validator always present here
        logger.debug("Could not patch in-memory descriptor id: %s", exc)
        return

    original = getattr(_RVROCrate.from_metadata_dict, "__wrapped__", None)
    if original is not None:
        return  # already patched (idempotent)

    _original_from_metadata_dict = _RVROCrate.from_metadata_dict

    def _from_metadata_dict_no_walk(metadata_dict: dict):  # noqa: ANN202
        crate = _original_from_metadata_dict(metadata_dict)
        # Short-circuit the rglob over the CWD: seed the cached descriptor id with
        # the canonical constant (the upstream no-candidate fallback value).
        crate._metadata_descriptor_id = _RVMetadata.METADATA_FILE_DESCRIPTOR  # ty: ignore[unresolved-attribute]
        return crate

    _from_metadata_dict_no_walk.__wrapped__ = _original_from_metadata_dict  # ty: ignore[unresolved-attribute]
    # from_metadata_dict is a @staticmethod; re-wrap to preserve that binding.
    _RVROCrate.from_metadata_dict = staticmethod(_from_metadata_dict_no_walk)  # ty: ignore[invalid-assignment]


_patch_in_memory_descriptor_id()


# ---------------------------------------------------------------------------
# Offline-safe context resolution (#117)
# ---------------------------------------------------------------------------
# The SHACL passes must expand the RO-Crate ``@context`` to build the data graph.
# That context is a *remote* IRI (``https://w3id.org/ro/crate/1.x/context``) that
# rocrate_validator resolves over HTTP. A transient fetch failure turned that into
# spurious REQUIRED base-pass issues (checks ``ro-crate-1.1_2.1`` / ``2.2``) and
# red CI (#116). We ship a pinned local copy of each RO-Crate context and serve it
# from disk, so validation never needs the network to expand the context.

# Directory holding the bundled JSON-LD contexts (committed alongside this file).
CONTEXTS_DIR = Path(__file__).resolve().parent / "contexts"

# Map of well-known RO-Crate context URL -> bundled file. Both http/https and
# trailing-slash variants are registered so the lookup is exact.
_BUNDLED_CONTEXT_FILES: dict[str, str] = {
    "https://w3id.org/ro/crate/1.1/context": "ro-crate-1.1-context.jsonld",
    "https://w3id.org/ro/crate/1.2/context": "ro-crate-1.2-context.jsonld",
}


def _load_local_contexts() -> dict[str, dict]:
    """Parse the bundled context files into a URL -> parsed-JSON map.

    A missing or malformed bundled file is skipped (logged at debug): validation
    then falls back to the network for that URL rather than crashing on import.
    """
    contexts: dict[str, dict] = {}
    for url, filename in _BUNDLED_CONTEXT_FILES.items():
        path = CONTEXTS_DIR / filename
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Bundled RO-Crate context %s unavailable: %s", path, exc)
            continue
        # Register both with and without a trailing slash to match how callers
        # may have written the IRI in ``@context``.
        contexts[url] = parsed
        contexts[url + "/"] = parsed
        if url.startswith("https://"):
            http_url = "http://" + url[len("https://") :]
            contexts[http_url] = parsed
            contexts[http_url + "/"] = parsed
    return contexts


# URL -> parsed context JSON. Patched to {} by tests that need to force the
# network path (verifying transport-error handling).
_LOCAL_CONTEXTS: dict[str, dict] = _load_local_contexts()


def _synthetic_jsonld_response(url: str, payload: dict, status: int = 200):
    """Build a synthetic ``requests.Response`` carrying a JSON-LD ``payload``."""
    import requests

    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/ld+json"
    response._content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    return response


def _local_context_response(url: str):
    """Build a synthetic 200 ``requests.Response`` carrying a bundled context.

    Returns ``None`` when ``url`` is not a bundled RO-Crate context (the caller
    then applies the deny-by-default policy instead of fetching it).
    """
    local = _LOCAL_CONTEXTS.get(url)
    if local is None:
        return None
    return _synthetic_jsonld_response(url, local)


def _blocked_remote_response(url: str):
    """Deny-by-default response for a non-allowlisted remote dereference (#168).

    SSRF guard. During validation the RO-Crate ``@context`` (and any other
    crate-controlled IRI) is dereferenced over HTTP. Only the bundled,
    well-known RO-Crate context URLs in :data:`_LOCAL_CONTEXTS` are served; a
    crafted ``@context`` in an *untrusted* crate pointing at an attacker URL
    (cloud metadata ``169.254.169.254``, internal hosts, a tracker) must **never**
    reach the network. Rather than fetching it, we fail closed and return a
    benign, empty JSON-LD document (a synthetic 200 carrying ``{"@context": {}}``).

    Returning a valid 200 — not raising — is deliberate: rocrate_validator's
    JSON-LD document loader (:func:`_patched_source_to_json`) catches a fetch
    *exception* and falls back to rdflib's own ``urllib`` opener, which would
    perform the very outbound request we are trying to block. Serving an empty
    context keeps the loader on our intercept (no urllib fallback, no network),
    contributes no term mappings (so the crafted context cannot inject IRIs), and
    does not surface as a spurious REQUIRED *content* issue (the empty document is
    well-formed JSON-LD). Genuine transport-error semantics on the *allowlisted*
    URLs are unaffected — those are served from disk and never reach this branch.
    """
    logger.warning(
        "Refusing remote dereference of non-allowlisted URL during validation "
        "(deny-by-default, SSRF guard): %s",
        url,
    )
    return _synthetic_jsonld_response(url, {"@context": {}})


def _install_offline_context_loader() -> None:
    """Serve bundled RO-Crate contexts locally, bypassing the network (#117).

    The RO-Crate ``@context`` (e.g. ``https://w3id.org/ro/crate/1.2/context``) is
    dereferenced over HTTP during the base pass through **two** code paths:

    1. ``rocrate_validator``'s JSON-LD document loader (``_fetch_json_ld``), used
       when rdflib expands the data graph (check ``ro-crate-1.1_2.1``);
    2. the ``FileDescriptorJsonLdFormat`` check (``ro-crate-1.1_2.2``), which calls
       ``HttpRequester().get(context_uri)`` *directly*, bypassing the loader.

    Both funnel through ``HttpRequester`` proxy methods (``.get`` / ``.head``), so
    we intercept there: for a bundled context URL we return a synthetic 200
    response from disk; everything else delegates to the real session. Patching
    the class-level ``__getattr__`` survives the singleton reset rocrate_validator
    performs on every ``ValidationSettings`` construction, and it is idempotent.
    """
    try:
        from rocrate_validator.utils import http as _http
    except ImportError as exc:  # pragma: no cover - validator always present here
        logger.debug("Could not install offline context loader: %s", exc)
        return

    requester_cls = _http.HttpRequester
    if getattr(requester_cls, "_vitro_offline_loader_installed", False):
        return

    # Disable rocrate_validator's best-effort cache warm-up. On a cold cache it
    # fetches every profile-declared artifact (the RO-Crate context *and* the spec
    # HTML page) over the network on each pass — pure network traffic we don't
    # need, since the context is bundled and the spec page is not used by any
    # check. Warm-up failures are swallowed, so disabling it is correctness-safe
    # and keeps validation from touching the wire on a fresh machine (e.g. CI).
    import os

    os.environ.setdefault("ROCRATE_VALIDATOR_AUTO_WARM", "0")

    _original_getattr = requester_cls.__getattr__

    def _offline_getattr(self, name):  # noqa: ANN001
        if name.upper() in {"GET", "HEAD"}:

            def _wrapped(url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                local = _local_context_response(url)
                if local is not None:
                    logger.debug("Serving RO-Crate context %s from bundled copy", url)
                    return local
                # Deny-by-default (#168): a non-allowlisted, crate-controlled URL
                # is refused, never fetched — closing the SSRF/data-egress vector.
                return _blocked_remote_response(url)

            return _wrapped
        return _original_getattr(self, name)

    requester_cls.__getattr__ = _offline_getattr

    # ``fetch_fresh`` is a real method (not proxied through ``__getattr__``); the
    # cache warm-up uses it. Wrap it too so a bundled context is served from disk
    # even if warm-up runs, and a non-allowlisted URL is refused rather than
    # fetched (deny-by-default, #168).
    def _offline_fetch_fresh(self, url, **kwargs):  # noqa: ANN001, ANN003
        local = _local_context_response(url)
        if local is not None:
            logger.debug("Serving RO-Crate context %s from bundled copy (fetch_fresh)", url)
            return local
        return _blocked_remote_response(url)

    requester_cls.fetch_fresh = _offline_fetch_fresh
    requester_cls._vitro_offline_loader_installed = True  # ty: ignore[unresolved-attribute]

    # Also wire the rdflib JSON-LD document loader so context expansion goes
    # through HttpRequester (and therefore our intercept) rather than rdflib's
    # own urllib fetch. Idempotent upstream call.
    try:
        from rocrate_validator.utils.document_loader import install_document_loader

        install_document_loader()
    except Exception as exc:  # noqa: BLE001 - best-effort; loader install is non-fatal
        logger.debug("install_document_loader failed: %s", exc)


_install_offline_context_loader()


class ValidationEngineError(RuntimeError):
    """Raised when a validation pass could not RUN its checks.

    Distinct from :class:`ValidationTransportError` (a real check that could not
    reach the network) and from any content violation: this means the check never
    executed, so the pass says nothing about the crate.

    rocrate_validator wraps every check in a bare ``except Exception``
    (``models/requirement.py``), logs ``"Unexpected error during check"`` at
    WARNING, and carries on. The check contributes NO issue, so a broken engine is
    indistinguishable from a clean crate by looking at the result — an empty issue
    list means "nothing wrong" and "nothing ran" alike.

    That is how a pyshacl upgrade silently emptied every pass: roc-validator
    imports ``ConjunctiveLike`` from ``pyshacl.rdfutil.consts``, which is not part
    of pyshacl's public API and was removed in 0.40.0. Every check raised
    ImportError, every warning was swallowed, and a crate that had never been
    examined came back conformant. Assertions of the form
    ``conformance == {base: True, ...}`` go GREEN on that.

    So the warning is promoted to an exception. A validator that cannot run is a
    broken tool, not a passing crate.
    """


class ValidationTransportError(RuntimeError):
    """Raised when a validation pass fails on a network transport error.

    Distinguishes a *transport* failure (a remote context/ontology IRI could not
    be dereferenced) from a genuine *content* violation. rocrate_validator
    swallows the connection error inside its checks and re-emits it as a REQUIRED
    issue (e.g. ``ro-crate-1.1_2.1`` / ``ro-crate-1.1_2.2``); surfacing it as this
    error instead prevents a transient network blip from masquerading as a real
    REQUIRED content issue (false negative in ``build_and_validate``).
    """


# Substrings that mark an issue as a transport/connection failure rather than a
# real content violation. Kept narrow so genuine violations are never reclassified.
_TRANSPORT_ERROR_MARKERS: tuple[str, ...] = (
    "connection aborted",
    "remotedisconnected",
    "remote end closed connection",
    "connection refused",
    "connection reset",
    "max retries exceeded",
    "failed to establish a new connection",
    "temporary failure in name resolution",
    "name or service not known",
    "unable to retrieve the json-ld context",
    "unable to retrieve json-ld document",
    "newconnectionerror",
    "connectionerror",
    "timed out",
)


def _is_transport_failure_message(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSPORT_ERROR_MARKERS)


@dataclass
class ValidationResult:
    profile: str
    passed: bool  # no issues at ANY severity
    issues: list[str] = field(default_factory=list)  # plain-English, all severities
    required_issues: list[str] = field(default_factory=list)  # REQUIRED-severity only
    passed_required: bool = True  # no REQUIRED-severity issues


# ---------------------------------------------------------------------------
# In-memory (no-disk) validation — routable per-entity feedback (#87)
# ---------------------------------------------------------------------------

# Layer key -> (profile_identifier, extra ValidationSettings kwargs). Mirrors the
# three passes in validate_crate(); the only difference is the document is fed as
# a dict via services.validate_metadata_as_dict instead of read from disk.
_PROFILE_PASSES: dict[str, tuple[str, dict]] = {
    # Base pass targets ro-crate-1.2 (crates emit `conformsTo` 1.2, #91). The isa/tox
    # domain profiles are 1.1-lineage (they extend the upstream isa-ro-crate 1.1
    # profile); RO-Crate 1.2 is backward-compatible, so this is by design, not drift.
    # See profiles/shapes/tox/profile.ttl and tests/test_profile_lineage.py (#361).
    "base": ("ro-crate-1.2", {}),
    "isa": ("isa-ro-crate", {"disable_inherited_profiles_issue_reporting": True}),
    "tox": (
        "tox-ro-crate",
        {
            "profiles_path": DEFAULT_PROFILES_PATH,
            "extra_profiles_path": SHAPES_DIR,
            "disable_inherited_profiles_issue_reporting": True,
        },
    ),
}

# Checks that validate the on-disk metadata FILE itself (e.g. its byte encoding)
# and therefore cannot be evaluated on the in-memory dict path — they false-positive
# on validate_crate_dict where no file exists. Dropped from the dict path only; the
# on-disk validate_crate still enforces them. (ro-crate-1.2_3.1 = descriptor UTF-8.)
_DICT_PATH_NA_CHECKS = frozenset({"ro-crate-1.2_3.1"})

# Severity name <-> roc-validator Severity enum.
_SEVERITY_BY_NAME = {
    "required": models.Severity.REQUIRED,
    "recommended": models.Severity.RECOMMENDED,
    "optional": models.Severity.OPTIONAL,
}
_SEVERITY_NAME = {v: k for k, v in _SEVERITY_BY_NAME.items()}


@lru_cache(maxsize=None)
def tiers_defined(layer: str) -> frozenset[str]:
    """The severity tiers *layer*'s validation pass can actually report at.

    A profile with no checks at a tier can never produce a finding there, so an
    empty result at that tier says nothing about the crate. The bundled
    ``isa-ro-crate`` profile and our tox profile declare no ``sh:Info`` shape at
    all, and their OPTIONAL column therefore came back clean for every crate ever
    built (#620); callers use this to tell "clean" apart from "never checked".

    Read from the validator's own profile registry — the same objects the pass
    runs — rather than from a hand-kept list, so a profile that gains a MAY rule
    is honoured the day it does. Inherited requirements count only where the pass
    reports them: ``isa`` and ``tox`` run with
    ``disable_inherited_profiles_issue_reporting``, so an inherited MAY rule could
    not surface under those layers.

    Args:
        layer: A key of :data:`_PROFILE_PASSES` ("base" | "isa" | "tox").

    Returns:
        The tier names ("required" | "recommended" | "optional") the layer
        defines at least one check at. An unknown layer, or a registry that will
        not load, yields the empty set: every tier then reads as unverified,
        which is the honest answer when we cannot say what was checked.
    """
    pass_config = _PROFILE_PASSES.get(layer)
    if pass_config is None:
        return frozenset()
    identifier, extra = pass_config
    kwargs = {
        key: extra[key] for key in ("profiles_path", "extra_profiles_path") if key in extra
    }
    try:
        profile = services.get_profile(profile_identifier=identifier, **kwargs)
        reporting = [profile]
        if not extra.get("disable_inherited_profiles_issue_reporting"):
            reporting.extend(profile.inherited_profiles)
        return frozenset(
            _SEVERITY_NAME[check.severity]
            for reported in reporting
            for requirement in reported.get_requirements(severity=models.Severity.OPTIONAL)
            for check in requirement.get_checks()
            if check.severity in _SEVERITY_NAME
        )
    except Exception as e:  # noqa: BLE001 — a broken registry must not fail a report
        logger.warning("Could not read the %s profile's requirement levels: %s", layer, e)
        return frozenset()


@dataclass
class RoutableIssue:
    """A SHACL issue keyed to the entity/property that failed.

    Unlike the prose strings in :class:`ValidationResult`, these fields let the
    agent route a fix to a specific graph node and property.
    """

    entity_id: str | None  # focus-node @id (crate-relative, e.g. "./", "#id")
    property: str | None  # failing property IRI (e.g. http://schema.org/name)
    property_value: str | None  # the offending value, when reported
    message: str  # human-readable SHACL message
    severity: str  # "required" | "recommended" | "optional"
    check_id: str | None  # check.identifier, e.g. "ro-crate-1.1_8.1"
    profile: str  # "base" | "isa" | "tox"


@dataclass
class DictValidationResult:
    """Result of one in-memory validation pass over a metadata document."""

    profile: str  # "base" | "isa" | "tox"
    passed: bool  # no issues at the gate severity
    passed_required: bool  # no REQUIRED-severity issues
    issues: list[RoutableIssue] = field(default_factory=list)


def _routable_issue(issue, profile: str) -> RoutableIssue:
    """Adapt a roc-validator CheckIssue to a :class:`RoutableIssue`."""

    def _opt_str(value) -> str | None:
        return str(value) if value is not None else None

    return RoutableIssue(
        entity_id=_opt_str(getattr(issue, "violatingEntity", None)),
        property=_opt_str(getattr(issue, "violatingProperty", None)),
        property_value=_opt_str(getattr(issue, "violatingPropertyValue", None)),
        message=issue.message or str(issue),
        severity=_SEVERITY_NAME.get(issue.severity, issue.severity.name.lower()),
        check_id=getattr(getattr(issue, "check", None), "identifier", None),
        profile=profile,
    )


def validate_crate_dict(
    metadata_doc: dict,
    *,
    severity: str = "required",
    profile: str = "all",
) -> list[DictValidationResult]:
    """Validate an in-memory RO-Crate metadata document — no disk round-trip.

    ``metadata_doc`` is the ``{"@context", "@graph"}`` dict returned by
    ``crate.metadata.generate()`` (ro-crate-py >=0.15 returns a dict, not JSON).
    It is validated directly via ``services.validate_metadata_as_dict``; nothing
    is written or read from disk.

    Args:
        metadata_doc: The RO-Crate metadata document as a dict.
        severity: Gate severity ("required" | "recommended" | "optional"). At
            "required" only REQUIRED-severity checks run (fastest, the inner-loop
            default); lower the gate to also surface recommendations.
        profile: "all" runs the base -> isa -> tox passes; "base"/"isa"/"tox"
            runs a single pass (the tox pass dominates wall-clock, so scoping the
            inner loop is the main speed lever).

    Returns:
        One :class:`DictValidationResult` per pass run, in dependency order.
    """
    if severity not in _SEVERITY_BY_NAME:
        # Fail loudly rather than silently falling back to the strictest gate,
        # which would under-report recommended/optional issues as a false pass.
        raise ValueError(
            f"Unknown severity {severity!r}; expected one of "
            f"{sorted(_SEVERITY_BY_NAME)}."
        )
    gate = _SEVERITY_BY_NAME[severity]
    if profile == "all":
        passes = ["base", "isa", "tox"]
    elif profile in _PROFILE_PASSES:
        passes = [profile]
    else:
        raise ValueError(
            f"Unknown profile {profile!r}; expected one of 'all', 'base', 'isa', 'tox'."
        )

    if len(passes) > 1 and not _serial_only():
        parallel = _validate_passes_parallel(metadata_doc, passes, severity)
        if parallel is not None:
            return parallel
    return [_validate_one_pass(metadata_doc, key, gate) for key in passes]


def _validate_one_pass(
    metadata_doc: dict, key: str, gate: "models.Severity"
) -> DictValidationResult:
    """Run a single profile pass over *metadata_doc*."""
    profile_identifier, extra = _PROFILE_PASSES[key]
    # rocrate_uri is required even on the dict path; its value is ignored when
    # the document is supplied as a dict (base IRI resolves to "./").
    settings = services.ValidationSettings(
        rocrate_uri=".",  # ty: ignore[unknown-argument]
        profile_identifier=profile_identifier,
        requirement_severity=gate,
        **extra,
    )
    with _checks_must_run(key):
        result = services.validate_metadata_as_dict(metadata_doc, settings)
    # Drop file-only checks that can't apply to an in-memory document (see
    # _DICT_PATH_NA_CHECKS), then derive pass/fail from the filtered issues.
    issues = [
        ri
        for i in result.get_issues()
        if (ri := _routable_issue(i, key)).check_id not in _DICT_PATH_NA_CHECKS
    ]
    _raise_on_transport_failure(issues, profile=key)
    return DictValidationResult(
        profile=key,
        passed=not issues,
        passed_required=not any(i.severity == "required" for i in issues),
        issues=issues,
    )


def _validate_pass_worker(payload: tuple[dict, str, str]) -> DictValidationResult:
    """Pool entry point — module-level so it is picklable."""
    metadata_doc, key, severity = payload
    return _validate_one_pass(metadata_doc, key, _SEVERITY_BY_NAME[severity])


def _serial_only() -> bool:
    """Whether to force the serial path (``VITRO_VALIDATE_SERIAL=1``)."""
    return os.environ.get("VITRO_VALIDATE_SERIAL", "").strip().lower() in {"1", "true", "yes"}


def _validate_passes_parallel(
    metadata_doc: dict, passes: list[str], severity: str
) -> list[DictValidationResult] | None:
    """Run the passes concurrently, or return None to fall back to serial.

    The passes are independent — each validates the same document against a
    different profile and nothing flows between them — but they are the dominant
    cost of the agent's loop, and running them one after another means waiting
    out their sum rather than their maximum.

    Processes, not threads: the work is pyshacl/rdflib SPARQL evaluation, which
    is pure Python and holds the GIL, so threads would serialise anyway.

    ``spawn``, not ``fork``: the loop invokes the model under a timeout wrapper,
    so this can be called from a process that already has threads running, and
    forking there risks inheriting a held lock and deadlocking in the child. A
    fresh interpreter cannot.

    Spawn is not free — each worker re-imports rdflib and pyshacl — so the win is
    real but well short of 3x, and it grows with the crate because the fixed
    startup cost amortises. Measured over the three-pass ``all`` profile, pinned
    to 2 cores (the CI runner's shape), serial vs parallel wall-clock:

        8 nodes    9.0s -> 8.2s   (1.10x)
        209 nodes 13.3s -> 8.9s   (1.49x)
        809 nodes 24.5s -> 18.5s  (1.32x)

    Two cores is the pessimistic case and still wins, because a worker's import
    overlaps the others' SPARQL evaluation rather than competing with it.

    Returns None on any failure to start or run the pool so the caller simply
    does the work serially. A validation error raised INSIDE a pass (a transport
    failure) is a real verdict and propagates.
    """
    from concurrent.futures import ProcessPoolExecutor

    try:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=len(passes), mp_context=context) as pool:
            return list(
                pool.map(_validate_pass_worker, [(metadata_doc, key, severity) for key in passes])
            )
    except ValidationTransportError:
        raise
    except Exception as exc:  # noqa: BLE001 — a speedup must never break validation
        logger.warning(
            "Parallel validation unavailable (%s: %s); running the passes serially",
            type(exc).__name__,
            exc,
        )
        return None


_UNRUNNABLE_CHECK_MARKER = "Unexpected error during check"


@contextlib.contextmanager
def _checks_must_run(profile: str):
    """Turn rocrate_validator's swallowed check errors into a raised failure.

    Listens on the ``rocrate_validator`` logger for the warning it emits when a
    check raises, because that warning is the ONLY evidence: the check adds no
    issue, so the result cannot be inspected for it (see
    :class:`ValidationEngineError`).

    Scoped to one pass and restored in a ``finally`` so a raise inside the pass
    cannot leave the handler attached. The handler only records; the decision is
    taken here, after the pass, so a single flaky check does not abort a run
    mid-way through with the engine half-used.
    """
    records: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 — a broken record must not break validation
                return
            if record.name.startswith("rocrate_validator") and _UNRUNNABLE_CHECK_MARKER in message:
                records.append(message)

    # Attached to the ROOT logger, not to "rocrate_validator": a handler on a
    # named logger is only reached by propagation, and pytest's logging plugin
    # manipulates that path — the listener saw nothing under pytest while the
    # record was demonstrably emitted. Root is where every propagated record
    # arrives regardless, so the name is matched on the record instead.
    root = logging.getLogger()
    handler = _Collector(level=logging.WARNING)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)

    if records:
        raise ValidationEngineError(
            f"Validation pass {profile!r} could not run {len(records)} check(s); "
            f"its result describes nothing. First failure: {records[0]}"
        )


def _raise_on_transport_failure(issues, profile: str) -> None:
    """Reclassify transport-failure issues as a :class:`ValidationTransportError`.

    rocrate_validator catches a connection error inside a remote-resolving check
    and re-emits it as a REQUIRED *content* issue (e.g. ``ro-crate-1.1_2.1`` /
    ``ro-crate-1.1_2.2``). That makes a transient network blip look like a real
    REQUIRED violation. We detect those issues (a transport-sensitive check with a
    connection-error message) and raise instead, so callers surface a clear
    transport error and never report a spurious REQUIRED content issue.
    """
    for issue in issues:
        message = getattr(issue, "message", None)
        if not _is_transport_failure_message(message):
            continue
        check_id = getattr(issue, "check_id", None) or "unknown"
        raise ValidationTransportError(
            f"Validation pass {profile!r} could not dereference a remote "
            f"resource (check {check_id}): {message}"
        )


def _raise_on_transport_failure_result(result, profile: str) -> None:
    """Same as :func:`_raise_on_transport_failure` for a raw validator result.

    The on-disk ``validate_crate`` path holds rocrate_validator ``CheckIssue``
    objects (not :class:`RoutableIssue`), so inspect their ``message`` and
    ``check.identifier`` directly before they are flattened to prose.
    """
    for issue in result.get_issues():
        message = issue.message or str(issue)
        if not _is_transport_failure_message(message):
            continue
        check_id = getattr(getattr(issue, "check", None), "identifier", None) or "unknown"
        raise ValidationTransportError(
            f"Validation pass {profile!r} could not dereference a remote "
            f"resource (check {check_id}): {message}"
        )


def validate_crate(crate_dir: Path) -> list[ValidationResult]:
    """Run all three validation passes against crate_dir.

    Returns one ValidationResult per pass in order:
      1. Base RO-Crate 1.2
      2. ISA RO-Crate Profile
      3. ISA-Tox RO-Crate Profile
    """
    results: list[ValidationResult] = []

    # --- Pass 1: base RO-Crate 1.1 ---
    settings = services.ValidationSettings(
        rocrate_uri=crate_dir,  # ty: ignore[unknown-argument]
        profile_identifier="ro-crate-1.2",
        requirement_severity=models.Severity.OPTIONAL,
    )
    with _checks_must_run("base"):
        result = services.validate(settings)
    _raise_on_transport_failure_result(result, profile="Base RO-Crate 1.2")
    results.append(
        ValidationResult(
            profile="Base RO-Crate 1.2",
            passed=not result.has_issues(),
            issues=_format_issues(result),
            required_issues=_format_issues(result, models.Severity.REQUIRED),
            passed_required=not result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    # --- Pass 2: ISA profile (bundled upstream isa-ro-crate) ---
    # disable_inherited_profiles_issue_reporting: isa-ro-crate is-profile-of
    # ro-crate-1.1, and rocrate_validator re-reports the inherited base-profile
    # checks here. Those duplicates are false positives (they pass cleanly in the
    # standalone base pass above), so we suppress inherited reporting and let each
    # pass cover only its own layer. Base coverage is NOT lost — pass 1 owns it.
    isa_settings = services.ValidationSettings(
        rocrate_uri=crate_dir,  # ty: ignore[unknown-argument]
        profile_identifier="isa-ro-crate",
        requirement_severity=models.Severity.OPTIONAL,
        disable_inherited_profiles_issue_reporting=True,
    )
    with _checks_must_run("isa"):
        isa_result = services.validate(isa_settings)
    _raise_on_transport_failure_result(isa_result, profile="ISA RO-Crate Profile")
    results.append(
        ValidationResult(
            profile="ISA RO-Crate Profile",
            passed=not isa_result.has_issues(),
            issues=_format_issues(isa_result),
            required_issues=_format_issues(isa_result, models.Severity.REQUIRED),
            passed_required=not isa_result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    # --- Pass 3: ISA-Tox profile (our extension on top of bundled isa-ro-crate) ---
    # profiles_path=DEFAULT (bundled) + extra_profiles_path=SHAPES_DIR so the
    # tox-ro-crate -> isa-ro-crate -> ro-crate inheritance chain resolves.
    # Same inherited-reporting suppression as the ISA pass; this pass reports only
    # tox-specific shapes.
    tox_settings = services.ValidationSettings(
        rocrate_uri=crate_dir,  # ty: ignore[unknown-argument]
        profiles_path=DEFAULT_PROFILES_PATH,
        extra_profiles_path=SHAPES_DIR,
        profile_identifier="tox-ro-crate",
        requirement_severity=models.Severity.OPTIONAL,
        disable_inherited_profiles_issue_reporting=True,
    )
    with _checks_must_run("tox"):
        tox_result = services.validate(tox_settings)
    _raise_on_transport_failure_result(tox_result, profile="ISA-Tox RO-Crate Profile")
    results.append(
        ValidationResult(
            profile="ISA-Tox RO-Crate Profile",
            passed=not tox_result.has_issues(),
            issues=_format_issues(tox_result),
            required_issues=_format_issues(tox_result, models.Severity.REQUIRED),
            passed_required=not tox_result.has_issues(min_severity=models.Severity.REQUIRED),
        )
    )

    return results


def _format_issues(result, min_severity=None) -> list[str]:
    """Return human-readable issue strings (severity + message, no SHACL IDs).

    When ``min_severity`` is given, only issues at that severity or higher are
    returned (used to express a REQUIRED-only validation gate).
    """
    kwargs = {"min_severity": min_severity} if min_severity is not None else {}
    issues = []
    for issue in result.get_issues(**kwargs):
        severity = issue.severity.name.capitalize()
        message = issue.message or str(issue)
        issues.append(f"[{severity}] {message}")
    return issues
