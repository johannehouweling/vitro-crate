"""
Cellosaurus REST API lookup.

Returns enriched, ontology-grounded cell-line metadata for a Cellosaurus
accession. Cellosaurus already cross-references each entry to standard
ontologies — species to NCBITaxon, disease to NCIt/ORDO, derived-from-site to
UBERON, plus a CLO (Cell Line Ontology) cross-reference — so this lookup pulls
those resolvable IRIs rather than discarding them: the values come back as
``schema:DefinedTerm`` objects carrying the obo PURL.
"""

from __future__ import annotations

import functools
import time

import requests

_BASE = "https://api.cellosaurus.org/cell-line"

# Cross-reference databases worth surfacing as schema:sameAs (those that carry a
# resolvable IRI: CLO/BTO are OBO ontologies; Wikidata is a global hub).
_SAMEAS_DBS = {"CLO", "BTO", "Wikidata", "Cell_Model_Passport", "DepMap"}


def _term(d: dict) -> dict | None:
    """A Cellosaurus xref/list entry -> a schema:DefinedTerm (only if it has an IRI)."""
    iri = (d or {}).get("iri")
    return {"@id": iri, "@type": "DefinedTerm", "name": d.get("label", "")} if iri else None


@functools.lru_cache(maxsize=256)
def lookup_cellosaurus(accession: str) -> dict:
    """Return enriched cell-line properties for the given Cellosaurus accession.

    Args:
        accession: Cellosaurus accession, e.g. "CVCL_0631"

    Returns:
        dict suitable for spreading onto a cell-line Sample. Keys may include:
        name, alternateName, url, identifier, taxonomicRange (NCBITaxon
        DefinedTerm), disease (list of NCIt/ORDO DefinedTerms), anatomicalSite
        (UBERON DefinedTerm), donorSex, donorAge, category, sameAs (CLO/… IRIs).
        Returns {} on failure so callers can spread safely.
    """
    try:
        time.sleep(0.1)
        r = requests.get(f"{_BASE}/{accession}?format=json", timeout=10)
        if r.status_code != 200:
            return {}

        data = r.json()
        # API wraps result: {"Cellosaurus": {"cell-line-list": [...]}}
        if "Cellosaurus" in data:
            cell_lines = data["Cellosaurus"].get("cell-line-list", [])
            data = cell_lines[0] if cell_lines else {}

        url = f"https://www.cellosaurus.org/{accession}"

        # name-list is a list of {"type": "identifier"/"synonym", "value": "..."}
        names: list = data.get("name-list", [])
        if isinstance(names, dict):
            names = names.get("name", [])
        primary = next((n for n in names if n.get("type") == "identifier"), None)
        name = primary.get("value", accession) if primary else accession
        alternate_names = [
            n.get("value", "") for n in names
            if n.get("type") == "synonym" and n.get("value")
        ]

        result: dict = {"name": name, "identifier": url, "url": url}
        if alternate_names:
            result["alternateName"] = alternate_names

        # species -> taxonomicRange (NCBITaxon DefinedTerm; label fallback)
        species_list = data.get("species-list", [])
        if isinstance(species_list, dict):
            species_list = [species_list]
        if species_list:
            result["taxonomicRange"] = _term(species_list[0]) or species_list[0].get("label", "")

        # disease(s) -> NCIt / ORDO DefinedTerms
        disease_list = data.get("disease-list", [])
        if isinstance(disease_list, dict):
            disease_list = [disease_list]
        diseases = [t for t in (_term(d) for d in disease_list) if t]
        if diseases:
            result["disease"] = diseases

        # derived-from-site -> UBERON DefinedTerm (anatomical origin)
        sites = data.get("derived-from-site-list", [])
        if isinstance(sites, dict):
            sites = [sites]
        for s in sites:
            site = (s or {}).get("site", {}) or {}
            term = next((_term(x) for x in (site.get("xref-list") or []) if x.get("iri")), None)
            if term:
                result["anatomicalSite"] = term
                break
            if site.get("value"):
                result.setdefault("anatomicalSite", site["value"])

        # donor / line facts (plain values; Cellosaurus does not ontologize these)
        if data.get("sex"):
            result["donorSex"] = data["sex"]
        if data.get("age"):
            result["donorAge"] = data["age"]
        if data.get("category"):
            result["category"] = data["category"]

        # cross-references -> sameAs (CLO and other resolvable hubs)
        same_as: list[str] = []
        for x in data.get("xref-list", []) or []:
            iri = x.get("iri")
            if x.get("database") in _SAMEAS_DBS and iri and iri not in same_as:
                same_as.append(iri)
        if same_as:
            result["sameAs"] = same_as[:8]

        return result

    except Exception:
        return {}
