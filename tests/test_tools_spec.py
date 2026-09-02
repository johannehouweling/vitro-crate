"""Tests for builder/agents/tools_spec.py — TOOL_SPECS definitions.

These tests ensure tool descriptions are self-consistent and don't reference
tools the LLM cannot call (Issue #70).
"""

from __future__ import annotations

import re

from builder.agents.react.tools_spec import TOOL_SPECS


def _tool_names() -> set[str]:
    """Return the set of all tool names registered in TOOL_SPECS."""
    return {spec["name"] for spec in TOOL_SPECS}  # ty: ignore


# Tool functions that are exported by builder.tools but deliberately omitted
# from TOOL_SPECS (they are internal helpers, not LLM-facing tools).
# Computed from builder/tools/__init__.py __all__ minus TOOL_SPECS names.
_INTERNAL_TOOL_NAMES: set[str] | None = None


def _get_registry_tool_names() -> set[str]:
    """Return tool names registered in the shared TOOL_REGISTRY.

    Delegates to the single-source parity contract (Issue #327) so the test and
    the runtime assert measure the same fully-populated registry.
    """
    from builder.agents.react.tools_spec import _registered_tool_names

    return _registered_tool_names()


def _get_engine_routable_llm_tools() -> set[str]:
    """Return tool names that the engine routes specially (not via registry)
    and that are intended to be callable by the LLM.

    The engine has special routing for HITL tools and scanner tools. This set is
    declared once, in the parity contract (Issue #327), so the test cannot drift
    from what the runtime assert enforces.
    """
    from builder.agents.react.tools_spec import _LLM_TOOLS_OUTSIDE_REGISTRY

    return set(_LLM_TOOLS_OUTSIDE_REGISTRY)


def _get_all_registry_tool_names() -> set[str]:
    """Return the COMPLETE set of registered tool names (single source, #327)."""
    from builder.agents.react.tools_spec import _registered_tool_names

    return _registered_tool_names()


def _expected_llm_tool_universe() -> set[str]:
    """The authoritative set of LLM-callable tools.

    A tool is LLM-callable iff it is either registered in the shared registry or
    routed specially by the engine (HITL + scanner tools). Delegates to the parity
    contract (Issue #327) so ``TOOL_SPECS``, the system prompt, and the runtime
    assert all measure against one definition.
    """
    from builder.agents.react.tools_spec import expected_tool_spec_names

    return expected_tool_spec_names()


def _get_tool_names_from_system_prompt() -> set[str]:
    """Extract tool names mentioned in the SYSTEM_PROMPT text.

    Looks for backtick-quoted identifiers and dash-list items.
    """
    from builder.agents.react.system_prompt import SYSTEM_PROMPT

    names: set[str] = set()
    # Find backtick-quoted identifiers like `build_crate`
    for m in re.finditer(r"`([a-z_]+)`", SYSTEM_PROMPT):
        names.add(m.group(1))
    return names


def _get_system_prompt_tool_list() -> set[str]:
    """Return the tool names in the SYSTEM_PROMPT's '## Your Tools' dash list.

    This is the explicit catalogue the LLM is shown, parsed from lines of the
    form ``- tool_name: description``. It must equal the TOOL_SPECS names so the
    prompt never advertises a tool that has no schema, nor omits one that does.
    """
    from builder.agents.react.system_prompt import SYSTEM_PROMPT

    # Isolate the "## Your Tools" section (up to the next "## " heading).
    m = re.search(r"## Your Tools\n(.*?)(?:\n## )", SYSTEM_PROMPT, re.S)
    body = m.group(1) if m else ""
    return set(re.findall(r"^- ([a-z_]+):", body, re.M))


def _get_internal_tool_names() -> set[str]:
    """Return tool-function names exported by builder.tools but NOT in TOOL_SPECS."""
    global _INTERNAL_TOOL_NAMES
    if _INTERNAL_TOOL_NAMES is not None:
        return _INTERNAL_TOOL_NAMES

    import builder.tools as bt

    spec_names = _tool_names()
    _INTERNAL_TOOL_NAMES = set(bt.__all__) - spec_names
    return _INTERNAL_TOOL_NAMES


# Patterns that indicate a reference to another tool by name.
# We look for quoted or backtick-wrapped references like "read_file_sample"
# or ``read_file_sample`` in description strings.
_REF_PATTERN = re.compile(r"`([a-z_]+)`|`{2}([a-z_]+)`{2}")


def _collect_referenced_tools(desc: str) -> set[str]:
    """Return set of tool-name-like identifiers found in *desc*.

    Looks for:
    - Backtick-quoted identifiers (the most explicit form).
    - Bare mentions of tool names that exist in the builder.tools module
      but NOT in TOOL_SPECS. This catches the case where a description
      says "use read_file_sample" or "instead of calling read_file_sample"
      without backticks.
    """
    refs: set[str] = set()

    # Backtick-quoted identifiers (single or double backticks)
    for match in _REF_PATTERN.finditer(desc):
        ref = match.group(1) or match.group(2)
        if ref:
            refs.add(ref)

    # Bare mentions of known internal (non-TOOL_SPECS) tool names
    for internal_name in _get_internal_tool_names():
        if internal_name in desc:
            refs.add(internal_name)

    return refs


def test_no_tool_description_references_absent_tool():
    """Every tool referenced by name in descriptions must exist in TOOL_SPECS.

    The 'read_multiple_files' description previously referenced 'read_file_sample'
    in its description, but 'read_file_sample' was NOT in TOOL_SPECS, causing
    the LLM to be told about a tool it cannot call.

    We detect both backtick-quoted identifiers and bare mentions of tool
    functions that exist in builder.tools but are not registered in TOOL_SPECS.
    """
    available = _tool_names()

    for spec in TOOL_SPECS:
        name = spec["name"]
        desc = str(spec.get("description", ""))

        refs = _collect_referenced_tools(desc)

        for ref in refs:
            # It's ok for a tool to reference itself
            if ref == name:
                continue
            if ref not in available:
                pytest.fail(
                    f"Tool '{name}' description references '{ref}' which is "
                    f"not a registered tool in TOOL_SPECS. Either add '{ref}' "
                    f"to TOOL_SPECS or reword the description."
                )


def _import_module() -> object:
    """Helper to import and return the tools_spec module for introspection."""
    import importlib

    return importlib.import_module("builder.agents.react.tools_spec")


# We need pytest for the fail helper
import pytest  # noqa: E402


def test_every_registry_tool_is_in_tool_specs():
    """Every tool registered in TOOL_REGISTRY must appear in TOOL_SPECS.

    The registry is the authoritative list of LLM-callable tools. If a tool
    is registered but missing from TOOL_SPECS, the LLM cannot call it.
    """
    spec_names = _tool_names()
    registry_names = _get_registry_tool_names()

    missing = registry_names - spec_names
    assert not missing, (
        f"Tools registered in TOOL_REGISTRY but missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_every_engine_routable_llm_tool_is_in_tool_specs():
    """Every engine-routable tool intended for LLM use must appear in TOOL_SPECS.

    The engine has special routing for HITL tools (present_to_human,
    request_input) and scanner tools (scan_files, read_file_sample, etc.).
    These tools are callable by the LLM but not in the registry. They still
    need TOOL_SPECS entries so the LLM knows their schemas.
    """
    spec_names = _tool_names()
    routable = _get_engine_routable_llm_tools()

    missing = routable - spec_names
    assert not missing, (
        f"Engine-routable LLM tools missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_every_system_prompt_tool_is_in_tool_specs():
    """Every tool named in SYSTEM_PROMPT must appear in TOOL_SPECS.

    If the system prompt tells the LLM about a tool that doesn't have a
    schema in TOOL_SPECS, the LLM will be confused or unable to call it.
    """
    spec_names = _tool_names()
    prompt_names = _get_tool_names_from_system_prompt()

    missing = prompt_names - spec_names
    assert not missing, (
        f"Tools mentioned in SYSTEM_PROMPT but missing from TOOL_SPECS: "
        f"{sorted(missing)}"
    )


def test_no_duplicate_tool_names_in_tool_specs():
    """TOOL_SPECS must not contain two entries with the same name.

    A duplicate (e.g. an upgraded tool spec added without removing the old one)
    is silently shadowed when specs are collapsed into a name->tool map, so the
    LLM sees one schema while another spec's description is dead. The build then
    disagrees with itself (see the read_multiple_files duplicate from the
    file-readers change).
    """
    from collections import Counter

    counts = Counter(spec["name"] for spec in TOOL_SPECS)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert not duplicates, f"Duplicate tool names in TOOL_SPECS: {duplicates}"


def test_tool_specs_match_llm_tool_universe_exactly():
    """TOOL_SPECS names == registry tools + engine-routable tools (bidirectional).

    The single source-of-truth assertion (Issue #90, sub-task 4): every callable
    tool has exactly one schema, and TOOL_SPECS advertises no tool that cannot be
    called. Prevents drift in either direction.
    """
    spec_names = _tool_names()
    universe = _expected_llm_tool_universe()

    missing_from_specs = universe - spec_names
    extra_in_specs = spec_names - universe
    assert not missing_from_specs, (
        f"Callable tools missing from TOOL_SPECS: {sorted(missing_from_specs)}"
    )
    assert not extra_in_specs, (
        f"TOOL_SPECS advertises uncallable tools: {sorted(extra_in_specs)}"
    )


def test_system_prompt_tool_list_matches_tool_specs_exactly():
    """The SYSTEM_PROMPT '## Your Tools' list == TOOL_SPECS names (bidirectional).

    The prompt catalogue used to omit 9 exposed tools and listed removed ones.
    This keeps the catalogue and the schemas in lockstep (Issue #90, sub-task 4).
    """
    spec_names = _tool_names()
    prompt_list = _get_system_prompt_tool_list()

    missing_from_prompt = spec_names - prompt_list
    extra_in_prompt = prompt_list - spec_names
    assert not missing_from_prompt, (
        f"Tools in TOOL_SPECS but missing from the prompt list: "
        f"{sorted(missing_from_prompt)}"
    )
    assert not extra_in_prompt, (
        f"Prompt list names not in TOOL_SPECS: {sorted(extra_in_prompt)}"
    )


def test_expected_tool_spec_names_is_the_single_source_of_truth():
    """``expected_tool_spec_names()`` == TOOL_SPECS names (Issue #327).

    The parity contract lives in one place — the module the specs live in — so the
    runtime assert and every test measure against the same authoritative set.
    """
    from builder.agents.react.tools_spec import expected_tool_spec_names

    assert expected_tool_spec_names() == _tool_names()


def test_assert_tool_spec_parity_passes_on_the_current_tree():
    """The runtime parity guard does not raise for the shipped tool set (#327)."""
    from builder.agents.react.tools_spec import assert_tool_spec_parity

    assert_tool_spec_parity()  # must not raise


def test_assert_tool_spec_parity_flags_a_callable_tool_with_no_spec(monkeypatch):
    """A tool the engine can run but TOOL_SPECS omits is caught (#327)."""
    import builder.agents.react.tools_spec as ts

    monkeypatch.setattr(
        ts,
        "_LLM_TOOLS_OUTSIDE_REGISTRY",
        ts._LLM_TOOLS_OUTSIDE_REGISTRY | {"phantom_registry_tool"},
    )
    with pytest.raises(ts.ToolSpecParityError, match="phantom_registry_tool"):
        ts.assert_tool_spec_parity()


def test_assert_tool_spec_parity_flags_a_spec_with_no_callable_tool(monkeypatch):
    """A spec advertising a tool the engine cannot run is caught (#327)."""
    import builder.agents.react.tools_spec as ts

    monkeypatch.setattr(
        ts,
        "TOOL_SPECS",
        [*ts.TOOL_SPECS, {"name": "phantom_spec_tool", "description": "", "parameters": {}}],
    )
    with pytest.raises(ts.ToolSpecParityError, match="phantom_spec_tool"):
        ts.assert_tool_spec_parity()


# ---------------------------------------------------------------------------
# Advertised-text lints (Issue #383)
#
# Two properties of the *shipped* model-facing text, both of which the
# tool-existence lint above cannot see:
#   1. no worked example hands the model a resolvable identifier it can lift
#      onto the wrong entity;
#   2. advice to "look it up first (tool)" names a tool the caller can actually
#      call from the arguments it holds.
# ---------------------------------------------------------------------------

# Identifier shapes that resolve at a real authority, so a model that lifts one
# out of an example ships a real-but-wrong id (a D5 violation on disk).
_RESOLVABLE_IDENTIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "Cellosaurus accession": re.compile(r"CVCL[_:]\d+"),
    "CAS RN": re.compile(r"\b\d{2,7}-\d{2}-\d\b"),
    "ORCID iD": re.compile(r"0000-000\d-\d{4}-\d{3}[\dX]"),
    "DOI": re.compile(r"10\.\d{4,9}/[^\s'\"]+"),
    "ontology term CURIE": re.compile(r"\b(?:BAO|GO|CHEBI|EFO|OBI|UBERON|NCIT)[:_]\d+"),
}

# Literals reserved by their registries as non-resolvable documentation
# examples, so they cannot be lifted onto a real entity.
_NON_RESOLVABLE_PLACEHOLDERS = frozenset({"10.1234/example"})


def _iter_descriptions(node: object, path: str) -> list[tuple[str, str]]:
    """Return every ``description`` string in *node*, with a dotted path.

    Walks the whole spec, so it reaches the nested ``hints`` parameter schemas
    that ``draft_hints_schema`` renders from ``_crate_mapping.ENTITY_DRAFT_SCHEMA``
    — the model sees those in the same function-call payload as the description.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                found.append((f"{path}.{key}", value))
            else:
                found.extend(_iter_descriptions(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_iter_descriptions(value, f"{path}[{index}]"))
    return found


def _resolvable_identifier_literals(spec: dict) -> list[tuple[str, str, str]]:
    """Return ``(path, kind, literal)`` for each resolvable id advertised by *spec*."""
    hits: list[tuple[str, str, str]] = []
    for path, text in _iter_descriptions(spec, str(spec.get("name", "?"))):
        for kind, pattern in _RESOLVABLE_IDENTIFIER_PATTERNS.items():
            for literal in pattern.findall(text):
                if literal in _NON_RESOLVABLE_PLACEHOLDERS:
                    continue
                hits.append((path, kind, literal))
    return hits


# The repo's advisory idiom for "call this tool first": a "look … up" sentence,
# and/or a bare tool name in parentheses.
_LOOKUP_ADVICE_CUE = re.compile(r"\blook(?:s|ed|ing)?\s[^.]*?\bup\b", re.I)
_PARENTHESISED_TOOL = re.compile(r"\(([a-z_]+)\)")


def _advised_tools(description: str, tool_names: set[str]) -> set[str]:
    """Return the tools *description* advises the caller to call."""
    advised: set[str] = set()
    for sentence in re.split(r"(?<=[.;])\s+", description):
        if _LOOKUP_ADVICE_CUE.search(sentence):
            advised |= {n for n in tool_names if re.search(rf"\b{re.escape(n)}\b", sentence)}
        advised |= {m for m in _PARENTHESISED_TOOL.findall(sentence) if m in tool_names}
    return advised


def _misdirected_advice(spec: dict, specs: list) -> list[tuple[str, str, list[str]]]:
    """Return ``(caller, advised, unsatisfiable_args)`` for misdirected advice.

    A caller's *own* top-level arguments are the only ones it can pass on. Its
    ``hints`` are excluded deliberately: hints carry the values the advised
    lookup is supposed to *return*, so a lookup that requires one of them as
    input is circular from where the caller stands.
    """
    required = {
        str(s["name"]): set(s.get("parameters", {}).get("required", []) or []) for s in specs
    }
    caller = str(spec["name"])
    own_args = set(spec.get("parameters", {}).get("properties", {}) or {}) - {"hints"}
    findings: list[tuple[str, str, list[str]]] = []
    for advised in sorted(_advised_tools(str(spec.get("description", "")), set(required)) - {caller}):
        unmet = required.get(advised, set()) - own_args
        if unmet:
            findings.append((caller, advised, sorted(unmet)))
    return findings


def test_no_advertised_description_contains_a_resolvable_identifier_literal():
    """No model-facing description hands the model a liftable real identifier.

    `CVCL_0027` (HepG2) used to appear twice in one ``draft_cell_line_sample``
    payload — in the worked example and again in the shared hint schema. A model
    that dead-ends on a lookup reaches for the nearest concrete accession, so a
    CHO-K1 sample could ship with HepG2's accession, verified (Issue #383).
    """
    offenders = [hit for spec in TOOL_SPECS for hit in _resolvable_identifier_literals(spec)]
    assert not offenders, (
        "Advertised text contains resolvable identifier literal(s) a model can "
        f"lift onto the wrong entity: {offenders}. Use a shape-only placeholder."
    )


def test_the_identifier_literal_lint_flags_a_planted_literal():
    """The detector fires on a planted literal, including a nested one.

    Proves the green state above is 'the shipped specs are clean', not 'the
    patterns match nothing'.
    """
    planted = {
        "name": "draft_planted",
        "description": "Example: draft_planted(doi='10.1016/j.tox.2021.152898').",
        "parameters": {
            "type": "object",
            "properties": {
                "hints": {
                    "type": "object",
                    "properties": {
                        "accession": {
                            "type": "string",
                            "description": "Cellosaurus accession, e.g. 'CVCL_0027'.",
                        }
                    },
                }
            },
        },
    }

    hits = _resolvable_identifier_literals(planted)

    assert ("draft_planted.description", "DOI", "10.1016/j.tox.2021.152898") in hits
    nested = "draft_planted.parameters.properties.hints.properties.accession.description"
    assert (nested, "Cellosaurus accession", "CVCL_0027") in hits


def test_no_description_advises_a_tool_it_cannot_call_from_its_own_arguments():
    """Advice to look something up must name a tool the caller can call.

    ``draft_cell_line_sample`` used to say "Look it up first (lookup_cell_line)"
    while ``lookup_cell_line`` requires the accession the caller is trying to
    obtain; the tool that goes name -> accession (``lookup_cell_line_by_name``)
    was never named. The existing existence lint passes on that, because the
    misdirecting tool does exist (Issue #383).
    """
    offenders = [f for spec in TOOL_SPECS for f in _misdirected_advice(spec, TOOL_SPECS)]
    assert not offenders, (
        "Description(s) advise a tool whose required argument(s) the caller "
        f"cannot supply: {offenders}. Name the tool that takes what the caller has."
    )


def test_the_lookup_advice_lint_flags_a_planted_misdirection():
    """The detector fires on advice naming a lookup keyed by the wanted value."""
    planted = {
        "name": "draft_planted",
        "description": "Create a thing from a name. Look it up first (lookup_cell_line).",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "hints": {"type": "object"}},
            "required": ["name", "hints"],
        },
    }

    findings = _misdirected_advice(planted, [*TOOL_SPECS, planted])

    assert findings == [("draft_planted", "lookup_cell_line", ["accession"])]


__all__: list[str] = []


class TestExportIsDescribedAsAFinalStep:
    """The spec told the agent to export as soon as REQUIRED went green.

    "Call export_crate once the crate is conformant" reads as an instruction to
    export the moment the REQUIRED gate passes — which is exactly what one
    profiled session did, 32 times, 16 of them with `ok=True`. The agent was
    following the instruction correctly; the instruction was wrong. Conformance
    is not completion: recommended findings, unanswered questions and unwired
    entities are all still work.

    The system prompt already said the right thing ("Call export_crate only when
    you are ready to write the finished crate", "No need to export_crate to
    check"). The tool description contradicted it, and the description is what
    the model reads at the moment it chooses a tool.
    """

    def _description(self) -> str:
        from builder.agents.react.tools_spec import TOOL_SPECS

        spec = next(s for s in TOOL_SPECS if s["name"] == "export_crate")
        return str(spec["description"])

    def test_it_does_not_invite_an_export_on_conformance(self):
        text = self._description().casefold()
        assert "once the crate is conformant" not in text
        assert "when the crate is conformant" not in text

    def test_it_says_to_call_it_when_finished(self):
        text = self._description().casefold()
        assert "finished" in text

    def test_it_points_at_the_zero_disk_alternative(self):
        """The agent needs somewhere to go, not just a prohibition."""
        assert "build_and_validate" in self._description()

    def test_it_says_conformance_is_not_completion(self):
        text = self._description().casefold()
        assert "required" in text and "not completion" in text


def test_present_to_human_can_carry_several_questions_each_with_its_own_options():
    """#596: the schema must not invite bundling three questions into one
    context with one catch-all option — it offers a per-question form instead."""
    from typing import Any, cast

    spec = next(s for s in TOOL_SPECS if s["name"] == "present_to_human")
    params = cast(dict[str, Any], spec["parameters"])

    questions = params["properties"]["questions"]
    assert questions["type"] == "array"
    entry = questions["items"]
    assert entry["type"] == "object"
    assert entry["required"] == ["question"]
    assert entry["properties"]["question"]["type"] == "string"
    assert entry["properties"]["options"]["type"] == "array"
    assert entry["properties"]["options"]["items"]["type"] == "string"
    # A prompt without `questions` is still the single decision it always was.
    assert params["required"] == ["context"]
