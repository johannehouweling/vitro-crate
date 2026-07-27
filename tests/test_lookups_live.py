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
