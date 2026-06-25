"""Tests for the evaluation corpus (offline, no validation runs here).

The corpus is a small fixed set of crate-build cases described as data. Each case
declares an input prompt, the kind of input it represents, and (for offline
testing) an optional ``build_state`` factory the *mock* agent uses to stand in for
a real build. These tests assert the corpus shape, not conformance — conformance
is exercised in the runner test under a 120s timeout.
"""

from __future__ import annotations

from eval.corpus import DEFAULT_CORPUS, EvalCase


class TestEvalCase:
    def test_fields_are_accessible(self) -> None:
        case = EvalCase(
            case_id="c1",
            description="desc",
            kind="minimal",
            prompt="build it",
        )
        assert case.case_id == "c1"
        assert case.description == "desc"
        assert case.kind == "minimal"
        assert case.prompt == "build it"
        # input_path / build_state are optional.
        assert case.input_path is None
        assert case.build_state is None


class TestDefaultCorpus:
    def test_is_non_empty_and_sized_three_to_four(self) -> None:
        assert 3 <= len(DEFAULT_CORPUS) <= 4

    def test_case_ids_are_unique(self) -> None:
        ids = [c.case_id for c in DEFAULT_CORPUS]
        assert len(ids) == len(set(ids))

    def test_covers_the_three_input_kinds(self) -> None:
        kinds = {c.kind for c in DEFAULT_CORPUS}
        assert {"minimal", "structured", "unstructured"} <= kinds

    def test_every_case_has_a_prompt(self) -> None:
        for c in DEFAULT_CORPUS:
            assert c.prompt, f"case {c.case_id} has no prompt"

    def test_structured_case_points_at_an_existing_in_repo_input(self) -> None:
        from pathlib import Path

        structured = [c for c in DEFAULT_CORPUS if c.kind == "structured"]
        assert structured, "expected a structured-metadata case"
        for c in structured:
            assert c.input_path is not None
            assert Path(c.input_path).exists(), f"{c.input_path} must be in-repo"
