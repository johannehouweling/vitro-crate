"""Opt-in live checks against the real lookup APIs.

Every test here is skipped unless ``VITRO_LIVE_LOOKUPS`` is set, so CI never
touches the network. They exist because the offline suites replay *recorded*
bytes: if Cellosaurus re-ranks its Solr results, a cell-line name can silently
stop resolving while every recorded-payload test stays green. This module is the
only guard against that, and it has to be run deliberately::

    VITRO_LIVE_LOOKUPS=1 uv run pytest tests/test_lookups_live.py -v
"""

from __future__ import annotations

import os

import pytest

from builder.tools.lookups import lookup_cell_line_by_name
from lookups.cellosaurus import search_cellosaurus

live_only = pytest.mark.skipif(
    not os.environ.get("VITRO_LIVE_LOOKUPS"),
    reason="live network test; set VITRO_LIVE_LOOKUPS=1 to run",
)

# Cell-line names common in in-vitro toxicology, paired with the accession
# Cellosaurus holds for each. Nine of these resolved to nothing before #385
# split the name search across the ``id`` and ``sy`` Solr fields.
RECALL_PANEL = (
    ("FRTL-5", "CVCL_0265"),
    ("Caco-2", "CVCL_0025"),
    ("HaCaT", "CVCL_0038"),
    ("RPTEC/TERT1", "CVCL_K278"),
    ("LLC-PK1", "CVCL_0391"),
    ("SH-SY5Y", "CVCL_0019"),
    ("3T3-L1", "CVCL_0123"),
    ("Hep-G2", "CVCL_0027"),
    ("CHO-K1", "CVCL_0214"),
    ("CHO K1", "CVCL_0214"),
    ("HepG2", "CVCL_0027"),
    ("HEPG2", "CVCL_0027"),
    ("A549", "CVCL_0023"),
    ("HEK293", "CVCL_0045"),
    ("MCF-7", "CVCL_0031"),
    ("MDCK", "CVCL_0422"),
    ("HepaRG", "CVCL_9720"),
)


@live_only
def test_cellosaurus_recall_panel_live():
    """Every panel name resolves to its accession against the live API."""
    search_cellosaurus.cache_clear()
    lookup_cell_line_by_name.cache_clear()

    resolved = {name: lookup_cell_line_by_name(name) for name, _ in RECALL_PANEL}
    actual = {
        name: (result["data"].get("accession") if result["found"] else None)
        for name, result in resolved.items()
    }

    assert actual == {name: accession for name, accession in RECALL_PANEL}


@live_only
def test_the_compound_wiki_property_numbers_still_mean_what_they_did():
    """Wikibase property ids are instance-local and editable.

    `lookups/compoundwiki.py` hard-codes P10 as InChIKey, P12 as SMILES and so
    on, recorded against this one wiki. The offline contract test replays a
    recorded payload, so a renumbering there would leave it green while every
    real lookup silently returned "" for a structure. Only this check notices.
    """
    from lookups.compoundwiki import lookup_compound_wiki

    got = lookup_compound_wiki("caffeine")
    assert got, "caffeine no longer resolves in the Compound Wiki"
    assert got["compoundwiki_id"].startswith("Q")
    assert got["@id"] == f"https://compoundcloud.wikibase.cloud/entity/{got['compoundwiki_id']}"
    assert got["inchikey"] == "RYYVLZVUVIJVGH-UHFFFAOYSA-N", got
    assert got["formula"] == "C8H10N4O2", got
    assert got["cas"] == "58-08-2", got


_OLS_TERMS = "https://www.ebi.ac.uk/ols4/api/terms"
_TERM_BASE = {"EFO": "http://www.ebi.ac.uk/efo/EFO_", "BAO": "http://www.bioassayontology.org/bao#BAO_"}


def _ols_term(iri: str) -> dict:
    """The defining ontology's record for *iri* in OLS4, or {} when OLS has none."""
    from lookups._http import NOT_FOUND, http_get_json

    data = http_get_json(_OLS_TERMS, params={"iri": iri})
    terms = [] if data is NOT_FOUND else (data or {}).get("_embedded", {}).get("terms", [])
    return next((t for t in terms if t.get("is_defining_ontology")), terms[0] if terms else {})


def _profile_terms() -> set[str]:
    """Every ontology term the shapes, the spec and the builder's mappings name."""
    import re
    from pathlib import Path

    root = Path(__file__).parents[1]
    text = "\n".join(
        p.read_text()
        for p in [
            *(root / "profiles" / "shapes" / "tox").glob("*.ttl"),
            root / "profiles" / "docs" / "isa_tox.md",
            root / "profiles" / "models" / "tox.py",
            root / "builder" / "tools" / "_crate_mapping.py",
        ]
    )
    terms = set(
        re.findall(
            r"http://purl\.obolibrary\.org/obo/[A-Za-z]+_[A-Z]?[0-9]+"
            r"|http://www\.ebi\.ac\.uk/efo/EFO_[0-9]+"
            r"|http://www\.bioassayontology\.org/bao#BAO_[0-9]+",
            text,
        )
    )
    for prefix, number in re.findall(
        r"\b(EFO|OBI|BAO|NCIT|UBERON|GO|CL|PATO|CHEBI|MSIO):(C?[0-9]{4,})\b", text
    ):
        terms.add(_TERM_BASE.get(prefix, f"http://purl.obolibrary.org/obo/{prefix}_") + number)
    return terms


@live_only
def test_every_ontology_term_the_profile_names_exists_and_is_current():
    """A misspelt or retired term is consistent everywhere it is copied, so no
    offline test can see it; only the ontology itself can."""
    problems = []
    for iri in sorted(_profile_terms()):
        term = _ols_term(iri)
        if not term:
            problems.append(f"missing: {iri}")
        elif term.get("is_obsolete"):
            problems.append(f"obsolete: {iri} ({term.get('label')})")
    assert not problems, problems


@live_only
def test_each_characteristic_term_means_its_characteristic():
    """Existence is not meaning: BAO:0002648 resolves, to "one cell type", and
    stood in for the growth conditions until #856."""
    from builder.tools._crate_mapping import _CELL_LINE_CHARACTERISTICS

    for c in _CELL_LINE_CHARACTERISTICS:
        label = _ols_term(c.property_id).get("label", "")
        assert c.name.lower() in label.lower(), (c.name, c.property_id, label)
