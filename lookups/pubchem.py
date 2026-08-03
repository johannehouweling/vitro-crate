"""
PubChem REST API lookup for chemical compounds.

Replaces the Node.js-based CompoundCloud lookup from enrich_isa/lookups.py
with a pure-Python implementation using the free PubChem PUG REST API.
No API key required. Rate limit: ~5 requests/second.
"""

from __future__ import annotations

import functools
import re
from urllib.parse import quote

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"
_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")

# PubChem returns several SMILES flavours under the one ``SMILES`` label, keyed
# by ``urn.name``, and has renamed them: the old ``Canonical`` was retired in
# favour of ``Absolute`` (with stereochemistry) and ``Connectivity`` (without).
# Lower rank wins. ``Absolute`` is preferred because we store a *stereochemical*
# InChI alongside it, and a flat SMILES next to a stereo InChI describes two
# different molecules. ``Canonical`` is kept so an older cached or mirrored
# response still parses.
_SMILES_PREFERENCE: dict[str, int] = {"Absolute": 0, "Canonical": 1, "Connectivity": 2}


@functools.lru_cache(maxsize=512)
def lookup_pubchem(name: str) -> dict:
    """Fetch chemical identifiers from PubChem by compound name.

    Returns a dict with keys: cas, smiles, inchikey, inchi, formula, mass,
    iupac_name, pubchem_cid. Returns an empty dict if the compound is not
    found. Raises TransientLookupError if the compound request fails
    transiently (timeout / connection / 429 / 5xx); the synonyms request is
    best-effort and never fatal.
    """
    try:
        compound_data = http_get_json(f"{_BASE}/{quote(name)}/JSON")
        if compound_data is NOT_FOUND:
            return {}

        compound = compound_data["PC_Compounds"][0]
        cid = str(compound["id"]["id"]["cid"])

        props: dict = {}
        _smiles_rank = len(_SMILES_PREFERENCE)
        for p in compound.get("props", []):
            urn = p.get("urn", {})
            label = urn.get("label", "")
            name_key = urn.get("name", "")
            val = p.get("value", {})
            if label == "SMILES":
                # PubChem ships several SMILES variants under one label and has
                # renamed them before; take the most preferred one present rather
                # than matching a single spelling. Matching only "Canonical" is
                # what silently emptied this field for every compound (#425).
                rank = _SMILES_PREFERENCE.get(name_key)
                if rank is not None and rank < _smiles_rank:
                    props["smiles"] = val.get("sval", "")
                    _smiles_rank = rank
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

        # CAS numbers appear in the synonyms list. This is best-effort
        # enrichment: a transient/absent synonyms response must not lose the
        # compound we already resolved.
        cas = ""
        try:
            syn_data = http_get_json(f"{_BASE}/{quote(name)}/synonyms/JSON")
            if syn_data is not NOT_FOUND:
                synonyms = (
                    syn_data.get("InformationList", {})
                    .get("Information", [{}])[0]
                    .get("Synonym", [])
                )
                cas = next((s for s in synonyms if _CAS_PATTERN.match(s)), "")
        except TransientLookupError:
            pass  # synonyms are optional; keep the compound result

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

    except TransientLookupError:
        raise
    except Exception:
        return {}
