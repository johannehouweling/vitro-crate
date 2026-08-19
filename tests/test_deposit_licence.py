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

    def test_a_document_that_declares_none_yields_nothing(self) -> None:
        # Applied blindly to any scanned file, so it must be quiet on the rest.
        assert extract_deposit_licence("not structured at all") is None
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


class TestAnUnstatedLicenceSaysSo:
    """The root carries a licence entity that states the absence (#540).

    ``license`` is a base MUST, so the field cannot simply be dropped — the
    earlier attempt cost 56 tests and every licence-less crate its base verdict.
    But answering that MUST with ``ALL RIGHTS RESERVED BY THE AUTHORS`` converts
    an unanswered question into the most restrictive claim available, asserted
    by machine over someone else's data.

    RO-Crate lets ``license`` be a ``CreativeWork`` rather than a URL, so the
    crate can satisfy the requirement while claiming nothing: an entity whose
    name and description say the terms were never stated. A consumer branches on
    its ``@id`` instead of string-matching English prose.
    """

    @staticmethod
    def _root(license: str | None) -> tuple[dict, dict]:
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate

        state = CrateState()
        state.metadata.title = "A crate"
        state.metadata.license = license
        crate = ROCrate()
        populate_crate(state, crate, None, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]
        root = next(n for n in graph if n.get("@id") == "./")
        return root, {str(n.get("@id")): n for n in graph}

    def test_the_licence_is_an_entity_not_a_restrictive_string(self) -> None:
        from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID

        root, by_id = self._root(None)

        assert root["license"] == {"@id": LICENCE_NOT_STATED_ID}
        entity = by_id[LICENCE_NOT_STATED_ID]
        assert entity["@type"] == "CreativeWork"
        assert entity["name"]
        assert entity["description"]

    def test_it_claims_neither_rights_nor_restrictions(self) -> None:
        from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID

        _root, by_id = self._root(None)
        text = f"{by_id[LICENCE_NOT_STATED_ID]['name']} {by_id[LICENCE_NOT_STATED_ID]['description']}"

        assert "all rights reserved" not in text.lower()
        # It must not read as a grant either — an unknown licence is not open.
        for grant in ("public domain", "freely available", "may be reused without"):
            assert grant not in text.lower(), grant

    def test_the_base_requirement_is_still_satisfied(self) -> None:
        """The whole point of an entity over an omission: crates stay conformant."""
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.validator import validate_crate_dict

        state = CrateState()
        state.metadata.title = "A crate"
        crate = ROCrate()
        populate_crate(state, crate, None, materialize_payload=False)
        doc = crate.metadata.generate()

        messages = [
            issue.message
            for result in validate_crate_dict(doc, severity="required", profile="base")
            for issue in result.issues
            if issue.severity == "required"
        ]
        assert not any("license" in m.lower() for m in messages), messages

    def test_a_declared_licence_is_untouched(self) -> None:
        root, by_id = self._root("https://creativecommons.org/licenses/by/4.0/")
        assert root["license"] == {"@id": "https://creativecommons.org/licenses/by/4.0/"}
        assert by_id["https://creativecommons.org/licenses/by/4.0/"]["name"]

    def test_an_unstated_licence_never_counts_as_filled(self) -> None:
        """It must not stop the loop asking for the real one.

        The MIT scorer discounted the old placeholder string for exactly this
        reason; the discount has to follow the value, not the spelling.
        """
        from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID
        from builder.tools.mit_assessment import _nonempty, _placeholder_values

        assert not _nonempty({"@id": LICENCE_NOT_STATED_ID})
        assert LICENCE_NOT_STATED_ID.lower() in {v.lower() for v in _placeholder_values()}


class TestAnyStructuredMetadataFileCanDeclareIt:
    """BioStudies is one convention among several, and deposits use the others.

    Gating on a BioStudies attribute tree made the reader answer for exactly one
    repository's export. A deposit that ships an RO-Crate, a CodeMeta record, a
    Frictionless datapackage or a DataCite payload states its licence just as
    plainly, in a field rather than an attribute — and got the fabricated
    fallback instead. Reading a NAMED field is still not guessing; what stays
    forbidden is inferring one from prose.
    """

    def test_an_ro_crate_declares_it_by_reference(self) -> None:
        doc = json.dumps(
            {
                "@context": "https://w3id.org/ro/crate/1.2/context",
                "@graph": [
                    {
                        "@id": "./",
                        "@type": "Dataset",
                        "license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
                    }
                ],
            }
        )

        assert extract_deposit_licence(doc) == "https://creativecommons.org/licenses/by/4.0/"

    def test_a_codemeta_record_declares_it_as_a_string(self) -> None:
        doc = json.dumps({"@type": "SoftwareSourceCode", "license": "https://spdx.org/licenses/MIT"})

        assert extract_deposit_licence(doc) == "https://spdx.org/licenses/MIT"

    def test_a_frictionless_datapackage_declares_it_in_a_list(self) -> None:
        doc = json.dumps(
            {
                "name": "deposit",
                "licenses": [
                    {"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/"}
                ],
            }
        )

        assert extract_deposit_licence(doc) == "https://creativecommons.org/licenses/by/4.0/"

    def test_a_datacite_payload_declares_it_as_a_rights_uri(self) -> None:
        doc = json.dumps(
            {
                "rightsList": [
                    {
                        "rights": "Creative Commons Attribution 4.0",
                        "rightsUri": "https://creativecommons.org/licenses/by/4.0/legalcode",
                    }
                ]
            }
        )

        assert extract_deposit_licence(doc) == (
            "https://creativecommons.org/licenses/by/4.0/legalcode"
        )

    def test_a_yaml_metadata_file_declares_it(self) -> None:
        assert extract_deposit_licence(
            "title: A study\nlicense: https://creativecommons.org/licenses/by/4.0/\n"
        ) == "https://creativecommons.org/licenses/by/4.0/"

    def test_a_uri_is_preferred_over_a_bare_label(self) -> None:
        """Machine-actionable beats a label, wherever in the document it sits."""
        doc = json.dumps(
            {
                "license": "CC-BY-4.0",
                "distribution": {"license": {"@id": "https://creativecommons.org/licenses/by/4.0/"}},
            }
        )

        assert extract_deposit_licence(doc) == "https://creativecommons.org/licenses/by/4.0/"

    def test_a_bare_label_is_still_returned_verbatim(self) -> None:
        """Mapping "CC-BY" onto a 4.0 URI would state a version nobody did (D5)."""
        assert extract_deposit_licence(json.dumps({"license": "CC-BY"})) == "CC-BY"

    def test_prose_is_never_a_source(self) -> None:
        """Every real deposit's README carries an unfilled template placeholder.

        `## License` / `[Default CC-BY 4.0 for data, CC0 for metadata unless
        specified otherwise]` — a bracketed instruction naming two licences and
        declaring neither. Reading a named field is not guessing; reading this
        would be.
        """
        readme = (
            "# Study README Template\n\n## License\n"
            "[Default CC-BY 4.0 for data, CC0 for metadata unless specified otherwise]\n"
        )

        assert extract_deposit_licence(readme) is None

    def test_a_markdown_bullet_list_is_not_a_declaration(self) -> None:
        """Prose parses as YAML, which is the trap.

        `- License: see the LICENSE file` is a valid YAML sequence of mappings,
        so a reader that accepted any YAML value would file "see the LICENSE
        file" as this deposit's legal terms. Only a document whose top level is
        a MAPPING is a metadata record; a list of bullets is a README.
        """
        readme = "- License: see the LICENSE file\n- Contact: someone@example.org\n"

        assert extract_deposit_licence(readme) is None

    def test_an_empty_declaration_is_not_a_declaration(self) -> None:
        assert extract_deposit_licence(json.dumps({"license": ""})) is None
        assert extract_deposit_licence(json.dumps({"license": {"@id": ""}})) is None


class TestWhichFileTheLicenceIsReadFrom:
    """Several files in a deposit can name a licence. Which one is the deposit's.

    Reading the first `.json` the scan happened to reach made the answer depend
    on directory order, and admitted any bundled manifest as the deposit's own
    terms.
    """

    CC_BY = "https://creativecommons.org/licenses/by/4.0/legalcode"

    def _engine_over(self, tmp_path: Path):
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.initialize(input_path=str(tmp_path))
        return engine

    def test_a_yaml_metadata_file_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "dataset_description.yaml").write_text(
            f"title: A study\nlicense: {self.CC_BY}\n", encoding="utf-8"
        )

        engine = self._engine_over(tmp_path)

        assert engine.state.metadata.license == self.CC_BY
        assert engine.state.metadata.license_from_deposit is True

    def test_a_bundled_manifest_does_not_become_the_deposits_terms(
        self, tmp_path: Path
    ) -> None:
        """A vendored `package.json` states its own licence, not the data's."""
        (tmp_path / "S-TEST1.json").write_text(
            _descriptor(
                {
                    "name": "License",
                    "value": "CC-BY",
                    "valqual": [{"name": "URL", "value": self.CC_BY}],
                }
            ),
            encoding="utf-8",
        )
        nested = tmp_path / "code" / "vendor" / "lib"
        nested.mkdir(parents=True)
        (nested / "package.json").write_text(
            json.dumps({"name": "lib", "license": "MIT"}), encoding="utf-8"
        )

        assert self._engine_over(tmp_path).state.metadata.license == self.CC_BY

    def test_the_answer_does_not_depend_on_directory_order(self, tmp_path: Path) -> None:
        """Two declarations at the same depth resolve the same way every run."""
        for name in ("zzz_meta.json", "aaa_meta.json"):
            (tmp_path / name).write_text(
                json.dumps({"license": self.CC_BY}), encoding="utf-8"
            )

        assert self._engine_over(tmp_path).state.metadata.license == self.CC_BY

    def test_a_machine_actionable_uri_beats_a_label_in_another_file(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "a_label.json").write_text(
            json.dumps({"license": "CC-BY"}), encoding="utf-8"
        )
        (tmp_path / "b_uri.json").write_text(
            json.dumps({"license": self.CC_BY}), encoding="utf-8"
        )

        assert self._engine_over(tmp_path).state.metadata.license == self.CC_BY


class TestTheConventionsOutsideAMetadataRecord:
    """Not every deposit ships a metadata record, and most ship a LICENSE file.

    A named field in a JSON document is one way to declare terms. The two that
    cover everything else are the SPDX identifier — a formal, machine-readable
    declaration that can sit in any text — and a file whose NAME says its whole
    content is the licence.
    """

    CC_BY = "https://creativecommons.org/licenses/by/4.0/"

    def test_an_spdx_identifier_is_a_declaration_wherever_it_sits(self) -> None:
        """`SPDX-License-Identifier:` is a standard, not a guess."""
        assert extract_deposit_licence("# SPDX-License-Identifier: CC-BY-4.0\n") == "CC-BY-4.0"
        assert extract_deposit_licence("<!-- SPDX-License-Identifier: MIT -->") == "MIT"

    def test_a_citation_file_declares_it(self) -> None:
        """CITATION.cff is YAML, and `license` is one of its standard keys."""
        cff = "cff-version: 1.2.0\ntitle: A study\nlicense: CC-BY-4.0\n"

        assert extract_deposit_licence(cff) == "CC-BY-4.0"

    def test_a_license_file_naming_a_url_is_read(self) -> None:
        """The filename declares that the content IS the licence, so a URI in it
        is the depositor's statement rather than a URL found in prose."""
        text = f"Creative Commons Attribution 4.0 International\n\n{self.CC_BY}\n"

        assert extract_deposit_licence(text, filename="LICENSE") == self.CC_BY

    def test_a_license_file_carrying_an_spdx_id_is_read(self) -> None:
        assert extract_deposit_licence("CC0-1.0\n", filename="COPYING") is None
        assert (
            extract_deposit_licence("SPDX-License-Identifier: CC0-1.0\n", filename="COPYING")
            == "CC0-1.0"
        )

    def test_legal_prose_alone_is_still_not_mined(self) -> None:
        """A LICENSE file holding only the legal text names no identifier.

        Reading "Creative Commons Attribution 4.0 International Public License"
        off the first line would be inventing a machine-actionable claim from a
        heading. Absent is honest (D5).
        """
        text = (
            "Creative Commons Attribution 4.0 International Public License\n\n"
            "By exercising the Licensed Rights, You accept and agree to be bound by\n"
            "the terms and conditions of this Public License.\n"
        )

        assert extract_deposit_licence(text, filename="LICENSE") is None

    def test_the_permissive_read_needs_the_filename(self) -> None:
        """The same URI in an unnamed text file is just a URL in prose."""
        text = f"See {self.CC_BY} for details.\n"

        assert extract_deposit_licence(text) is None
        assert extract_deposit_licence(text, filename="LICENCE.txt") == self.CC_BY


class TestXmlMetadataDeclaresItToo:
    """DataCite, OAI-DC and METS are XML, and repositories export them.

    Parsed through `defusedxml`: a deposit is untrusted input, and stdlib
    ElementTree will happily expand a billion-laughs entity out of one.
    """

    CC_BY = "https://creativecommons.org/licenses/by/4.0/legalcode"

    def test_a_datacite_record_declares_it_as_a_rights_uri_attribute(self) -> None:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <resource xmlns="http://datacite.org/schema/kernel-4">
          <identifier identifierType="DOI">10.1234/abcd</identifier>
          <rightsList>
            <rights rightsURI="{self.CC_BY}">Creative Commons Attribution 4.0</rights>
          </rightsList>
        </resource>"""

        assert extract_deposit_licence(xml) == self.CC_BY

    def test_a_namespaced_dublin_core_rights_element_is_read(self) -> None:
        xml = f"""<?xml version="1.0"?>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>A study</dc:title>
          <dc:rights>{self.CC_BY}</dc:rights>
        </oai_dc:dc>"""

        assert extract_deposit_licence(xml) == self.CC_BY

    def test_a_license_element_is_read(self) -> None:
        xml = f'<metadata><license>{self.CC_BY}</license></metadata>'

        assert extract_deposit_licence(xml) == self.CC_BY

    def test_a_uri_attribute_beats_the_elements_label(self) -> None:
        xml = f'<rightsList><rights rightsURI="{self.CC_BY}">CC BY 4.0</rights></rightsList>'

        assert extract_deposit_licence(xml) == self.CC_BY

    def test_a_label_with_no_uri_is_returned_verbatim(self) -> None:
        assert extract_deposit_licence("<resource><rights>CC-BY-4.0</rights></resource>") == (
            "CC-BY-4.0"
        )

    def test_xml_that_declares_nothing_yields_nothing(self) -> None:
        assert extract_deposit_licence("<resource><title>A study</title></resource>") is None

    def test_malformed_xml_is_not_a_declaration(self) -> None:
        assert extract_deposit_licence("<resource><rights>oops") is None

    def test_an_entity_bomb_is_refused_rather_than_expanded(self) -> None:
        """The reason this goes through defusedxml at all.

        Stdlib ElementTree expands these; a deposit is untrusted input, and one
        crafted file must not be able to exhaust memory during a scan.
        """
        bomb = """<?xml version="1.0"?>
        <!DOCTYPE lolz [
          <!ENTITY lol "lol">
          <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
          <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
        ]>
        <resource><rights>&lol3;</rights></resource>"""

        assert extract_deposit_licence(bomb) is None


class TestTheEngineReadsThoseToo:
    CC_BY = "https://creativecommons.org/licenses/by/4.0/"

    def _licence_over(self, tmp_path: Path) -> str | None:
        from builder.engine import AgentEngine

        engine = AgentEngine()
        engine.initialize(input_path=str(tmp_path))
        return engine.state.metadata.license

    def test_a_standalone_license_file_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "LICENSE").write_text(
            f"Creative Commons Attribution 4.0\n{self.CC_BY}\n", encoding="utf-8"
        )

        assert self._licence_over(tmp_path) == self.CC_BY

    def test_a_citation_file_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "CITATION.cff").write_text(
            "cff-version: 1.2.0\nlicense: CC-BY-4.0\n", encoding="utf-8"
        )

        assert self._licence_over(tmp_path) == "CC-BY-4.0"

    def test_a_datacite_xml_export_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "datacite.xml").write_text(
            '<resource><rightsList><rights rightsURI="'
            + self.CC_BY
            + '">CC BY 4.0</rights></rightsList></resource>',
            encoding="utf-8",
        )

        assert self._licence_over(tmp_path) == self.CC_BY

    def test_a_metadata_record_still_outranks_a_deeper_license_file(
        self, tmp_path: Path
    ) -> None:
        """Depth decides, so a root descriptor beats a nested LICENSE."""
        (tmp_path / "S-TEST1.json").write_text(
            _descriptor({"name": "License", "value": "CC-BY", "valqual": [
                {"name": "URL", "value": "https://creativecommons.org/licenses/by/4.0/legalcode"}
            ]}),
            encoding="utf-8",
        )
        nested = tmp_path / "code"
        nested.mkdir()
        (nested / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")

        assert self._licence_over(tmp_path) == (
            "https://creativecommons.org/licenses/by/4.0/legalcode"
        )
