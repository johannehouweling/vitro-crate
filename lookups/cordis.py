"""CORDIS lookup: an EU research grant by its grant agreement number.

CORDIS (https://cordis.europa.eu) is the European Commission's record of the
projects the EU's research framework programmes fund. A deposit often declares its
funding as a programme and a number ("EU Horizon 2020", ``825161``). The programme
is no organisation and has no ROR record, but the number is the project's id in
CORDIS, whose record states the framework programme that funds it, the European
Commission's contribution, and the grant's DOI. This client reads that record and
nothing else: whether it is the grant the deposit means is
``composites.resolve_declared_funders``' question.

A project is served as JSON at ``https://cordis.europa.eu/project/id/<id>?format=json``.
An id CORDIS does not hold is a 404.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from lookups._http import NOT_FOUND, TransientLookupError, http_get_json

BASE = "https://cordis.europa.eu/project/id/"

# A CORDIS project id, as the Bioregistry's `cordis.project` record states it.
_PROJECT_ID = re.compile(r"^\d+$")


@functools.lru_cache(maxsize=256)
def fetch_cordis_project(grant_id: str) -> dict[str, Any]:
    """The CORDIS project *grant_id* names, or ``{}`` when CORDIS holds none.

    Returns ``{id, acronym, title, url, grant_doi, framework_programmes,
    ec_contribution}``: the project's address in CORDIS, the DOI the EU
    Publications Office registered for the grant (``""`` for an older project that
    has none), the codes of the framework programmes the record lists it under
    (``H2020``, ``HORIZON``, ``FP7``), and the European Commission's maximum
    contribution (``""`` where the record states none).

    Raises:
        TransientLookupError: CORDIS could not be reached. A failure is not cached,
            so the question is asked again on a later call.
    """
    number = str(grant_id or "").strip()
    if not _PROJECT_ID.match(number):
        return {}
    try:
        data = http_get_json(f"{BASE}{number}", params={"format": "json"})
    except TransientLookupError:
        raise
    except Exception:
        return {}
    if data is NOT_FOUND or not isinstance(data, dict) or str(data.get("id") or "") != number:
        return {}
    associations = (data.get("relations") or {}).get("associations") or []
    programmes = sorted(
        {
            str(item["frameworkProgramme"])
            for item in associations
            if isinstance(item, dict)
            and item.get("contenttype") == "programme"
            and item.get("frameworkProgramme")
        }
    )
    identifiers = data.get("identifiers")
    identifiers = identifiers if isinstance(identifiers, dict) else {}
    return {
        "id": number,
        "acronym": str(data.get("acronym") or ""),
        "title": str(data.get("title") or ""),
        "url": f"{BASE}{number}",
        "grant_doi": str(identifiers.get("grantDoi") or ""),
        "framework_programmes": programmes,
        "ec_contribution": str(data.get("ecMaxContribution") or ""),
    }
