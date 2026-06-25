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
