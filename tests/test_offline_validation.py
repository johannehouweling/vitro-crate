"""Offline-safety regression tests for the SHACL validator (Issue #117).

The three-pass SHACL validator must expand the RO-Crate ``@context`` to build
the data graph. ``@context`` points at the *remote* IRI
``https://w3id.org/ro/crate/1.2/context``; ``rocrate_validator`` resolves it
through an HTTP requester. On PR #116 CI that fetch flaked
(``RemoteDisconnected``) and the base pass emitted spurious REQUIRED issues
(checks ``ro-crate-1.1_2.1`` / ``ro-crate-1.1_2.2``), turning a transient
network blip into red CI — violating #59's "runs offline" criterion.

These tests pin two guarantees, with the live network simulated as DOWN:

1. Validation runs green with the network disabled — the bundled local copy of
   the RO-Crate context is served instead of being fetched (offline-safe).
2. A genuine transport/connection error during a pass is surfaced as an *error*,
   never a spurious REQUIRED *content* issue (no false negatives in production
   ``build_and_validate``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
import requests
from rocrate.rocrate import ROCrate

from profiles.context import ISA_TOX_CONTEXT

# The exact flake message observed on PR #116 CI.
_FLAKE_MESSAGE = (
    "('Connection aborted.', "
    "RemoteDisconnected('Remote end closed connection without response'))"
)


def _base_valid_doc() -> dict:
    """A metadata document that passes the base RO-Crate pass cleanly.

    Mirrors ``tests/test_validate_dict._minimal_doc``: a base-valid root with
    name/description/license and a versioned descriptor ``conformsTo``. The base
    pass is the one that needs the remote ``@context``, so this is the document
    that exposes the offline regression.
    """
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    crate.root_dataset["name"] = "Test"
    crate.root_dataset["description"] = "Test crate"
    crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
    crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.2"}
    return crate.metadata.generate()


@contextlib.contextmanager
def _network_down() -> Iterator[list[str]]:
    """Simulate the live network being down at the HTTP transport layer.

    Patches ``requests``' adapter ``send`` (the lowest common denominator under
    every ``requests``/``requests_cache`` session) to raise the exact CI flake
    error. This blocks real outbound requests while leaving the validator's
    bundled-context intercept (which short-circuits *before* the transport) in
    place — so an offline-safe validator serves the context locally and never
    reaches this guard.

    Yields the list of URLs that escaped to the wire, so a test can assert the
    bundled context kept that count at zero.
    """
    from requests.adapters import HTTPAdapter

    attempted: list[str] = []
    original_send = HTTPAdapter.send

    def blocked_send(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        attempted.append(request.url)
        raise requests.exceptions.ConnectionError(_FLAKE_MESSAGE)

    HTTPAdapter.send = blocked_send  # ty: ignore[invalid-assignment]
    try:
        yield attempted
    finally:
        HTTPAdapter.send = original_send


@contextlib.contextmanager
def _requester_raises() -> Iterator[None]:
    """Force every ``HttpRequester`` GET/HEAD to raise the CI flake error.

    Unlike :func:`_network_down`, this intercepts *above* the HTTP cache, so a
    cached response cannot mask the failure — making the "a real fetch failed"
    case deterministic regardless of cache state. It also replaces the validator's
    own ``__getattr__`` intercept, which is what we want here: we are simulating a
    genuine fetch failure with no local fallback available.
    """
    from rocrate_validator.utils import http as rv_http

    original_getattr = rv_http.HttpRequester.__getattr__

    def patched_getattr(self, name):  # noqa: ANN001
        if name.upper() in {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"}:

            def _blocked(url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                raise requests.exceptions.ConnectionError(_FLAKE_MESSAGE)

            return _blocked
        return original_getattr(self, name)

    rv_http.HttpRequester.__getattr__ = patched_getattr  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        rv_http.HttpRequester.__getattr__ = original_getattr


class TestOfflineContextResolution:
    """The base pass resolves the RO-Crate ``@context`` without the network."""

    def test_base_pass_green_with_network_down(self):
        from profiles.validator import validate_crate_dict

        doc = _base_valid_doc()
        with _network_down() as attempted:
            results = validate_crate_dict(doc, profile="base")

        base = next(r for r in results if r.profile == "base")
        # No spurious REQUIRED issue from a remote-context fetch failure.
        assert base.passed_required is True, [
            (i.check_id, i.message) for i in base.issues
        ]
        # The bundled context must mean nothing was fetched over the wire.
        assert attempted == [], attempted

    def test_no_remote_context_check_failures_with_network_down(self):
        """The base pass stays clean with the network down — the remote-context
        resolution checks (e.g. ro-crate-1.2_2.*) are served from the bundled
        context, so they don't spuriously fail (the regression that flaked CI)."""
        from profiles.validator import validate_crate_dict

        with _network_down():
            results = validate_crate_dict(_base_valid_doc(), profile="base")

        base = next(r for r in results if r.profile == "base")
        assert base.passed_required, [(i.check_id, i.message) for i in base.issues]


class TestChebiResolvedCompoundContext:
    """A ChEBI-resolved compound must serialise to context-valid JSON-LD (#243).

    The PubChem-miss → ChEBI fallback in ``lookup_compound`` historically emitted
    bare ``chebi_id`` / ``chebi_iri`` keys onto the MolecularEntity. Those keys are
    NOT declared in the RO-Crate ``@context``, so RO-Crate 1.2 compaction rejects
    them and the base pass fails on every crate that resolved a compound via ChEBI
    (and the failure leaked into the HITL loop as an unanswerable gap). The ChEBI
    identity must instead round-trip through context-valid keys.
    """

    @staticmethod
    def _chebi_state(monkeypatch):
        """A CrateState with a single ChEBI-resolved MolecularEntity.

        Drives the *real* ``lookup_compound`` ChEBI fallback (PubChem patched to
        miss, OLS/ChEBI patched to hit) and the real ``resolve_compound`` so the
        keys the entity carries are exactly the ones the production path emits.
        """
        from builder.state import CrateState
        from builder.tools import composites, lookups

        lookups.lookup_compound.cache_clear()

        def fake_pubchem(query):  # noqa: ANN001 — no PubChem hit, force the ChEBI branch
            return {}

        def fake_chebi(query, ontology):  # noqa: ANN001
            assert ontology == "chebi"
            return {
                "@id": "http://purl.obolibrary.org/obo/CHEBI_28748",
                "termCode": "CHEBI:28748",
                "name": "doxorubicin",
            }

        monkeypatch.setattr(lookups, "lookup_pubchem", fake_pubchem)
        monkeypatch.setattr(lookups, "lookup_ontology_term_ols", fake_chebi)

        state = CrateState()
        state.metadata.title = "ChEBI crate"
        # verify=False keeps this fully offline (no source round-trip).
        result = composites.resolve_compound(state, name="Doxorubicin", verify=False)
        assert "error" not in result, result
        return state, result

    def test_chebi_compound_passes_base_validation(self, monkeypatch):
        from builder.tools.validation import build_and_validate

        state, _ = self._chebi_state(monkeypatch)
        report = build_and_validate(state, profile="base")

        assert report["conformance"].get("base") is True, [
            (i["entity_id"], i["property"], i["message"]) for i in report["issues"]
        ]
        # No @context violation about an undeclared key (the #243 symptom).
        joined = " ".join(i["message"] for i in report["issues"]).lower()
        assert "not present in the @context" not in joined, report["issues"]
        assert "chebi_id" not in joined and "chebi_iri" not in joined, report["issues"]

    def test_chebi_identity_present_in_context_valid_form(self, monkeypatch):
        state, _ = self._chebi_state(monkeypatch)
        mol = next(e for e in state.list_entities() if e.type == "MolecularEntity")

        # The ChEBI identity is retained — but only under context-declared keys,
        # never the bare ``chebi_id`` / ``chebi_iri`` keys that fail compaction.
        assert "chebi_id" not in mol.fields
        assert "chebi_iri" not in mol.fields

        serialised = " ".join(str(v) for v in mol.fields.values())
        assert "CHEBI:28748" in serialised
        assert "http://purl.obolibrary.org/obo/CHEBI_28748" in serialised

        from profiles.context import ISA_TOX_CONTEXT

        context_terms = set(ISA_TOX_CONTEXT[0])
        for key in mol.fields:
            if key.startswith("@") or key == "entity_id":
                continue
            assert key in context_terms, f"MolecularEntity key {key!r} not in @context"


class TestTransportErrorNotReportedAsRequired:
    """A connection error during a pass is an error, not a spurious REQUIRED."""

    def test_connection_error_is_not_a_required_content_issue(self, monkeypatch):
        """Even if a remote fetch genuinely fails, the validator must not pass off
        the resulting ``RemoteDisconnected`` as a REQUIRED content violation.

        We disable the local-context shortcut (forcing the document loader to the
        network) and force that fetch to fail, then assert the connection error is
        surfaced as a transport error rather than a REQUIRED ``ro-crate-1.1_2.*``
        content issue.
        """
        import profiles.validator as validator

        # Disable the bundled-context shortcut so the fetch must reach the wire.
        monkeypatch.setattr(validator, "_LOCAL_CONTEXTS", {}, raising=False)

        with _requester_raises():
            with pytest.raises(validator.ValidationTransportError):
                validator.validate_crate_dict(_base_valid_doc(), profile="base")
