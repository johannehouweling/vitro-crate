"""Regression tests for the warm cache of parsed SHACL profiles (#63).

rocrate_validator re-parses every profile's spec graph, SHACL shapes and
ontology graphs on every ``validate()`` call — there is no public hook to inject
pre-parsed graphs. ``profiles/validator.py`` therefore installs a module-level
warm cache of the *crate-independent* artifacts it controls: the loaded +
warmed ``Profile`` objects (parsed spec graphs, per-profile shape registries and
requirements). The cache is keyed by ``(profiles_path, extra_profiles_path,
severity, shapes-dir mtime)`` so it invalidates when any shape ``.ttl`` changes.

Only the crate's data graph is re-read per call.

These tests assert the load-bearing, non-flaky guarantees:
  * the warmed profile list is the *same object* across consecutive calls
    (cache hit — the shapes/ontology are not re-loaded), and
  * the cache key changes (i.e. the cache invalidates) when a shape file's
    mtime changes.

A loose warm-path timing assertion guards against a gross regression without
being flaky — the dominant per-call cost lives inside the library's own
inference, so we only assert the warm call is not dramatically slower.
"""

from __future__ import annotations

import time

import pytest
from rocrate.rocrate import ROCrate

from profiles.context import ISA_TOX_CONTEXT


def _minimal_doc() -> dict:
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    crate.root_dataset["name"] = "Test"
    crate.root_dataset["description"] = "Test crate"
    crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
    crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.1"}
    return crate.metadata.generate()


@pytest.fixture(autouse=True)
def _clear_cache():
    from profiles.validator import clear_profile_cache

    clear_profile_cache()
    yield
    clear_profile_cache()


class TestProfileCacheReuse:
    def test_warmed_profiles_are_reused_across_calls(self):
        """A second validate reuses the cached (same-object) warmed profiles."""
        from profiles import validator

        doc = _minimal_doc()
        validator.validate_crate_dict(doc, profile="tox")
        snapshot = dict(validator._PROFILE_CACHE)
        assert snapshot, "expected the tox pass to populate the profile cache"

        validator.validate_crate_dict(doc, profile="tox")
        # The cached profile lists must be the *same objects* — proof the shapes
        # and ontology graphs were not re-parsed on the warm call.
        for key, profiles in validator._PROFILE_CACHE.items():
            assert profiles is snapshot[key]

    def test_cache_key_invalidates_on_shape_mtime_change(self):
        """Bumping a shape file's mtime must change the cache key."""
        from pathlib import Path

        from profiles.validator import SHAPES_DIR, _profile_cache_key

        ttl = next(SHAPES_DIR.rglob("*.ttl"))
        key_before = _profile_cache_key("tox-ro-crate", "required")

        # Touch the shape file forward in time and rebuild the key.
        future = time.time() + 10
        import os

        os.utime(ttl, (future, future))
        try:
            key_after = _profile_cache_key("tox-ro-crate", "required")
        finally:
            now = time.time()
            os.utime(ttl, (now, now))

        assert key_before != key_after
        assert Path(SHAPES_DIR).exists()

    def test_warm_call_not_dramatically_slower(self):
        """The 2nd consecutive validate must not be slower than the 1st (warm path).

        Loose ratio rather than an absolute timing to avoid flakiness on busy CI.
        """
        from profiles import validator

        doc = _minimal_doc()
        t0 = time.perf_counter()
        validator.validate_crate_dict(doc, profile="tox")
        cold = time.perf_counter() - t0

        t1 = time.perf_counter()
        validator.validate_crate_dict(doc, profile="tox")
        warm = time.perf_counter() - t1

        # Warm must not be meaningfully slower than cold (allow scheduler noise).
        assert warm <= cold * 1.5 + 0.5
