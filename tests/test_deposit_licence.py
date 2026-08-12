"""The depositor's declared licence is read, not guessed (#535).

Nothing in the builder ever read the licence: `set_metadata` — an LLM-callable
tool — was its only writer, and assembly hardcoded "ALL RIGHTS RESERVED BY THE
AUTHORS" when no one had set one. Both branches assert a licence nobody
declared, and on a real deposit the guessed one inverted the depositor's:
S-VHPS26 declares CC-BY-4.0, the crate claimed all rights reserved.

Reading it is not a guess: a BioStudies descriptor states the licence as an
attribute and usually qualifies it with a canonical URL, so the extractor
prefers what the deposit says and never invents a version number.
"""

from __future__ import annotations

import json
from pathlib import Path

from builder.state import CrateMetadata, CrateState
from builder.tools.file_readers import extract_deposit_licence


def _descriptor(licence: dict | None) -> str:
    """A BioStudies descriptor, optionally carrying a licence attribute."""
    attributes = [{"name": "Title", "value": "A study"}]
    if licence is not None:
        attributes.append(licence)
    return json.dumps({"accno": "S-TEST1", "section": {"attributes": attributes}})


class TestExtractDepositLicence:
    def test_prefers_the_canonical_url_the_deposit_qualifies_it_with(self) -> None:
        # The real S-VHPS26 shape: a short form plus a URL value qualifier.
        text = _descriptor(
            {
                "name": "License",
                "value": "CC-BY",
                "valqual": [
                    {
                        "name": "URL",
                        "value": "https://creativecommons.org/licenses/by/4.0/legalcode",
                    }
                ],
            }
        )
        assert (
            extract_deposit_licence(text)
            == "https://creativecommons.org/licenses/by/4.0/legalcode"
        )

    def test_returns_the_declared_value_verbatim_when_no_url_is_given(self) -> None:
        # "CC-BY" alone does not say which version; mapping it to a 4.0 URI would
        # state something the depositor did not (D5).
        assert extract_deposit_licence(_descriptor({"name": "License", "value": "CC-BY"})) == (
            "CC-BY"
        )

    def test_matches_the_attribute_name_case_and_spelling_insensitively(self) -> None:
        for name in ("license", "LICENSE", "Licence"):
            text = _descriptor({"name": name, "value": "CC0"})
            assert extract_deposit_licence(text) == "CC0", name

    def test_a_descriptor_without_a_licence_yields_nothing(self) -> None:
        assert extract_deposit_licence(_descriptor(None)) is None

    def test_non_biostudies_input_yields_nothing(self) -> None:
        # Applied blindly to any scanned file, so it must be quiet on the rest.
        assert extract_deposit_licence("not json at all") is None
        assert extract_deposit_licence(json.dumps({"unrelated": True})) is None
        assert extract_deposit_licence("") is None

    def test_the_real_deposit_descriptor(self) -> None:
        # The file that exposed the bug, read as the tool would read it.
        descriptor = Path("output/svhps26_real_input_crate/S-VHPS26.json")
        if not descriptor.exists():  # pragma: no cover - crate not checked in
            return
        assert (
            extract_deposit_licence(descriptor.read_text(encoding="utf-8"))
            == "https://creativecommons.org/licenses/by/4.0/legalcode"
        )


class TestDeclaredLicenceWins:
    """A depositor's statement is a fact; a drafter's is a guess."""

    def test_set_metadata_does_not_overwrite_a_licence_read_from_the_deposit(self) -> None:
        from builder.tools.management import set_crate_metadata as set_metadata

        state = CrateState()
        state.metadata.license = "https://creativecommons.org/licenses/by/4.0/legalcode"
        state.metadata.license_from_deposit = True

        result = set_metadata(state, license="https://en.wikipedia.org/wiki/All_rights_reserved")

        assert state.metadata.license == (
            "https://creativecommons.org/licenses/by/4.0/legalcode"
        )
        assert "license" in str(result).lower()

    def test_set_metadata_still_sets_a_licence_nobody_declared(self) -> None:
        from builder.tools.management import set_crate_metadata as set_metadata

        state = CrateState()
        set_metadata(state, license="https://creativecommons.org/publicdomain/zero/1.0/")
        assert state.metadata.license == "https://creativecommons.org/publicdomain/zero/1.0/"

    def test_the_marker_round_trips(self) -> None:
        meta = CrateMetadata(license="CC-BY", license_from_deposit=True)
        assert CrateMetadata.from_dict(meta.to_dict()).license_from_deposit is True
        # A checkpoint written before the field existed still loads.
        legacy = meta.to_dict()
        legacy.pop("license_from_deposit", None)
        assert CrateMetadata.from_dict(legacy).license_from_deposit is False


class TestBothArmsGetTheDeclaredLicence:
    """Wired where discovery is, so neither arm has to remember to ask."""

    def test_initializing_over_a_deposit_reads_its_licence(self, tmp_path: Path) -> None:
        from builder.engine import AgentEngine

        (tmp_path / "S-TEST1.json").write_text(
            _descriptor(
                {
                    "name": "License",
                    "value": "CC-BY",
                    "valqual": [
                        {
                            "name": "URL",
                            "value": "https://creativecommons.org/licenses/by/4.0/legalcode",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        engine = AgentEngine()
        engine.initialize(input_path=str(tmp_path))

        assert engine.state.metadata.license == (
            "https://creativecommons.org/licenses/by/4.0/legalcode"
        )
        assert engine.state.metadata.license_from_deposit is True

    def test_a_deposit_without_a_licence_leaves_it_unset(self, tmp_path: Path) -> None:
        # Never invent one: absent stays absent, and the drafter may still set it.
        from builder.engine import AgentEngine

        (tmp_path / "S-TEST1.json").write_text(_descriptor(None), encoding="utf-8")

        engine = AgentEngine()
        engine.initialize(input_path=str(tmp_path))

        assert engine.state.metadata.license is None
        assert engine.state.metadata.license_from_deposit is False


class TestTheDeclaredLicenceReachesTheCrate:
    """Reading it is only worth anything if it lands on the Root Data Entity."""

    def test_a_declared_licence_still_reaches_the_root(self) -> None:
        """It reaches the root as a described entity, not a bare URL.

        The profile asks a License entity for a name and a description, so a
        recognised licence is emitted as a contextual entity keyed on its URL —
        the root still points at exactly the URL read from the deposit.
        """
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate

        state = CrateState()
        state.metadata.title = "Licensed"
        state.metadata.license = "https://creativecommons.org/licenses/by/4.0/legalcode"
        crate = ROCrate()
        populate_crate(state, crate, None, materialize_payload=False)

        licence = crate.root_dataset["license"]
        assert licence.id == "https://creativecommons.org/licenses/by/4.0/legalcode"
        assert licence["name"] == "Creative Commons Attribution 4.0 International"
        assert licence["description"]
