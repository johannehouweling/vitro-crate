"""Some questions have several right answers, and the agent can now ask for them.

The HITL layer offered exactly two shapes: a decision (`present`, one of N) and
a free-text value (`request_input`). A question like "which entities are
affiliated with this organization?" is neither — its honest answer is often
three of the choices at once. Asked as one-of-N, the agent must either record a
single answer it knows is incomplete or invent a link, and inventing links into
a scientific record is the worse of the two.

`select_many` adds the many-of-N shape. It is an OPTIONAL capability, detected
per frontend rather than declared on the Protocol: adding a required method
would break every adapter and test double that already exists. A frontend
without one degrades to the single-choice prompt instead of failing.

The distinction these tests protect most carefully is **empty versus skipped**.
Confirming with nothing ticked means "none of these" — a real answer that should
stop the agent asking again. Cancelling means "I am not answering", which is
not the same thing, and a caller that conflates them either loses a user's
deliberate "none" or treats an unanswered question as settled.
"""

from __future__ import annotations

from builder.tools.hitl import (
    SCAN_ROOT_PURPOSE,
    ConsoleHumanInterface,
    SimulatedHumanInterface,
    select_many,
    supports_multi_choice,
)

OPTIONS = ["Timo Hamers", "Zhongli Chen", "Martin Scholze"]


class _SingleChoiceOnly:
    """A frontend written before multi-choice existed."""

    is_interactive = True

    def __init__(self, answer: str | None = None, action: str = "approved") -> None:
        self.answer = answer
        self.action = action
        self.seen: list[list[str]] = []

    def present(self, context, options=None, purpose=None):
        self.seen.append(list(options or []))
        return {"action": self.action, "comments": self.answer, "edits": None}

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}


class _MultiCapable:
    is_interactive = True

    def __init__(self, values, skipped=False) -> None:
        self.values = values
        self.skipped = skipped
        self.calls = 0

    def present(self, context, options=None, purpose=None):
        raise AssertionError("a multi-capable frontend must not be asked one-of-N")

    def request_input(self, prompt, field_type="text"):
        return {"value": None, "skipped": True}

    def select_many(self, context, options, purpose=None):
        self.calls += 1
        return {"values": list(self.values), "skipped": self.skipped}


class TestTheCapabilityIsDetected:
    def test_a_multi_capable_frontend_is_recognised(self):
        assert supports_multi_choice(_MultiCapable([])) is True

    def test_an_older_frontend_is_not(self):
        assert supports_multi_choice(_SingleChoiceOnly()) is False

    def test_no_frontend_at_all_is_not(self):
        assert supports_multi_choice(None) is False

    def test_the_console_frontend_offers_it(self):
        assert supports_multi_choice(ConsoleHumanInterface()) is True


class TestSeveralAnswersSurvive:
    def test_every_selected_option_comes_back(self):
        human = _MultiCapable(["Timo Hamers", "Martin Scholze"])
        result = select_many(human, "Who is affiliated?", OPTIONS)
        assert result["values"] == ["Timo Hamers", "Martin Scholze"]
        assert result["skipped"] is False

    def test_an_option_never_offered_is_refused(self):
        """A frontend bug must not put an unoffered value into the crate."""
        human = _MultiCapable(["Timo Hamers", "Someone Not Listed"])
        result = select_many(human, "Who?", OPTIONS)
        assert result["values"] == ["Timo Hamers"]


class TestNoneIsAnAnswerAndSkippingIsNot:
    def test_confirming_nothing_is_a_real_answer(self):
        human = _MultiCapable([], skipped=False)
        result = select_many(human, "Who?", OPTIONS)
        assert result["values"] == []
        assert result["skipped"] is False, "'none of these' is an answer, not a skip"

    def test_cancelling_is_a_skip(self):
        human = _MultiCapable([], skipped=True)
        result = select_many(human, "Who?", OPTIONS)
        assert result["values"] == []
        assert result["skipped"] is True


class TestOlderFrontendsStillWork:
    def test_it_degrades_to_the_single_choice_prompt(self):
        human = _SingleChoiceOnly(answer="Zhongli Chen")
        result = select_many(human, "Who?", OPTIONS)
        assert result["values"] == ["Zhongli Chen"]
        assert result["skipped"] is False
        assert human.seen == [OPTIONS], "the options must reach the fallback prompt"

    def test_a_rejected_single_choice_is_a_skip(self):
        result = select_many(_SingleChoiceOnly(action="rejected"), "Who?", OPTIONS)
        assert result["skipped"] is True

    def test_an_approval_carrying_no_option_is_a_skip(self):
        """Bare approval answers a yes/no, not a which-of-these."""
        result = select_many(_SingleChoiceOnly(answer=None), "Who?", OPTIONS)
        assert result["values"] == []
        assert result["skipped"] is True


class TestItNeverAnswersOnTheUsersBehalf:
    def test_the_headless_simulator_skips(self):
        """It must not fall through to `present`, which auto-approves."""
        result = select_many(SimulatedHumanInterface(), "Who?", OPTIONS)
        assert result["values"] == []
        assert result["skipped"] is True

    def test_no_interface_skips(self):
        assert select_many(None, "Who?", OPTIONS)["skipped"] is True

    def test_no_options_is_not_a_question(self):
        human = _MultiCapable(["x"])
        assert select_many(human, "Who?", [])["skipped"] is True
        assert human.calls == 0, "an empty pick-list must not reach the user"

    def test_a_scan_root_escalation_is_never_a_pick_list(self):
        """Widening filesystem access stays one fail-closed decision (#197)."""
        result = ConsoleHumanInterface().select_many(
            "Approve root?", ["/etc", "/home"], SCAN_ROOT_PURPOSE
        )
        assert result["values"] == []
        assert result["skipped"] is True


class TestTheConsoleReadsTheTerminal:
    def test_it_maps_picked_indices_back_to_options(self):
        human = ConsoleHumanInterface(select_many_func=lambda hint, choices: [0, 2])
        result = human.select_many("Who?", OPTIONS)
        assert result["values"] == ["Timo Hamers", "Martin Scholze"]
        assert result["skipped"] is False

    def test_cancelling_at_the_terminal_skips(self):
        human = ConsoleHumanInterface(select_many_func=lambda hint, choices: None)
        assert human.select_many("Who?", OPTIONS)["skipped"] is True

    def test_an_out_of_range_index_is_dropped(self):
        human = ConsoleHumanInterface(select_many_func=lambda hint, choices: [0, 99])
        assert human.select_many("Who?", OPTIONS)["values"] == ["Timo Hamers"]

    def test_the_question_reaches_the_chooser(self):
        seen: list[str] = []

        def chooser(hint, choices):
            seen.append(hint)
            return []

        ConsoleHumanInterface(select_many_func=chooser).select_many("Who is affiliated?", OPTIONS)
        assert seen == ["Who is affiliated?"]
