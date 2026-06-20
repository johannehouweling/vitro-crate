"""
PubChem REST API lookup for chemical compounds.

Replaces the Node.js-based CompoundCloud lookup from enrich_isa/lookups.py
with a pure-Python implementation using the free PubChem PUG REST API.
No API key required. Rate limit: ~5 requests/second.
"""

from __future__ import annotations

import functools
import re
import time
from urllib.parse import quote

import requests

_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"
_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")


@functools.lru_cache(maxsize=512)
def lookup_pubchem(name: str) -> dict:
    """Fetch chemical identifiers from PubChem by compound name.

    Returns a dict with keys: cas, smiles, inchikey, inchi, formula, mass,
    iupac_name, pubchem_cid. Returns an empty dict if the compound is not found
    or the request fails.
    """
    try:
        r = requests.get(f"{_BASE}/{quote(name)}/JSON", timeout=10)
        if r.status_code != 200:
            return {}

        compound = r.json()["PC_Compounds"][0]
        cid = str(compound["id"]["id"]["cid"])

        props: dict = {}
        for p in compound.get("props", []):
            urn = p.get("urn", {})
            label = urn.get("label", "")
            name_key = urn.get("name", "")
            val = p.get("value", {})
            if label == "SMILES" and name_key == "Canonical":
                props["smiles"] = val.get("sval", "")
            elif label == "InChIKey":
                props["inchikey"] = val.get("sval", "")
            elif label == "InChI" and name_key == "Standard":
                props["inchi"] = val.get("sval", "")
            elif label == "Molecular Formula":
                props["formula"] = val.get("sval", "")
            elif label == "Molecular Weight":
                props["mass"] = val.get("sval", "") or (str(val["fval"]) if "fval" in val else "")
            elif label == "IUPAC Name":
                # PubChem returns several IUPAC names; prefer the Preferred one,
                # else keep the first seen.
                if name_key == "Preferred":
                    props["iupac_name"] = val.get("sval", "")
                else:
                    props.setdefault("iupac_name", val.get("sval", ""))

        # CAS numbers appear in the synonyms list
        time.sleep(0.2)  # stay under rate limit
        sr = requests.get(
            f"{_BASE}/{quote(name)}/synonyms/JSON", timeout=10
        )
        cas = ""
        if sr.status_code == 200:
            synonyms = (
                sr.json()
                .get("InformationList", {})
                .get("Information", [{}])[0]
                .get("Synonym", [])
            )
            cas = next((s for s in synonyms if _CAS_PATTERN.match(s)), "")

        return {
            "cas": cas,
            "smiles": props.get("smiles", ""),
            "inchikey": props.get("inchikey", ""),
            "inchi": props.get("inchi", ""),
            "formula": props.get("formula", ""),
            "mass": props.get("mass", ""),
            "iupac_name": props.get("iupac_name", ""),
            "pubchem_cid": cid,
        }

    except Exception:
        return {}
