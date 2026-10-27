"""The deposit's folder tree states its studies and assays (#852).

Every build minted one Investigation, one Study and one Assay and hung every
file off that one Assay. S-VHPS16 ships four assay folders and all three builds
of it carried one Assay. The folder tree is the depositor's own statement of the
structure, so it is summarised, labelled by one bounded model call, and the
labelling is accepted only when fixed checks hold; otherwise the build keeps
one study and one assay and says so.

The fixtures are the member lists of six real deposits, paths only.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from builder.state import CrateState, FileClassification
from builder.tools.deposit_structure import (
    check_structure,
    describe_structure,
    fallback_structure,
    tree_summary,
)
from builder.tools.document_discovery import classify_file

TREES = Path("tests/fixtures/deposit_trees")
# A fake absolute root: nothing in these tests reads the disk.
ROOT = "/deposits"


def _deposit(name: str) -> tuple[list[FileClassification], str]:
    root = f"{ROOT}/{name}"
    files = []
    for rel in (TREES / f"{name}.txt").read_text(encoding="utf-8").splitlines():
        filename = PurePosixPath(rel).name
        classification, _ = classify_file(filename, "", rel)
        files.append(
            FileClassification(
                path=f"{root}/{rel}",
                filename=filename,
                size=1,
                mime_type="application/octet-stream",
                classification=classification,
            )
        )
    return files, root


def _study(folder: str, *assays: str) -> dict[str, Any]:
    return {
        "folder": folder,
        "name": PurePosixPath(folder).name or "Deposit",
        "assays": [{"folder": a, "name": PurePosixPath(a).name} for a in assays],
    }


# What the spike's model call returned for each real deposit: the labellings the
# checks must accept.
LABELLED: dict[str, dict[str, Any]] = {
    "svhps16": {
        "studies": [
            _study(
                "Exposure scenarios",
                "Exposure scenarios/Assays/Alamar blue analysis",
                "Exposure scenarios/Assays/LDH analysis",
                "Exposure scenarios/Assays/BMD analysis",
                "Exposure scenarios/Assays/Internal dose",
            )
        ]
    },
    "wp23": {
        "studies": [
            _study(
                "Exposure scenarios",
                "Exposure scenarios/Assays/Alamar blue analysis",
                "Exposure scenarios/Assays/LDH analysis",
                "Exposure scenarios/Assays/BMD analysis",
                "Exposure scenarios/Assays/Internal dose",
            ),
            _study(
                "Cell model comparison",
                "Cell model comparison/Assays/Alamar blue",
                "Cell model comparison/Assays/LDH",
            ),
            _study(
                "qAOP",
                "qAOP/Assays/ATP assay",
                "qAOP/Assays/Alamar blue",
                "qAOP/Assays/CFDA",
                "qAOP/Assays/Calcium imaging",
                "qAOP/Assays/H2DCFDA",
            ),
        ]
    },
    "svhps27": {
        "studies": [
            _study(
                "Cell model comparison",
                "Cell model comparison/Assays/Alamar blue",
                "Cell model comparison/Assays/LDH",
            )
        ]
    },
    "svhps22": {
        "studies": [
            _study(
                "study_01_TH-DNT_Tier1_NeuralCellLines",
                "study_01_TH-DNT_Tier1_NeuralCellLines/assay_01_TH_uptake",
                "study_01_TH-DNT_Tier1_NeuralCellLines/assay_02_deiodinase",
                "study_01_TH-DNT_Tier1_NeuralCellLines/assay_03_metabolism",
                "study_01_TH-DNT_Tier1_NeuralCellLines/assay_04_TRactivation",
            )
        ]
    },
    "svhps26": {"studies": [_study(".", "Assay_OATP1C1")]},
    "svhps21": {"studies": [_study(".", "Assay_MCT8-MDCK1")]},
}


class TestTreeSummary:
    """One line per folder, bounded, carrying what the model needs to label it."""

    def test_every_assay_folder_is_a_line(self) -> None:
        files, root = _deposit("svhps16")
        summary = tree_summary(files, input_root=root)
        for folder in (
            "Exposure scenarios/Assays/Alamar blue analysis/",
            "Exposure scenarios/Assays/LDH analysis/",
            "Exposure scenarios/Assays/BMD analysis/",
            "Exposure scenarios/Assays/Internal dose/",
        ):
            assert any(line.startswith(folder) for line in summary.splitlines()), summary

    def test_a_folder_line_counts_its_files_by_class(self) -> None:
        files, root = _deposit("svhps16")
        summary = tree_summary(files, input_root=root)
        line = next(
            line
            for line in summary.splitlines()
            if line.startswith("Exposure scenarios/Assays/Alamar blue analysis/raw data/")
        )
        assert "12 files: 11 raw data, 1 metadata" in line, line

    def test_a_folder_line_names_its_documents_and_some_data_files(self) -> None:
        files, root = _deposit("svhps27")
        summary = tree_summary(files, input_root=root)
        root_line = summary.splitlines()[0]
        assert root_line.startswith("./")
        assert "S-VHPS27.json" in root_line and "README.txt" in root_line, root_line
        data_line = next(
            line
            for line in summary.splitlines()
            if line.startswith("Cell model comparison/Assays/LDH/")
        )
        assert "minimalmetadata_CC_LDH.xlsx" in data_line, data_line

    def test_folders_below_the_depth_cap_are_counted_not_listed(self) -> None:
        files, root = _deposit("svhps22")
        summary = tree_summary(files, input_root=root)
        lines = summary.splitlines()
        run = (
            "study_01_TH-DNT_Tier1_NeuralCellLines/assay_01_TH_uptake/EDCs/"
            "20231110_uptake PBDE-99 en Amiodarone/"
        )
        deep = run + "raw data/"
        assert not any(line.startswith(deep) for line in lines), "depth 5 must not be listed"
        run_line = next(line for line in lines if line.startswith(run))
        assert "below" in run_line, run_line

    def test_the_line_count_is_capped_and_the_remainder_stated(self) -> None:
        files, root = _deposit("svhps22")
        summary = tree_summary(files, input_root=root, max_lines=20)
        lines = summary.splitlines()
        assert len(lines) == 21, len(lines)
        assert lines[-1].startswith("+ ") and "more folders" in lines[-1], lines[-1]

    def test_the_readme_excerpt_comes_from_the_ranked_documents(self) -> None:
        files, root = _deposit("svhps16")
        text = "This folder holds the exposure scenario study on SH-SY5Y cells. " * 5
        documents = [
            {
                "relative_path": "Exposure scenarios/README.txt",
                "classification": "metadata",
                "preview": text,
            }
        ]
        summary = tree_summary(files, input_root=root, documents=documents)
        line = next(
            line for line in summary.splitlines() if line.startswith("Exposure scenarios/ ")
        )
        assert text[:160] in line, line
        assert text[:200] not in line, "the excerpt is bounded to 160 characters"


class TestChecks:
    """A labelling is accepted only when the fixed checks hold."""

    @pytest.mark.parametrize("name", sorted(LABELLED))
    def test_accepts_the_labelling_of_every_real_deposit(self, name: str) -> None:
        files, root = _deposit(name)
        assert check_structure(LABELLED[name], files, input_root=root) == []

    def test_rejects_a_folder_with_no_scanned_file(self) -> None:
        files, root = _deposit("svhps16")
        structure = {"studies": [_study("Exposure scenarios", "Exposure scenarios/Assays/Comet")]}
        problems = check_structure(structure, files, input_root=root)
        assert problems and "Exposure scenarios/Assays/Comet" in problems[0], problems

    def test_rejects_an_assay_inside_another_assay(self) -> None:
        files, root = _deposit("svhps22")
        study = "study_01_TH-DNT_Tier1_NeuralCellLines"
        structure = {
            "studies": [
                _study(study, f"{study}/assay_01_TH_uptake", f"{study}/assay_01_TH_uptake/EDCs")
            ]
        }
        problems = check_structure(structure, files, input_root=root)
        assert problems and "inside" in problems[0], problems

    def test_rejects_an_assay_outside_its_study(self) -> None:
        files, root = _deposit("svhps16")
        structure = {
            "studies": [
                _study("Exposure scenarios/Protocols", "Exposure scenarios/Assays/LDH analysis")
            ]
        }
        problems = check_structure(structure, files, input_root=root)
        assert problems and "outside" in problems[0], problems

    def test_rejects_a_labelling_with_no_assay(self) -> None:
        files, root = _deposit("svhps16")
        assert check_structure({"studies": [_study("Exposure scenarios")]}, files, input_root=root)
        assert check_structure({"studies": []}, files, input_root=root)

    def test_rejects_a_malformed_labelling(self) -> None:
        files, root = _deposit("svhps16")
        assert check_structure({"studies": "Exposure scenarios"}, files, input_root=root)
        assert check_structure(["Exposure scenarios"], files, input_root=root)  # type: ignore[arg-type]
        assert check_structure(
            {"studies": [{"folder": 3, "assays": [{"folder": None}]}]}, files, input_root=root
        )

    def test_a_study_with_no_assay_is_a_literature_folder_and_is_dropped(self) -> None:
        """WP2.3's AOP and Genetic susceptibility folders hold papers, not assays."""
        from builder.tools.deposit_structure import normalise_structure

        files, root = _deposit("wp23")
        labelled = {
            "studies": [
                _study("AOP"),
                *LABELLED["wp23"]["studies"],
                _study("Genetic susceptibility"),
            ]
        }

        assert check_structure(labelled, files, input_root=root) == []
        kept = normalise_structure(labelled)
        assert [s["folder"] for s in kept["studies"]] == [
            "Exposure scenarios",
            "Cell model comparison",
            "qAOP",
        ]
        assert describe_structure(kept).startswith("Deposit read as: 3 studies")

    def test_a_flat_deposit_is_one_study_and_one_assay_at_the_root(self) -> None:
        files, root = _deposit("svhps26")
        assert check_structure({"studies": [_study(".", ".")]}, files, input_root=root) == []


class TestFallbackAndDescription:
    def test_the_fallback_is_one_study_and_one_assay_at_the_root(self) -> None:
        structure = fallback_structure()
        assert structure["source"] == "fallback"
        assert [s["folder"] for s in structure["studies"]] == ["."]
        assert [a["folder"] for a in structure["studies"][0]["assays"]] == ["."]

    def test_the_line_counts_studies_and_assays(self) -> None:
        line = describe_structure(dict(LABELLED["wp23"], source="model"))
        assert line == (
            "Deposit read as: 3 studies — Exposure scenarios (4 assays), "
            "Cell model comparison (2), qAOP (5)"
        )

    def test_the_line_for_one_study(self) -> None:
        line = describe_structure(dict(LABELLED["svhps16"], source="model"))
        assert line == "Deposit read as: 1 study — Exposure scenarios (4 assays)"

    def test_the_fallback_line_says_what_was_built_instead(self) -> None:
        assert describe_structure(fallback_structure()) == (
            "Could not read the deposit's structure; built one study with one assay."
        )


class TestStateCarriesTheStructure:
    def test_the_structure_survives_a_session_round_trip(self) -> None:
        state = CrateState()
        state.deposit_structure = dict(LABELLED["svhps16"], source="model")
        restored = CrateState.from_dict(state.to_dict())
        assert restored.deposit_structure == state.deposit_structure

    def test_a_session_saved_before_the_field_existed_loads_as_none(self) -> None:
        data = CrateState().to_dict()
        del data["deposit_structure"]
        assert CrateState.from_dict(data).deposit_structure is None


class TestReadDepositStructure:
    """The shared hook: derive once, check, fall back, say one line, store."""

    @staticmethod
    def _engine(name: str = "svhps16"):
        from builder.engine import AgentEngine

        files, root = _deposit(name)
        engine = AgentEngine(state=CrateState())
        engine.state.scanned_files = files
        engine.state.metadata.input_path = root
        engine.state.approved_scan_roots.add(root)
        return engine

    def test_no_provider_falls_back_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: None)
        calls: list[Any] = []
        monkeypatch.setattr(build_mod, "derive_deposit_structure", lambda *a, **k: calls.append(a))
        engine = self._engine()
        lines: list[str] = []

        structure = build_mod.read_deposit_structure(engine, output=lines.append)

        assert structure == fallback_structure() == engine.state.deposit_structure
        assert calls == []
        assert lines == ["Could not read the deposit's structure; built one study with one assay."]

    def test_an_accepted_labelling_is_stored_and_described(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")
        seen: dict[str, Any] = {}

        def _leaf(summary: str, **kwargs: Any) -> dict[str, Any]:
            seen["summary"] = summary
            seen["kwargs"] = kwargs
            return LABELLED["svhps16"]

        monkeypatch.setattr(build_mod, "derive_deposit_structure", _leaf)
        engine = self._engine()
        lines: list[str] = []
        from builder.agents.llm import ModelOverrides

        overrides = ModelOverrides(model="drafter-x")
        structure = build_mod.read_deposit_structure(
            engine, overrides=overrides, output=lines.append
        )

        assert structure["source"] == "model"
        assert structure["studies"] == LABELLED["svhps16"]["studies"]
        assert engine.state.deposit_structure == structure
        assert "Exposure scenarios/Assays/LDH analysis/" in seen["summary"]
        assert seen["kwargs"]["overrides"] is overrides
        assert lines == ["Deposit read as: 1 study — Exposure scenarios (4 assays)"]

    def test_a_rejected_labelling_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")
        bad = {"studies": [_study("Exposure scenarios", "Exposure scenarios/Assays/Comet")]}
        monkeypatch.setattr(build_mod, "derive_deposit_structure", lambda *a, **k: bad)
        engine = self._engine()
        lines: list[str] = []

        structure = build_mod.read_deposit_structure(engine, output=lines.append)

        assert structure == fallback_structure() == engine.state.deposit_structure
        assert lines == ["Could not read the deposit's structure; built one study with one assay."]

    def test_a_failing_model_call_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")

        def _boom(*a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("model down")

        monkeypatch.setattr(build_mod, "derive_deposit_structure", _boom)
        engine = self._engine()

        assert build_mod.read_deposit_structure(engine) == fallback_structure()

    def test_the_leaf_usage_lands_on_the_generator_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")

        def _leaf(summary: str, **kwargs: Any) -> dict[str, Any]:
            kwargs["usage_sink"](120, 30, "drafter-x", None)
            return LABELLED["svhps16"]

        monkeypatch.setattr(build_mod, "derive_deposit_structure", _leaf)
        engine = self._engine()

        build_mod.read_deposit_structure(engine)

        assert engine.state.generator.input_tokens == 120
        assert engine.state.generator.output_tokens == 30

    def test_a_resumed_session_keeps_its_structure_without_a_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")
        calls: list[Any] = []
        monkeypatch.setattr(build_mod, "derive_deposit_structure", lambda *a, **k: calls.append(a))
        engine = self._engine()
        kept = dict(LABELLED["svhps16"], source="model")
        engine.state.deposit_structure = kept
        lines: list[str] = []

        assert build_mod.read_deposit_structure(engine, output=lines.append) == kept
        assert calls == []
        assert lines == ["Deposit read as: 1 study — Exposure scenarios (4 assays)"]

    def test_no_scanned_files_means_no_deposit_and_no_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod
        from builder.engine import AgentEngine

        monkeypatch.setattr(build_mod, "get_provider", lambda: "openai")
        calls: list[Any] = []
        monkeypatch.setattr(build_mod, "derive_deposit_structure", lambda *a, **k: calls.append(a))
        engine = AgentEngine(state=CrateState())
        lines: list[str] = []

        assert build_mod.read_deposit_structure(engine, output=lines.append) is None
        assert engine.state.deposit_structure is None
        assert calls == [] and lines == []


class TestBothArmsGetTheStructure:
    """``run_build`` reads the structure before dispatching either arm."""

    @pytest.mark.parametrize("mode_name", ["REACT", "PIPELINE"])
    def test_the_arm_starts_with_the_structure_on_the_state(
        self, mode_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builder.agents.build as build_mod
        import builder.agents.react.agent_loop as agent_loop
        from builder.agents.build import BuildMode, run_build

        monkeypatch.setattr(build_mod, "get_provider", lambda: None)
        engine = TestReadDepositStructure._engine()
        seen: dict[str, Any] = {}

        def _arm(eng: Any, *a: Any, **k: Any) -> dict[str, Any]:
            seen["structure"] = eng.state.deposit_structure
            return {}

        monkeypatch.setattr(agent_loop, "run_interactive_agent", _arm)
        monkeypatch.setattr(build_mod, "run_interactive_build", _arm)
        lines: list[str] = []

        run_build(BuildMode[mode_name], engine, output=lines.append)

        assert seen["structure"] == fallback_structure()
        assert lines[0].startswith("Could not read the deposit's structure")
