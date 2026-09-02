"""Tests for ``draft_publication_with_authors`` — ORCID harmonization (#180).

The composite resolves a citation author's ``@id`` to their **ORCID** (with safe
fallbacks) so authors stop getting synthesized blank ids like
``#CitationAuthor_Fabian_Wagenaars``. The resolution cascade (stop at first
success) is:

    (a) Crossref ORCID on the author       -> verify -> https://orcid.org/<id>
    (b) in-crate Person with a verified ORCID matching family + given/initial
    (c) public ORCID search:
          - exactly ONE strong match       -> verify -> use it
          - multiple / weak / initial-only -> escalate to HITL
    (d) fallback: synthesized #CitationAuthor_<Given>_<Family> Person

D5: an ORCID from (a) or (c) is only attached after :func:`lookup_orcid` resolves
it and the name roughly matches; (b) is already verified; an HITL-chosen ORCID is
verified before use. HITL fires ONLY on genuine ambiguity.

All lookups + HITL are mocked, so these run fully offline (graph-only assertions).
"""

from __future__ import annotations

import pytest

from builder.state import CrateState, Entity, EntityProvenance
from builder.tools.composites import draft_publication_with_authors


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingHumanInterface:
    """A HumanInterface double that returns scripted responses and records calls."""

    def __init__(self, present_response=None, input_response=None) -> None:
        self.present_response = present_response or {
            "action": "skipped",
            "comments": None,
            "edits": None,
        }
        self.input_response = input_response or {"value": None, "skipped": True}
        self.present_calls: list[tuple[str, list[str] | None]] = []
        self.input_calls: list[tuple[str, str]] = []

    def present(self, context, options=None, purpose=None):
        self.present_calls.append((context, options))
        return self.present_response

    def request_input(self, prompt, field_type="text"):
        self.input_calls.append((prompt, field_type))
        return self.input_response


def _doi_result(*authors: dict, name: str = "A nephrotoxicity study") -> dict:
    """A Crossref-shaped lookup result (the `{found, data}` wrapper)."""
    return {
        "found": True,
        "data": {
            "@id": "https://doi.org/10.1234/example",
            "@type": "ScholarlyArticle",
            "identifier": "https://doi.org/10.1234/example",
            "name": name,
            "headline": name,
            "author": list(authors),
            "datePublished": "2021",
            "url": "https://doi.org/10.1234/example",
        },
        "error": None,
    }


def _orcid_record(orcid: str, given: str, family: str, affiliation: str = "") -> dict:
    """A `lookup_orcid` `{found, data}` result for verification."""
    return {
        "found": True,
        "data": {
            "@id": f"https://orcid.org/{orcid}",
            "@type": "Person",
            "identifier": f"https://orcid.org/{orcid}",
            "givenName": given,
            "familyName": family,
            "name": f"{given} {family}".strip(),
            "affiliation_name": affiliation,
            "affiliation_ror": "",
        },
        "error": None,
    }


def _patch(monkeypatch, *, doi=None, orcid=None, by_name=None):
    """Patch the three external lookups the tool consumes (offline)."""
    if doi is not None:
        monkeypatch.setattr("builder.tools.composites.lookup_doi", doi)
    if orcid is not None:
        monkeypatch.setattr("builder.tools.composites.lookup_orcid", orcid)
    if by_name is not None:
        monkeypatch.setattr("builder.tools.composites.lookup_orcid_by_name", by_name)


def _person_nodes(state: CrateState) -> list[Entity]:
    return [e for e in state.list_entities() if e.type == "Person"]


def _author_ids(state: CrateState, pub: Entity) -> set[str]:
    refs = pub.fields.get("author") or []
    if not isinstance(refs, list):
        refs = [refs]
    out: set[str] = set()
    for r in refs:
        out.add(r.get("@id") if isinstance(r, dict) else r)
    return out


# ---------------------------------------------------------------------------
# (a) Crossref ORCID
# ---------------------------------------------------------------------------


class TestCrossrefOrcid:
    def test_uses_crossref_orcid_when_present_and_verified(self, monkeypatch):
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: _orcid_record("0000-0002-1825-0097", "Jane", "Doe"),
        )

        result = draft_publication_with_authors(state, "10.1234/example")

        persons = _person_nodes(state)
        assert len(persons) == 1
        person = persons[0]
        # The ORCID became the @id source (orcid field) and is verified.
        assert person.fields.get("orcid") in (
            "0000-0002-1825-0097",
            "https://orcid.org/0000-0002-1825-0097",
        )
        status = person.get_field_status("orcid")
        assert status is not None and status.status == "verified"
        # No synthesized CitationAuthor id was used.
        assert not person.entity_id.startswith("#CitationAuthor")
        # The author is wired onto the publication.
        pub = next(e for e in state.list_entities() if e.type == "Publication")
        assert person.entity_id in {a.lstrip("#") for a in _author_ids(state, pub)} or (
            f"https://orcid.org/0000-0002-1825-0097" in _author_ids(state, pub)
        )
        assert "0000-0002-1825-0097" in str(result)

    def test_crossref_orcid_rejected_when_name_mismatches(self, monkeypatch):
        """D5: a Crossref ORCID whose resolved name does not match is NOT used."""
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            # ORCID resolves to a completely different person.
            orcid=lambda o: _orcid_record("0000-0002-1825-0097", "Carlos", "Santana"),
            by_name=lambda g, f, affiliation=None: [],
        )

        draft_publication_with_authors(state, "10.1234/example")

        person = _person_nodes(state)[0]
        # Mismatch -> no ORCID attached; falls back to synthesized author.
        assert not person.fields.get("orcid")
        assert person.entity_id.startswith("#CitationAuthor")


# ---------------------------------------------------------------------------
# (b) In-crate Person match (incl. the F.M.A. <-> Fabian initial case)
# ---------------------------------------------------------------------------


class TestInCratePersonMatch:
    def _seed_root_person(self, state: CrateState) -> Entity:
        """A root Person 'F.M.A. Wagenaars' with a verified ORCID."""
        person = Entity(
            entity_id="https://orcid.org/0000-0003-4766-7358",
            type="Person",
            _provenance=EntityProvenance(created_by="lookup"),
        )
        person.set_fields_from_dict(
            {
                "name": "F.M.A. Wagenaars",
                "givenName": "F.M.A.",
                "familyName": "Wagenaars",
                "orcid": "0000-0003-4766-7358",
                "affiliation": "Utrecht University",
            },
            source="lookup",
        )
        person.set_field_status("orcid", "verified", "lookup")
        state.add_entity(person)
        return person

    def test_reuses_in_crate_person_via_initial_match(self, monkeypatch):
        """Citation 'Fabian Wagenaars' resolves to root 'F.M.A. Wagenaars' ORCID."""
        state = CrateState()
        root = self._seed_root_person(state)

        # No Crossref ORCID; search must NOT be consulted (in-crate wins first).
        def _no_search(*a, **k):
            raise AssertionError("ORCID search should not be called for an in-crate hit")

        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {"givenName": "Fabian", "familyName": "Wagenaars"}
            ),
            orcid=lambda o: _orcid_record(
                "0000-0003-4766-7358", "F.M.A.", "Wagenaars"
            ),
            by_name=_no_search,
        )

        draft_publication_with_authors(state, "10.1234/example")

        # No NEW Person was created — the root Person is reused as the author.
        assert len(_person_nodes(state)) == 1
        pub = next(e for e in state.list_entities() if e.type == "Publication")
        assert root.entity_id in _author_ids(state, pub)

    def test_in_crate_match_requires_verified_orcid(self, monkeypatch):
        """An in-crate Person whose ORCID is only 'filled' (unverified) is not reused."""
        state = CrateState()
        person = Entity(entity_id="person_x", type="Person")
        person.set_fields_from_dict(
            {"name": "Fabian Wagenaars", "familyName": "Wagenaars", "givenName": "Fabian"}
        )  # no orcid at all
        state.add_entity(person)

        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {"givenName": "Fabian", "familyName": "Wagenaars"}
            ),
            orcid=lambda o: {"found": False, "data": {}, "error": "x"},
            by_name=lambda g, f, affiliation=None: [],  # no search hit -> fallback
        )

        draft_publication_with_authors(state, "10.1234/example")

        # A synthesized CitationAuthor was created (the unverified person not reused).
        synth = [p for p in _person_nodes(state) if p.entity_id.startswith("#CitationAuthor")]
        assert len(synth) == 1


# ---------------------------------------------------------------------------
# (c-auto) unambiguous public search
# ---------------------------------------------------------------------------


class TestSearchAutoAccept:
    def test_single_strong_search_match_auto_accepted(self, monkeypatch):
        state = CrateState()
        human = RecordingHumanInterface()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {"givenName": "Fabian", "familyName": "Wagenaars"}
            ),
            by_name=lambda g, f, affiliation=None: [
                {
                    "orcid": "0000-0003-4766-7358",
                    "given": "Fabian",
                    "family": "Wagenaars",
                    "affiliation": "Utrecht University",
                }
            ],
            orcid=lambda o: _orcid_record(
                "0000-0003-4766-7358", "Fabian", "Wagenaars"
            ),
        )

        draft_publication_with_authors(state, "10.1234/example", human_interface=human)

        person = _person_nodes(state)[0]
        assert person.fields.get("orcid") in (
            "0000-0003-4766-7358",
            "https://orcid.org/0000-0003-4766-7358",
        )
        # Confidently resolved -> no HITL prompt.
        assert human.present_calls == []
        assert human.input_calls == []

    def test_initial_only_search_match_is_not_auto_accepted(self, monkeypatch):
        """A weak (initial-only given) match escalates rather than auto-accepting."""
        state = CrateState()
        human = RecordingHumanInterface()  # default: skip
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result({"givenName": "F.", "familyName": "Wagenaars"}),
            by_name=lambda g, f, affiliation=None: [
                {
                    "orcid": "0000-0003-4766-7358",
                    "given": "Fabian",
                    "family": "Wagenaars",
                    "affiliation": "Utrecht University",
                }
            ],
            orcid=lambda o: _orcid_record(
                "0000-0003-4766-7358", "Fabian", "Wagenaars"
            ),
        )

        draft_publication_with_authors(state, "10.1234/example", human_interface=human)

        # Weak match -> a HITL escalation happened.
        assert human.present_calls, "expected a HITL prompt for the weak/initial match"


# ---------------------------------------------------------------------------
# (c-HITL) ambiguous -> simulated human picks
# ---------------------------------------------------------------------------


class TestSearchHitlPick:
    def test_multiple_candidates_escalate_and_human_picks(self, monkeypatch):
        state = CrateState()
        candidates = [
            {
                "orcid": "0000-0001-1111-1111",
                "given": "Jane",
                "family": "Smith",
                "affiliation": "University A",
            },
            {
                "orcid": "0000-0002-2222-2222",
                "given": "Jane",
                "family": "Smith",
                "affiliation": "University B",
            },
        ]
        # Human approves option index 1 (the second candidate) via comments/edits.
        human = RecordingHumanInterface(
            present_response={
                "action": "approved",
                "comments": "0000-0002-2222-2222",
                "edits": {"orcid": "0000-0002-2222-2222"},
            }
        )
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result({"givenName": "Jane", "familyName": "Smith"}),
            by_name=lambda g, f, affiliation=None: candidates,
            orcid=lambda o: _orcid_record(
                o.rsplit("/", 1)[-1], "Jane", "Smith"
            ),
        )

        draft_publication_with_authors(state, "10.1234/example", human_interface=human)

        assert human.present_calls, "expected a HITL prompt with the candidate options"
        person = _person_nodes(state)[0]
        assert person.fields.get("orcid") in (
            "0000-0002-2222-2222",
            "https://orcid.org/0000-0002-2222-2222",
        )

    def test_human_pastes_orcid_via_request_input(self, monkeypatch):
        state = CrateState()
        # present() returns no usable pick -> tool asks request_input; user pastes.
        human = RecordingHumanInterface(
            present_response={"action": "approved", "comments": None, "edits": None},
            input_response={"value": "0000-0002-2222-2222", "skipped": False},
        )
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result({"givenName": "Jane", "familyName": "Smith"}),
            by_name=lambda g, f, affiliation=None: [
                {"orcid": "0000-0001-1111-1111", "given": "Jane", "family": "Smith", "affiliation": "A"},
                {"orcid": "0000-0009-9999-9999", "given": "Jane", "family": "Smith", "affiliation": "B"},
            ],
            orcid=lambda o: _orcid_record(o.rsplit("/", 1)[-1], "Jane", "Smith"),
        )

        draft_publication_with_authors(state, "10.1234/example", human_interface=human)

        person = _person_nodes(state)[0]
        assert person.fields.get("orcid") in (
            "0000-0002-2222-2222",
            "https://orcid.org/0000-0002-2222-2222",
        )


# ---------------------------------------------------------------------------
# (d) fallback synthesized + D5 negative
# ---------------------------------------------------------------------------


class TestFallbackAndD5:
    def test_no_signal_synthesizes_citation_author(self, monkeypatch):
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {"givenName": "Fabian", "familyName": "Wagenaars"}
            ),
            by_name=lambda g, f, affiliation=None: [],  # no search hit
            orcid=lambda o: {"found": False, "data": {}, "error": "x"},
        )

        draft_publication_with_authors(state, "10.1234/example")

        person = _person_nodes(state)[0]
        assert person.entity_id == "#CitationAuthor_Fabian_Wagenaars"
        assert not person.fields.get("orcid")

    def test_d5_uncertain_and_human_skip_attaches_no_orcid(self, monkeypatch):
        """Ambiguous candidates + human SKIP => no ORCID, synthesized fallback used."""
        state = CrateState()
        human = RecordingHumanInterface(
            present_response={"action": "skipped", "comments": None, "edits": None},
            input_response={"value": None, "skipped": True},
        )
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result({"givenName": "Jane", "familyName": "Smith"}),
            by_name=lambda g, f, affiliation=None: [
                {"orcid": "0000-0001-1111-1111", "given": "Jane", "family": "Smith", "affiliation": "A"},
                {"orcid": "0000-0002-2222-2222", "given": "Jane", "family": "Smith", "affiliation": "B"},
            ],
            orcid=lambda o: _orcid_record(o.rsplit("/", 1)[-1], "Jane", "Smith"),
        )

        draft_publication_with_authors(state, "10.1234/example", human_interface=human)

        assert human.present_calls, "expected a HITL prompt"
        person = _person_nodes(state)[0]
        assert not person.fields.get("orcid"), "no ORCID may be attached after a skip (D5)"
        assert person.entity_id.startswith("#CitationAuthor")

    def test_returns_summary_and_reuses_existing_publication(self, monkeypatch):
        """Re-running with an already-present ScholarlyArticle does not duplicate it."""
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: _orcid_record("0000-0002-1825-0097", "Jane", "Doe"),
        )

        r1 = draft_publication_with_authors(state, "10.1234/example")
        r2 = draft_publication_with_authors(state, "10.1234/example")

        pubs = [e for e in state.list_entities() if e.type == "Publication"]
        assert len(pubs) == 1
        assert r1["publication_id"] == r2["publication_id"]
        assert "authors" in r1

    def test_doi_not_found_returns_error(self, monkeypatch):
        state = CrateState()
        _patch(monkeypatch, doi=lambda d: {"found": False, "data": {}, "error": "nope"})

        result = draft_publication_with_authors(state, "10.0000/missing")

        assert result.get("ok") is False
        assert not [e for e in state.list_entities() if e.type == "Publication"]


# ---------------------------------------------------------------------------
# Author affiliation must be an Organization reference, not a string (#179)
# ---------------------------------------------------------------------------


def _orgs(state: CrateState) -> list[Entity]:
    return [e for e in state.list_entities() if e.type == "Organization"]


def _affiliation_ref(person: Entity) -> str | None:
    """The Organization @id a Person's ``affiliation`` references (bare or {@id})."""
    ref = person.fields.get("affiliation")
    if isinstance(ref, dict):
        return ref.get("@id")
    return ref if isinstance(ref, str) and ref else None


class TestAuthorAffiliationIsOrganization:
    """The ISA shape requires Person.affiliation to reference an Organization.

    ROOT CAUSE (#179): ``_ensure_person_for_orcid`` stored
    ``data['affiliation_name']`` as a STRING on ``affiliation`` and dropped the
    ORCID-provided ``affiliation_ror``. The builder emitted that string (a literal
    on a reference-only property) -> SHACL Violation, re-asked forever. The fix
    find-or-drafts an Organization (preferring the ROR so its @id resolves to the
    ROR IRI) and wires the Person's ``affiliation`` to that Organization.
    """

    def test_affiliation_becomes_organization_reference_with_ror(self, monkeypatch):
        state = CrateState()
        rec = _orcid_record("0000-0002-1825-0097", "Jane", "Doe", "Utrecht University")
        rec["data"]["affiliation_ror"] = "05wg1m734"
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: rec,
        )

        draft_publication_with_authors(state, "10.1234/example")

        person = _person_nodes(state)[0]
        # NOT a bare name string.
        assert person.fields.get("affiliation") != "Utrecht University"
        # An Organization was drafted and the affiliation references it.
        orgs = _orgs(state)
        assert len(orgs) == 1
        org = orgs[0]
        assert org.fields.get("name") == "Utrecht University"
        # D5: the ROR is preserved so the Organization @id resolves to the ROR IRI.
        assert org.fields.get("ror") == "05wg1m734"
        # The Person.affiliation is a REFERENCE to that Organization.
        assert _affiliation_ref(person) == org.entity_id

    def test_affiliation_name_only_still_becomes_organization(self, monkeypatch):
        """An ORCID affiliation with a name but no ROR still yields an Organization."""
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: _orcid_record(
                "0000-0002-1825-0097", "Jane", "Doe", "Utrecht University"
            ),
        )

        draft_publication_with_authors(state, "10.1234/example")

        person = _person_nodes(state)[0]
        orgs = _orgs(state)
        assert len(orgs) == 1
        assert person.fields.get("affiliation") != "Utrecht University"
        assert _affiliation_ref(person) == orgs[0].entity_id

    def test_two_authors_share_one_organization(self, monkeypatch):
        """Two authors with the same affiliation reuse ONE Organization (no dup)."""
        state = CrateState()

        def _orcid(o):  # noqa: ANN001
            bare = o.rsplit("/", 1)[-1]
            given = "Jane" if bare.endswith("0097") else "John"
            return _orcid_record(bare, given, "Doe", "Utrecht University")

        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                },
                {
                    "givenName": "John",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0098",
                },
            ),
            orcid=_orcid,
        )

        draft_publication_with_authors(state, "10.1234/example")

        # Both authors resolved; ONE shared Organization.
        assert len(_person_nodes(state)) == 2
        orgs = _orgs(state)
        assert len(orgs) == 1
        org_id = orgs[0].entity_id
        for person in _person_nodes(state):
            assert _affiliation_ref(person) == org_id

    def test_no_affiliation_leaves_field_unset(self, monkeypatch):
        """No ORCID affiliation -> no Organization, no affiliation literal (D5)."""
        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: _orcid_record("0000-0002-1825-0097", "Jane", "Doe"),
        )

        draft_publication_with_authors(state, "10.1234/example")

        assert _orgs(state) == []
        person = _person_nodes(state)[0]
        assert not person.fields.get("affiliation")

    def test_affiliation_resolves_to_org_node_in_build(self, monkeypatch):
        """The built crate emits Person.affiliation as an @id ref to the Org node."""
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.context import ISA_TOX_CONTEXT

        state = CrateState()
        rec = _orcid_record("0000-0002-1825-0097", "Jane", "Doe", "Utrecht University")
        rec["data"]["affiliation_ror"] = "05wg1m734"
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: rec,
        )

        draft_publication_with_authors(state, "10.1234/example")

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, None, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]

        person_node = next(
            n for n in graph if n.get("@id") == "https://orcid.org/0000-0002-1825-0097"
        )
        affiliation = person_node.get("affiliation")
        # Emitted as an @id reference, not a bare string.
        assert isinstance(affiliation, dict) and affiliation.get("@id")
        # The reference resolves to the Organization node (ROR IRI @id).
        assert affiliation["@id"] == "https://ror.org/05wg1m734"
        org_node = next(n for n in graph if n.get("@id") == "https://ror.org/05wg1m734")
        assert "Organization" in (
            org_node.get("@type")
            if isinstance(org_node.get("@type"), list)
            else [org_node.get("@type")]
        )
        assert org_node.get("name") == "Utrecht University"


# ---------------------------------------------------------------------------
# Registration & engine routing
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_with_takes_human(self):
        from builder.tools.registry import TOOL_REGISTRY

        import builder.tools.composites  # noqa: F401  (triggers registration)

        spec = TOOL_REGISTRY.get_spec("draft_publication_with_authors")
        assert spec is not None
        assert spec.takes_state is True
        assert spec.takes_human is True

    def test_orcid_person_round_trips_with_identifier_pv(self, monkeypatch):
        """An ORCID-resolved author builds with the ORCID @id + ORCID PropertyValue."""
        from rocrate.rocrate import ROCrate

        from builder.tools._crate_mapping import populate_crate
        from profiles.context import ISA_TOX_CONTEXT

        state = CrateState()
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result(
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "identifier": "0000-0002-1825-0097",
                }
            ),
            orcid=lambda o: _orcid_record("0000-0002-1825-0097", "Jane", "Doe"),
        )

        draft_publication_with_authors(state, "10.1234/example")

        crate = ROCrate()
        crate.metadata.extra_contexts = ISA_TOX_CONTEXT
        populate_crate(state, crate, None, materialize_payload=False)
        graph = crate.metadata.generate()["@graph"]

        # The Person node @id is the ORCID URL.
        person_node = next(
            n for n in graph if n.get("@id") == "https://orcid.org/0000-0002-1825-0097"
        )
        # It carries an ORCID identifier PropertyValue (#185's _identifier_pv path).
        pv_nodes = [n for n in graph if n.get("name") == "ORCID"]
        assert pv_nodes, "expected an ORCID PropertyValue identifier node"
        assert any("0000-0002-1825-0097" in str(n.get("value")) for n in pv_nodes)
        # The ScholarlyArticle's author references the ORCID Person.
        article = next(
            n for n in graph if "ScholarlyArticle" in (
                n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]
            )
        )
        authors = article.get("author")
        author_ids = {
            (a.get("@id") if isinstance(a, dict) else a)
            for a in (authors if isinstance(authors, list) else [authors])
        }
        assert person_node["@id"] in author_ids

    def test_engine_injects_human_interface(self, monkeypatch):
        from builder.engine import AgentEngine

        human = RecordingHumanInterface()
        engine = AgentEngine(human_interface=human)
        _patch(
            monkeypatch,
            doi=lambda d: _doi_result({"givenName": "Jane", "familyName": "Smith"}),
            by_name=lambda g, f, affiliation=None: [
                {"orcid": "0000-0001-1111-1111", "given": "Jane", "family": "Smith", "affiliation": "A"},
                {"orcid": "0000-0002-2222-2222", "given": "Jane", "family": "Smith", "affiliation": "B"},
            ],
            orcid=lambda o: _orcid_record(o.rsplit("/", 1)[-1], "Jane", "Smith"),
        )

        engine.run_tool("draft_publication_with_authors", doi="10.1234/example")

        # The engine routed the injected interface through to the tool.
        assert human.present_calls, "engine should have injected the HITL adapter"


class TestAnOrcidTypedAtTheCandidateMenu:
    """#596: the console's free-text row is a third way to answer the candidate
    menu. An ORCID typed there is the pick — verified like any other (D5) — and
    the user is not asked to paste it a second time."""

    def _search(self, typed: str, looked_up: list[str]):
        from builder.tools.composites import _resolve_via_search

        class _Types:
            def __init__(self) -> None:
                self.inputs: list[str] = []

            def present(self, context, options=None, purpose=None):
                return {"action": "edited", "comments": typed, "edits": {"value": typed}}

            def request_input(self, prompt, field_type="text"):
                self.inputs.append(prompt)
                return {"value": None, "skipped": True}

        def by_name(given, family, affiliation=None):
            return [
                {"given": "A.", "family": family, "orcid": "0000-0001-0000-0001"},
                {"given": "Alice", "family": family, "orcid": "0000-0002-0000-0002"},
            ]

        def lookup(orcid_id):
            looked_up.append(orcid_id)
            return {"found": True, "data": {"familyName": "Smith", "givenNames": "Alice"}}

        human = _Types()
        return _resolve_via_search("Alice", "Smith", None, human, lookup, by_name), human

    def test_a_typed_orcid_is_the_pick_and_is_verified(self):
        looked_up: list[str] = []
        chosen, human = self._search("https://orcid.org/0000-0003-0000-0003", looked_up)

        assert chosen == "0000-0003-0000-0003"
        assert looked_up == ["0000-0003-0000-0003"], "a typed ORCID is verified before use"
        assert human.inputs == [], "no second prompt for what was just typed"

    def test_typed_prose_still_falls_back_to_the_paste_prompt(self):
        chosen, human = self._search("the second one I think", [])

        assert chosen is None
        assert len(human.inputs) == 1
