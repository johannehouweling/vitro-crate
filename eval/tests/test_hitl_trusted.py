"""Tests for the eval-only ``TrustedCorpusHumanInterface`` (scan-root fairness).

The A/B harness compares ReAct vs pipeline over trusted, in-repo corpus fixtures.
The production-default :class:`~builder.tools.hitl.SimulatedHumanInterface` is
fail-closed on scan-root escalations (#197/#198), which hobbles the ReAct arm: it
is refused when it explores a fixture directory the pipeline arm never has to ask
for, so the A/B measures a security handicap rather than the architectures. This
interface approves scan-root escalations for the trusted corpus ONLY, keeping the
A/B fair.

These tests are offline (no LLM, no live build): they exercise the interface and
the single engine seam (:meth:`~builder.engine.AgentEngine._authorize_scan_root`)
that the fairness fix turns on.
"""

from __future__ import annotations

from pathlib import Path

from builder.engine import AgentEngine
from builder.tools.hitl import SCAN_ROOT_PURPOSE, SimulatedHumanInterface
from eval.hitl import TrustedCorpusHumanInterface


class TestTrustedCorpusInterface:
    def test_is_a_simulated_interface(self) -> None:
        # Subclasses the headless default, so existing isinstance checks (and the
        # inherited request_input skip behaviour) still hold.
        assert isinstance(TrustedCorpusHumanInterface(), SimulatedHumanInterface)

    def test_is_interactive_is_true(self) -> None:
        # The engine fails closed on a non-interactive human BEFORE it consults
        # present(), so the interface must present as interactive to be reached.
        assert TrustedCorpusHumanInterface().is_interactive is True

    def test_approves_scan_root_escalation(self) -> None:
        # The production default DENIES this; the trusted-corpus interface approves.
        decision = TrustedCorpusHumanInterface().present(
            context="scan /trusted/corpus/fixture", purpose=SCAN_ROOT_PURPOSE
        )
        assert decision["action"] == "approved"

    def test_approves_benign_checkpoint(self) -> None:
        decision = TrustedCorpusHumanInterface().present(context="review entity")
        assert decision["action"] == "approved"

    def test_request_input_still_skips(self) -> None:
        # Inherited from SimulatedHumanInterface: nothing ever blocks on a real
        # stdin (the eval is headless).
        resp = TrustedCorpusHumanInterface().request_input("name?")
        assert resp["skipped"] is True
        assert resp["value"] is None


class TestEngineApprovesTrustedScanRoot:
    """The behaviour the A/B fairness turns on, at the engine authorisation seam.

    ``_authorize_scan_root`` returns ``None`` to let a scan proceed (and adds the
    directory to ``approved_scan_roots``), or a refusal dict the caller must return
    instead of scanning. Behind the production default it fails closed; behind the
    trusted-corpus interface it approves.
    """

    def test_simulated_interface_refuses_unapproved_root(self, tmp_path: Path) -> None:
        engine = AgentEngine(human_interface=SimulatedHumanInterface())
        result = engine._authorize_scan_root(str(tmp_path))
        # Fail-closed: a refusal dict is returned and nothing joins the allowlist.
        assert result is not None
        assert engine.state.approved_scan_roots == set()

    def test_trusted_interface_approves_unapproved_root(self, tmp_path: Path) -> None:
        engine = AgentEngine(human_interface=TrustedCorpusHumanInterface())
        result = engine._authorize_scan_root(str(tmp_path))
        # Approved: the scan proceeds (None) and the directory joins the allowlist.
        assert result is None
        assert str(tmp_path.resolve()) in engine.state.approved_scan_roots
