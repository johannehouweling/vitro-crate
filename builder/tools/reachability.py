"""Static reachability of registered tools from the deterministic arm (#386).

``builder/agents/react/tools_spec.py`` enforces ``TOOL_REGISTRY ⇄ TOOL_SPECS``:
every registered tool is advertised to the LLM, so on the ReAct arm the parity
assert *is* a reachability guard — the model can always reach the whole toolbox.

The deterministic pipeline arm had no analogue. A capability could sit in the
shared toolbox, be registered, be unit-tested, work — and have no call site on
the arm that is now the ``--interactive`` default. Nothing went red. The gap
surfaced only in a live run, as a crate missing something the toolbox can
produce. This module supplies the missing invariant.

It is arm-agnostic on purpose (#98's MCP front-end is a plausible third caller),
and it deliberately does **not** register anything: a ``TOOL_REGISTRY`` entry
with no ``TOOL_SPECS`` schema would trip ``assert_tool_spec_parity()``.

Two deliberate divergences from the ReAct precedent it otherwise mirrors:

* **The waiver is a ``Mapping[str, str]``, not a ``frozenset``.** A reason is
  structurally mandatory, so the waiver cannot decay into a mute allowlist.
* **``assert_pipeline_reachability()`` is called from the test only, never at
  runtime.** Parsing the tree on every build would be pure waste, and this
  guard must not change either arm's behaviour.

How the graph is built
----------------------
An AST call graph over first-party source that **resolves import bindings**.
Three cheaper designs were considered and each is provably wrong here:

* *Instrumenting* ``engine.run_tool`` at runtime sees only the ~15 literals the
  spine dispatches. ``builder/tools/`` contains zero ``run_tool(`` call sites —
  composites call their peers as plain Python — so every composite-internal tool
  would be a false failure.
* *Grepping* for ``"<tool_name>"`` under the pipeline package cannot tell a call
  from prose. ``populate_condition_table`` is named in a docstring inside the
  toolbox itself, so a grep guard would declare the motivating bug reachable.
* *Bare attribute-name matching* cannot tell a tool from a same-named method:
  ``state.list_entities()`` appears in ~20 places and would mask the registered
  ``management.list_entities``. Only a receiver bound by an import resolves here,
  which is exactly what removes that false positive.

The analysis **over-approximates on purpose**: it can call a branch reachable
that cannot in fact fire, so it never produces a false failure. It fails only
when no call site exists anywhere on the arm — precisely the recurring class.
A guard that cries wolf is worse than no guard.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterable, Mapping
from pathlib import Path

from builder.agents.react.tools_spec import _TOOL_REGISTRY_MODULES

# Repository root: builder/tools/reachability.py -> builder/tools -> builder -> root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# First-party trees the graph spans. `builder/agents/react/` is excluded (see
# _EXCLUDED_PREFIXES) so an edge into the ReAct arm terminates and the question
# stays "reachable on the *default* arm".
_SOURCE_ROOTS: tuple[str, ...] = ("builder", "lookups", "profiles")
_SOURCE_FILES: tuple[str, ...] = ("main.py",)

_EXCLUDED_PREFIXES: tuple[str, ...] = ("builder.agents.react.",)

# Entry points of the deterministic arm. `main.py` must be here: `list_sessions`
# and `load_session` are reached from the CLI resume path and are arm-agnostic,
# so a pipeline-package-only seed set reports both as false failures.
PIPELINE_SEEDS: frozenset[str] = frozenset(
    {
        "main",
        "builder.agents.build",
        "builder.agents.pipeline",
        "builder.agents.pipeline.pipeline",
        "builder.agents.pipeline.guidance",
        "builder.agents.pipeline.leaves",
    }
)


class ToolReachabilityError(RuntimeError):
    """A registered tool has no call site on the deterministic arm (#386)."""


# Registered tools with no call site on the deterministic arm, each with the
# reason that makes the waiver an answer rather than an allowlist. Two kinds sit
# here and the reason says which: a tool that SHOULD be wired (with the lane that
# owns it), and a tool that is honestly ReAct-only or superseded.
#
# This mapping is self-cleaning by construction: `assert_pipeline_reachability`
# fails on a waiver naming a now-reachable tool, so wiring one forces its row out.
PIPELINE_UNREACHED: Mapping[str, str] = {
    # --- should be wired; each names the lane that owns it -------------------
    "lookup_cell_line_by_name": (
        "Owned by #372 — the only name->Cellosaurus resolver, while the arm "
        "materialises cell lines through draft_cell_line_sample, which does no "
        "lookup; default-arm CellLineSamples therefore carry no accession."
    ),
    "validate_table": (
        "The Frictionless payload layer (#409) is documented REQUIRED but never "
        "runs on the default arm, so populated tables are never validated."
    ),
    "lookup_ror": (
        "No caller. composites._find_or_draft_organization sets `ror` only when "
        "a caller supplies one, and nothing on the arm does, so default-arm "
        "Organizations ship with no verified ROR. Needs its own lane: network "
        "cost plus name ambiguity."
    ),
    "verify_all_identifiers": (
        "No caller. The arm verifies compound identifiers only, and only inside "
        "resolve_compound. Needs a decision on where a D5 sweep report surfaces."
    ),
    "check_provenance": (
        "No caller, though it is report-only and offline — the cheapest genuine "
        "win here, one call after the fix loop."
    ),
    "lookup_unit": (
        "No deterministic unit resolution yet; tied to the placeholder "
        "ParameterValue lane, which is exactly where units are needed."
    ),
    # --- honestly ReAct-only or superseded ------------------------------------
    "lookup_ontology_term": (
        "Generic OLS escape hatch across EFO/OBI/NCIT/UBERON. No deterministic "
        "field needs an arbitrary ontology term; this is LLM-discretionary."
    ),
    "remove_entity": (
        "Repair-only escape hatch. repair.fix_required_issues repairs by setting "
        "fields, never by deleting entities."
    ),
    "list_entities": (
        "Thin wrapper over CrateState.list_entities; the arm calls the state "
        "method directly (e.g. main.py's session summary). Introspection for an "
        "LLM that cannot see the state object."
    ),
    "list_scanned_files": (
        "Same class as list_entities — the spine already holds the scan result "
        "in hand and has no need to ask for it."
    ),
    "get_status": (
        "Introspection for an LLM. The arm's only route is AgentEngine.get_status, "
        "called directly from main.py's summary."
    ),
    "get_hint": (
        "Next-step advice for a model deciding what to do next. On the "
        "deterministic arm the next step *is* code."
    ),
    "validate": (
        "The on-disk validator. Both arms validate in memory via "
        "build_and_validate; this stays as the on-disk entry the ReAct LLM may "
        "reach for. Not dead code."
    ),
    "build_crate": (
        "Its only production route is writers/rocrate_writer <- writers/arc_writer, "
        "and write_arc has no production caller. Overlaps #360."
    ),
    "assess_mit_coverage": (
        "The registered wrapper _assess_mit_coverage_tool has no arm call site. "
        "The capability is not missing: writers/maturity_report calls the "
        "underlying assess_mit_coverage directly, with the assembled graph."
    ),
    "set_validation_preference": (
        "Records a mid-session change of mind about the RECOMMENDED/OPTIONAL "
        "tiers. The deterministic arm has no dialogue in which the user changes "
        "their mind, so there is nothing for it to do there."
    ),
}


# ---------------------------------------------------------------------------
# Source discovery + parsing
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    """Return the dotted module name for a first-party source *path*."""
    rel = path.relative_to(_REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _iter_source_paths() -> Iterable[Path]:
    for name in _SOURCE_FILES:
        candidate = _REPO_ROOT / name
        if candidate.is_file():
            yield candidate
    for root in _SOURCE_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            yield path


def _is_excluded(module: str) -> bool:
    return any(module.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


class _ModuleIndex:
    """One parsed module: its import bindings and its module-level functions."""

    def __init__(self, module: str, tree: ast.Module) -> None:
        self.module = module
        self.tree = tree
        # local name -> fully-qualified target ("pkg.mod.func" or "pkg.mod")
        self.bindings: dict[str, str] = {}
        # local name -> module, for `import pkg.mod as m` / `import pkg.mod`
        self.module_bindings: dict[str, str] = {}
        # function name -> node, for module-level defs
        self.functions: dict[str, ast.AST] = {}
        self._index()

    def _index(self) -> None:
        # Imports are collected from the WHOLE module, not just module scope:
        # the codebase uses function-local imports heavily to break cycles
        # (`from builder.tools.validation import ensure_validated` inside
        # `export_crate`), and those bind real edges.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative import
                    base = self._resolve_relative(node)
                else:
                    base = node.module or ""
                if not base:
                    continue
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.bindings[local] = f"{base}.{alias.name}"
                    # `from pkg import mod` also binds a module for `mod.f` use.
                    self.module_bindings[local] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    self.module_bindings[local] = target
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions[node.name] = node

    def _resolve_relative(self, node: ast.ImportFrom) -> str:
        parts = self.module.split(".")
        # `from . import x` inside a package __init__ has one fewer level to strip.
        drop = node.level - 1 if self.module in _PACKAGE_MODULES else node.level
        base = parts[: len(parts) - drop] if drop else parts
        if node.module:
            return ".".join([*base, node.module])
        return ".".join(base)


_PACKAGE_MODULES: set[str] = set()


@functools.lru_cache(maxsize=1)
def _index_sources() -> dict[str, _ModuleIndex]:
    """Parse every first-party module once; cached for the process."""
    paths: list[tuple[str, Path]] = []
    for path in _iter_source_paths():
        module = _module_name(path)
        if _is_excluded(module):
            continue
        if path.name == "__init__.py":
            _PACKAGE_MODULES.add(module)
        paths.append((module, path))

    index: dict[str, _ModuleIndex] = {}
    for module, path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):  # unparseable file is simply not a node
            continue
        index[module] = _ModuleIndex(module, tree)
    return index


# ---------------------------------------------------------------------------
# Edge resolution
# ---------------------------------------------------------------------------


def _tool_targets() -> dict[str, str]:
    """Return tool name -> ``"module.qualname"`` of its registered function."""
    import importlib

    for module in _TOOL_REGISTRY_MODULES:
        importlib.import_module(module)
    from builder.tools.registry import TOOL_REGISTRY

    targets: dict[str, str] = {}
    for name, spec in TOOL_REGISTRY.all().items():
        fn = spec.fn
        fn_module = getattr(fn, "__module__", None)
        fn_qualname = getattr(fn, "__qualname__", None)
        if fn_module and fn_qualname:
            targets[name] = f"{fn_module}.{fn_qualname}"
    return targets


def _edges_from(idx: _ModuleIndex, node: ast.AST, tools: Mapping[str, str]) -> set[str]:
    """Return every resolvable target referenced inside *node*'s subtree."""
    out: set[str] = set()
    for child in ast.walk(node):
        # (c) `<anything>.run_tool("literal")` -> the registry entry.
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "run_tool"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, str)
        ):
            target = tools.get(child.args[0].value)
            if target:
                out.add(target)
            continue
        # (a) a name bound by an import, used as a call or passed as a value.
        if isinstance(child, ast.Name):
            bound = idx.bindings.get(child.id)
            if bound:
                out.add(bound)
            # (b) a sibling function defined in the same module.
            elif child.id in idx.functions:
                out.add(f"{idx.module}.{child.id}")
        # (a') `m.f` where `m` is an import-bound module. A receiver that is a
        # local variable (`state.list_entities()`) resolves to nothing — the rule
        # that keeps a CrateState method from masquerading as a tool.
        elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            mod = idx.module_bindings.get(child.value.id)
            if mod:
                out.add(f"{mod}.{child.attr}")
    return out


def _definition_key(target: str) -> tuple[str, str] | None:
    """Split ``"pkg.mod.func"`` into ``(module, func)`` if that module exists."""
    index = _index_sources()
    if "." not in target:
        return None
    module, _, func = target.rpartition(".")
    if module in index and func in index[module].functions:
        return module, func
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_graph() -> dict[str, set[str]]:
    """Return ``"module.func" -> {resolved targets}`` for every first-party func."""
    index = _index_sources()
    tools = _tool_targets()
    graph: dict[str, set[str]] = {}
    for module, idx in index.items():
        for name, node in idx.functions.items():
            graph[f"{module}.{name}"] = _edges_from(idx, node, tools)
    return graph


def reachable_functions(seeds: Iterable[str] = PIPELINE_SEEDS) -> set[str]:
    """Return every ``"module.func"`` reachable from the *seeds* modules.

    Every function defined in a seed module is a root, as is module-level code
    there — the arm's entry points are files, not a single ``main()``.
    """
    index = _index_sources()
    tools = _tool_targets()
    graph = call_graph()

    frontier: set[str] = set()
    for seed in seeds:
        idx = index.get(seed)
        if idx is None:
            continue
        for name in idx.functions:
            frontier.add(f"{seed}.{name}")
        # Module-level statements (CLI wiring, constants holding callables).
        module_level = ast.Module(
            body=[
                stmt
                for stmt in idx.tree.body
                if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            ],
            type_ignores=[],
        )
        frontier |= _edges_from(idx, module_level, tools)

    reached: set[str] = set()
    while frontier:
        target = frontier.pop()
        if target in reached:
            continue
        reached.add(target)
        key = _definition_key(target)
        if key is None:
            continue
        module, func = key
        frontier |= graph.get(f"{module}.{func}", set()) - reached
    return reached


def reachable_tools(seeds: Iterable[str] = PIPELINE_SEEDS) -> set[str]:
    """Return the registered tool names with a call site reachable from *seeds*."""
    reached = reachable_functions(seeds)
    return {name for name, target in _tool_targets().items() if target in reached}


def unreached_tools(seeds: Iterable[str] = PIPELINE_SEEDS) -> set[str]:
    """Return the registered tool names with no call site reachable from *seeds*."""
    return set(_tool_targets()) - reachable_tools(seeds)


def assert_pipeline_reachability() -> None:
    """Raise :class:`ToolReachabilityError` if the waiver has drifted (#386).

    Two-sided, exactly like ``missing``/``extra`` in ``assert_tool_spec_parity``:
    an unwaived unreached tool fails, **and** a waiver naming a now-reached tool
    fails. That second direction is what keeps the waiver self-cleaning — when a
    tool gets wired, its row must be deleted or CI goes red.
    """
    unreached = unreached_tools()
    unwaived = unreached - set(PIPELINE_UNREACHED)
    stale = set(PIPELINE_UNREACHED) - unreached
    if unwaived or stale:
        raise ToolReachabilityError(
            "Tool reachability on the deterministic arm has drifted (#386): "
            f"registered tools with no call site and no waiver: {sorted(unwaived)}; "
            f"waivers naming tools that are now reachable: {sorted(stale)}."
        )


__all__ = [
    "PIPELINE_SEEDS",
    "PIPELINE_UNREACHED",
    "ToolReachabilityError",
    "assert_pipeline_reachability",
    "call_graph",
    "reachable_functions",
    "reachable_tools",
    "unreached_tools",
]
