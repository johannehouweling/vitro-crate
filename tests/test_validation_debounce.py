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
    assert original is not None  # always registered; narrows ToolSpec | None for ty
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


class TestExportFingerprint:
    """#380: the ReAct auto-export gate needs a CONTENT fingerprint.

    It used to compare `len(state.list_entities())`, which is invariant under
    every field-level mutation the arm exposes (`set_fields`,
    `set_crate_metadata`, `fix_required_issues`, `link`) — so once the crate had
    been exported once, none of that work ever reached disk.
    """

    def _state_with_investigation(self) -> CrateState:
        state = CrateState()
        draft_investigation(state, {"name": "Inv"})
        return state

    def test_export_fingerprint_changes_on_field_only_mutation(self):
        """The exact case the entity count missed."""
        from builder.tools.management import set_fields

        state = self._state_with_investigation()
        entity_id = state.list_entities("Investigation")[0].entity_id
        before = state.export_fingerprint()

        set_fields(state, entity_id, {"description": "added after the export"})

        assert state.export_fingerprint() != before
        assert len(state.list_entities()) == 1, (
            "the entity count is unchanged — which is precisely why it was the "
            "wrong fingerprint"
        )

    def test_export_fingerprint_changes_on_new_scanned_file(self):
        """Honesty control for the two-helper split.

        `export_crate` packages scanned files (`include_all_scanned=True`) that
        `build_and_validate` never sees (`include_all_scanned=False`), so a new
        scan changes the exported payload without changing the validation hash.
        If `export_fingerprint` were merely an alias, this fails.
        """
        from builder.state import FileClassification

        state = self._state_with_investigation()
        export_before = state.export_fingerprint()
        validation_before = state.validation_fingerprint()

        state.scanned_files.append(
            FileClassification(
                path="/data/raw/plate1.csv",
                filename="plate1.csv",
                size=1234,
                mime_type="text/csv",
            )
        )

        assert state.export_fingerprint() != export_before
        assert state.validation_fingerprint() == validation_before, (
            "a scanned file is not a validation input — including it would "
            "defeat the #155 debounce"
        )

    def test_export_fingerprint_stable_across_validation_writeback(self):
        """Guards the double-export regression.

        The backstop runs `build_and_validate` before exporting, and the #153
        write-back mutates `state.validation`. If that fed the fingerprint, the
        two exit paths (quit and EOF) would each see a "change" and export twice.
        """
        state = self._state_with_investigation()
        before = state.export_fingerprint()

        state.validation.base_passed = True
        state.validation.required_issues = ["something the validator found"]

        assert state.export_fingerprint() == before

    def test_validation_input_hash_delegates_to_the_state_method(self):
        """`_validation_input_hash` stays importable and equivalent.

        `tests/test_validation_debounce.py` and the engine's debounce key both
        use the module-level name, so the promotion must be a delegation, not a
        move.
        """
        state = self._state_with_investigation()
        assert _validation_input_hash(state) == state.validation_fingerprint()
