"""Performance regression tests for the in-memory SHACL validator (Issue #115).

The in-memory ``validate_crate_dict`` path builds the RO-Crate from a metadata
*dict* via ``rocrate_validator``'s ``ROCrate.from_metadata_dict``, which hardcodes
the crate URI to ``"./"`` (the current working directory). The base-pass check
``ro-crate-1.2`` resolves the metadata-descriptor id through
``ROCrateLocalFolder.metadata_descriptor_id``, which does
``base_path.rglob("*ro-crate-metadata.json")`` over that URI — i.e. a **recursive
walk of the entire current working directory tree** on every pass, every call.

In a real run the CWD is a developer/CI checkout (``.venv``, ``.git``, dozens of
git worktrees) or a large extracted dataset, so that walk dominated wall-clock:
profiling pinned it at ~57s of a ~69s three-pass sweep (see #115). It is also
pure waste on the dict path — there is no crate on disk; the descriptor id is the
fixed convention ``ro-crate-metadata.json`` (exactly what the upstream walk falls
back to when it finds nothing).

``profiles.validator`` installs an idempotent patch
(``_patch_in_memory_descriptor_id``) that pre-seeds the cached descriptor id with
the canonical constant for crates built from a dict, so the CWD walk is skipped.
These tests pin:

1. The hot path performs **no recursive walk of the current working directory**
   (the regression guard for the bottleneck).
2. The descriptor id is the canonical ``ro-crate-metadata.json``.
3. Validation results are **unchanged** — a known-good crate still passes and a
   known-bad crate still fails the same REQUIRED check (no weakened validation).
"""

from __future__ import annotations

import pathlib

import pytest
from rocrate.rocrate import ROCrate

from profiles.context import ISA_TOX_CONTEXT


def _base_valid_doc() -> dict:
    """A metadata document that passes the *base* RO-Crate REQUIRED gate cleanly.

    Mirrors ``tests/test_validate_dict._minimal_doc``: a base-valid root with
    name/description/license and a versioned descriptor ``conformsTo``. The base
    pass is the one whose ``ro-crate-1.2`` check triggers the working-directory
    walk, so this is the right fixture for the no-walk guard and the
    base-still-passes equivalence assertion. (It does *not* satisfy the deeper ISA
    shapes — that is unrelated to this perf fix.)
    """
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    crate.root_dataset["name"] = "Test"
    crate.root_dataset["description"] = "Test crate"
    crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
    crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.1"}
    return crate.metadata.generate()


def _isa_bad_doc() -> dict:
    """A base-valid crate that is *missing* the ISA root ``identifier``.

    ro-crate-py emits a base-valid root, but a bare crate has no
    ``schema:identifier`` on the Root Data Entity, which the ISA profile requires
    (``isa-ro-crate_3.2``) — a reliable REQUIRED violation. Known-*bad* fixture.
    """
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    crate.root_dataset["name"] = "Test"
    crate.root_dataset["description"] = "Test crate"
    crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
    crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.1"}
    return crate.metadata.generate()


class TestNoCwdWalk:
    """The bottleneck: a recursive walk of the CWD on every in-memory pass."""

    def test_in_memory_validation_does_not_rglob_the_cwd(self, monkeypatch):
        """``validate_crate_dict`` must not recursively walk the working directory.

        Regression guard for #115: the dominant cost was
        ``ROCrateLocalFolder.metadata_descriptor_id`` doing ``cwd.rglob(...)`` on
        the dict path. We spy on ``pathlib.Path.rglob`` and assert it is never
        invoked on the current working directory (or any ancestor) during an
        in-memory validation.
        """
        from profiles.validator import validate_crate_dict

        cwd = pathlib.Path.cwd().resolve()
        offending: list[str] = []
        real_rglob = pathlib.Path.rglob

        def _spy_rglob(self, pattern, *args, **kwargs):
            resolved = self.resolve()
            # The walk we are killing is rooted at the CWD (URI "./"). Flag any
            # rglob anchored at the CWD or an ancestor of it.
            if resolved == cwd or resolved in cwd.parents:
                offending.append(f"{resolved} :: {pattern}")
            return real_rglob(self, pattern, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "rglob", _spy_rglob)

        validate_crate_dict(_base_valid_doc(), severity="required", profile="base")

        assert not offending, (
            "in-memory validation recursively walked the working directory "
            f"(the #115 bottleneck): {offending}"
        )

    def test_from_metadata_dict_descriptor_id_is_canonical(self):
        """The in-memory crate reports the canonical descriptor id without a walk."""
        # importing profiles.validator installs the patch at import time
        from rocrate_validator.rocrate.base import ROCrate as RVROCrate
        from rocrate_validator.rocrate.metadata import ROCrateMetadata

        import profiles.validator  # noqa: F401

        crate = RVROCrate.from_metadata_dict(_base_valid_doc())
        assert crate.metadata_descriptor_id == ROCrateMetadata.METADATA_FILE_DESCRIPTOR


def _issue_signature(results):
    """A stable, order-independent signature of every issue across all passes."""
    return sorted(
        (r.profile, r.passed, r.passed_required, i.severity, i.check_id,
         i.entity_id, i.property, i.message)
        for r in results
        for i in r.issues
    )


class TestResultsUnchanged:
    """The optimization must not weaken validation: same pass, same failures."""

    def test_base_pass_still_clean_for_base_valid_doc(self):
        """The base pass (the one doing the CWD walk) still passes at REQUIRED."""
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_base_valid_doc(), severity="required", profile="all")
        base = next(r for r in results if r.profile == "base")
        assert base.passed_required is True

    # Two full SHACL sweeps (patched vs unpatched, profile="all", severity="optional")
    # sit right at the 30s edge on CI's 2-vCPU runner, intermittently tripping the
    # global --timeout=30 (Issue #278). Give this intentionally-heavy comparison its
    # own budget; the rest of the suite keeps the tight 30s default.
    @pytest.mark.timeout(120)
    def test_patched_results_byte_identical_to_unpatched(self, monkeypatch):
        """Issue sets at every severity are identical with and without the patch.

        Temporarily restores the original (un-wrapped) ``from_metadata_dict`` —
        the CWD-walking behaviour — and asserts the full issue signature over the
        bad fixture matches the patched (no-walk) path. This is the core
        correctness guarantee: the speedup changes *how* the descriptor id is
        resolved, never *what* issues are reported.
        """
        from rocrate_validator.rocrate.base import ROCrate as RVROCrate

        import profiles.validator  # noqa: F401
        from profiles.validator import validate_crate_dict

        doc = _isa_bad_doc()

        # Patched (no-walk) signature, at the most permissive gate.
        patched_sig = _issue_signature(
            validate_crate_dict(doc, severity="optional", profile="all")
        )

        # Restore the original CWD-walking factory and re-measure.
        original = getattr(RVROCrate.from_metadata_dict, "__wrapped__", None)
        assert original is not None, "patch not installed; cannot compare"
        monkeypatch.setattr(RVROCrate, "from_metadata_dict", staticmethod(original))

        unpatched_sig = _issue_signature(
            validate_crate_dict(doc, severity="optional", profile="all")
        )

        assert patched_sig == unpatched_sig

    def test_known_bad_crate_still_fails_isa_required(self):
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_isa_bad_doc(), severity="required", profile="all")
        isa = next(r for r in results if r.profile == "isa")
        assert isa.passed_required is False
        # the specific REQUIRED violation (missing root identifier) is still routed
        ident = [
            i
            for i in isa.issues
            if i.property and i.property.endswith("identifier") and i.entity_id == "./"
        ]
        assert ident, [(i.entity_id, i.property, i.check_id) for i in isa.issues]
        assert ident[0].severity == "required"


class TestParallelPassesAgreeWithSerial:
    """The three profile passes run concurrently; the verdict must not change.

    The passes are independent — same document, different profile, nothing flows
    between them — so running them in a process pool trades startup cost for
    overlap. A speedup that changes a verdict is not a speedup, so the contract
    tested here is equivalence, plus a fallback that can never make validation
    fail on its own.
    """

    def _verdict(self, results):
        return sorted(
            (r.profile, r.passed, r.passed_required, len(r.issues)) for r in results
        )

    # Two full three-pass sweeps, same shape as
    # test_patched_results_byte_identical_to_unpatched — which needed its own
    # budget because a pair of sweeps sits at the 30s edge on CI's 2-vCPU runner
    # (#278). Equivalence is only demonstrable by running both paths, so this one
    # gets the same allowance rather than a weaker assertion.
    @pytest.mark.timeout(120)
    def test_a_good_crate_gets_the_same_verdict_either_way(self, monkeypatch):
        from profiles.validator import validate_crate_dict

        doc = _base_valid_doc()
        monkeypatch.setenv("VITRO_VALIDATE_SERIAL", "1")
        serial = validate_crate_dict(doc, severity="required", profile="all")
        monkeypatch.delenv("VITRO_VALIDATE_SERIAL")
        parallel = validate_crate_dict(doc, severity="required", profile="all")
        assert self._verdict(parallel) == self._verdict(serial)

    def test_a_bad_crate_still_fails_through_the_pool(self, monkeypatch):
        """A failure must survive the trip back from a worker process.

        Only the parallel path runs here — the serial verdict for this fixture is
        already pinned by ``test_known_bad_crate_still_fails_isa_required``, and a
        second full three-pass sweep would double this test's runtime for nothing.
        """
        from profiles.validator import validate_crate_dict

        monkeypatch.delenv("VITRO_VALIDATE_SERIAL", raising=False)
        results = validate_crate_dict(_isa_bad_doc(), severity="required", profile="all")
        assert {r.profile for r in results} == {"base", "isa", "tox"}
        isa = next(r for r in results if r.profile == "isa")
        assert isa.passed_required is False
        # The issues themselves survive pickling, not just the boolean verdict.
        assert isa.issues and all(i.message for i in isa.issues)

    def test_a_single_pass_does_not_start_a_pool(self, monkeypatch):
        """One pass has nothing to overlap, so it must stay in-process."""
        import profiles.validator as validator

        called = False

        def _spy(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not be reached")

        monkeypatch.setattr(validator, "_validate_passes_parallel", _spy)
        validator.validate_crate_dict(_base_valid_doc(), severity="required", profile="base")
        assert called is False

    def test_a_broken_pool_falls_back_instead_of_failing(self, monkeypatch, caplog):
        """A speedup must never be the reason validation breaks.

        A pool that cannot start is not a verdict — the caller gets the same
        answer, computed serially, and a warning explaining the degrade.
        """
        import profiles.validator as validator

        def _explode(*args, **kwargs):
            raise OSError("no workers for you")

        monkeypatch.delenv("VITRO_VALIDATE_SERIAL", raising=False)
        monkeypatch.setattr(validator.multiprocessing, "get_context", _explode)
        results = validator.validate_crate_dict(
            _base_valid_doc(), severity="required", profile="all"
        )
        # A complete, ordinary answer — the caller cannot tell it degraded.
        assert [r.profile for r in results] == ["base", "isa", "tox"]
        assert next(r for r in results if r.profile == "base").passed_required is True
        assert "serially" in caplog.text

    def test_the_serial_escape_hatch_is_honoured(self, monkeypatch):
        import profiles.validator as validator

        monkeypatch.setenv("VITRO_VALIDATE_SERIAL", "1")
        assert validator._serial_only() is True
        monkeypatch.setenv("VITRO_VALIDATE_SERIAL", "0")
        assert validator._serial_only() is False
        monkeypatch.delenv("VITRO_VALIDATE_SERIAL")
        assert validator._serial_only() is False
