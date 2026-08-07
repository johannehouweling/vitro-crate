"""Every registered tool is reachable from the deterministic arm — or waived (#386).

The ReAct arm's ``TOOL_REGISTRY ⇄ TOOL_SPECS`` parity assert doubles as its
reachability guard: every registered tool is advertised, so the model can always
reach it. The pipeline arm had no analogue, and a capability with no call site
there stayed green in CI and surfaced only in a live run.

The first five tests are discriminators: each one **fails against a plausible
cheaper implementation** of the guard, which is what keeps this module from
being tautological. No test hand-builds a tool list — the expected side is always
the real ``TOOL_REGISTRY``, the measured side is always computed from the real
source tree.

Pure AST parse plus a registry import: no network, no LLM, no SHACL, no crate
build. Measured at ~0.4 s, so no timeout mark is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.tools.reachability import (
    PIPELINE_SEEDS,
    PIPELINE_UNREACHED,
    ToolReachabilityError,
    assert_pipeline_reachability,
    call_graph,
    reachable_functions,
    reachable_tools,
    unreached_tools,
)
from builder.tools.registry import TOOL_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestCallGraphDiscriminators:
    """Each test here is red against a cheaper guard that would ship the bug."""

    def test_call_graph_finds_a_transitive_composite_edge(self) -> None:
        """A tool reached only as a plain Python call, two modules away, counts.

        ``draft_process``'s route to the arm is
        ``pipeline.run_tool("draft_process_chain")`` -> ``composites.draft_process_chain``
        -> ``drafters.draft_process``. Only the first hop is a ``run_tool``
        literal; ``builder/tools/`` contains no ``run_tool`` call sites at all.

        Discriminates against: a run_tool-literal-only implementation, which
        would see 15 names and call ~45 tools unreachable.
        """
        assert "draft_process" in reachable_tools()
        assert "builder.tools.composites.draft_process_chain" in reachable_functions()

    def test_direct_import_counts_as_reachable(self) -> None:
        """Six tools reach the arm by direct import, never through ``run_tool``.

        Discriminates against: anything that only follows ``engine.run_tool``.
        (This set is also the standing counter-example to AGENTS.md's claim that
        the spine routes every step through ``engine.run_tool(...)``.)
        """
        by_import = {
            "save_session",
            "export_crate",
            "read_file",
            "extract_pdf_text",
            "lookup_orcid",
            "build_and_validate",
        }
        assert by_import <= reachable_tools()

    def test_state_method_name_is_not_reachability(self) -> None:
        """``engine.state.list_entities()`` is not the ``list_entities`` tool.

        Honesty control. The premise is asserted before the conclusion: the name
        appears in ``main.py`` — a *seed* module — as an attribute call, and the
        registered tool is still correctly unreached, because the receiver is a
        local object rather than an import binding.

        Discriminates against: bare attribute-name matching, and against a grep
        over the seed modules. Both would call this tool reachable.
        """
        main_src = (_REPO_ROOT / "main.py").read_text(encoding="utf-8")
        assert "state.list_entities()" in main_src, (
            "premise gone: main.py no longer calls the CrateState method, so "
            "this test no longer discriminates against name matching"
        )
        assert "list_entities" not in reachable_tools()

    def test_string_literal_mention_is_not_reachability(self) -> None:
        """A tool named in a string literal is not thereby called.

        Honesty control. ``check_provenance`` appears verbatim as a dict key in
        the profiler's icon map in ``builder/tools/dashboard.py``, and has no
        call site on the arm.

        Discriminates against: a grep implementation — i.e. this proves the guard
        would have caught the class of bug that motivated it.
        """
        dashboard_src = (_REPO_ROOT / "builder" / "tools" / "dashboard.py").read_text(
            encoding="utf-8"
        )
        assert '"check_provenance"' in dashboard_src, (
            "premise gone: the icon map no longer names check_provenance, so "
            "this test no longer discriminates against a grep guard"
        )
        assert "check_provenance" not in reachable_tools()

    def test_react_modules_are_not_graph_nodes(self) -> None:
        """Traversal terminates at the ReAct arm, so ``main.py`` is a safe seed.

        Without this, ``main.py`` (which wires both arms) would drag the entire
        ReAct arm in and every tool would look reachable — the guard would be
        vacuous rather than wrong, which is harder to notice.
        """
        react_nodes = [k for k in call_graph() if k.startswith("builder.agents.react.")]
        assert react_nodes == []


class TestTheGuard:
    """The invariant itself, and the waiver that records its exceptions."""

    def test_every_registered_tool_is_reachable_or_waived(self) -> None:
        unreached = unreached_tools()
        unwaived = unreached - set(PIPELINE_UNREACHED)
        stale = set(PIPELINE_UNREACHED) - unreached
        assert not unwaived, (
            f"registered tools with no call site on the deterministic arm and no "
            f"waiver in PIPELINE_UNREACHED: {sorted(unwaived)}"
        )
        assert not stale, (
            f"PIPELINE_UNREACHED names tools that are now reachable — delete "
            f"these rows: {sorted(stale)}"
        )

    def test_dropping_the_main_seed_loses_exactly_the_cli_resume_tools(self) -> None:
        """Mutation control: the verdict is computed, not a constant.

        ``main.py`` is the only seed whose removal changes the answer, and it
        changes it by exactly the two tools the CLI resume path owns. This is
        also the standing evidence for *why* ``main.py`` is a seed: without it
        both tools are reported as false failures.
        """
        full = reachable_tools()
        without_main = reachable_tools(PIPELINE_SEEDS - {"main"})
        assert full - without_main == {"list_sessions", "load_session"}

    def test_waiver_entries_carry_a_reason(self) -> None:
        for name, reason in PIPELINE_UNREACHED.items():
            assert isinstance(reason, str) and len(reason) >= 20, (
                f"waiver for {name!r} must state why, in a sentence"
            )

    def test_assert_pipeline_reachability_passes_on_the_current_tree(self) -> None:
        assert_pipeline_reachability()


class TestAssertBitesBothWays:
    """The two failure directions, mirroring tests/test_tools_spec.py."""

    def test_flags_an_unwaived_tool(self) -> None:
        snapshot = TOOL_REGISTRY.all()
        try:
            TOOL_REGISTRY.register("zz_unwired_probe", lambda **_: None)
            with pytest.raises(ToolReachabilityError) as excinfo:
                assert_pipeline_reachability()
            assert "zz_unwired_probe" in str(excinfo.value)
        finally:
            # ToolRegistry has no unregister; restore the snapshot wholesale.
            TOOL_REGISTRY._tools = dict(snapshot)  # noqa: SLF001
        assert_pipeline_reachability()

    def test_flags_a_stale_waiver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A waiver naming a reachable tool fails — what keeps it self-cleaning."""
        import builder.tools.reachability as reach

        stale = {**PIPELINE_UNREACHED, "resolve_compound": "wired, so this is stale"}
        monkeypatch.setattr(reach, "PIPELINE_UNREACHED", stale)
        with pytest.raises(ToolReachabilityError) as excinfo:
            reach.assert_pipeline_reachability()
        assert "resolve_compound" in str(excinfo.value)
