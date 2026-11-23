"""A crate built with nobody at the keyboard (``--unattended``).

The run builds from the input folder, asks nobody and stops: a question the agent
asks gets no answer, so nothing is approved or chosen on a person's behalf, and
what the deposit and the lookups do not state stays empty, a finding in the
report. The question is kept in the session, so a later ``--resume`` with
``--interactive`` can put it to a person. The exit code says whether a crate was
written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from builder.engine import AgentEngine
from builder.state import CrateState, StateSerializer
from builder.tools.hitl import SimulatedHumanInterface
from main import main, parse_args


def _stub_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import builder.config as cfg

    monkeypatch.setattr(cfg, "is_configured", lambda: True)
    monkeypatch.setattr(cfg, "load_config", lambda: {})
    monkeypatch.setattr(cfg, "merge_with_env", lambda c: None)


def _deposit(tmp_path: Path) -> Path:
    folder = tmp_path / "deposit"
    folder.mkdir()
    (folder / "README.txt").write_text("An assay.\n")
    return folder


class TestTheFlag:
    def test_it_builds_from_the_input_folder(self, tmp_path: Path) -> None:
        args = parse_args(["--unattended", "--input", str(tmp_path)])
        assert args.unattended is True
        assert args.interactive is False

    @pytest.mark.parametrize("other", ["--interactive", "--smoke-test"])
    def test_it_is_not_combined_with_a_mode_that_answers(self, other: str, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--unattended", other, "--input", str(tmp_path)])

    def test_it_needs_a_deposit_or_a_session(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--unattended"])


class TestTheRun:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, exports: bool
    ) -> tuple[int, dict[str, Any]]:
        import builder.agents.react.agent_loop as agent_loop

        _stub_config(monkeypatch)
        seen: dict[str, Any] = {}

        def _loop(engine: AgentEngine, **kw: Any) -> dict[str, Any]:
            seen.update(kw)
            seen["engine"] = engine
            if exports:
                agent_loop.mark_crate_written(engine, str(tmp_path / "crate"))
            return {"stop_reason": "completed", "error": None}

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _loop)
        code = main(["--unattended", "--input", str(_deposit(tmp_path))])
        return code, seen

    def test_the_loop_runs_with_nobody_to_answer(self, monkeypatch, tmp_path) -> None:
        from builder.tools.hitl import is_interactive

        _, seen = self._run(monkeypatch, tmp_path, exports=True)

        assert seen["interactive"] is False
        assert not is_interactive(seen["engine"].human_interface)

    def test_the_agent_is_told_nobody_answers(self, monkeypatch, tmp_path) -> None:
        from builder.agents.build import UNATTENDED_INSTRUCTION

        _, seen = self._run(monkeypatch, tmp_path, exports=True)

        assert seen["initial_prompt"] == UNATTENDED_INSTRUCTION
        assert "nobody" in UNATTENDED_INSTRUCTION.lower()

    def test_a_written_crate_is_success(self, monkeypatch, tmp_path) -> None:
        code, _ = self._run(monkeypatch, tmp_path, exports=True)
        assert code == 0

    def test_no_crate_is_a_failure(self, monkeypatch, tmp_path, capsys) -> None:
        code, _ = self._run(monkeypatch, tmp_path, exports=False)
        assert code == 1
        assert "No crate was written" in capsys.readouterr().out

    def test_the_unanswered_questions_are_named_at_the_end(self, monkeypatch, tmp_path, capsys):
        import builder.agents.react.agent_loop as agent_loop

        _stub_config(monkeypatch)

        def _loop(engine: AgentEngine, **kw: Any) -> dict[str, Any]:
            engine.run_tool("request_input", prompt="Which medium was used?")
            agent_loop.mark_crate_written(engine, str(tmp_path / "crate"))
            return {"stop_reason": "completed", "error": None}

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _loop)
        assert main(["--unattended", "--input", str(_deposit(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "1 question" in out
        assert "--resume" in out and "--interactive" in out


class TestAQuestionNobodyAnswers:
    def _engine(self) -> AgentEngine:
        return AgentEngine(state=CrateState(), human_interface=SimulatedHumanInterface())

    def test_a_choice_is_neither_approved_nor_made(self) -> None:
        engine = self._engine()

        result = engine.run_tool(
            "present_to_human", context="Which H4 is meant?", options=["CVCL_1239", "CVCL_6C19"]
        )

        assert result["action"] == "skipped"
        assert engine.state.user_answers == []
        assert engine.state.unanswered_questions == ["Which H4 is meant?"]

    def test_a_free_text_question_is_kept(self) -> None:
        engine = self._engine()

        result = engine.run_tool("request_input", prompt="Which medium was used?")

        assert result["skipped"] is True
        assert engine.state.unanswered_questions == ["Which medium was used?"]

    def test_each_question_of_a_form_is_kept(self) -> None:
        engine = self._engine()

        engine.run_tool(
            "present_to_human",
            context="Two things:",
            questions=[{"question": "Which medium?"}, {"question": "Which line?", "options": ["A", "B"]}],
        )

        assert engine.state.unanswered_questions == ["Which medium?", "Which line?"]

    def test_a_question_asked_twice_is_kept_once(self) -> None:
        engine = self._engine()

        engine.run_tool("request_input", prompt="Which medium was used?")
        engine.run_tool("request_input", prompt="Which medium was used?")

        assert engine.state.unanswered_questions == ["Which medium was used?"]

    def test_a_person_who_skips_leaves_no_open_question(self) -> None:
        """A person at the keyboard chose not to answer: that is an answer of its own."""

        class _Person(SimulatedHumanInterface):
            is_interactive = True

        engine = AgentEngine(state=CrateState(), human_interface=_Person())

        engine.run_tool("request_input", prompt="Which medium was used?")

        assert engine.state.unanswered_questions == []

    def test_the_question_is_kept_with_the_session(self) -> None:
        state = CrateState()
        state.record_unanswered_question("Which medium was used?")

        again = StateSerializer.from_dict(StateSerializer.to_dict(state))

        assert again.unanswered_questions == ["Which medium was used?"]

    def test_an_answer_given_later_closes_it(self) -> None:
        state = CrateState()
        state.record_unanswered_question("Which medium was used?")

        state.record_user_answer("Which medium was used?", "DMEM")

        assert state.unanswered_questions == []


class TestAResumedSessionPutsThemToAPerson:
    def test_the_brief_names_them(self) -> None:
        from builder.agents.react.agent_loop import _format_unanswered_questions

        engine = AgentEngine(state=CrateState())
        engine.state.record_unanswered_question("Which medium was used?")

        brief = _format_unanswered_questions(engine)

        assert "Which medium was used?" in brief

    def test_the_brief_says_nothing_when_none_are_open(self) -> None:
        from builder.agents.react.agent_loop import _format_unanswered_questions

        assert _format_unanswered_questions(AgentEngine(state=CrateState())) == ""


class TestTheHeadlessInterfaceDecidesNothing:
    def test_a_checkpoint_is_not_approved(self) -> None:
        response = SimulatedHumanInterface().present("Proceed?", ["Yes", "No"])
        assert response["action"] == "skipped"

    def test_a_new_folder_is_still_refused(self) -> None:
        from builder.tools.hitl import SCAN_ROOT_PURPOSE

        response = SimulatedHumanInterface().present("Read /data?", purpose=SCAN_ROOT_PURPOSE)
        assert response["action"] == "rejected"
