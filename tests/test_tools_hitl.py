"""Tests for the HITL HumanInterface protocol and engine injection."""

from __future__ import annotations

from builder.engine import AgentEngine
from builder.tools.hitl import (
    ConsoleHumanInterface,
    HumanInterface,
    InputResponse,
    SimulatedHumanInterface,
    is_interactive,
    present_to_human,
    request_input,
)


class MockHumanInterface:
    """Test double returning controlled responses and recording calls."""

    def __init__(self) -> None:
        self.present_calls: list[tuple[str, list[str] | None]] = []
        self.input_calls: list[tuple[str, str]] = []

    def present(self, context, options=None, purpose=None):
        self.present_calls.append((context, options))
        return {"action": "edited", "comments": "fix it", "edits": {"name": "X"}}

    def request_input(self, prompt, field_type="text"):
        self.input_calls.append((prompt, field_type))
        return {"value": "42", "skipped": False}


class TestSimulatedHumanInterface:
    """The default simulator implements the protocol and auto-approves."""

    def test_satisfies_human_interface_protocol(self):
        assert isinstance(SimulatedHumanInterface(), HumanInterface)

    def test_present_returns_approved_human_response(self):
        resp = SimulatedHumanInterface().present("Review investigation")
        assert resp == {"action": "approved", "comments": None, "edits": None}

    def test_request_input_returns_skipped_input_response(self):
        resp = SimulatedHumanInterface().request_input("DOI?", "identifier")
        assert resp == {"value": None, "skipped": True}

    def test_present_denies_scan_root_escalation(self):
        """Fail-closed (#197): the simulator must NOT auto-approve a request to
        add a new scan root — it cannot be the approver for filesystem access."""
        resp = SimulatedHumanInterface().present(
            "Approve scanning /etc?", options=["Approve", "Deny"], purpose="scan_root"
        )
        assert resp["action"] == "rejected"

    def test_present_still_approves_benign_checkpoints(self):
        """Non-scan-root checkpoints keep the convenient auto-approve behaviour."""
        resp = SimulatedHumanInterface().present("Review investigation", purpose="entity_review")
        assert resp["action"] == "approved"


class TestConsoleHumanInterface:
    """The CLI console interface is REAL-interactive and prompts via stdin."""

    def test_satisfies_human_interface_protocol(self):
        assert isinstance(ConsoleHumanInterface(), HumanInterface)

    def test_is_interactive_true(self):
        assert ConsoleHumanInterface().is_interactive is True
        assert is_interactive(ConsoleHumanInterface()) is True

    def test_present_approves_on_affirmative(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_a: "y")
        resp = ConsoleHumanInterface().present("Review entity")
        assert resp["action"] == "approved"

    def test_present_rejects_on_negative(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_a: "n")
        resp = ConsoleHumanInterface().present("Review entity")
        assert resp["action"] == "rejected"

    def test_present_scan_root_requires_explicit_yes(self, monkeypatch):
        """Fail-closed (#197): an empty answer must NOT approve a new scan root."""
        from builder.tools.hitl import SCAN_ROOT_PURPOSE

        monkeypatch.setattr("builtins.input", lambda *_a: "")
        resp = ConsoleHumanInterface().present(
            "Approve /etc?", options=["Approve", "Deny"], purpose=SCAN_ROOT_PURPOSE
        )
        assert resp["action"] == "rejected"

    def test_request_input_returns_value(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_a: "Acme Corp")
        resp = ConsoleHumanInterface().request_input("Name?")
        assert resp == {"value": "Acme Corp", "skipped": False}

    def test_request_input_empty_is_skip(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_a: "")
        resp = ConsoleHumanInterface().request_input("Name?")
        assert resp == {"value": None, "skipped": True}

    def test_request_input_eof_is_skip(self, monkeypatch):
        def _raise(*_a):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        resp = ConsoleHumanInterface().request_input("Name?")
        assert resp == {"value": None, "skipped": True}

    def test_request_input_uses_injected_prompt_func(self):
        """An injected prompt_func replaces the bare input() so the CLI can render
        the shared rounded box (#344); the field_type is threaded to the reader."""
        seen: list[str] = []

        def fake_box(field_type: str) -> str:
            seen.append(field_type)
            return "Acme Corp"

        resp = ConsoleHumanInterface(prompt_func=fake_box).request_input("Name?", "text")
        assert resp == {"value": "Acme Corp", "skipped": False}
        assert seen == ["text"]

    def test_injected_prompt_func_empty_is_skip(self):
        resp = ConsoleHumanInterface(prompt_func=lambda _ft: "").request_input("Name?")
        assert resp == {"value": None, "skipped": True}

    def test_injected_prompt_func_eof_is_skip(self):
        def _raise(_ft: str) -> str:
            raise EOFError

        resp = ConsoleHumanInterface(prompt_func=_raise).request_input("Name?")
        assert resp == {"value": None, "skipped": True}

    def test_request_input_displays_question_via_show_func(self):
        """An injected show_func renders the question so the CLI can style it as a
        green-● reply like the ReAct arm, instead of a bare print (#344)."""
        shown: list[str] = []

        resp = ConsoleHumanInterface(
            prompt_func=lambda _ft: "10.1234/x",
            show_func=shown.append,
        ).request_input("What is the DOI?", "identifier")

        assert resp == {"value": "10.1234/x", "skipped": False}
        assert shown == ["What is the DOI?"]


class TestIsInteractive:
    """The interactive signal distinguishes a real user from a headless run."""

    def test_simulated_interface_is_not_interactive(self):
        """The default simulator is headless — it must NOT be treated interactive."""
        assert SimulatedHumanInterface().is_interactive is False

    def test_helper_returns_false_for_simulated(self):
        assert is_interactive(SimulatedHumanInterface()) is False

    def test_helper_returns_false_for_none(self):
        """No interface at all (a headless engine) is non-interactive."""
        assert is_interactive(None) is False

    def test_helper_returns_false_when_attribute_absent(self):
        """An interface that does not declare the signal defaults to non-interactive."""

        class _Bare:
            def present(self, context, options=None, purpose=None):
                return {"action": "approved", "comments": None, "edits": None}

            def request_input(self, prompt, field_type="text"):
                return {"value": None, "skipped": True}

        assert is_interactive(_Bare()) is False

    def test_helper_returns_true_when_declared_interactive(self):
        class _Live:
            is_interactive = True

            def present(self, context, options=None, purpose=None):
                return {"action": "approved", "comments": None, "edits": None}

            def request_input(self, prompt, field_type="text"):
                return {"value": None, "skipped": True}

        assert is_interactive(_Live()) is True


class TestBackwardCompatibleFunctions:
    """The module-level functions still work via the default simulator."""

    def test_present_to_human_delegates_to_default_simulator(self):
        assert present_to_human("ctx", ["Approve"]) == {
            "action": "approved",
            "comments": None,
            "edits": None,
        }

    def test_request_input_delegates_to_default_simulator(self):
        resp: InputResponse = request_input("Name?")
        assert resp == {"value": None, "skipped": True}


class TestEngineHumanInterfaceInjection:
    """AgentEngine routes HITL tool calls through the injected interface."""

    def test_defaults_to_simulated_interface(self):
        engine = AgentEngine()
        assert isinstance(engine.human_interface, SimulatedHumanInterface)

    def test_accepts_injected_interface(self):
        mock = MockHumanInterface()
        engine = AgentEngine(human_interface=mock)
        assert engine.human_interface is mock

    def test_run_tool_present_uses_injected_interface(self):
        mock = MockHumanInterface()
        engine = AgentEngine(human_interface=mock)

        result = engine.run_tool("present_to_human", context="Review", options=["Approve", "Edit"])

        assert result == {
            "action": "edited",
            "comments": "fix it",
            "edits": {"name": "X"},
        }
        assert mock.present_calls == [("Review", ["Approve", "Edit"])]

    def test_run_tool_request_input_uses_injected_interface(self):
        mock = MockHumanInterface()
        engine = AgentEngine(human_interface=mock)

        result = engine.run_tool("request_input", prompt="Enter DOI", field_type="identifier")

        assert result == {"value": "42", "skipped": False}
        assert mock.input_calls == [("Enter DOI", "identifier")]

    def test_run_tool_present_defaults_to_simulated_when_not_injected(self):
        engine = AgentEngine()
        result = engine.run_tool("present_to_human", context="Review")
        assert result == {"action": "approved", "comments": None, "edits": None}


class TestConsoleAnimationSuspension:
    """A console HITL prompt must pause any registered terminal animation (e.g. the
    ReAct agent loop's "thinking" spinner) for the duration of ``input()`` — else
    the spinner repaints over the prompt and the user cannot read or answer it.
    """

    @staticmethod
    def _recording_anim(events: list[str]):
        class _Anim:
            def pause(self) -> None:
                events.append("pause")

            def resume(self) -> None:
                events.append("resume")

        return _Anim()

    def test_present_suspends_animation_around_input(self, monkeypatch):
        from builder.tools.hitl import (
            SCAN_ROOT_PURPOSE,
            ConsoleHumanInterface,
            register_console_animation,
            unregister_console_animation,
        )

        events: list[str] = []
        anim = self._recording_anim(events)
        register_console_animation(anim)
        try:
            monkeypatch.setattr(
                "builtins.input", lambda *a, **k: events.append("input") or "y"
            )
            resp = ConsoleHumanInterface().present("ctx", purpose=SCAN_ROOT_PURPOSE)
        finally:
            unregister_console_animation(anim)

        # The spinner is paused BEFORE the prompt is read and resumed AFTER.
        assert events == ["pause", "input", "resume"]
        assert resp["action"] == "approved"

    def test_request_input_suspends_animation_around_input(self, monkeypatch):
        from builder.tools.hitl import (
            ConsoleHumanInterface,
            register_console_animation,
            unregister_console_animation,
        )

        events: list[str] = []
        anim = self._recording_anim(events)
        register_console_animation(anim)
        try:
            monkeypatch.setattr(
                "builtins.input",
                lambda *a, **k: events.append("input") or "Methimazole",
            )
            resp = ConsoleHumanInterface().request_input("Name?", "text")
        finally:
            unregister_console_animation(anim)

        assert events == ["pause", "input", "resume"]
        assert resp == {"value": "Methimazole", "skipped": False}

    def test_animation_resumes_even_on_eof(self, monkeypatch):
        from builder.tools.hitl import (
            SCAN_ROOT_PURPOSE,
            ConsoleHumanInterface,
            register_console_animation,
            unregister_console_animation,
        )

        events: list[str] = []
        anim = self._recording_anim(events)

        def _raise_eof(*_a, **_k):
            events.append("input")
            raise EOFError

        register_console_animation(anim)
        try:
            monkeypatch.setattr("builtins.input", _raise_eof)
            resp = ConsoleHumanInterface().present("ctx", purpose=SCAN_ROOT_PURPOSE)
        finally:
            unregister_console_animation(anim)

        # resume must run even though input() raised (fail-closed deny).
        assert events == ["pause", "input", "resume"]
        assert resp["action"] == "rejected"

    def test_noop_without_registered_animation(self, monkeypatch):
        from builder.tools.hitl import ConsoleHumanInterface, suspend_console_animation

        # Nothing registered -> the context manager is a harmless no-op.
        with suspend_console_animation():
            pass
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        resp = ConsoleHumanInterface().present("ctx")
        assert resp["action"] == "approved"

    def test_unregister_only_clears_matching_animation(self, monkeypatch):
        from builder.tools.hitl import (
            register_console_animation,
            suspend_console_animation,
            unregister_console_animation,
        )

        events: list[str] = []
        anim = self._recording_anim(events)
        register_console_animation(anim)
        try:
            # Unregistering a DIFFERENT animation must not clear the active one.
            unregister_console_animation(object())
            with suspend_console_animation():
                events.append("body")
        finally:
            unregister_console_animation(anim)

        assert events == ["pause", "body", "resume"]


class TestPresentLetsTheUserTypeTheirOwnAnswer:
    """#596: a choice prompt can always be answered with words of the user's own.

    The console appends one row to every prompt except a scan-root escalation;
    picking it reads the shared ❯ box and the text comes back as an *edit*, so
    a caller that only understands the offered rows sees neither an approval
    nor a rejection it did not get.
    """

    def test_the_row_comes_after_the_callers_choices_and_the_default_stays(self):
        from builder.tools.hitl import OWN_ANSWER_CHOICE

        seen: dict[str, object] = {}

        def fake_select(choices: list[str], default: int) -> int:
            seen["choices"] = list(choices)
            seen["default"] = default
            return 0

        resp = ConsoleHumanInterface(select_func=fake_select).present(
            "Proceed?", options=["Yes, go ahead", "No, don't do that"]
        )

        assert seen["choices"] == ["Yes, go ahead", "No, don't do that", OWN_ANSWER_CHOICE]
        assert seen["default"] == 0
        assert resp["action"] == "approved"

    def test_picking_the_row_reads_the_box_and_returns_the_text_as_an_edit(self):
        shown: list[str] = []
        human = ConsoleHumanInterface(
            show_func=shown.append,
            select_func=lambda choices, _default: len(choices) - 1,
            prompt_func=lambda _ft: "  The corresponding author, not the PI  ",
        )

        resp = human.present("Who publishes the crate?", options=["Dr X", "RIVM"])

        assert resp == {
            "action": "edited",
            "comments": "The corresponding author, not the PI",
            "edits": {"value": "The corresponding author, not the PI"},
        }
        # The question is shown once, ahead of the choices — not again for the box.
        assert shown == ["Who publishes the crate?"]

    def test_the_row_is_there_when_the_caller_offered_no_choices(self):
        from builder.tools.hitl import OWN_ANSWER_CHOICE

        seen: dict[str, list[str]] = {}

        def fake_select(choices: list[str], default: int) -> int:
            seen["choices"] = list(choices)
            return len(choices) - 1

        resp = ConsoleHumanInterface(
            select_func=fake_select, prompt_func=lambda _ft: "neither"
        ).present("Review entity")

        assert seen["choices"][-1] == OWN_ANSWER_CHOICE
        assert len(seen["choices"]) == 3
        assert resp["action"] == "edited" and resp["comments"] == "neither"

    def test_an_empty_typed_answer_is_a_skip(self):
        human = ConsoleHumanInterface(
            select_func=lambda choices, _default: len(choices) - 1,
            prompt_func=lambda _ft: "   ",
        )
        assert human.present("Proceed?") == {"action": "skipped", "comments": None, "edits": None}

    def test_eof_in_the_box_is_a_skip_that_ends_guidance(self):
        def _raise(_ft: str) -> str:
            raise EOFError

        human = ConsoleHumanInterface(
            select_func=lambda choices, _default: len(choices) - 1, prompt_func=_raise
        )
        assert human.present("Proceed?")["action"] == "skipped"
        assert human.is_done()

    def test_a_typed_stop_word_ends_guidance(self):
        human = ConsoleHumanInterface(
            select_func=lambda choices, _default: len(choices) - 1,
            prompt_func=lambda _ft: "build",
        )
        assert human.present("Proceed?")["action"] == "skipped"
        assert human.is_done()

    def test_a_scan_root_escalation_stays_a_plain_allow_or_deny(self):
        """Widening filesystem access is one fail-closed decision, never prose (#197)."""
        from builder.tools.hitl import OWN_ANSWER_CHOICE, SCAN_ROOT_PURPOSE

        seen: dict[str, list[str]] = {}

        def fake_select(choices: list[str], default: int) -> int:
            seen["choices"] = list(choices)
            return default

        resp = ConsoleHumanInterface(select_func=fake_select).present(
            "Approve /etc?", purpose=SCAN_ROOT_PURPOSE
        )

        assert OWN_ANSWER_CHOICE not in seen["choices"]
        assert len(seen["choices"]) == 2
        assert resp["action"] == "rejected"

    def test_a_typed_answer_at_the_plain_terminal_fallback(self, monkeypatch):
        """No injected box: the numbered chooser still offers the row, and the
        value is read with a second ``input()``."""
        answers = iter(["3", "my own words"])
        monkeypatch.setattr("builtins.input", lambda *_a: next(answers))

        resp = ConsoleHumanInterface().present("Proceed?")

        assert resp["action"] == "edited"
        assert resp["edits"] == {"value": "my own words"}


class TestPresentToHumanAsksSeveralQuestionsInTurn:
    """#596: a model with three open items asks three questions, not one bundle.

    ``questions`` is asked one entry at a time through the same HITL door: an
    entry with ``options`` is a choice prompt, one without is a free-text field.
    Every exchange lands in ``user_answers`` and the tool result carries them all.
    """

    def test_each_question_is_asked_through_its_own_channel(self):
        mock = MockHumanInterface()
        engine = AgentEngine(human_interface=mock)

        result = engine.run_tool(
            "present_to_human",
            context="The workbook names Dr X at RIVM.",
            questions=[
                {"question": "Record Dr X as publisher?", "options": ["Yes", "No"]},
                {"question": "Culture medium for the uptake culture?"},
            ],
        )

        # The shared context is shown once, ahead of the first question only.
        assert mock.present_calls == [
            ("The workbook names Dr X at RIVM.\n\nRecord Dr X as publisher?", ["Yes", "No"])
        ]
        assert mock.input_calls == [("Culture medium for the uptake culture?", "text")]
        assert result == {
            "action": "answered",
            "answers": [
                {"question": "Record Dr X as publisher?", "answer": "fix it"},
                {"question": "Culture medium for the uptake culture?", "answer": "42"},
            ],
        }

    def test_every_exchange_is_remembered(self):
        engine = AgentEngine(human_interface=MockHumanInterface())

        engine.run_tool(
            "present_to_human",
            context="Two things.",
            questions=[
                {"question": "Publisher?", "options": ["Dr X", "RIVM"]},
                {"question": "Medium?"},
            ],
        )

        assert engine.state.user_answers == [
            {"question": "Publisher?", "answer": "fix it"},
            {"question": "Medium?", "answer": "42"},
        ]

    def test_a_skipped_free_text_question_says_so_and_is_not_remembered(self):
        engine = AgentEngine()  # the simulated interface skips every input

        result = engine.run_tool(
            "present_to_human", context="One thing.", questions=[{"question": "Medium?"}]
        )

        assert result == {
            "action": "answered",
            "answers": [{"question": "Medium?", "answer": "skipped"}],
        }
        assert engine.state.user_answers == []

    def test_a_yes_no_question_with_no_comment_reports_the_decision(self):
        engine = AgentEngine()  # the simulated interface approves every choice

        result = engine.run_tool(
            "present_to_human",
            context="One thing.",
            questions=[{"question": "Record Dr X?", "options": ["Yes", "No"]}],
        )

        assert result["answers"] == [{"question": "Record Dr X?", "answer": "approved"}]

    def test_blank_entries_are_dropped_and_an_empty_list_is_the_single_prompt(self):
        mock = MockHumanInterface()
        engine = AgentEngine(human_interface=mock)

        engine.run_tool(
            "present_to_human",
            context="Ctx",
            questions=[{"question": "   "}, {"question": "Real one?", "options": ["A"]}],
        )
        assert mock.present_calls == [("Ctx\n\nReal one?", ["A"])]

        mock.present_calls.clear()
        result = engine.run_tool(
            "present_to_human", context="Ctx", options=["A", "B"], questions=[]
        )
        assert mock.present_calls == [("Ctx", ["A", "B"])]
        assert result["action"] == "edited"
