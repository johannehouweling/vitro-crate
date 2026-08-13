"""An author's affiliation, recovered from the papers their ORCID record lists.

Many ORCID records carry no employment section at all, and the profile still
asks every author for an institution. The papers are on the same record and
Crossref names the affiliation on the author line, so the fact exists one hop
away: ORCID iD -> works -> DOIs -> Crossref -> affiliation.

This is retrieval, not inference — every value was published by a registry about
this person. What makes it safe is knowing when NOT to answer, and that is what
these tests pin:

* every affiliation agrees -> take it, even from two papers. Two independent
  records saying the same thing is corroboration; demanding a third would throw
  away a good answer for someone who has published twice.
* they disagree -> the three most recent decide, because people move and an old
  paper is evidence of where someone USED to be.
* still split -> answer nothing and let a human settle it. A confident wrong
  affiliation in a scientific record is worse than an open question.

No test here touches the network.
"""

from __future__ import annotations

import pytest

from lookups import affiliation_from_works as mod
from lookups.affiliation_from_works import affiliation_from_works, institution_of

BRUNEL_A = "Centre for Pollution Research and Policy, Brunel University London, Uxbridge, UK."
BRUNEL_B = "Institute of Environment, Health and Societies, Brunel University London, Uxbridge, UK"
BRUNEL_C = "Brunel University London, Kingston Lane, Uxbridge UB8 3PH, U.K."
UTRECHT = "Institute for Risk Assessment Sciences, Utrecht University, Utrecht, The Netherlands"


@pytest.fixture
def papers(monkeypatch):
    """Drive the chain from a list of (doi, affiliation) pairs, newest first."""

    def _install(pairs: list[tuple[str, str]]) -> None:
        monkeypatch.setattr(mod, "recent_dois", lambda oid, limit=8: [d for d, _ in pairs])
        table = dict(pairs)
        monkeypatch.setattr(
            mod,
            "_affiliations_on",
            lambda doi, orcid, family: [table[doi]] if table.get(doi) else [],
        )

    return _install


class TestAgreementIsEnough:
    def test_two_papers_that_agree_settle_it(self, papers):
        """Corroboration, not a quorum: a third paper adds nothing here."""
        papers([("10.1/a", BRUNEL_A), ("10.1/b", BRUNEL_B)])
        assert "Brunel" in affiliation_from_works("0000-0002-9569-7562", "Scholze")

    def test_one_paper_is_still_an_answer(self, papers):
        """Nothing contradicts it, and it is what a registry published."""
        papers([("10.1/a", BRUNEL_A)])
        assert "Brunel" in affiliation_from_works("0000-0002-9569-7562", "Scholze")

    def test_different_spellings_of_one_employer_still_agree(self, papers):
        """Department, street and postcode differ; the institution does not."""
        papers([("10.1/a", BRUNEL_A), ("10.1/b", BRUNEL_B), ("10.1/c", BRUNEL_C)])
        assert "Brunel" in affiliation_from_works("0000-0002-9569-7562", "Scholze")


class TestConflictGoesToRecency:
    def test_the_recent_majority_wins(self, papers):
        """People move: two recent papers outweigh an older employer."""
        papers(
            [
                ("10.1/new1", BRUNEL_A),
                ("10.1/new2", BRUNEL_B),
                ("10.1/old", UTRECHT),
            ]
        )
        assert "Brunel" in affiliation_from_works("0000-0002-9569-7562", "Scholze")

    def test_an_old_paper_cannot_outvote_the_recent_ones(self, papers):
        papers(
            [
                ("10.1/new1", UTRECHT),
                ("10.1/new2", UTRECHT),
                ("10.1/old1", BRUNEL_A),
                ("10.1/old2", BRUNEL_B),
                ("10.1/old3", BRUNEL_C),
            ]
        )
        assert "Utrecht" in affiliation_from_works("0000-0002-9569-7562", "Scholze")


class TestAnUnsettledAnswerIsNoAnswer:
    def test_a_three_way_split_asks_a_human(self, papers):
        """1/1/1 has no majority — guessing here would invent an employer."""
        papers(
            [
                ("10.1/a", BRUNEL_A),
                ("10.1/b", UTRECHT),
                ("10.1/c", "Erasmus MC, Rotterdam, The Netherlands"),
            ]
        )
        assert affiliation_from_works("0000-0002-9569-7562", "Scholze") == ""

    def test_an_even_split_asks_a_human(self, papers):
        papers([("10.1/a", BRUNEL_A), ("10.1/b", UTRECHT)])
        assert affiliation_from_works("0000-0002-9569-7562", "Scholze") == ""

    def test_no_works_is_no_answer(self, papers):
        papers([])
        assert affiliation_from_works("0000-0002-9569-7562", "Scholze") == ""

    def test_works_without_affiliations_are_no_answer(self, papers):
        papers([("10.1/a", ""), ("10.1/b", "")])
        assert affiliation_from_works("0000-0002-9569-7562", "Scholze") == ""

    def test_no_orcid_is_no_lookup(self):
        assert affiliation_from_works("", "Scholze") == ""


class TestTheInstitutionIsPickedOutOfTheString:
    @pytest.mark.parametrize(
        ("affiliation", "expected"),
        [
            (BRUNEL_A, "Brunel University London"),
            (BRUNEL_B, "Brunel University London"),
            (UTRECHT, "Utrecht University"),
            ("Erasmus MC, Rotterdam, The Netherlands", "Erasmus MC, Rotterdam, The Netherlands"),
        ],
    )
    def test_the_employer_beats_the_department(self, affiliation, expected):
        """A centre or institute is part of an employer, not the employer."""
        assert institution_of(affiliation) == expected

    def test_an_empty_string_stays_empty(self):
        assert institution_of("") == ""


class TestFailureIsNeverFatal:
    def test_a_broken_works_call_yields_nothing(self, monkeypatch):
        def boom(orcid_id, limit=8):
            raise RuntimeError("ORCID is down")

        monkeypatch.setattr(mod, "recent_dois", boom)
        assert affiliation_from_works("0000-0002-9569-7562", "Scholze") == ""

    def test_a_transient_outage_propagates(self, monkeypatch):
        """A caller must be able to tell an outage from a genuine absence."""
        from lookups._http import TransientLookupError

        def flaky(orcid_id, limit=8):
            raise TransientLookupError("429")

        monkeypatch.setattr(mod, "recent_dois", flaky)
        with pytest.raises(TransientLookupError):
            affiliation_from_works("0000-0002-9569-7562", "Scholze")


class TestTheRorMatchIsCheckedNotTrusted:
    """A search is a guess; a guess that renames an employer is not readable back."""

    def test_a_registered_name_differing_by_a_connective_is_accepted(self):
        from builder.tools.composites import _same_institution

        assert _same_institution("Brunel University of London", "Brunel University London")

    def test_a_real_but_different_institution_is_refused(self):
        """ "University of London" is not "Brunel University London"."""
        from builder.tools.composites import _same_institution

        assert not _same_institution("University of London", "Brunel University London")

    def test_an_unrelated_match_is_refused(self):
        from builder.tools.composites import _same_institution

        assert not _same_institution("Utrecht University", "Brunel University London")

    def test_an_unverified_match_keeps_the_printed_name_and_no_ror(self, monkeypatch):
        """Better the paper's own words than a confidently wrong registry id."""
        import builder.tools.composites as composites

        monkeypatch.setattr(
            composites, "search_ror", lambda name: {"name": "Somewhere Else", "@id": "x"}
        )
        name, ror = composites._registered_institution(BRUNEL_A)
        assert name == "Brunel University London"
        assert ror is None

    def test_a_ror_outage_keeps_the_printed_name(self, monkeypatch):
        import builder.tools.composites as composites

        def boom(name):
            raise RuntimeError("ROR is down")

        monkeypatch.setattr(composites, "search_ror", boom)
        name, ror = composites._registered_institution(BRUNEL_A)
        assert name == "Brunel University London"
        assert ror is None


class TestTheAuthorIsMatchedNotAssumed:
    def test_an_orcid_on_the_author_line_is_decisive(self):
        assert mod._author_matches(
            {"ORCID": "https://orcid.org/0000-0002-9569-7562"}, "0000-0002-9569-7562", ""
        )

    def test_a_different_orcid_is_not_this_person(self):
        """Two Schmidts on one paper — the name alone would take either."""
        assert not mod._author_matches(
            {"ORCID": "https://orcid.org/0000-0000-0000-0001", "family": "Scholze"},
            "0000-0002-9569-7562",
            "Scholze",
        )

    def test_the_family_name_is_the_fallback(self):
        assert mod._author_matches({"family": "Scholze"}, "0000-0002-9569-7562", "Scholze")

    def test_without_a_name_or_orcid_nobody_matches(self):
        assert not mod._author_matches({"family": "Scholze"}, "", "")
