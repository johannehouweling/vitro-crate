"""``resolve_cell_line`` — the deterministic arm's name→Cellosaurus path (#372).

Every test drives the **real** composite over the **real** lookup stack
(``lookup_cell_line_by_name`` → ``search_cellosaurus`` → ``_candidate`` →
``cell_line_names_match``, and ``lookup_cell_line`` → ``lookup_cellosaurus``),
with only the HTTP layer replayed by ``responses`` from fixtures recorded
verbatim from ``api.cellosaurus.org``. Nothing here stubs the D5 exact+unique
gate, which is the point: the composite's job is to choose *which name to ask
about*, never to relax *what counts as an answer*.

Two payload provenances, both honest about what they are:

* the search bodies and the ``CVCL_0027`` record body are recorded fixtures,
  already committed for #385 and the lookup contract tests;
* the ``CVCL_0265`` record body is assembled **here** from the name-list recorded
  in ``cellosaurus_search_id_frtl5.json``. That is real recorded data re-served
  on the record endpoint; it pins the composite's *use* of the record, not the
  endpoint's shape (``tests/test_lookups_contract.py`` owns that).

The suite-wide ``_stub_composites_cellosaurus`` fixture defaults both primitives
in the ``composites`` namespace to a miss; this module puts the real ones back,
because a stub at that seam would leave the gate untested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses
from requests.exceptions import Timeout
from responses import matchers

import builder.tools.composites as composites
from builder.state import CrateState
from builder.tools.composites import resolve_cell_line
from builder.tools.lookups import lookup_cell_line, lookup_cell_line_by_name
from lookups._http import reset_circuit_breaker, reset_host_throttle
from lookups.cellosaurus import lookup_cellosaurus, search_cellosaurus

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://api.cellosaurus.org/search/cell-line"
RECORD_URL = "https://api.cellosaurus.org/cell-line"
EMPTY_SEARCH: dict[str, Any] = {"Cellosaurus": {"cell-line-list": []}}

# The name as the S-VHPS22 documents word it, and the short catalogue name that
# is the only thing Cellosaurus will match on.
FRTL5_DESCRIPTIVE = "FRTL-5 TPO-overexpressing rat thyroid follicular cells"
FRTL5_CATALOG = "FRTL-5"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _recorded_name_list(fixture: str, accession: str) -> list[dict]:
    """The name-list recorded for *accession* in a committed search fixture."""
    for entry in _load(fixture)["Cellosaurus"]["cell-line-list"]:
        values = [a.get("value") for a in entry.get("accession-list", [])]
        if accession in values:
            return entry["name-list"]
    raise AssertionError(f"{accession} is not in {fixture} — fixture drifted")


def _record_body(name_list: list[dict]) -> dict:
    """A record-endpoint envelope around a recorded name-list."""
    return {"Cellosaurus": {"cell-line-list": [{"name-list": name_list}]}}


def _route_search(query: str, **kwargs) -> None:
    """Register a search response for exactly one Solr ``q`` value."""
    responses.add(
        responses.GET,
        SEARCH_URL,
        match=[matchers.query_param_matcher({"q": query}, strict_match=False)],
        **kwargs,
    )


def _route_record(accession: str, **kwargs) -> None:
    responses.add(responses.GET, f"{RECORD_URL}/{accession}?format=json", **kwargs)


def _route_frtl5_search() -> None:
    """The two-field union for both FRTL-5 spellings.

    The descriptive phrase is routed to an EMPTY body, which is what the live API
    returns for it — that miss is the whole reason ``catalog_name`` exists.
    """
    for field in ("id", "sy"):
        _route_search(f'{field}:"{FRTL5_DESCRIPTIVE}"', json=EMPTY_SEARCH)
    _route_search(f'id:"{FRTL5_CATALOG}"', json=_load("cellosaurus_search_id_frtl5.json"))
    _route_search(f'sy:"{FRTL5_CATALOG}"', json=_load("cellosaurus_search_sy_frtl5.json"))


def _route_frtl5_record(**kwargs) -> None:
    if not kwargs:
        kwargs = {
            "json": _record_body(
                _recorded_name_list("cellosaurus_search_id_frtl5.json", "CVCL_0265")
            ),
            "status": 200,
        }
    _route_record("CVCL_0265", **kwargs)


def _route_hepg2(*, name: str = "HepG2") -> None:
    """The recorded HepG2 union plus the recorded CVCL_0027 record body."""
    _route_search(f'id:"{name}"', json=_load("cellosaurus_search_id_hepg2.json"))
    _route_search(f'sy:"{name}"', json=_load("cellosaurus_search_sy_hepg2.json"))
    _route_record("CVCL_0027", json=_load("cellosaurus_hepg2.json"), status=200)


@pytest.fixture(autouse=True)
def _real_cellosaurus_primitives(monkeypatch):
    """Undo the suite-wide offline stub — this module tests the real stack.

    ``tests/conftest.py`` defaults ``composites.lookup_cell_line_by_name`` /
    ``composites.lookup_cell_line`` to a miss so no other module can reach the
    network through the newly-wired composite. Here the network is already
    intercepted by ``responses``, and stubbing the primitives would mean the D5
    gate, the id∪sy union and the record parse are never exercised at all.
    """
    monkeypatch.setattr(composites, "lookup_cell_line_by_name", lookup_cell_line_by_name)
    monkeypatch.setattr(composites, "lookup_cell_line", lookup_cell_line)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Four ``lru_cache``s sit between the composite and the socket."""
    for fn in (search_cellosaurus, lookup_cellosaurus, lookup_cell_line_by_name, lookup_cell_line):
        fn.cache_clear()
    reset_host_throttle()
    reset_circuit_breaker()
    yield
    for fn in (search_cellosaurus, lookup_cellosaurus, lookup_cell_line_by_name, lookup_cell_line):
        fn.cache_clear()
    reset_circuit_breaker()


def _cell_lines(state: CrateState) -> list:
    return list(state.list_entities("CellLineSample"))


def _entity(state: CrateState, entity_id: str):
    """The entity a resolve returned, asserted present (the composite always mints)."""
    entity = state.get_entity(entity_id)
    assert entity is not None, f"resolve_cell_line reported {entity_id} but minted nothing"
    return entity


class TestResolveCellLine:
    """The composite's contract: always mint, never fabricate."""

    @responses.activate
    def test_exact_name_resolves_to_the_looked_up_accession(self):
        """A name Cellosaurus knows verbatim resolves and verifies in one call."""
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_CATALOG)

        assert result["accession"] == "CVCL_0265"
        assert result["match"] == "exact"
        assert result["query"] == FRTL5_CATALOG
        assert result["verified"] is True
        assert result["source"] == "cellosaurus"

        entity = _entity(state, result["entity_id"])
        assert entity.fields["accession"] == "CVCL_0265"
        assert entity._completion["CellLineSample:accession"].status == "verified"
        assert "cellosaurus" in entity._provenance.lookups_used

    @responses.activate
    def test_a_hint_accession_is_never_committed(self):
        """Honesty control: a caller-supplied accession is refused outright (D5).

        The ReAct arm's ``hints`` are model-authored, and a model that cannot
        resolve a name has every incentive to reach for the nearest CVCL id in
        its context (#383). The value committed must be the one the lookup
        returned — and when the lookup returns nothing, the field must be absent
        rather than falling back to the hint.
        """
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()

        hit = resolve_cell_line(state, FRTL5_CATALOG, hints={"accession": "CVCL_9999"})

        assert hit["accession"] == "CVCL_0265"
        assert _entity(state, hit["entity_id"]).fields["accession"] == "CVCL_0265"

        miss_state = CrateState()
        for field in ("id", "sy"):
            _route_search(f'{field}:"Unlisted line"', json=EMPTY_SEARCH)

        miss = resolve_cell_line(miss_state, "Unlisted line", hints={"accession": "CVCL_9999"})

        assert miss["accession"] == ""
        assert "accession" not in _entity(miss_state, miss["entity_id"]).fields

    @responses.activate
    def test_catalog_name_resolves_when_the_descriptive_name_misses(self):
        """The marquee case: a descriptive phrase reaches its record via the catalogue name.

        The documents call the line "FRTL-5 TPO-overexpressing rat thyroid
        follicular cells"; Cellosaurus knows it as "FRTL-5". The exact-match gate
        is untouched — the phrase genuinely returns nothing — so the second
        candidate is what closes the gap, and the entity's ``name`` stays the
        documents' wording.
        """
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_DESCRIPTIVE, catalog_name=FRTL5_CATALOG)

        assert result["accession"] == "CVCL_0265"
        assert result["match"] == "catalog"
        assert result["query"] == FRTL5_CATALOG

        entity = _entity(state, result["entity_id"])
        assert entity.fields["name"] == FRTL5_DESCRIPTIVE
        # The Cellosaurus label is an alias, never the name.
        assert "FRTL-5" in (entity.fields.get("alternateName") or [])

    @responses.activate
    def test_an_accession_shaped_catalog_name_is_refused(self):
        """``catalog_name`` is a NAME slot; an accession may not ride in on it.

        ``catalog_name`` is deliberately absent from ``_PLAN_IDENTIFIER_FIELDS``
        so ``_strip_plan_identifiers`` leaves it — which is exactly what makes
        this guard load-bearing rather than belt-and-braces.
        """
        for field in ("id", "sy"):
            _route_search(f'{field}:"{FRTL5_DESCRIPTIVE}"', json=EMPTY_SEARCH)
        state = CrateState()

        for smuggled in ("CVCL_0265", "cvcl_0265", "CVCL-0265"):
            result = resolve_cell_line(state, FRTL5_DESCRIPTIVE, catalog_name=smuggled)
            assert result["accession"] == ""
            assert result["match"] == "none"

        # It was never even asked about: only the descriptive phrase was queried.
        queried = " ".join(str(call.request.url or "") for call in responses.calls)
        assert "0265" not in queried

    @responses.activate
    def test_a_miss_still_mints_the_cell_line_sample(self):
        """The deliberate divergence from ``resolve_compound``.

        A ``CellLineSample`` with only a name is a valid ISA Sample and is what
        the arm produced before this composite. Returning ``{ok: False}`` would
        delete the cell line from every crate whose line is not catalogued,
        taking ``CellCulture.cell_line`` and the Study's ``cell_lines`` mention
        with it — so the entity is always minted and there is no ``ok`` key.
        """
        for field in ("id", "sy"):
            _route_search(f'{field}:"Nonesuch primary cells"', json=EMPTY_SEARCH)
        state = CrateState()

        result = resolve_cell_line(state, "Nonesuch primary cells")

        assert "ok" not in result
        assert result["accession"] == ""
        assert result["match"] == "none"
        entity = _entity(state, result["entity_id"])
        assert entity.type == "CellLineSample"
        assert entity.fields["name"] == "Nonesuch primary cells"

    @responses.activate
    def test_transient_outage_keeps_a_name_only_sample(self):
        """An outage is not a miss, and not a failure either: mint, no accession."""
        for field in ("id", "sy"):
            _route_search(f'{field}:"{FRTL5_CATALOG}"', body=Timeout("timed out"))
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_CATALOG)

        assert result["accession"] == ""
        assert result["verified"] is None
        assert result["verifications"] == []
        assert _cell_lines(state)[0].fields["name"] == FRTL5_CATALOG

    @responses.activate
    def test_a_transient_on_the_first_candidate_does_not_fall_through(self):
        """An outage on the strongest candidate must not promote a weaker one.

        Falling through would turn "Cellosaurus was down" into "the catalogue
        name answered", which is a different — and quietly less supported —
        claim about which record the documents mean.
        """
        for field in ("id", "sy"):
            _route_search(f'{field}:"{FRTL5_DESCRIPTIVE}"', body=Timeout("timed out"))
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_DESCRIPTIVE, catalog_name=FRTL5_CATALOG)

        assert result["accession"] == ""
        assert result["match"] == "none"

    @responses.activate
    def test_step_two_transient_failure_keeps_the_accession_unverified(self):
        """The record endpoint being down says nothing about the accession."""
        _route_frtl5_search()
        _route_frtl5_record(body=Timeout("timed out"))
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_CATALOG)

        assert result["accession"] == "CVCL_0265"
        assert result["verified"] is False
        entity = _entity(state, result["entity_id"])
        assert entity.fields["accession"] == "CVCL_0265"
        assert entity._completion["CellLineSample:accession"].status == "filled"

    @responses.activate
    def test_step_two_definitive_miss_clears_the_accession(self):
        """A search hit the record endpoint denies is evidence of nothing (D5).

        404 is the *definitive* answer, as opposed to the timeout above: the
        accession does not name a record, so publishing it would put a CVCL id
        in the crate that does not dereference.
        """
        _route_frtl5_search()
        _route_frtl5_record(json={"error": "not found"}, status=404)
        state = CrateState()

        result = resolve_cell_line(state, FRTL5_CATALOG)

        assert result["accession"] == ""
        assert result["match"] == "none"
        assert result["verified"] is False
        assert "accession" not in _cell_lines(state)[0].fields

    @responses.activate
    def test_a_definitive_miss_also_clears_an_accession_a_previous_call_wrote(self):
        """Clearing must reach the ENTITY, not just the return value.

        Without this the re-resolve would report no accession while the crate
        still published one — and ``_mint_id`` would keep keying the node's
        ``@id`` on an id the record endpoint denies.
        """
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()
        first = resolve_cell_line(state, FRTL5_CATALOG)
        assert first["accession"] == "CVCL_0265"

        responses.reset()
        for fn in (search_cellosaurus, lookup_cellosaurus, lookup_cell_line_by_name):
            fn.cache_clear()
        lookup_cell_line.cache_clear()
        _route_frtl5_search()
        _route_frtl5_record(json={"error": "not found"}, status=404)

        second = resolve_cell_line(state, FRTL5_CATALOG)

        assert second["entity_id"] == first["entity_id"]
        assert second["accession"] == ""
        assert "accession" not in _entity(state, second["entity_id"]).fields

    @responses.activate
    def test_the_record_url_never_lands_on_the_identifier_field(self):
        """``lookup_cellosaurus``'s own ``identifier`` is a URL and must not persist.

        ``verify_all_identifiers`` would re-query Cellosaurus with that URL
        percent-encoded into the cell-line path, miss, and **pop** the field —
        D5 destroying a value the authority actually gave us.
        """
        _route_hepg2()
        state = CrateState()

        result = resolve_cell_line(state, "HepG2")

        assert result["accession"] == "CVCL_0027"
        fields = _entity(state, result["entity_id"]).fields
        assert "identifier" not in fields
        assert fields["url"] == "https://www.cellosaurus.org/CVCL_0027"

    @responses.activate
    def test_only_the_documented_cellosaurus_fields_are_persisted(self):
        """Every other record field is dropped, each for a reason that is written down.

        Driven over the recorded CVCL_0027 body, which really does carry the
        DefinedTerm node objects and the donor facts, so this is a claim about
        the composite rather than about a thin double.
        """
        _route_hepg2()
        state = CrateState()

        result = resolve_cell_line(state, "HepG2")
        fields = _entity(state, result["entity_id"]).fields

        assert set(fields) <= {"name", "accession", "alternateName", "url", "sameAs"}
        for dropped, _reason in composites._CELL_LINE_DROPPED_FIELDS:
            if dropped == "name":
                continue  # the entity keeps its OWN name — see the test below
            assert dropped not in fields, f"{dropped} must not be persisted"
        # The record really did offer them, so the assertion above has teeth.
        record = lookup_cellosaurus("CVCL_0027")
        assert {"identifier", "taxonomicRange", "disease", "anatomicalSite"} <= set(record)
        # ...and the source name survives the Cellosaurus label ("Hep-G2").
        assert fields["name"] == "HepG2"
        assert "Hep-G2" in fields["alternateName"]

    @responses.activate
    def test_repeat_resolution_reuses_one_entity(self):
        """Idempotent: a second resolve refreshes the entity, never replaces it.

        ``draft_cell_line_sample`` is not idempotent and ``state.add_entity``
        silently *replaces* under ``CellLineSample:<eid>``, so without the reuse
        check in the composite a re-resolve would wipe the accession, its
        verified status and its provenance.
        """
        _route_frtl5_search()
        _route_frtl5_record()
        state = CrateState()

        first = resolve_cell_line(state, FRTL5_CATALOG)
        second = resolve_cell_line(state, FRTL5_CATALOG)

        assert first["entity_id"] == second["entity_id"]
        assert len(_cell_lines(state)) == 1
        entity = _entity(state, second["entity_id"])
        assert entity.fields["accession"] == "CVCL_0265"
        assert entity._completion["CellLineSample:accession"].status == "verified"

    @responses.activate
    def test_two_names_for_one_line_collapse_to_one_entity(self):
        """One line under two names is ONE node, deduped by accession.

        The shipped S-VHPS22 fixture names the same line two ways, and once the
        accession drives the ``@id`` (``_crate_mapping._mint_id``) two entities
        would mint the SAME ``@id`` — which ro-crate-py silently overwrites.
        """
        _route_frtl5_search()
        _route_frtl5_record()
        other = "FRTL-5 TPO-overexpressing cells"
        _route_search(f'id:"{other}"', json=EMPTY_SEARCH)
        _route_search(f'sy:"{other}"', json=EMPTY_SEARCH)
        state = CrateState()

        first = resolve_cell_line(state, FRTL5_DESCRIPTIVE, catalog_name=FRTL5_CATALOG)
        second = resolve_cell_line(state, other, catalog_name=FRTL5_CATALOG)

        assert first["entity_id"] == second["entity_id"]
        assert len(_cell_lines(state)) == 1
        entity = _entity(state, first["entity_id"])
        # The first name stays primary; the second is kept as an alias, not lost.
        assert entity.fields["name"] == FRTL5_DESCRIPTIVE
        assert other in entity.fields["alternateName"]

    @responses.activate
    def test_the_lookup_primitive_still_requires_an_exact_match(self):
        """Honesty control: the D5 gate was not relaxed to make the marquee green.

        The candidate logic lives in the composite. The primitive it calls must
        still refuse the descriptive phrase on its own — if this ever passes,
        the catalogue-name test above is proving nothing.
        """
        for field in ("id", "sy"):
            _route_search(f'{field}:"{FRTL5_DESCRIPTIVE}"', json=_load(
                "cellosaurus_search_id_frtl5.json"
            ))

        result = lookup_cell_line_by_name(FRTL5_DESCRIPTIVE)

        assert result["found"] is False
        assert result["data"] == {}
