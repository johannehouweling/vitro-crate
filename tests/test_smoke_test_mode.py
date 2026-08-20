"""Tests for ``--smoke-test`` — driving the interactive build with nobody there.

The mode exists to exercise the HITL path (the guidance tail included) unattended:
every choice prompt confirms its PRE-SELECTED option and every open field is
answered with the literal ``"yes, continue"``. Three of its properties are
load-bearing and are pinned here rather than left to inspection:

1. ``is_interactive`` is True — the tail is gated on that single signal, so the
   headless ``SimulatedHumanInterface`` cannot be used to exercise it;
2. a scan-root escalation is still DENIED. That is *accidental* correctness (the
   pre-selection for a ``scan_root`` purpose happens to be the refusal, #197), so
   these tests fail loudly if the pre-selection rule ever changes — an unattended
   mode that silently widened filesystem access is the worst bug this could carry;
3. the run SAYS the answers are synthesised, at the start and beside the exported
   crate path — and writes no such marker into the crate itself (D5).

No LLM, no network, no SHACL: the build wiring is exercised with injected runners.
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState
from builder.tools.hitl import (
    SCAN_ROOT_PURPOSE,
    SMOKE_TEST_ANSWER,
    SYNTHETIC_ANSWER_NOTICE,
    ConsoleHumanInterface,
    HumanInterface,
    SmokeTestHumanInterface,
    answers_are_synthetic,
    is_interactive,
)
from main import main, parse_args


class TestProtocolAndInteractiveSignal:
    """The mode must present as a REAL interactive frontend or it does nothing."""

    def test_satisfies_human_interface_protocol(self):
        assert isinstance(SmokeTestHumanInterface(), HumanInterface)

    def test_is_interactive_true(self):
        """The guidance tail is gated on this one signal (AGENTS.md §14.6.1).

        Behind a non-interactive interface ``run_interactive_build`` degrades to
        the automated pipeline + export and ``run_guidance`` is never called — so
        a smoke mode reporting False would exercise nothing it was asked to.
        """
        assert SmokeTestHumanInterface().is_interactive is True
        assert is_interactive(SmokeTestHumanInterface()) is True

    def test_declares_its_answers_synthetic(self):
        assert answers_are_synthetic(SmokeTestHumanInterface()) is True

    def test_console_interface_is_not_marked_synthetic(self):
        """The marker must be opt-in: a real person's answers are not synthetic."""
        from builder.tools.hitl import ConsoleHumanInterface, SimulatedHumanInterface

        assert answers_are_synthetic(ConsoleHumanInterface()) is False
        assert answers_are_synthetic(SimulatedHumanInterface()) is False
        assert answers_are_synthetic(None) is False


class TestConfirmsThePreSelection:
    """Every choice prompt takes the row an Enter would have taken."""

    def test_default_approval_prompt_approves(self):
        """With no options the prompt is the yes/no approval, pre-selected yes."""
        resp = SmokeTestHumanInterface().present("Review the investigation")
        assert resp["action"] == "approved"

    def test_draft_confirmation_approves(self):
        """The guidance tail's draft-confirm prompt offers ``["approve", "reject"]``.

        Confirming its pre-selection is what lets a drafted value commit and the
        loop advance to the next gap — the path the mode exists to walk.
        """
        resp = SmokeTestHumanInterface().present(
            "Approve to commit this drafted value", options=["approve", "reject"]
        )
        assert resp["action"] == "approved"

    def test_a_menu_of_alternatives_is_skipped_not_answered(self):
        """A real menu is not a pre-selection to confirm — it is a claim to make.

        The only such prompt in the tree is the ambiguous-author escalation, whose
        caller reads ``comments`` to learn which candidate was picked. Returning
        row 1 would have this harness assert WHICH HUMAN wrote a paper, on the
        strength of candidate ordering. Confirming means taking the answer an
        Enter would give; there is no such answer in a list of people, so the mode
        declines — the same line ``select_many`` refuses to cross.
        """
        options = ["Ada Lovelace — 0000-0001", "Ada Byron — 0000-0002", "None of these / skip"]
        resp = SmokeTestHumanInterface().present("Pick the correct one", options)
        assert resp["action"] == "skipped"
        assert resp["comments"] is None, "a skipped menu must not hand back a pick"

    def test_negative_pre_selection_is_a_rejection(self):
        """When the pre-selected row is a refusal, confirming it REJECTS.

        The mode confirms the pre-selection; it does not approve on principle.
        """
        resp = SmokeTestHumanInterface().present(
            "Proceed?", options=["No, don't do that", "Yes, go ahead"]
        )
        assert resp["action"] == "rejected"


class TestScanRootStaysFailClosed:
    def test_refused_even_when_no_option_reads_as_a_refusal(self):
        """The case that was actually broken (found in review).

        `_default_choice_index` falls back to the LAST option when nothing reads
        as negative, so a caller offering ["Show me the folder first", "Yes,
        allow this folder"] got an APPROVAL out of this mode. No production
        caller passes options today, which is exactly why it was invisible — and
        exactly why the refusal cannot be inherited from a rule written for a
        human at a keyboard.
        """
        resp = SmokeTestHumanInterface().present(
            "Approve this folder?",
            ["Show me the folder first", "Yes, allow this folder"],
            SCAN_ROOT_PURPOSE,
        )
        assert resp["action"] == "rejected"

    """#197: an unattended mode must NEVER widen filesystem access.

    The deny is inherited, not implemented: ``_default_choice_index`` pre-selects
    the refusal for a ``scan_root`` purpose. These tests exist so a change to that
    rule breaks HERE instead of silently handing the smoke mode the filesystem.
    """

    def test_default_scan_root_prompt_is_denied(self):
        resp = SmokeTestHumanInterface().present(
            "The agent wants to scan /etc", purpose=SCAN_ROOT_PURPOSE
        )
        assert resp["action"] == "rejected"

    def test_denied_even_when_the_allow_option_is_listed_first(self):
        """The pre-selection must track the REFUSAL, not the ordinal position.

        If ``_default_choice_index`` ever regressed to "the first option" for a
        scan-root escalation, this is the assertion that catches it: the allowing
        choice is deliberately offered first.
        """
        resp = SmokeTestHumanInterface().present(
            "Approve scanning /etc?",
            options=["Yes, allow this folder", "No, keep the current access"],
            purpose=SCAN_ROOT_PURPOSE,
        )
        assert resp["action"] == "rejected"

    def test_denied_when_no_option_is_recognisably_negative(self):
        """No identifiable refusal => nothing is safe to pre-approve => deny.

        The pre-selection falls to the last option, which is not an explicit
        affirmative, so the escalation is still refused.
        """
        resp = SmokeTestHumanInterface().present(
            "Approve scanning /etc?",
            options=["Grant access", "Grant access recursively"],
            purpose=SCAN_ROOT_PURPOSE,
        )
        assert resp["action"] == "rejected"

    def test_engine_refuses_an_unapproved_scan_through_the_real_guard(self, tmp_path):
        """End-to-end through ``engine.run_tool`` — not just the interface in isolation.

        The engine consults ``present(purpose="scan_root")`` only for an
        interactive human (a non-interactive one is refused earlier), so the smoke
        interface genuinely reaches the escalation — and is refused, leaving
        ``approved_scan_roots`` empty.
        """
        secret = tmp_path / "not-approved"
        secret.mkdir()
        (secret / "secret.txt").write_text("classified\n")

        engine = AgentEngine(human_interface=SmokeTestHumanInterface())
        engine.initialize()
        result = engine.run_tool("scan_files", path=str(secret))

        assert engine.state.approved_scan_roots == set(), "smoke mode must not widen access"
        assert not (result.get("files") if isinstance(result, dict) else result)
        # The refusal must come from the ESCALATION the smoke interface answered,
        # not from an earlier guard — otherwise this test would pass without ever
        # exercising the pre-selection that does the denying.
        assert "declined" in str(result.get("message", "") if isinstance(result, dict) else "")


class TestOpenFieldsAndOptionalCapabilities:
    """Open fields get the literal answer; the optional capabilities are deliberate."""

    def test_request_input_answers_with_the_literal_string(self):
        resp = SmokeTestHumanInterface().request_input("Who ran the study?")
        assert resp == {"value": SMOKE_TEST_ANSWER, "skipped": False}

    def test_answer_is_a_commit_not_a_skip(self):
        """A skip would leave guidance nothing to commit, so no gap would resolve
        and the commit -> re-assess -> next-gap path would never run."""
        assert SmokeTestHumanInterface().request_input("Describe the assay")["skipped"] is False

    def test_select_many_skips(self):
        """Nothing is pre-ticked in a many-of-N box, so there is no pre-selection
        to confirm; picking a subset would be inventing an answer."""
        resp = SmokeTestHumanInterface().select_many(
            "Which entities does this apply to?", ["Assay A", "Assay B"]
        )
        assert resp == {"values": [], "skipped": True}

    def test_select_many_is_native_so_it_never_degrades_to_present(self):
        """Routed through the shared helper, the answer must stay a skip.

        Without a native ``select_many`` the helper degrades to ``present``, whose
        pre-selection would come back looking like a deliberately ticked box.
        """
        from builder.tools.hitl import select_many, supports_multi_choice

        human = SmokeTestHumanInterface()
        assert supports_multi_choice(human) is True
        assert select_many(human, "Which apply?", ["A", "B"]) == {"values": [], "skipped": True}

    def test_is_done_is_always_false(self):
        """The mode never ends guidance early — saying "done" would exercise
        nothing. Termination is left to run_guidance's own bounds."""
        human = SmokeTestHumanInterface()
        assert human.is_done() is False
        assert human.is_done() is False  # and it never flips, however much is asked


class TestIdentifiersAreNeverSynthesised:
    """D5: ``"yes, continue"`` must not be able to become a CAS or an ORCID."""

    def test_guidance_forces_an_identifier_commit_to_a_skip(self):
        """The guidance interpret fallback skips identifier-bearing fields.

        Confirming the guard still holds for THIS answer rather than assuming it:
        the same reply commits happily on a descriptive field.
        """
        from builder.agents.pipeline.guidance import _deterministic_decision
        from builder.tools.gap_analysis import Gap

        def _gap(prop: str) -> Gap:
            return Gap(
                tier="MUST",
                source="shacl",
                entity_id="#compound-1",
                entity_type="MolecularEntity",
                property=prop,
                message="missing",
                suggestion=None,
                fix_hint="ask-user",
                auto_fixable=False,
            )

        assert _deterministic_decision(_gap("identifier"), SMOKE_TEST_ANSWER) == {"action": "skip"}
        assert _deterministic_decision(_gap("description"), SMOKE_TEST_ANSWER) == {
            "action": "commit",
            "value": SMOKE_TEST_ANSWER,
        }

    def test_pasted_orcid_prose_is_rejected_by_verification(self):
        """The citation-author escalation asks for an ORCID as free text.

        The smoke answer is pasted in, but an HITL-supplied ORCID is still resolved
        and name-matched before use, so the prose resolves to nothing rather than
        landing on a Person.
        """
        from builder.tools.composites import _resolve_via_search

        looked_up: list[str] = []

        def fake_lookup_by_name(given, family, affiliation=None):
            # Two candidates => ambiguous => the escalation runs.
            return [
                {"given": "A.", "family": family, "orcid": "0000-0001-0000-0001"},
                {"given": "Alice", "family": family, "orcid": "0000-0002-0000-0002"},
            ]

        def fake_lookup_orcid(orcid_id):
            looked_up.append(orcid_id)
            return {"found": False}

        chosen = _resolve_via_search(
            "Alice",
            "Smith",
            None,
            SmokeTestHumanInterface(),
            fake_lookup_orcid,
            fake_lookup_by_name,
        )

        assert chosen is None
        # The pick (or the pasted prose) was verified against the lookup, never
        # trusted on the strength of the answer alone.
        assert looked_up, "an HITL-chosen identifier must be verified before use"


class TestTheRunSaysTheAnswersAreSynthetic:
    """The notice is printed at the start AND beside the exported crate path."""

    def test_notice_names_the_literal_answer(self):
        """Whoever reads the scrollback must be able to recognise the placeholder
        where it landed — in a name, a description — so the notice quotes it."""
        assert SMOKE_TEST_ANSWER in SYNTHETIC_ANSWER_NOTICE
        assert "SMOKE TEST" in SYNTHETIC_ANSWER_NOTICE

    def test_export_line_is_followed_by_the_notice(self, tmp_path):
        """Emitted by the build, immediately after the crate path (#233 line).

        Scrollback read later must not present a crate full of placeholder prose
        as a real one, so the two lines are adjacent by construction.
        """
        from builder.agents.build import run_interactive_build

        out_dir = tmp_path / "smoke-ro-crate"
        engine = AgentEngine(state=CrateState(), human_interface=SmokeTestHumanInterface())
        engine.initialize()
        engine.state.metadata.output_path = str(out_dir)
        lines: list[str] = []

        def fake_exporter(state: CrateState, **kw: Any) -> dict[str, Any]:
            return {"success": True, "crate_path": str(out_dir), "error": None}

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: {"ok": True, "conformance": {}, "issues": []},
            guidance_runner=lambda eng, human, **kw: {"resolved": [], "asked": []},
            exporter=fake_exporter,
            output=lines.append,
        )

        path_index = next(i for i, line in enumerate(lines) if line.startswith("Crate written to:"))
        assert lines[path_index + 1] == SYNTHETIC_ANSWER_NOTICE

    def test_a_real_frontend_gets_no_notice(self, tmp_path):
        """A build whose answers came from a person must not be labelled synthetic."""
        from builder.agents.build import run_interactive_build
        from builder.tools.hitl import SimulatedHumanInterface

        out_dir = tmp_path / "plain-ro-crate"
        engine = AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())
        engine.initialize()
        engine.state.metadata.output_path = str(out_dir)
        lines: list[str] = []

        run_interactive_build(
            engine,
            pipeline_runner=lambda eng: {"ok": True, "conformance": {}, "issues": []},
            exporter=lambda state, **kw: {
                "success": True,
                "crate_path": str(out_dir),
                "error": None,
            },
            output=lines.append,
        )

        assert not any("SMOKE TEST" in line for line in lines)


class TestTheConversationTerminates:
    """The bound that makes ``--smoke-test --react`` finite.

    Every OTHER channel this mode drives stops on its own: guidance runs out of
    gaps, the report runs out of findings, ``max_rounds`` bounds the rest. A
    conversational loop has no such bound — it ends when the person says so. And
    :data:`SMOKE_TEST_ANSWER` is not a stop word, so an unbounded synthetic
    interface does not hang (the old failure mode) but something worse: it drives
    turns forever, one model call each. These tests exist for that.
    """

    def test_the_conversation_channel_runs_out(self):
        from builder.tools.hitl import (
            CONVERSATION_FIELD_TYPE,
            SMOKE_TEST_ANSWER,
            SMOKE_TEST_CONVERSATION_TURNS,
            SmokeTestHumanInterface,
        )

        human = SmokeTestHumanInterface()
        answers = [
            human.request_input("next?", CONVERSATION_FIELD_TYPE)
            for _ in range(SMOKE_TEST_CONVERSATION_TURNS + 3)
        ]
        driven = answers[:SMOKE_TEST_CONVERSATION_TURNS]
        after = answers[SMOKE_TEST_CONVERSATION_TURNS:]
        assert [a["value"] for a in driven] == [SMOKE_TEST_ANSWER] * len(driven)
        # A SKIP, which the loop reads as end-of-input — not a "quit" string,
        # which would be this mode typing a command the user never typed.
        assert all(a["skipped"] and a["value"] is None for a in after)

    def test_the_budget_does_not_bleed_into_metadata_fields(self):
        """The two channels are separate. A field is a field however long the
        conversation ran — bounding open fields would silently stop answering
        guidance questions, which is the path the mode exists to exercise."""
        from builder.tools.hitl import (
            CONVERSATION_FIELD_TYPE,
            SMOKE_TEST_ANSWER,
            SmokeTestHumanInterface,
        )

        human = SmokeTestHumanInterface(conversation_turns=1)
        human.request_input("next?", CONVERSATION_FIELD_TYPE)
        assert human.request_input("next?", CONVERSATION_FIELD_TYPE)["skipped"] is True
        for _ in range(5):
            assert human.request_input("describe the study") == {
                "value": SMOKE_TEST_ANSWER,
                "skipped": False,
            }

    def test_a_real_frontend_is_not_bounded(self):
        """The console interface must not inherit any of this: a person is not on
        a turn budget, and `conversation` is just a text field to them."""
        from builder.tools.hitl import CONVERSATION_FIELD_TYPE, ConsoleHumanInterface

        typed = ["first", "second", "third", "fourth"]
        human = ConsoleHumanInterface(prompt_func=lambda _ft: typed.pop(0))
        for expected in ("first", "second", "third", "fourth"):
            assert human.request_input("next?", CONVERSATION_FIELD_TYPE)["value"] == expected


class TestTheWallClockBudget:
    """``--smoke-test 20`` — run for a while, then wind down and export.

    Time is exercised with real budgets rather than a patched clock: a budget of
    an hour cannot expire inside a test, and one of 60 microseconds cannot fail to
    have expired after a millisecond of sleep. Both directions are decided by
    margins of several orders of magnitude, so neither depends on scheduling.
    """

    # Far enough away that nothing in this file can reach it.
    LIVE = 60.0
    # Already spent by the time the constructor returns.
    SPENT = 1e-6

    def _expired(self):
        """An interface whose budget is definitively spent."""
        import time

        human = SmokeTestHumanInterface(minutes=self.SPENT)
        time.sleep(0.001)
        return human

    def test_the_clock_supersedes_the_turn_cap(self):
        """The whole point of asking for twenty minutes. A turn count of 3 would
        otherwise stop the run in under a minute, which is the opposite of what
        the flag was used for."""
        from builder.tools.hitl import CONVERSATION_FIELD_TYPE, SMOKE_TEST_CONVERSATION_TURNS

        human = SmokeTestHumanInterface(minutes=self.LIVE)
        turns = SMOKE_TEST_CONVERSATION_TURNS + 5
        answers = [human.request_input("next?", CONVERSATION_FIELD_TYPE) for _ in range(turns)]
        assert all(a["value"] == SMOKE_TEST_ANSWER for a in answers)
        assert not any(a["skipped"] for a in answers)

    def test_a_spent_clock_ends_the_conversation(self):
        from builder.tools.hitl import CONVERSATION_FIELD_TYPE

        assert self._expired().request_input("next?", CONVERSATION_FIELD_TYPE) == {
            "value": None,
            "skipped": True,
        }

    def test_a_spent_clock_ends_guidance(self):
        """run_guidance consults is_done() at the top of every round, so this is
        what stops the DEFAULT arm — the conversational skip only reaches ReAct."""
        assert self._expired().is_done() is True

    def test_without_a_budget_guidance_is_never_cut_short(self):
        """The control, and the pre-existing contract: an unbudgeted smoke test
        must exercise the tail to exhaustion, not return before asking anything."""
        assert SmokeTestHumanInterface().is_done() is False
        assert SmokeTestHumanInterface(minutes=self.LIVE).is_done() is False

    def test_a_question_in_flight_is_still_answered(self):
        """"Winds down at the next question", not "stops mid-question". The loops
        read the deadline BETWEEN gaps/turns; a field asked as it lapses is still
        answered, so nothing lands half-applied."""
        assert self._expired().request_input("describe the study") == {
            "value": SMOKE_TEST_ANSWER,
            "skipped": False,
        }

    def test_the_deadline_does_not_move_with_the_wall_date(self):
        """Monotonic, not a wall date: a clock adjustment mid-run must not end a
        session early or extend one forever."""
        human = SmokeTestHumanInterface(minutes=self.LIVE)
        assert human._deadline is not None
        # A monotonic reading, which is an uptime-relative float — never a POSIX
        # timestamp (~1.7e9 and climbing).
        assert human._deadline < 1e9


class TestTheLoopReadsTheInterfaceNotStdin:
    """The rewiring itself, driven against the REAL loop.

    Everything else in this file stubs ``run_interactive_agent`` out, so nothing
    there would notice the read site reverting to ``ui.boxed_input``. These drive
    the actual loop with ``boxed_input`` booby-trapped: if the conversational read
    ever goes back to stdin, it raises instead of blocking forever, which is the
    only way a test can catch the old failure mode at all.
    """

    def _stub_model(self, monkeypatch):
        """Get the driver as far as the prompt without a provider or a network."""
        import builder.agents.react.agent_loop as loop_mod

        monkeypatch.setattr(loop_mod, "_build_chat_model", lambda **kw: object())
        monkeypatch.setattr(
            loop_mod, "_build_agent_graph", lambda llm, tools, engine=None: object()
        )

    def _trap_stdin(self, monkeypatch):
        import builder.agents.ui as ui

        def _boom(*a, **kw):
            raise AssertionError("the loop read stdin instead of the HumanInterface")

        monkeypatch.setattr(ui, "boxed_input", _boom)

    def test_a_synthetic_interface_drives_and_ends_the_session(self, monkeypatch):
        """Budget 0 => the first read is a skip, which ends the session the way
        Ctrl+D does — no model turn is spent proving the wiring."""
        import builder.agents.react.agent_loop as loop_mod

        self._stub_model(monkeypatch)
        self._trap_stdin(monkeypatch)

        engine = AgentEngine(human_interface=SmokeTestHumanInterface(conversation_turns=0))
        engine.initialize()
        # Returns rather than hanging or raising: the loop asked the interface,
        # got a skip, finalised and broke.
        loop_mod.run_interactive_agent(engine)

    def test_a_real_frontend_still_reads_stdin(self, monkeypatch):
        """The control. Without it the test above passes just as well on a loop
        that never prompts at all, which would be a different bug entirely."""
        import builder.agents.react.agent_loop as loop_mod

        self._stub_model(monkeypatch)
        self._trap_stdin(monkeypatch)

        engine = AgentEngine(human_interface=ConsoleHumanInterface(prompt_func=lambda _ft: "hi"))
        engine.initialize()
        with pytest.raises(AssertionError, match="read stdin"):
            loop_mod.run_interactive_agent(engine)


class TestCli:
    """``--smoke-test`` implies --interactive and wires the mode on both arms."""

    def _stub_config(self, monkeypatch):
        """Make the interactive path proceed without a real LLM config check."""
        import builder.config as cfg

        monkeypatch.setattr(cfg, "is_configured", lambda: True)
        monkeypatch.setattr(cfg, "load_config", lambda: {})
        monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)

    def test_flag_defaults_off(self):
        assert parse_args([]).smoke_test is False
        assert parse_args([]).interactive is False

    def test_minutes_are_optional_and_the_bare_flag_is_not_a_budget(self):
        """`--smoke-test` stays a boolean; `--smoke-test 20` is a number of
        minutes. The distinction is `isinstance(x, float)`, which is False for
        True — unlike `isinstance(x, int)`, which would read the bare flag as a
        one-minute budget."""
        assert parse_args(["--smoke-test"]).smoke_test is True
        assert parse_args(["--smoke-test", "20"]).smoke_test == 20.0
        assert isinstance(parse_args(["--smoke-test"]).smoke_test, float) is False
        assert parse_args(["--smoke-test", "0.5"]).smoke_test == 0.5

    def test_a_non_positive_budget_is_refused_not_silently_ignored(self):
        """0 and -5 are FALSY, so accepting them would leave `args.smoke_test`
        falsy and quietly run an ordinary interactive build — waiting forever on
        a person who is not there. Exactly the silent misfire the flag exists to
        prevent, so argparse rejects them."""
        for bad in ("0", "-5", "abc"):
            with pytest.raises(SystemExit):
                parse_args(["--smoke-test", bad])

    def test_the_budget_reaches_the_interface(self, monkeypatch, capsys, tmp_path):
        """End to end: the number on the command line becomes the interface's
        deadline, and the run says so before spending anything."""
        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        seen: list[Any] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: seen.append(engine.human_interface) or {"pipeline": {}},
        )

        assert main(["--smoke-test", "20", "--input", str(d)]) == 0
        human = seen[0]
        assert isinstance(human, SmokeTestHumanInterface)
        assert human._deadline is not None, "the budget never reached the interface"
        assert human.is_done() is False, "20 minutes must not be spent on arrival"
        assert "20 minute" in capsys.readouterr().out

    def test_a_bare_smoke_test_sets_no_deadline(self):
        """The control for the test above — without a number nothing is on a
        clock, so the turn cap stays the bound."""
        assert SmokeTestHumanInterface()._deadline is None

    def test_flag_implies_interactive(self):
        """On its own it would have nothing to answer — a batch run never prompts."""
        args = parse_args(["--smoke-test"])
        assert args.smoke_test is True
        assert args.interactive is True

    def test_react_is_driven_not_refused(self, monkeypatch, capsys, tmp_path):
        """The combination this mode most needs, and the one it used to refuse.

        The refusal was real while it stood: the ReAct loop read its conversation
        straight off stdin, so a synthetic interface had nothing to answer and the
        run sat on an empty terminal. Now that the read goes through the
        interface, refusing would be turning away the arm a smoke test is FOR.
        """
        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        seen: list[Any] = []

        import builder.agents.react.agent_loop as agent_loop

        monkeypatch.setattr(
            agent_loop,
            "run_interactive_agent",
            lambda engine, *a, **kw: seen.append(engine.human_interface),
        )

        assert main(["--smoke-test", "--react", "--input", str(d)]) == 0
        assert len(seen) == 1, "the ReAct loop must actually be started"
        assert isinstance(seen[0], SmokeTestHumanInterface)
        # The notice is not skipped just because this is the other arm.
        assert "SMOKE TEST" in capsys.readouterr().out

    def test_wires_the_smoke_interface_and_prints_the_opening_notice(
        self, monkeypatch, capsys, tmp_path
    ):
        """The engine handed to the build answers itself and reports interactive."""
        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        seen: list[Any] = []

        import builder.agents.build as build_mod

        def _capture(engine, **kw):
            seen.append(engine.human_interface)
            return {"pipeline": {}, "guidance": None}

        monkeypatch.setattr(build_mod, "run_interactive_build", _capture)

        assert main(["--smoke-test", "--input", str(d)]) == 0
        assert len(seen) == 1
        assert isinstance(seen[0], SmokeTestHumanInterface)
        assert is_interactive(seen[0]) is True
        # The notice lands BEFORE the build, so an accidental --smoke-test is
        # obvious immediately rather than only at the end.
        assert "SMOKE TEST" in capsys.readouterr().out

    def test_plain_interactive_still_gets_the_console_interface(self, monkeypatch, tmp_path):
        """No --smoke-test => the real stdin frontend, unchanged."""
        from builder.tools.hitl import ConsoleHumanInterface

        self._stub_config(monkeypatch)
        d = tmp_path / "data"
        d.mkdir()
        (d / "test.txt").write_text("hello\n")
        seen: list[Any] = []

        import builder.agents.build as build_mod

        monkeypatch.setattr(
            build_mod,
            "run_interactive_build",
            lambda engine, **kw: seen.append(engine.human_interface) or {"pipeline": {}},
        )

        assert main(["--interactive", "--input", str(d)]) == 0
        assert isinstance(seen[0], ConsoleHumanInterface)
