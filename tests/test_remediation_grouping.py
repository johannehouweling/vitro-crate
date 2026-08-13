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


def _f(entity_id, message, prop=None, severity="recommended"):
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
    # NB: "required", not "MUST". These used to say MUST/SHOULD — the verbs the
    # SHACL messages are written in — and passed while the production vocabulary
    # ("required"/"recommended"/"optional") matched no rank at all, so every
    # action tied and tier ordering was a no-op. A test that supplies a
    # vocabulary no producer emits is how that shipped green; see
    # TestTheTierVocabularyMatchesTheValidator, which pins the two together.

    def test_a_required_finding_outranks_a_recommended_one(self):
        findings = [
            _f("#a", "one", severity="recommended"),
            _f("#a", "two", severity="required"),
        ]
        assert group_findings(findings)[0].tier == "REQUIRED"

    def test_actions_are_ordered_by_tier_then_reach(self):
        findings = [
            _f("#a", "one", severity="recommended"),
            _f("#a", "two", severity="recommended"),
            _f("#b", "three", severity="required"),
            _f("#b", "four", severity="required"),
            _f("#b", "five", severity="required"),
        ]
        actions = group_findings(findings)
        assert actions[0].tier == "REQUIRED"
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


class TestTheTierVocabularyMatchesTheValidator:
    """`_TIER_RANK` must key on the words the validator actually emits.

    It keyed on "MUST"/"SHOULD"/"MAY" — the verbs the SHACL *messages* are
    written in — while `group_findings` derives the tier from
    ``finding["severity"]``, which `builder/tools/validation.py` sets to
    "required"/"recommended"/"optional". Nothing matched, `.get(tier, 3)`
    returned 3 for every action, and the tier term dropped silently out of the
    sort key. The report then ordered purely by size and the cap hid the work
    that blocks the build.

    Two vocabularies that must agree, in two files, with no type to bind them —
    so it is asserted rather than trusted.
    """

    def test_every_severity_the_validator_emits_has_a_rank(self) -> None:
        from builder.tools.remediation import _TIER_RANK

        # The literal set builder/tools/validation.py writes into issue_records.
        for severity in ("required", "recommended", "optional"):
            assert severity.upper() in _TIER_RANK, (
                f"the validator emits severity={severity!r} and the report has no "
                f"rank for it, so every action ties and tier ordering is a no-op"
            )

    def test_required_work_outranks_bulk_advisory_work(self) -> None:
        """The user-visible consequence, at the size where it bites."""
        from builder.tools.remediation import _TIER_RANK, group_findings

        records = [
            {"profile": "tox", "severity": "optional", "entity_id": f"#e{i}",
             "message": f"SHOULD have a {field} of kind {i}"}
            for i in range(12)
            for field in ("description", "url")
        ]
        records.append({
            "profile": "base", "severity": "required", "entity_id": "./",
            "message": "The root Dataset MUST have a licence",
        })

        live = [a for a in group_findings(records, labels={}) if a.actionable and a.cleared]
        live.sort(key=lambda a: (_TIER_RANK.get(a.tier, 3), -a.cleared, a.subject))
        assert live[0].tier == "REQUIRED", (
            "a required conformance failure must lead the list; sorted by size "
            "alone it ranked 12th of 13 and fell past the cap entirely"
        )

    def test_an_unknown_severity_sorts_last_rather_than_first(self) -> None:
        """A new validator severity should rank LOW, not be promoted above real
        conformance failures by defaulting to the strongest tier."""
        from builder.tools.remediation import _TIER_RANK, _strongest

        assert _strongest(["SOMETHING_NEW", "OPTIONAL"]) == "OPTIONAL"
        assert _TIER_RANK.get("SOMETHING_NEW", 3) == 3


class TestTheDateInstructionNamesTheRightDate:
    """A bare "date" needle answered every date question with the same sentence.

    `_wanted` substring-matches the joined message blob, so "date" fired on
    datePublished and dateModified alike — and on any message containing the
    letters, e.g. "validate" — and returned "Add the date it was created". A
    specific, confident, wrong instruction, which is worse than none: the
    contract for `describe()` is the most specific instruction that FITS.
    """

    def test_each_date_property_gets_its_own_instruction(self) -> None:
        from builder.tools.remediation import _wanted

        assert _wanted(["A Dataset SHOULD have a dateCreated"]) == "Add the date it was created"
        assert _wanted(["A Dataset SHOULD have a datePublished"]) == (
            "Add the date it was published"
        )
        assert _wanted(["A Dataset SHOULD have a dateModified"]) == (
            "Add the date it was last modified"
        )

    def test_a_word_that_merely_contains_date_does_not_trip_it(self) -> None:
        for message in ("the crate must validate against the profile", "SHOULD have an update"):
            from builder.tools.remediation import _wanted

            assert "date it was" not in _wanted([message]), (
                f"{message!r} answered with a date instruction"
            )


class TestActionsNameEntitiesAReaderRecognises:
    """The list said "PropertyValue pv sample role" while the crate knew the name.

    `labels` is keyed on the graph's `@id` (`#Thing`), but the validator reports
    the same entity resolved against the crate base (`./#Thing`). Not one node in
    a real crate carries the `./#` form, so the exact-match lookup missed EVERY
    fragment entity and each one fell through to mangling its id — in a list
    whose whole job is telling a human what to go and fix.
    """

    def test_a_validator_id_finds_the_graph_name(self):
        from builder.tools.remediation import _entity_label

        labels = {"#PropertyValue_pv_sample_role": "sample role"}
        assert _entity_label("./#PropertyValue_pv_sample_role", labels) == "sample role"
        # …and the bare form still works, so nothing that resolved before stops.
        assert _entity_label("#PropertyValue_pv_sample_role", labels) == "sample role"

    def test_an_unnamed_entity_is_described_not_reduced_to_digits(self):
        """A reader shown "0000-0002-7685-9462" cannot tell a person from a grant.

        This entity has no name ON PURPOSE in the data — "MUST have a name" is
        the finding being reported — so there is nothing to look up and nothing
        may be invented. Saying what the thing IS costs nothing and is honest.
        """
        from builder.tools.remediation import _entity_label

        assert (
            _entity_label("https://orcid.org/0000-0002-7685-9462", {})
            == "the person with ORCID 0000-0002-7685-9462"
        )
        assert _entity_label("https://ror.org/012p63287", {}) == (
            "the organization with ROR 012p63287"
        )

    def test_a_name_in_the_graph_still_wins_over_the_description(self):
        """The control: describing is the FALLBACK, never a replacement. A reader
        searches the report for the name the crate uses."""
        from builder.tools.remediation import _entity_label

        labels = {"https://orcid.org/0000-0002-7685-9462": "Nathalie Dierichs"}
        assert (
            _entity_label("https://orcid.org/0000-0002-7685-9462", labels)
            == "Nathalie Dierichs"
        )

    def test_the_root_is_called_the_crate_not_a_dot_slash(self):
        from builder.tools.remediation import _entity_label

        assert _entity_label("./", {}) == "the crate itself"

    def test_the_repeated_type_token_is_dropped(self):
        """`_make_entity_id` builds `#<Type>_<internal_id>` and the internal id
        usually repeats the type, so stripping one layer left "Sample sample proc
        … output sample"."""
        from builder.tools.remediation import _entity_label

        assert _entity_label("./#Sample_sample_proc_x_output_sample", {}) == (
            "proc x output sample"
        )
        assert _entity_label("./#LabProcess_proc_exposure_process", {}) == "exposure process"


class TestTheSplitSentenceIsTheSameSentence:
    """`describe_parts` exists so the HTML can weight the two halves differently.

    It must not become a second, drifting wording: the plain-text sentence and
    the rendered one are the same claim, and a reader comparing them should not
    find two different instructions.
    """

    def test_the_parts_rejoin_into_describe_exactly(self):
        from builder.tools.remediation import Action, describe, describe_parts

        actions = [
            Action(key="k", kind="entity", subject="a.csv and 15 others",
                   findings=["SHOULD have an identifier"] * 16),
            Action(key="k", kind="orphan", subject="the AOP-Wiki subgraph",
                   findings=["unreachable"] * 36),
            Action(key="k", kind="property", subject="name", findings=["x"] * 3),
            Action(key="k", kind="entity", subject="X", findings=["y"],
                   actionable=False, note="Left as-is."),
        ]
        for action in actions:
            instruction, subject = describe_parts(action)
            joined = f"{instruction} {subject}".strip() if subject else instruction
            assert joined == describe(action)
