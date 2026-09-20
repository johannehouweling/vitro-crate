#!/usr/bin/env python3
"""Fetch the published DSM indicator nanopublications into ``fair/dsm_nanopub_refs.yaml``.

The FAIRplus DSM indicator set is published on the nanopublication network as 83
nanopublications, one per indicator, under ``https://w3id.org/Data-Maturity/``. They
carry two mappings the vendored v1.2 assessment workbook has no column for —
``dsm:relatedFAIRPrinciple`` (35 of 83) and an ``rdfs:seeAlso`` to a T4FS OBO term
(73 of 83) — and nothing else this repo takes: their ``rdfs:label`` is a transcription
of the docs site, not of the workbook, and differs substantively from the workbook text
on 40 of the 83 indicators.

Retrieval is one unauthenticated GET against Nanopub Query. Two traps:

* **The namespace filter is load-bearing.** The set was published twice: a 2026-09-04
  batch under ``https://w3id.org/spaces/fairplus/r/dsm/``, retracted 83 of 83, and the
  live 2026-09-05 batch under ``https://w3id.org/Data-Maturity/``. Without
  ``FILTER(STRSTARTS(...))`` the query returns both, roughly double the rows.
* **An indicator has several ``rdfs:seeAlso`` values** — the docs-site anchor is one of
  them — so only the ``purl.obolibrary.org/obo/T4FS_`` one is taken.

There is no set-level or version IRI to pin to, so the pin is per indicator: the 83
trusty URIs are checked in, and a re-published batch shows up as a diff. Run::

    uv run python scripts/fetch_dsm_nanopub_refs.py

Like ``scripts/refresh_type_vocabulary.py``, this is a dev-time script that reaches the
network; nothing it touches runs at build time. ``gen_dsm_indicators.py`` and the
assessors read the vendored file off disk, so they stay offline (#117).
"""

from __future__ import annotations

import csv
import datetime
import io
import pathlib
from typing import Any

import requests
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "fair" / "dsm_nanopub_refs.yaml"

ENDPOINT = "https://query.knowledgepixels.com/repo/full"
PUBLISHED_TOTAL = 83
FAIR_TERMS = "https://w3id.org/fair/principles/terms/"

QUERY = """\
PREFIX dsm: <https://w3id.org/spaces/fairplus/r/dsm/>
PREFIX np: <http://www.nanopub.org/nschema#>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?nanopub ?code ?created ?signer
       (GROUP_CONCAT(DISTINCT STR(?principle); separator=" ") AS ?principles)
       (GROUP_CONCAT(DISTINCT STR(?term); separator=" ") AS ?t4fs)
WHERE {
  GRAPH ?assertion {
    ?ind dsm:indicatorCode ?code .
    OPTIONAL { ?ind dsm:relatedFAIRPrinciple ?principle }
    OPTIONAL {
      ?ind rdfs:seeAlso ?term .
      FILTER(STRSTARTS(STR(?term), "http://purl.obolibrary.org/obo/T4FS_"))
    }
  }
  GRAPH ?head { ?nanopub np:hasAssertion ?assertion }
  GRAPH ?pubinfo {
    ?nanopub dct:created ?created ; dct:creator ?key .
    ?key foaf:name ?signer .
  }
  FILTER(STRSTARTS(STR(?ind), "https://w3id.org/Data-Maturity/DSM"))
}
GROUP BY ?nanopub ?code ?created ?signer
ORDER BY ?code
"""

_HEADER = """\
# VENDORED FETCH RESULT — do not edit by hand.
#
# Refresh with:  uv run python scripts/fetch_dsm_nanopub_refs.py
#
# The published FAIRplus DSM indicator nanopublications, one per indicator: the trusty
# URI of the nanopublication that asserted it, its FAIR-principle mapping and its T4FS
# ontology term. Definitions (text, level, category, granularity, cross-references) are
# NOT taken from here — they come from the vendored v1.2 workbook, whose wording these
# nanopublications do not reproduce. scripts/gen_dsm_indicators.py merges this file into
# fair/dsm_indicators.yaml.
"""


class _BlockStringDumper(yaml.SafeDumper):
    """Dumps the recorded query as a literal block, not a ``\\n``-escaped one-liner.

    A subclass rather than a representer on ``yaml.SafeDumper`` itself: that one is a
    process-global, so registering there would change how every ``yaml.safe_dump`` in
    the interpreter renders strings the moment anything imports this module.
    """


_BlockStringDumper.add_representer(
    str,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    ),
)


def _one(values: set[str]) -> Any:
    """A batch-wide constant, or the sorted values when the batch is not uniform.

    A second timestamp or signing key means the 83 nanopublications are not one batch,
    which the header must show rather than hide behind whichever value sorts first.
    """
    if len(values) == 1:
        return next(iter(values))
    print(f"note: not a uniform batch, recording all {len(values)} values: {sorted(values)}")
    return sorted(values)


def main() -> None:
    response = requests.get(
        ENDPOINT, params={"query": QUERY}, headers={"Accept": "text/csv"}, timeout=60
    )
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))

    if len(rows) != PUBLISHED_TOTAL:
        raise SystemExit(
            f"{len(rows)} indicators returned, expected {PUBLISHED_TOTAL} — "
            f"refusing to write {OUT.relative_to(REPO)}"
        )

    indicators: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: r["code"]):
        entry: dict[str, Any] = {"nanopub": row["nanopub"]}
        if row["principles"]:
            entry["fair_principles"] = sorted(
                term.removeprefix(FAIR_TERMS) for term in row["principles"].split()
            )
        if row["t4fs"]:
            # GROUP_CONCAT joins on a space, so a second term would vendor as one
            # space-joined pseudo-IRI. No indicator carries two today; refuse rather
            # than write a corrupt string if a re-published batch ever does.
            terms = row["t4fs"].split()
            if len(terms) > 1:
                raise SystemExit(f"{row['code']} carries {len(terms)} T4FS terms: {terms}")
            entry["t4fs_ref"] = terms[0]
        indicators[row["code"]] = entry

    data = {
        "source": {
            "name": "FAIRplus DSM indicator nanopublications",
            "endpoint": ENDPOINT,
            "query": QUERY,
            "created": _one({r["created"] for r in rows}),
            "license": "CC-BY-4.0",
            "signed_by": _one({r["signer"] for r in rows}),
            "fair_principles_vocabulary": FAIR_TERMS,
            "retrieved": datetime.date.today().isoformat(),
        },
        "indicators": indicators,
    }
    body = yaml.dump(
        data, Dumper=_BlockStringDumper, sort_keys=False, allow_unicode=True, width=100
    )
    OUT.write_text(_HEADER + "\n" + body)
    print(
        f"Wrote {OUT.relative_to(REPO)} ({len(indicators)} indicators, "
        f"{sum(1 for e in indicators.values() if 'fair_principles' in e)} with a FAIR "
        f"principle, {sum(1 for e in indicators.values() if 't4fs_ref' in e)} with a "
        "T4FS term)."
    )


if __name__ == "__main__":
    main()
