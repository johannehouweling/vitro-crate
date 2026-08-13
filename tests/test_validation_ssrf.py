"""SSRF regression tests for the SHACL validator (Issue #168).

When validating an *untrusted* crate, a crafted ``@context`` (or any other
dereferenceable IRI) in ``ro-crate-metadata.json`` must NOT cause the validator
to make an outbound HTTP request to the crate-controlled URL. The #117 offline
loader only serves the two pinned well-known RO-Crate context URLs from disk and
otherwise fell through to the real network — an SSRF / data-egress primitive
(cloud metadata at ``169.254.169.254``, internal hosts, user deanonymization).

The fix is **deny-by-default**: during validation, any outbound dereference to a
URL that is not one of the bundled/allowlisted well-known context URLs is refused
(served a benign empty document) rather than fetched. These tests assert:

1. A crate whose ``@context`` points at a non-allowlisted attacker URL validates
   **without any outbound request reaching that URL** (deny-by-default), and does
   not raise a spurious REQUIRED issue.
2. The legitimate pinned ``@context`` is still served from the bundled copy, so a
   normal crate validates fully offline (no regression of #117).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

# Validation is heavy; keep each test bounded.
pytestmark = pytest.mark.timeout(120)


# Attacker-controlled URLs a malicious crate might point @context at. None of
# these are in the bundled allowlist, so the validator must never reach them.
_ATTACKER_URLS = (
    "http://169.254.169.254/latest/meta-data/",
    "http://evil.test/x",
    "http://127.0.0.1:1/evil",
)


@contextlib.contextmanager
def _record_outbound() -> Iterator[list[str]]:
    """Record every URL that escapes to the wire, hard-blocking the network.

    Patches ``requests``' adapter ``send`` (the lowest common denominator under
    every ``requests``/``requests_cache`` session) to record the URL and raise a
    connection error instead of touching the network. The recorded list lets a
    test assert that the attacker URL never reached the transport at all.
    """
    import requests
    from requests.adapters import HTTPAdapter

    attempted: list[str] = []
    original_send = HTTPAdapter.send

    def recording_send(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        attempted.append(request.url)
        raise requests.exceptions.ConnectionError(
            f"network hard-blocked in test: {request.url}"
        )

    HTTPAdapter.send = recording_send
    try:
        yield attempted
    finally:
        HTTPAdapter.send = original_send


def _malicious_doc(context_url: str) -> dict:
    """A metadata document whose ``@context`` is an attacker-chosen remote URL.

    The graph is otherwise a minimal, base-valid RO-Crate (root dataset with
    name/description/license and a versioned descriptor ``conformsTo``), so the
    only reason to touch the network would be to dereference the crafted
    ``@context`` — which the deny-by-default loader must refuse.
    """
    return {
        "@context": context_url,
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": "Untrusted crate",
                "description": "A crate pointing @context at an attacker URL",
                "license": "ALL RIGHTS RESERVED BY THE AUTHORS",
            },
        ],
    }


class TestContextFetchIsDenyByDefault:
    """A crate-controlled @context URL must never be fetched during validation."""

    @pytest.mark.parametrize("attacker_url", _ATTACKER_URLS)
    def test_no_outbound_request_to_attacker_context(self, attacker_url):
        from profiles.validator import validate_crate_dict

        doc = _malicious_doc(attacker_url)
        with _record_outbound() as attempted:
            # Must not raise a transport error and must not reach the network for
            # the crate-controlled URL: the loader fails closed (serves nothing).
            validate_crate_dict(doc, profile="base")

        # Deny-by-default: the attacker URL must never have hit the transport.
        leaked = [u for u in attempted if attacker_url.rstrip("/") in (u or "")]
        assert leaked == [], (
            f"validator made an outbound request to crate-controlled URL: {leaked}"
        )
        # Nothing at all should have reached the wire for this crate.
        assert attempted == [], attempted


class TestPinnedContextStillServedOffline:
    """The bundled pinned @context still validates a normal crate offline (#117)."""

    def test_pinned_context_validates_with_network_blocked(self):
        from rocrate.rocrate import ROCrate

        from profiles.context import ISA_TOX_CONTEXT
        from profiles.validator import validate_crate_dict

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        crate.root_dataset["name"] = "Test"
        crate.root_dataset["description"] = "Test crate"
        crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
        crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.2"}
        doc = crate.metadata.generate()

        with _record_outbound() as attempted:
            results = validate_crate_dict(doc, profile="base")

        base = next(r for r in results if r.profile == "base")
        assert base.passed_required is True, [
            (i.check_id, i.message) for i in base.issues
        ]
        # The pinned context is served from disk: nothing reached the wire.
        assert attempted == [], attempted
