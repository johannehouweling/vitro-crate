"""The report caps what it shows, never what a reader can reach.

The advisory tiers are capped per profile group so the page stays a page. That
cap used to end at "+9 further recommended findings not listed here" — a number
with no way to reach it. The crate's own report is the one place those findings
exist, so a reader who wanted them had nowhere else to look.

The overflow now sits in a nested `<details>`: closed by default, so the page is
the length it was, and one click from complete.
"""

from __future__ import annotations

import re
from typing import cast

import pytest

from builder.writers.maturity_report import _SUGGESTION_CAPS, _capped_tier_items

# `_SUGGESTION_CAPS` is `int | None` — REQUIRED is deliberately uncapped. These
# tests are about the capped tiers, so narrow once here rather than at every use.
REC_CAP = cast(int, _SUGGESTION_CAPS["recommended"])
assert REC_CAP is not None, "the recommended tier is expected to have a cap"


def _items(n):
    return [f"<li>Recommended: finding {i}</li>" for i in range(n)]


class TestTheOverflowIsReachable:
    def test_everything_over_the_cap_is_still_in_the_html(self):
        cap = REC_CAP
        html = "".join(_capped_tier_items("recommended", _items(cap + 7)))
        for i in range(cap + 7):
            assert f"finding {i}</li>" in html, f"finding {i} is unreachable"

    def test_the_remainder_is_behind_a_fold(self):
        cap = REC_CAP
        html = "".join(_capped_tier_items("recommended", _items(cap + 7)))
        assert 'details class="more-fold"' in html
        assert "<summary>+7 further recommended findings</summary>" in html

    def test_no_dead_end_text_remains(self):
        html = "".join(_capped_tier_items("recommended", _items(30)))
        assert "not listed here" not in html

    def test_the_visible_list_is_still_capped(self):
        """The page stays bounded — that is what the cap is for."""
        cap = REC_CAP
        items = _capped_tier_items("recommended", _items(cap + 7))
        before_fold = "".join(items).split('<li class="more">')[0]
        assert before_fold.count("<li>") == cap


class TestItStaysOutOfTheWayOtherwise:
    def test_nothing_over_the_cap_adds_no_fold(self):
        cap = REC_CAP
        html = "".join(_capped_tier_items("recommended", _items(cap)))
        assert "more-fold" not in html

    def test_a_single_extra_reads_as_singular(self):
        cap = REC_CAP
        html = "".join(_capped_tier_items("recommended", _items(cap + 1)))
        assert "+1 further recommended finding<" in html

    def test_required_is_never_capped(self):
        """Those block conformance, so every one is named up front."""
        assert _SUGGESTION_CAPS["required"] is None
        html = "".join(_capped_tier_items("required", _items(40)))
        assert "more-fold" not in html
        assert html.count("<li>") == 40

    @pytest.mark.parametrize("tier", ["recommended", "optional"])
    def test_an_empty_tier_renders_nothing(self, tier):
        assert _capped_tier_items(tier, []) == []
