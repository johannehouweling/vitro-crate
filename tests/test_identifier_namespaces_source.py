"""The vendored Bioregistry records, and the patterns derived from them.

`profiles/vocabulary/identifier_namespaces.json` is written by
`scripts/refresh_identifier_namespaces.py`, which reads the `bioregistry`
package and is run by hand. These tests read only the committed file, so they
stay offline (#117); what they hold is that the file says where it came from,
that it carries every prefix the script asks for, and that the patterns
`profiles.identifier_namespaces` derives from it mean what the shapes need them
to mean.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from profiles import identifier_namespaces as ns
from profiles.ontology_iris import PREFIXES

_REPO = Path(ns.__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "refresh_identifier_namespaces.py"


def _script():
    spec = importlib.util.spec_from_file_location("refresh_identifier_namespaces", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSnapshotSaysWhereItCameFrom:
    def test_the_source_is_named_versioned_and_dated(self):
        source = ns.snapshot()["source"]
        assert source["name"] == "Bioregistry"
        assert source["package"] == "bioregistry"
        assert re.fullmatch(r"\d+\.\d+\.\d+", source["version"]), source["version"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", source["retrieved"])
        assert "CC0-1.0" in source["license"]
        assert source["citation"] == "https://doi.org/10.1038/s41597-022-01807-3"

    def test_it_holds_exactly_the_prefixes_the_script_asks_for(self):
        assert set(ns.snapshot()["prefixes"]) == set(_script().PREFIXES)

    def test_every_record_names_its_registry_page(self):
        for prefix, entry in ns.snapshot()["prefixes"].items():
            assert entry["record"] == f"{ns.REGISTRY_RECORD}{prefix}"

    def test_a_decision_says_it_is_one_and_why(self):
        for prefix, entry in ns.snapshot().get("decisions", {}).items():
            assert prefix not in ns.snapshot()["prefixes"], prefix
            assert "NOT a Bioregistry record" in entry["source"]
            assert len(entry["rationale"]) > 80


class TestEveryPatternIsUsable:
    @pytest.mark.parametrize("prefix", sorted(ns.records()))
    def test_it_compiles_and_stays_in_the_shacl_dialect(self, prefix):
        for pattern in ns.derived_patterns(prefix):
            re.compile(pattern)
            assert "(?" not in pattern, pattern

    @pytest.mark.parametrize("prefix", sorted(ns.records()))
    def test_its_uri_format_has_one_placeholder(self, prefix):
        assert ns.record(prefix)["uri_format"].count("$1") == 1


class TestTheOntologyNamespacesAreTheRegistrys:
    """`profiles.ontology_iris.PREFIXES` is typed by hand, and every term IRI the
    builder writes is built from it. It must agree with the registry."""

    @pytest.mark.parametrize("curie_prefix", sorted(PREFIXES))
    def test_the_namespace_is_the_records_uri_prefix(self, curie_prefix):
        assert PREFIXES[curie_prefix] == ns.uri_prefix(curie_prefix.lower())


# What the derivation must accept and refuse, on identifiers the builder writes.
_IRI_CASES = {
    "orcid": (
        ["https://orcid.org/0000-0003-4766-7358", "https://orcid.org/0000-0002-1825-009X"],
        ["https://orcid.org/0000-0002-1825-009", "http://orcid.org/0000-0003-4766-7358",
         "https://orcid.org", "https://example.org/crate/#Person_jane"],
    ),
    "ror": (
        ["https://ror.org/05gq02987", "https://ror.org/04pp8hn57"],
        ["https://ror.org/brown", "https://ror.org/05gq0298", "#Organization_x"],
    ),
    "aop": (["https://aopwiki.org/aops/610"], ["https://aopwiki.org/events/2258"]),
    "aop.events": (["https://aopwiki.org/events/2258"], ["https://aopwiki.org/events/2258.json"]),
    "cellosaurus": (
        ["https://www.cellosaurus.org/CVCL_0265"],
        ["https://www.cellosaurus.org/HeLa", "https://web.expasy.org/cellosaurus/CVCL_0265"],
    ),
    "pubchem.compound": (
        ["https://pubchem.ncbi.nlm.nih.gov/compound/441764"],
        ["https://pubchem.ncbi.nlm.nih.gov/compound", "https://pubchem.ncbi.nlm.nih.gov/compound/aspirin"],
    ),
    "doi": (
        ["https://doi.org/10.1016/j.tiv.2023.105770", "https://doi.org/10.1007/s00204-024-03787-2"],
        ["https://doi.org/paper"],
    ),
    "bao": (["http://www.bioassayontology.org/bao#BAO_0000523"], ["http://www.bioassayontology.org/bao#BAO_523"]),
    "ncit": (
        ["http://purl.obolibrary.org/obo/NCIT_C16403", "http://purl.obolibrary.org/obo/NCIT_C172259"],
        ["http://purl.obolibrary.org/obo/ncit.owl", "http://purl.obolibrary.org/obo/NCIT_16403"],
    ),
    "ncbitaxon": (
        ["http://purl.obolibrary.org/obo/NCBITaxon_9606"],
        ["http://purl.obolibrary.org/obo/NCBITaxon_", "http://purl.obolibrary.org/obo/NCBITaxon_96-06"],
    ),
    "compoundwiki": (
        ["https://compoundcloud.wikibase.cloud/entity/Q2026"],
        ["https://compoundcloud.wikibase.cloud/entity/2026"],
    ),
}


class TestTheDerivedPatterns:
    @pytest.mark.parametrize("prefix", sorted(_IRI_CASES))
    def test_an_iri_pattern_accepts_the_real_and_refuses_the_malformed(self, prefix):
        accepted, refused = _IRI_CASES[prefix]
        pattern = re.compile(ns.iri_pattern(prefix))
        assert all(pattern.search(iri) for iri in accepted), accepted
        assert not any(pattern.search(iri) for iri in refused), refused

    def test_an_alternation_is_grouped_so_it_spans_the_whole_iri(self):
        # Ungrouped, `^…NCBITaxon_(\d+)|([a-zA-Z_]+)$` would accept any IRI ending in a letter.
        assert not re.search(ns.iri_pattern("ncbitaxon"), "https://example.org/anything")

    def test_only_regex_metacharacters_are_escaped(self):
        assert ns.iri_pattern("bao") == r"^http://www\.bioassayontology\.org/bao#BAO_(\d{7})$"
        assert "\\#" not in ns.iri_pattern("bao") and "\\/" not in ns.iri_pattern("orcid")

    def test_a_cellosaurus_accession_is_written_with_its_banana_or_as_an_rrid(self):
        pattern = ns.accession_pattern("cellosaurus", also_under="rrid")
        assert pattern == r"^(RRID:)?CVCL_([A-Z0-9]{4})$"
        assert [bool(re.search(pattern, v)) for v in ("CVCL_0440", "RRID:CVCL_0440", "HeLa", "CVCL_04400")] == [
            True, True, False, False,
        ]

    def test_a_value_pattern_is_the_registrys_own_anchored_whole(self):
        assert ns.record("cas")["pattern"] == r"^\d{1,7}\-\d{2}\-\d$"
        assert ns.value_pattern("cas") == r"^(\d{1,7}\-\d{2}\-\d)$"
        assert re.search(ns.value_pattern("cas"), "33889-69-9")
        assert not re.search(ns.value_pattern("cas"), "33889699")
        assert re.search(ns.value_pattern("inchikey"), "XQZYPMVTSDWCCE-UHFFFAOYSA-N")
        assert not re.search(ns.value_pattern("doi"), "https://doi.org/10.1234/paper")

    def test_an_unanchored_alternation_is_anchored_whole(self):
        # NCBITaxon's registry pattern `^(\d+)|([a-zA-Z_]+)$` accepts "12 and anything".
        assert re.search(ns.record("ncbitaxon")["pattern"], "12 and anything")
        assert not re.search(ns.value_pattern("ncbitaxon"), "12 and anything")
        assert re.search(ns.value_pattern("ncbitaxon"), "9606")

    def test_a_namespace_pattern_asks_only_whether_the_iri_is_in_it(self):
        assert ns.namespace_pattern("orcid") == r"^https://orcid\.org/"
        assert re.search(ns.namespace_pattern("orcid"), "https://orcid.org/anything")
        assert not re.search(ns.namespace_pattern("orcid"), "#Person_ada")

    def test_only_a_real_pairing_is_a_derivation(self):
        assert r"^(RRID:)?CVCL_([A-Z0-9]{4})$" in ns.derived_patterns("cellosaurus")
        assert not any("CHEBI" in p for p in ns.derived_patterns("cellosaurus"))
        with pytest.raises(KeyError):
            ns.accession_pattern("cellosaurus", also_under="chebi")

    def test_a_record_page_names_its_prefix(self):
        assert ns.prefix_of_record("https://bioregistry.io/registry/orcid") == "orcid"
        assert ns.prefix_of_record("https://compoundcloud.wikibase.cloud") == "compoundwiki"
        assert ns.prefix_of_record("https://bioregistry.io/registry/nope") is None

    def test_an_unknown_prefix_is_an_error(self):
        with pytest.raises(KeyError):
            ns.record("nope")


class TestTheChecksTables:
    """`NAMESPACE_CHECKS` and `VALUE_CHECKS` are what the shapes are generated from."""

    def test_every_checked_namespace_is_vendored(self):
        assert set(ns.NAMESPACE_CHECKS) <= set(ns.records())
        assert set(ns.VALUE_CHECKS.values()) <= set(ns.records())

    @pytest.mark.parametrize("prefix", sorted(ns.records()))
    def test_every_record_has_an_example_its_pattern_accepts(self, prefix):
        entry = ns.record(prefix)
        assert re.search(ns.value_pattern(prefix), entry["example"]), entry["example"]

    @pytest.mark.parametrize("prefix", list(ns.NAMESPACE_CHECKS))
    def test_the_example_iri_is_in_its_namespace_and_well_formed(self, prefix):
        iri = ns.example_iri(prefix)
        assert ns.namespace_of(iri) == prefix
        assert re.search(ns.iri_pattern(prefix), iri), iri

    def test_no_namespace_start_contains_another(self):
        # Otherwise an @id would be in two namespaces and `namespace_of` would pick one.
        starts = [ns.namespace_start(p) for p in ns.NAMESPACE_CHECKS]
        assert [(a, b) for a in starts for b in starts if a != b and b.startswith(a)] == []

    def test_a_cellosaurus_address_is_selected_whatever_follows_the_host(self):
        # `…/HeLa` is the address to report, so it must be in the namespace.
        assert ns.namespace_of("https://www.cellosaurus.org/HeLa") == "cellosaurus"

    def test_what_is_in_no_checked_namespace_is_none(self):
        for iri in (
            "#Person_ada",
            "https://orcid.org",
            "http://purl.obolibrary.org/obo/ncit.owl",
            "https://example.org/x",
            None,
        ):
            assert ns.namespace_of(iri) is None, iri
