"""Tests for the build_and_validate debounce (Issue #155).

build_and_validate is the agent's single biggest time sink (~3.7s SHACL per
call, called ~21x in one build) and has zero incrementality. The engine memoizes
a result keyed on (validation-input hash, profile, severity) so consecutive
validations of an UNCHANGED crate skip the SHACL re-run. The key hashes only the
validation inputs (entities + metadata), NOT the validation outputs the #153
write-back mutates — otherwise the write-back would invalidate the cache every
call and the debounce would never hit.
"""

from __future__ import annotations

import profiles.validator as pv
from builder.engine import AgentEngine, _validation_input_hash
from builder.state import CrateState
from builder.tools.drafters import draft_investigation


def _shacl_spy(monkeypatch):
    """Count real SHACL passes (validate_crate_dict is imported lazily inside
    build_and_validate, so patching the module attribute is observed at call
    time). Calls through so results stay realistic."""
    calls = {"n": 0}
    real = pv.validate_crate_dict

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pv, "validate_crate_dict", counting)
    return calls


class TestValidationInputHash:
    def test_excludes_validation_output(self):
        """The critical property: mutating validation OUTPUTS leaves the key
        unchanged (else the #153 write-back self-invalidates the cache)."""
        state = CrateState()
        before = _validation_input_hash(state)
        state.validation.base_passed = True
        state.validation.required_issues = ["something"]
        assert _validation_input_hash(state) == before

    def test_changes_on_entity_mutation(self):
        state = CrateState()
        before = _validation_input_hash(state)
        draft_investigation(state, {"name": "X"})
        assert _validation_input_hash(state) != before

    def test_changes_on_metadata_mutation(self):
        state = CrateState()
        before = _validation_input_hash(state)
        state.metadata.title = "A title"
        assert _validation_input_hash(state) != before


class TestDebounce:
    def test_unchanged_state_skips_shacl(self, monkeypatch):
        calls = _shacl_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert calls["n"] == 1
        engine.run_tool("build_and_validate", profile="base")  # unchanged -> cache hit
        assert calls["n"] == 1  # SHACL NOT re-run

    def test_mutation_busts_cache(self, monkeypatch):
        calls = _shacl_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        engine.run_tool("build_and_validate", profile="base")  # hit
        assert calls["n"] == 1
        engine.run_tool("draft_investigation", hints={"name": "X"})  # mutate inputs
        engine.run_tool("build_and_validate", profile="base")  # miss -> re-run
        assert calls["n"] == 2

    def test_different_scope_is_a_miss(self, monkeypatch):
        calls = _shacl_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert calls["n"] == 1
        engine.run_tool("build_and_validate", profile="all")  # different scope
        assert calls["n"] == 2

    def test_writeback_still_runs_on_cache_hit(self, monkeypatch):
        """A debounced (cached) result must still feed the #153 write-back."""
        _shacl_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert engine.state.validation.base_passed is True
        # Corrupt the output, then re-validate (cache hit): write-back re-applies.
        engine.state.validation.base_passed = False
        engine.run_tool("build_and_validate", profile="base")
        assert engine.state.validation.base_passed is True

    def test_errored_validation_is_not_cached(self, monkeypatch):
        calls = _shacl_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        # bogus severity -> build_and_validate returns an error result
        r1 = engine.run_tool("build_and_validate", severity="bogus")
        assert "error" in r1
        r2 = engine.run_tool("build_and_validate", severity="bogus")
        assert "error" in r2
        # not cached, so it is dispatched both times (no stale-cache short-circuit)
        assert ("bogus" not in str(engine._validation_cache.keys()))
