"""Tests for the HITL HumanInterface protocol and engine injection."""

from __future__ import annotations

from builder.engine import AgentEngine
from builder.tools.hitl import (
    HumanInterface,
    InputResponse,
    SimulatedHumanInterface,
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
