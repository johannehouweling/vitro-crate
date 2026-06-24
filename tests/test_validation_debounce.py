"""Tests for the build_and_validate debounce (Issue #155).

build_and_validate is the agent's single biggest time sink (~3.7s SHACL per
call, called ~21x in one build) and has zero incrementality. The engine memoizes
a result keyed on (validation-input hash, profile, severity) so consecutive
validations of an UNCHANGED crate skip the dispatch (and its SHACL pass). The key
hashes only the validation inputs (entities + metadata), NOT the validation
outputs the #153 write-back mutates — otherwise the write-back would invalidate
the cache every call and the debounce would never hit.
"""

from __future__ import annotations

from builder.engine import AgentEngine, _validation_input_hash
from builder.state import CrateState
from builder.tools.drafters import draft_investigation


def _dispatch_spy(monkeypatch):
    """Count how many times the engine DISPATCHES build_and_validate (vs serving
    it from the debounce cache).

    Spies at the registry boundary the engine actually calls. This is immune to
    validator import/global-state pollution from other tests — an earlier version
    patched ``profiles.validator.validate_crate_dict`` and silently observed
    nothing once that module was swapped under it (the import-error test in
    test_tools_validation.py perturbs the validator import). A debounce hit
    short-circuits before the engine reaches the tool fn, so the counter only
    advances on a real dispatch, which is exactly when SHACL runs.
    """
    from builder.tools.registry import ToolSpec

    registry = AgentEngine._build_registry()
    original = registry.get_spec("build_and_validate")
    calls = {"n": 0}

    def counting(state, **kwargs):
        calls["n"] += 1
        return original.fn(state, **kwargs)

    monkeypatch.setitem(
        registry._tools,
        "build_and_validate",
        ToolSpec(
            name=original.name,
            fn=counting,
            description=original.description,
            takes_state=original.takes_state,
        ),
    )
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
    def test_unchanged_state_skips_dispatch(self, monkeypatch):
        calls = _dispatch_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert calls["n"] == 1
        engine.run_tool("build_and_validate", profile="base")  # unchanged -> cache hit
        assert calls["n"] == 1  # not re-dispatched (SHACL skipped)

    def test_mutation_busts_cache(self, monkeypatch):
        calls = _dispatch_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        engine.run_tool("build_and_validate", profile="base")  # hit
        assert calls["n"] == 1
        engine.run_tool("draft_investigation", hints={"name": "X"})  # mutate inputs
        engine.run_tool("build_and_validate", profile="base")  # miss -> re-dispatch
        assert calls["n"] == 2

    def test_different_scope_is_a_miss(self, monkeypatch):
        calls = _dispatch_spy(monkeypatch)
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert calls["n"] == 1
        engine.run_tool("build_and_validate", profile="all")  # different scope
        assert calls["n"] == 2

    def test_writeback_still_runs_on_cache_hit(self):
        """A debounced (cached) result must still feed the #153 write-back."""
        engine = AgentEngine()
        engine.initialize()
        engine.run_tool("build_and_validate", profile="base")
        assert engine.state.validation.base_passed is True
        # Corrupt the output, then re-validate (cache hit): write-back re-applies.
        engine.state.validation.base_passed = False
        engine.run_tool("build_and_validate", profile="base")
        assert engine.state.validation.base_passed is True

    def test_errored_validation_is_not_cached(self):
        engine = AgentEngine()
        engine.initialize()
        # bogus severity -> build_and_validate returns an error result
        r1 = engine.run_tool("build_and_validate", severity="bogus")
        assert "error" in r1
        # error results are never cached, so nothing was memoised
        assert engine._validation_cache == {}
        r2 = engine.run_tool("build_and_validate", severity="bogus")
        assert "error" in r2
