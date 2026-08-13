"""Findings are grouped into the actions that would clear them.

A validator reports one finding per unsatisfied shape. That is right for a
validator and wrong for a person: one author with no ORCID opens four of them,
and a reader working down the list meets the same person four times without
being told once what to do. On a real crate this turned 73 findings into 16
actions.

The grouping is deterministic on purpose. Wording an action is a job a model can
do; deciding WHICH findings belong together is not — a model that mis-groups
produces a confident summary of the wrong work, and nothing downstream would
catch it. These tests pin the decisions:

* every finding lands in exactly one action, so the counts can be trusted;
* entity groups come first, because "Zhongli Chen needs an ORCID" is a task and
  "nine things are missing an affiliation" is the same list re-sorted;
* entities needing exactly the same thing merge into one line;
* findings we deliberately leave open are separated, never recommended.
"""

from __future__ import annotations

from builder.tools.remediation import Action, describe, group_findings, group_orphans


def _f(entity_id, message, prop=None, severity="RECOMMENDED"):
    return {
        "entity_id": entity_id,
        "message": message,
        "property": prop,
        "severity": severity,
    }


class TestEveryFindingIsAccountedFor:
    def test_the_counts_sum_to_the_input(self):
        """A summary that quietly drops findings is worse than no summary."""
        findings = [
            _f("#a", "needs an ORCID", "identifier"),
            _f("#a", "needs an affiliation", "affiliation"),
            _f("#b", "needs an affiliation", "affiliation"),
            _f("#c", "needs a URL", "url"),
        ]
        actions = group_findings(findings)
        assert sum(a.cleared for a in actions) == len(findings)

    def test_no_finding_appears_twice(self):
        findings = [
            _f("#a", "needs an ORCID", "identifier"),
            _f("#a", "needs an affiliation", "affiliation"),
            _f("#b", "needs an affiliation", "affiliation"),
        ]
        seen = [m for a in group_findings(findings) for m in a.findings]
        assert len(seen) == len(set(map(id, findings))) == 3

    def test_an_empty_list_yields_nothing(self):
        assert group_findings([]) == []


class TestOneEntityWithSeveralProblemsIsOneAction:
    def test_the_authors_four_findings_become_one_action(self):
        """The case that prompted this: an author with no ORCID."""
        findings = [
            _f("./#CitationAuthor_Zhongli_Chen", "SHOULD have an ORCID as its @id"),
            _f("./#CitationAuthor_Zhongli_Chen", "SHOULD have a non-empty identifier"),
            _f("./#CitationAuthor_Zhongli_Chen", "SHOULD have an affiliation"),
            _f("./#CitationAuthor_Zhongli_Chen", "SHOULD have a contactPoint"),
        ]
        actions = [a for a in group_findings(findings) if a.actionable]
        assert len(actions) == 1
        assert actions[0].cleared == 4

    def test_the_subject_reads_as_a_name(self):
        findings = [
            _f("./#CitationAuthor_Zhongli_Chen", "one"),
            _f("./#CitationAuthor_Zhongli_Chen", "two"),
        ]
        assert group_findings(findings)[0].subject == "Zhongli Chen"

    def test_a_supplied_label_wins_over_the_id(self):
        findings = [_f("#p1", "one"), _f("#p1", "two")]
        actions = group_findings(findings, labels={"#p1": "Timo Hamers"})
        assert actions[0].subject == "Timo Hamers"

    def test_entity_grouping_beats_a_larger_property_group(self):
        """Bigger is not more actionable — naming the entity surfaces the cause."""
        findings = [
            _f("#a", "needs an ORCID", "identifier"),
            _f("#a", "needs an affiliation", "affiliation"),
            _f("#b", "needs an affiliation", "affiliation"),
            _f("#c", "needs an affiliation", "affiliation"),
        ]
        actions = group_findings(findings)
        assert actions[0].kind == "entity"
        assert actions[0].entity_ids == ["#a"]


class TestIdenticalNeedsMerge:
    def test_entities_needing_the_same_thing_become_one_line(self):
        findings = [
            _f("#a", "needs an affiliation"),
            _f("#a", "needs a contactPoint"),
            _f("#b", "needs an affiliation"),
            _f("#b", "needs a contactPoint"),
        ]
        actions = [a for a in group_findings(findings) if a.actionable]
        assert len(actions) == 1
        assert actions[0].kind == "entities"
        assert actions[0].cleared == 4
        assert actions[0].entity_ids == ["#a", "#b"]

    def test_different_needs_stay_separate(self):
        """Similar-looking is not the same: two needs are two jobs."""
        findings = [
            _f("#a", "needs an affiliation"),
            _f("#a", "needs a contactPoint"),
            _f("#b", "needs an affiliation"),
            _f("#b", "needs an ORCID"),
        ]
        actions = [a for a in group_findings(findings) if a.actionable]
        assert len(actions) == 2

    def test_the_subject_names_them_and_stays_short(self):
        findings = [
            f
            for name in ("#a", "#b", "#c", "#d", "#e")
            for f in (_f(name, "needs an affiliation"), _f(name, "needs a contactPoint"))
        ]
        labels = {f"#{c}": f"Person {c.upper()}" for c in "abcde"}
        subject = group_findings(findings, labels=labels)[0].subject
        assert "and 2 others" in subject
        assert subject.count(",") == 2


class TestDeliberateFindingsAreNotRecommended:
    def test_the_root_identifier_is_set_aside_with_a_reason(self):
        """Acting on it would break an ISA MUST — recommending it is harmful."""
        findings = [
            _f("./", "The Root Data Entity SHOULD use PropertyValue entities for identifiers")
        ]
        actions = group_findings(findings)
        assert len(actions) == 1
        assert actions[0].actionable is False
        assert "ISA" in (actions[0].note or "")

    def test_a_landing_page_property_is_set_aside(self):
        findings = [_f("./data/x.csv", "The File Data Entity MAY have a `mainEntityOfPage`")]
        actions = group_findings(findings)
        assert actions[0].actionable is False

    def test_they_share_one_line_per_reason(self):
        findings = [
            _f("./data/x.csv", "MAY have a `mainEntityOfPage` property"),
            _f("./data/y.csv", "MAY have a `mainEntityOfPage` property"),
        ]
        deferred = [a for a in group_findings(findings) if not a.actionable]
        assert len(deferred) == 1
        assert deferred[0].cleared == 2

    def test_they_are_still_counted(self):
        """Set aside is not discarded: the totals must still add up."""
        findings = [
            _f("./", "SHOULD use PropertyValue entities for identifiers"),
            _f("#a", "needs an affiliation"),
        ]
        assert sum(a.cleared for a in group_findings(findings)) == 2


class TestTheStrongestTierWins:
    def test_a_must_outranks_a_should(self):
        findings = [
            _f("#a", "one", severity="RECOMMENDED"),
            _f("#a", "two", severity="MUST"),
        ]
        assert group_findings(findings)[0].tier == "MUST"

    def test_actions_are_ordered_by_tier_then_reach(self):
        findings = [
            _f("#a", "one", severity="RECOMMENDED"),
            _f("#a", "two", severity="RECOMMENDED"),
            _f("#b", "three", severity="MUST"),
            _f("#b", "four", severity="MUST"),
            _f("#b", "five", severity="MUST"),
        ]
        actions = group_findings(findings)
        assert actions[0].tier == "MUST"
        assert actions[0].cleared == 3


class TestOrphansClusterByWhatTheyAre:
    def test_the_aop_subgraph_is_one_job(self):
        orphans = [f"https://aopwiki.org/events/{n}" for n in range(20)]
        orphans.append("https://aopwiki.org/aops/610")
        actions = group_orphans(orphans)
        assert len(actions) == 1
        assert actions[0].cleared == 21
        assert "AOP" in actions[0].subject

    def test_unrelated_orphans_stay_apart(self):
        actions = group_orphans(
            ["https://aopwiki.org/events/1", "#PropertyValue_pv_seeding", "#DefinedTerm_dt_role"]
        )
        assert len(actions) == 3

    def test_the_biggest_cluster_leads(self):
        orphans = [f"https://aopwiki.org/events/{n}" for n in range(5)] + ["#PropertyValue_pv_x"]
        assert group_orphans(orphans)[0].cleared == 5

    def test_nothing_orphaned_is_nothing_to_do(self):
        assert group_orphans([]) == []


class TestThePhrasingFloor:
    """The report must say something useful with no model configured."""

    def test_the_specific_instruction_beats_the_vague_one(self):
        """An author missing an ORCID also trips generic "identifier" wording."""
        findings = [
            _f("#a", "A Person entity SHOULD have an ORCID identifier as its @id"),
            _f("#a", "Person entity SHOULD have a non-empty identifier of type string"),
        ]
        action = group_findings(findings, labels={"#a": "Zhongli Chen"})[0]
        assert describe(action) == "Add an ORCID for Zhongli Chen."

    def test_it_names_the_subject(self):
        findings = [_f("#o", "The organization SHOULD have a URL"), _f("#o", "needs a website")]
        assert "Utrecht University" in describe(
            group_findings(findings, labels={"#o": "Utrecht University"})[0]
        )

    def test_an_orphan_cluster_reads_as_one_job(self):
        action = group_orphans([f"https://aopwiki.org/events/{n}" for n in range(36)])[0]
        sentence = describe(action)
        assert "36" in sentence
        assert "AOP" in sentence

    def test_an_unrecognised_finding_gets_a_plain_sentence(self):
        """Better vague than confidently wrong."""
        findings = [_f("#x", "some future shape nobody has seen"), _f("#x", "and another")]
        sentence = describe(group_findings(findings, labels={"#x": "Thing"})[0])
        assert "Thing" in sentence
        assert sentence.endswith(".")

    def test_a_deliberate_action_states_the_reason(self):
        findings = [_f("./", "SHOULD use PropertyValue entities for identifiers")]
        action = group_findings(findings)[0]
        assert "ISA" in describe(action)


class TestTheActionShape:
    def test_cleared_counts_the_findings(self):
        action = Action(key="k", kind="entity", subject="s", findings=["a", "b"])
        assert action.cleared == 2

    def test_an_action_carries_the_entities_it_touches(self):
        findings = [_f("#a", "needs an affiliation"), _f("#a", "needs a URL")]
        assert group_findings(findings)[0].entity_ids == ["#a"]
