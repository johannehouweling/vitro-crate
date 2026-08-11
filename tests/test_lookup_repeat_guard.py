"""A lookup is asked once, and its answer is handed back after that.

Session 20260810_201118 re-issued `lookup_orcid("0009-0000-5074-6239")` EIGHT
times. The first took 0.7s; the other seven were served from the lookup cache in
0.0s each and still cost a full ~6s model turn — ~42s spent re-reading an answer
the model already held. Only the generic idle ladder noticed, after five
repeats, because `_STATE_QUERY_TOOLS` never covered the lookup family.

A lookup is not a state query. Its answer is a pure function of its arguments
against an external registry, so this guard keys on (name, args) with no
fingerprint — the observed loop drafted people between the repeats, and every
one of those drafts would have reset a state-keyed guard.
"""

from __future__ import annotations

import pytest

from builder.agents.react.agent_loop import _LOOKUP_TOOLS, _build_langchain_tools
from builder.engine import AgentEngine


@pytest.fixture
def engine():
    return AgentEngine()


def _tool(engine, name):
    return next(t for t in _build_langchain_tools(engine) if t.name == name)


class TestTheSetDoesNotDrift:
    def test_every_registered_lookup_is_guarded(self):
        """A new lookup_* must not quietly fall outside the guard."""
        from builder.agents.react.tools_spec import TOOL_SPECS

        registered = {
            str(spec["name"]) for spec in TOOL_SPECS if str(spec["name"]).startswith("lookup_")
        }
        assert registered == _LOOKUP_TOOLS


class TestRepeatsAreServedFromTheFirstAnswer:
    def test_second_identical_call_does_not_reach_the_registry(self, engine, monkeypatch):
        calls = []

        def run_tool(tool_name, **kw):
            calls.append((tool_name, kw))
            return {"found": True, "data": {"name": "Jane Doe"}, "error": None}

        monkeypatch.setattr(engine, "run_tool", run_tool)
        tool = _tool(engine, "lookup_orcid")

        first = tool.invoke({"orcid_id": "0000-0001-6004-8653"})
        second = tool.invoke({"orcid_id": "0000-0001-6004-8653"})

        assert len(calls) == 1
        assert first == {"found": True, "data": {"name": "Jane Doe"}, "error": None}
        assert isinstance(second, str)
        assert "already been answered" in second
        assert "Jane Doe" in second

    def test_suppression_starts_at_the_first_repeat(self, engine, monkeypatch):
        """Not the third — the answer cannot change, so one repeat is enough."""
        calls = []
        monkeypatch.setattr(
            engine, "run_tool", lambda tool_name, **kw: calls.append(kw) or {"found": True}
        )
        tool = _tool(engine, "lookup_ror")
        for _ in range(4):
            tool.invoke({"name": "Maastricht University"})
        assert len(calls) == 1

    def test_different_arguments_are_a_different_question(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(
            engine, "run_tool", lambda tool_name, **kw: calls.append(kw) or {"found": True}
        )
        tool = _tool(engine, "lookup_compound")
        tool.invoke({"name": "Amiodarone"})
        tool.invoke({"name": "Chlorpyrifos"})
        assert len(calls) == 2

    def test_an_edit_does_not_re_enable_the_lookup(self, engine, monkeypatch):
        """The differentiator from `_guard_state_query`.

        The observed loop interleaved draft_person with the repeats. A guard
        keyed on the crate fingerprint would have been reset by each one; a
        lookup does not depend on the crate, so this one is not.
        """
        calls = []
        monkeypatch.setattr(
            engine, "run_tool", lambda tool_name, **kw: calls.append(kw) or {"found": True}
        )
        tool = _tool(engine, "lookup_orcid")
        tool.invoke({"orcid_id": "0000-0001-6004-8653"})

        from builder.tools.drafters import draft_person

        draft_person(engine.state, "Somebody New", {})

        tool.invoke({"orcid_id": "0000-0001-6004-8653"})
        assert len(calls) == 1


class TestFailuresAreHandledByKind:
    def test_a_transient_failure_is_retried(self, engine, monkeypatch):
        """A momentary outage must never freeze into 'you already asked that'."""
        calls = []

        def run_tool(tool_name, **kw):
            calls.append(kw)
            return {"found": False, "data": {}, "error": "timed out", "transient": True}

        monkeypatch.setattr(engine, "run_tool", run_tool)
        tool = _tool(engine, "lookup_doi")
        tool.invoke({"doi": "10.1016/j.tox.2021.152898"})
        tool.invoke({"doi": "10.1016/j.tox.2021.152898"})
        assert len(calls) == 2

    def test_a_definitive_failure_repeats_its_own_fix(self, engine, monkeypatch):
        """The lookup already said what to do; don't invent a weaker instruction."""
        monkeypatch.setattr(
            engine,
            "run_tool",
            lambda tool_name, **kw: {
                "found": False,
                "data": {},
                "error": "no public record",
                "transient": False,
                "fix": "Draft the person without one — draft_person(name='...').",
            },
        )
        tool = _tool(engine, "lookup_orcid")
        tool.invoke({"orcid_id": "0000-0009-9999-9999"})
        second = tool.invoke({"orcid_id": "0000-0009-9999-9999"})
        assert "draft_person(name='...')" in second
        assert "Do that now." in second

    def test_a_not_found_without_a_fix_still_gets_a_direction(self, engine, monkeypatch):
        monkeypatch.setattr(
            engine,
            "run_tool",
            lambda tool_name, **kw: {"found": False, "data": {}, "error": "nope"},
        )
        tool = _tool(engine, "lookup_cell_line")
        tool.invoke({"accession": "CVCL_0000"})
        second = tool.invoke({"accession": "CVCL_0000"})
        assert "definitive not-found" in second
        assert "do not look it up again" in second

    def test_a_tool_body_error_is_not_remembered_as_an_answer(self, engine, monkeypatch):
        """A ValueError out of the tool body is a bug, not a registry verdict."""
        calls = []

        def run_tool(tool_name, **kw):
            calls.append(kw)
            raise ValueError("bad argument")

        monkeypatch.setattr(engine, "run_tool", run_tool)
        tool = _tool(engine, "lookup_unit")
        first = tool.invoke({"unit_string": "millimolar"})
        assert "error" in first
        tool.invoke({"unit_string": "millimolar"})
        assert len(calls) == 2


class TestNonLookupsAreUntouched:
    def test_a_mutation_is_never_guarded_by_this(self, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(
            engine, "run_tool", lambda tool_name, **kw: calls.append(kw) or {"ok": True}
        )
        tool = _tool(engine, "validate")
        tool.invoke({"crate_path": "/x"})
        tool.invoke({"crate_path": "/x"})
        assert len(calls) == 2
