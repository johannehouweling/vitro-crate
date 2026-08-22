"""What the status footer spends its width on.

Three complaints, all about the same thing — the line was showing facts that had
stopped being news:

* the cost read ``@$0.004821``, six digits nobody acts on, changing width as it
  grew so the whole segment jittered;
* ``54 files`` is settled before the first entity exists and never moves again,
  so a run spends its life looking at it;
* what a run actually wants there is what is still open — and, while the gate is
  REQUIRED, the honest answer for the other tiers is "not looked at yet", which
  is not the same as zero.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from builder.agents.ui import UiSnapshot, render_status_markup, status_field_values


def _plain(markup: str) -> str:
    return re.sub(r"\[[^\]]*\]", "", markup)


def _snap(**kw) -> UiSnapshot:
    # Annotated `dict[str, Any]`: inferred from the literal, ty narrows the value
    # type to `str | int | bool` and then rejects every `**base` field whose real
    # type is anything else (`assessed_tiers` is a tuple, `cost_usd` a float) — 21
    # errors for a helper that is correct. The annotation says what this genuinely
    # is: a bag of keyword defaults, typed by UiSnapshot at the call below.
    base: dict[str, Any] = dict(
        session_id="s1",
        entity_count=0,
        file_count=54,
        base_passed=True,
        isa_passed=True,
        tox_passed=True,
        required_issue_count=0,
    )
    base.update(kw)
    return UiSnapshot(**base)


class TestCost:
    @pytest.mark.parametrize(
        ("cost", "shown"),
        [(0.004821, "@$0.00"), (12.3456, "@$12.35"), (0.0, "@$0.00"), (999.999, "@$1000.00")],
    )
    def test_two_decimal_places(self, cost, shown):
        line = _plain(render_status_markup(_snap(tokens_in=1000, tokens_out=50, cost_usd=cost)))
        assert shown in line

    def test_no_cost_segment_when_unknown(self):
        line = _plain(render_status_markup(_snap(tokens_in=1000, tokens_out=50, cost_usd=None)))
        assert "@$" not in line


class TestTheMiddleSlot:
    def test_shows_the_scan_before_anything_is_drafted(self):
        line = _plain(render_status_markup(_snap(entity_count=0)))
        assert "54 files" in line

    def test_swaps_to_open_findings_once_drafting_starts(self):
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    required_issue_count=2,
                    should_issue_count=121,
                    may_issue_count=66,
                    assessed_tiers=("required", "recommended", "optional"),
                )
            )
        )
        assert "54 files" not in line
        assert "2 req" in line
        assert "121 rec" in line
        assert "66 opt" in line

    def test_unassessed_tiers_read_as_locked_not_zero(self):
        """A REQUIRED-gated sweep did not evaluate the other tiers at all.

        Printing `0 rec` there would claim a clean bill of health for checks that
        were never run — the same lie the maturity report used to ship when an
        export followed a REQUIRED-only validation.
        """
        line = _plain(
            render_status_markup(
                _snap(entity_count=97, required_issue_count=0, assessed_tiers=("required",))
            )
        )
        assert "0 req" in line
        assert "rec/opt locked" in line
        assert "0 rec" not in line
        assert "0 opt" not in line

    def test_partial_gate_locks_only_what_was_not_swept(self):
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    should_issue_count=121,
                    assessed_tiers=("required", "recommended"),
                )
            )
        )
        assert "121 rec" in line
        assert "opt locked" in line
        assert "rec/opt" not in line

    def test_nothing_validated_yet_says_so(self):
        line = _plain(render_status_markup(_snap(entity_count=3, assessed_tiers=())))
        assert "req/rec/opt locked" in line


class TestASweptTierNeverLocksAgain:
    """Retirement is about freshness, and the footer read it as ignorance.

    A REQUIRED-gated sweep over an edited crate retires the wider tiers — their
    findings described the crate as it was. The counts were then dropped and the
    tiers rendered exactly like tiers nobody had ever run, so a session that had
    been reporting "121 rec 66 opt" for half its life went back to "rec/opt
    locked". The user's rule: once a tier has been shown, it stays shown.
    """

    def test_a_retired_tier_keeps_its_count(self):
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    required_issue_count=3,
                    assessed_tiers=("required",),
                    stale_tier_counts=(("optional", 66), ("recommended", 121)),
                )
            )
        )
        assert "3 req" in line
        assert "121 rec?" in line
        assert "66 opt?" in line
        assert "locked" not in line

    def test_a_stale_zero_is_marked_unverified(self):
        """`0 rec` is a clean bill of health; `0 rec?` is what we last saw."""
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    assessed_tiers=("required",),
                    stale_tier_counts=(("recommended", 0),),
                )
            )
        )
        assert "0 rec?" in line

    def test_a_tier_nobody_ran_is_still_locked(self):
        """Only what has actually been swept is remembered — the rest is unknown."""
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    assessed_tiers=("required",),
                    stale_tier_counts=(("recommended", 121),),
                )
            )
        )
        assert "121 rec?" in line
        assert "opt locked" in line

    def test_a_fresh_sweep_wins_over_the_memory(self):
        """The stale count is a fallback, never something that shadows a result."""
        line = _plain(
            render_status_markup(
                _snap(
                    entity_count=97,
                    should_issue_count=4,
                    assessed_tiers=("required", "recommended"),
                    stale_tier_counts=(("recommended", 121),),
                )
            )
        )
        assert "4 rec" in line
        assert "121" not in line


class TestFader:
    def test_issue_counts_are_comparable_for_highlighting(self):
        """The fader tints a field when its value changes, so the new slot needs one."""
        before = status_field_values(_snap(entity_count=97, assessed_tiers=("required",)))
        after = status_field_values(
            _snap(
                entity_count=97,
                should_issue_count=121,
                assessed_tiers=("required", "recommended"),
            )
        )
        assert "issues" in before
        assert before["issues"] != after["issues"]


class TestTheDotsCannotContradictThemselves:
    """`● base  ○ ISA  ● Tox` says the crate fails ISA and passes ISA-Tox.

    It cannot. The profile is a stack — ISA-Tox is adopted on top of ISA, which
    is adopted on top of RO-Crate — so conformance is cumulative and a layer
    cannot pass where the layer it extends fails. The footer read the three
    per-pass flags raw, each of which reports only what its own layer adds.
    """

    def test_a_layer_cannot_pass_where_the_one_it_extends_fails(self) -> None:
        markup = render_status_markup(_snap(base_passed=True, isa_passed=False, tox_passed=True))

        dots = re.findall(r"\[(green|grey50)\]([●○])\[/\1\]", markup)
        assert len(dots) == 3, markup
        assert [d[1] for d in dots] == ["●", "○", "○"], markup

    def test_a_whole_stack_that_passes_is_untouched(self) -> None:
        markup = render_status_markup(_snap(base_passed=True, isa_passed=True, tox_passed=True))

        assert [d[1] for d in re.findall(r"\[(green|grey50)\]([●○])\[/\1\]", markup)] == [
            "●",
            "●",
            "●",
        ], markup

    def test_a_failing_base_takes_every_layer_with_it(self) -> None:
        markup = render_status_markup(_snap(base_passed=False, isa_passed=True, tox_passed=True))

        assert [d[1] for d in re.findall(r"\[(green|grey50)\]([●○])\[/\1\]", markup)] == [
            "○",
            "○",
            "○",
        ], markup
