"""Issue #361: the tox profile's RO-Crate base lineage mirrors the upstream
``isa-ro-crate`` profile it extends.

The tox ``profile.ttl`` declares ``isTransitiveProfileOf .../crate/1.1`` because the
bundled ``isa-ro-crate`` profile (which it extends) is a profile of RO-Crate 1.1. This
is intentional and NOT inconsistent with the validator running its base pass against
``ro-crate-1.2`` — 1.2 is backward-compatible (see ``profiles/validator.py``,
``profiles/shapes/tox/profile.ttl``). This guard fails if the two lineages diverge,
e.g. if upstream ``isa-ro-crate`` bumps its base version and the tox profile is left
behind.
"""

from __future__ import annotations

import pathlib
import re

TOX_PROFILE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "profiles"
    / "shapes"
    / "tox"
    / "profile.ttl"
)


def _crate_base_versions(ttl_text: str) -> set[str]:
    """RO-Crate base versions a profile targets (``w3id.org/ro/crate/<x.y>``)."""
    return set(re.findall(r"w3id\.org/ro/crate/(\d+\.\d+)\b", ttl_text))


def _isa_ro_crate_profile() -> pathlib.Path:
    import rocrate_validator

    return (
        pathlib.Path(rocrate_validator.__file__).parent
        / "profiles"
        / "isa-ro-crate"
        / "profile.ttl"
    )


def test_tox_profile_base_lineage_mirrors_isa_ro_crate():
    isa_versions = _crate_base_versions(_isa_ro_crate_profile().read_text())
    tox_versions = _crate_base_versions(TOX_PROFILE.read_text())

    assert isa_versions == {"1.1"}, (
        f"upstream isa-ro-crate profile changed its RO-Crate base to {isa_versions}; "
        "reconcile profiles/shapes/tox/profile.ttl to match"
    )
    assert tox_versions == isa_versions, (
        f"tox profile RO-Crate lineage {tox_versions} must mirror the isa-ro-crate "
        f"profile it extends {isa_versions}"
    )


def test_validator_base_pass_is_1_2_by_design():
    """The base validation pass is 1.2 (backward-compatible) even though the domain
    profiles are 1.1-lineage — pin it so the deliberate choice is visible."""
    from profiles.validator import _PROFILE_PASSES

    assert _PROFILE_PASSES["base"][0] == "ro-crate-1.2"
