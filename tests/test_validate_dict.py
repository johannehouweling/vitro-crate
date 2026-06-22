"""Tests for profiles.validator.validate_crate_dict — in-memory SHACL validation.

These pin the in-memory (no-disk) validation path introduced for Issue #87: a
JSON-LD metadata document (the dict returned by ``crate.metadata.generate()``)
is validated directly via ``rocrate_validator.services.validate_metadata_as_dict``,
returning per-pass results whose issues carry routable focus-node / property
information instead of opaque prose.
"""

from __future__ import annotations

from rocrate.rocrate import ROCrate

from profiles.context import ISA_TOX_CONTEXT


def _minimal_doc() -> dict:
    """A minimal RO-Crate metadata document with no ISA identifier on the root.

    ro-crate-py always emits a base-valid root (name/description default via the
    library), but a bare crate has no ``schema:identifier`` on the Root Data
    Entity, which the ISA profile requires (isa-ro-crate_3.2) — a reliable,
    routable REQUIRED violation to assert against.
    """
    crate = ROCrate()
    crate.metadata.extra_contexts = ISA_TOX_CONTEXT
    crate.root_dataset["name"] = "Test"
    crate.root_dataset["description"] = "Test crate"
    # Base RO-Crate requires a license on the root (ro-crate-1.1_8.3); populate_crate
    # supplies this same placeholder, so a base-valid doc mirrors the real path.
    crate.root_dataset["license"] = "ALL RIGHTS RESERVED BY THE AUTHORS"
    # The metadata descriptor MUST conformsTo the versioned spec (ro-crate-1.1_5.3);
    # populate_crate sets this explicitly rather than relying on the library default.
    crate.metadata["conformsTo"] = {"@id": "https://w3id.org/ro/crate/1.1"}
    return crate.metadata.generate()


class TestValidateCrateDict:
    def test_returns_one_result_per_pass(self):
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_minimal_doc())
        # default profile="all" -> base, isa, tox
        assert [r.profile for r in results] == ["base", "isa", "tox"]

    def test_profile_scope_runs_single_pass(self):
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_minimal_doc(), profile="base")
        assert len(results) == 1
        assert results[0].profile == "base"

    def test_issues_are_routable(self):
        """Each issue exposes the focus-node id and failing property IRI."""
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_minimal_doc())
        isa = next(r for r in results if r.profile == "isa")
        assert isa.passed_required is False
        # The root (./) is missing schema:identifier — a routable REQUIRED issue.
        ident_issues = [
            i
            for i in isa.issues
            if i.property and i.property.endswith("identifier") and i.entity_id == "./"
        ]
        assert ident_issues, [
            (i.entity_id, i.property, i.check_id) for i in isa.issues
        ]
        issue = ident_issues[0]
        assert issue.severity == "required"
        assert issue.profile == "isa"
        assert issue.check_id  # e.g. isa-ro-crate_3.2

    def test_base_passes_for_minimal_doc(self):
        from profiles.validator import validate_crate_dict

        results = validate_crate_dict(_minimal_doc())
        base = next(r for r in results if r.profile == "base")
        assert base.passed_required is True

    def test_writes_no_files(self, tmp_path, monkeypatch):
        """Validation must not touch disk — cwd stays empty."""
        from profiles.validator import validate_crate_dict

        monkeypatch.chdir(tmp_path)
        validate_crate_dict(_minimal_doc())
        assert list(tmp_path.iterdir()) == []

    def test_invalid_severity_raises(self):
        """An unknown severity must fail loudly, not silently pick the strictest gate."""
        import pytest

        from profiles.validator import validate_crate_dict

        with pytest.raises(ValueError):
            validate_crate_dict(_minimal_doc(), severity="bogus")

    def test_invalid_profile_raises(self):
        import pytest

        from profiles.validator import validate_crate_dict

        with pytest.raises(ValueError):
            validate_crate_dict(_minimal_doc(), profile="bogus")
