"""#422 observability: the harness must not discard ``run_pipeline``'s report.

``run_pipeline`` returns a structured report — conformance, issues, the
materialization outcomes (including ``condition_table.populated``) and the
Frictionless ``data_issues`` (#409) — and the eval harness used to drop it on
the floor: only a raised exception was recorded. A silent domain-level failure
(a header-only condition table) was therefore invisible to every eval metric.

Offline: injected runner, no LLM, no network.
"""

from __future__ import annotations

from typing import cast

import pytest

from builder.state import CrateState
from eval.agent_api import BuildOutcome
from eval.tests._cases import first_folder_case
from eval.pipeline_factory import PipelineBuildAgent, PipelineRunner

pytestmark = pytest.mark.timeout(120)


class TestPipelineResultCapture:
    def test_the_runners_report_reaches_the_outcome(self) -> None:
        report = {
            "ok": True,
            "materialized": {"condition_table": {"populated": False, "reason": "declined"}},
        }
        agent = PipelineBuildAgent(pipeline_runner=lambda engine: dict(report))
        outcome = agent.build(first_folder_case())
        assert outcome.pipeline_result == report

    def test_a_runner_without_a_report_yields_none(self) -> None:
        # Deliberately violates PipelineRunner's declared `-> dict` to pin the
        # defensive capture: a misbehaving runner yields None, never garbage.
        misbehaving = cast(PipelineRunner, lambda engine: None)
        agent = PipelineBuildAgent(pipeline_runner=misbehaving)
        outcome = agent.build(first_folder_case())
        assert outcome.pipeline_result is None

    def test_the_field_defaults_to_none_for_other_producers(self) -> None:
        outcome = BuildOutcome(state=CrateState())
        assert outcome.pipeline_result is None
