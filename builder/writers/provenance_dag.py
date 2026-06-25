"""Render the LabProcess provenance DAG from an assembled RO-Crate.

The paper's core value proposition is that a receiving lab can trace how an
output was produced::

    Sample →[CellCulture]→ Sample →[Exposure]→ condition_table
           →[EndpointReadout]→ raw_measurements →[DataAnalysis]→ figures

This writer reads a serialized metadata document — the ``@graph`` from
``crate.metadata.generate()`` or a parsed ``ro-crate-metadata.json`` — and emits
a Mermaid ``flowchart`` of that derivation chain, drawn **from real data** rather
than by hand. It plots only the provenance subgraph: each ``LabProcess`` and the
entities it consumes (``schema:object``) and produces (``schema:result``), plus
the optional ``about`` / ``derivesFrom`` annotation edges that connect the
compound (a ``MolecularEntity``, never a process object) through the condition
table. Contextual entities off the chain (people, organisations, parameters,
protocols) are deliberately excluded so the DAG stays a derivation graph.

The edge vocabulary mirrors ``builder.tools.provenance`` (``_INPUT_FIELDS`` /
``_OUTPUT_FIELDS``) and the ``@context`` aliases in ``profiles.context`` so the
renderer and the builder agree on what an input/output edge is; the bare
``schema:`` IRIs are accepted too so externally-authored crates render as well.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Property keys that carry a process's consumed (input) and produced (output)
# edges. The compact aliases match the tox/ISA models (``input``/``output`` on
# the tox subclasses, ``object``/``result`` on the base LabProcess) and the
# ``@context`` aliases; the full schema.org IRIs cover expanded documents.
_INPUT_KEYS: tuple[str, ...] = (
    "object",
    "input",
    "samples",
    "cell_line",
    "http://schema.org/object",
    "https://schema.org/object",
)
_OUTPUT_KEYS: tuple[str, ...] = (
    "result",
    "output",
    "http://schema.org/result",
    "https://schema.org/result",
)
_LINEAGE_KEYS: tuple[str, ...] = (
    "derivesFrom",
    "derives_from",
    "isBasedOn",
    "http://schema.org/isBasedOn",
    "https://schema.org/isBasedOn",
)
_ABOUT_KEYS: tuple[str, ...] = (
    "about",
    "http://schema.org/about",
    "https://schema.org/about",
)
_TYPE_KEYS: tuple[str, ...] = ("@type",)
_NAME_KEYS: tuple[str, ...] = ("name", "http://schema.org/name", "https://schema.org/name")
_ADDTYPE_KEYS: tuple[str, ...] = (
    "additionalType",
    "http://schema.org/additionalType",
    "https://schema.org/additionalType",
)

# The four domain LabProcess discriminators (ISA-Tox profile).
_PROCESS_DISCRIMINATORS = frozenset(
    {"CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"}
)


# ---------------------------------------------------------------------------
# Full crate entity-graph (#130): three-layer classification + typed edges
# ---------------------------------------------------------------------------
#
# Layer classification follows the authoritative precedence derived from the
# models/context/shapes (crate-graph-inventory): root short-circuit → csvw:Table
# @type override → additionalType (domain) → additionalType (structural) → base
# @type. PropertyValue subtypes (ParameterValue/…) stay structural despite having
# an additionalType — the one deliberate exception.
_LAYER_NAMES: dict[int, str] = {
    1: "Packaging — RO-Crate",
    2: "Structural — ISA",
    3: "Domain — ISA-Tox",
}

_DOMAIN_ADDTYPES = frozenset(
    {"CellLine", "CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"}
)
_STRUCT_ADDTYPES = frozenset(
    {
        "Investigation", "Study", "Assay",
        "ParameterValue", "FactorValue", "CharacteristicValue", "Component",
    }
)
# Local names only — `_short` strips any CURIE prefix (csvw:Table → Table), so
# the canonical compact form, the expanded IRI, and a non-standard prefix all
# reduce to the same token before these checks.
_CSVW_TABLE_TYPES = frozenset({"Table"})
_DOMAIN_TYPES = frozenset(
    {"MolecularEntity", "Schema", "Column",
     "AdverseOutcomePathway", "KeyEvent", "KeyEventRelationship"}
)
_STRUCT_TYPES = frozenset(
    {"LabProcess", "LabProtocol", "Sample", "DefinedTerm", "PropertyValue", "Dataset"}
)

# Plumbing nodes dropped from the visualization (used only to locate the root).
# Includes the embedded graph artifact itself, so re-rendering an exported crate
# never depicts its own diagram file.
_EXCLUDED_IDS = frozenset(
    {
        "ro-crate-metadata.json",
        "./ro-crate-metadata.json",
        "ro-crate-preview.html",
        "ro-crate-graph.mmd",
    }
)

# Layer-synonym map for the --layer filter.
_LAYER_SYNONYMS: dict[str, int] = {
    "1": 1, "crate": 1, "rocrate": 1, "ro-crate": 1, "packaging": 1,
    "2": 2, "isa": 2, "structural": 2, "structure": 2,
    "3": 3, "isa-tox": 3, "isatox": 3, "tox": 3, "domain": 3, "all": 3,
}


def normalize_layer(value: str | int | None) -> int:
    """Resolve a ``--layer`` value (number or synonym) to a cumulative depth 1-3.

    ``crate``→1, ``isa``→2, ``isa-tox``/``tox``/``all``→3. ``None``→3 (show all).
    """
    if value is None:
        return 3
    key = str(value).strip().lower()
    if key not in _LAYER_SYNONYMS:
        raise ValueError(
            f"Unknown layer {value!r}. Use 1/2/3 or crate/isa/isa-tox (or 'all')."
        )
    return _LAYER_SYNONYMS[key]


def _is_uri(value: Any) -> bool:
    """True for an absolute, dereferenceable identifier (contains a scheme)."""
    return isinstance(value, str) and "://" in value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _short(token: str) -> str:
    """Local name of a type token: last path/hash segment of an IRI, then any
    remaining CURIE prefix stripped.

    ``http://schema.org/Person`` → ``Person``; ``.../ns/csvw#Table`` → ``Table``;
    ``csvw:Table`` → ``Table``; ``mycsv:Table`` → ``Table``. The writer has no
    ``@context`` to expand prefixes, so it classifies on local names — this keeps
    classification prefix-agnostic (a crate that aliases the csvw namespace to a
    non-standard prefix still classifies its tables/columns as domain entities).
    """
    seg = token.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return seg.rsplit(":", 1)[-1]


def _types(node: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in _TYPE_KEYS:
        for t in _as_list(node.get(key)):
            if isinstance(t, str):
                out.add(_short(t))
    return out


def _first(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in node and node[key] is not None:
            return node[key]
    return None


def _is_ref_string(value: Any) -> bool:
    """A bare string that denotes a link, not a literal: a local fragment
    (``#…``) or an absolute URI (contains ``://``).

    Most RO-Crate references are ``{"@id": …}`` objects, but ro-crate-py
    serializes some reference-typed properties as bare strings — notably CSVW
    ``valueUrl`` (e.g. ``"#compound_aflb1"``). We accept those, while a plain
    literal (``intendedUse: "culture"``, ``measurementTechnique: "qPCR"``) is
    correctly ignored so it never becomes a spurious dangling reference.
    """
    return isinstance(value, str) and (value.startswith("#") or "://" in value)


def _refs(node: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """The reference targets across any of ``keys``.

    A reference is either a ``{"@id": …}`` object (or a list of them) or a bare
    string that looks like a link (see :func:`_is_ref_string`). Plain string
    literals are ignored.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        for item in _as_list(node.get(key)):
            ref: Any = None
            if isinstance(item, dict):
                ref = item.get("@id")
            elif _is_ref_string(item):
                ref = item
            if isinstance(ref, str) and ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _additional_type(node: dict[str, Any]) -> str | None:
    value = _first(node, _ADDTYPE_KEYS)
    if isinstance(value, dict):
        value = value.get("@id")
    return _short(value) if isinstance(value, str) else None


def _is_process(node: dict[str, Any]) -> bool:
    if "LabProcess" in _types(node):
        return True
    return _additional_type(node) in _PROCESS_DISCRIMINATORS


def _node_class(node: dict[str, Any]) -> str:
    """Style bucket: proc | mat (Sample) | data (File/Table) | ctx."""
    if _is_process(node):
        return "proc"
    types = _types(node)
    if {"File", "MediaObject", "Table"} & types:
        return "data"
    if "Sample" in types:
        return "mat"
    return "ctx"


def _name(node: dict[str, Any]) -> str:
    value = _first(node, _NAME_KEYS)
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("@value") or value.get("@id")
    return str(value) if value else str(node.get("@id", "?"))


def _tag(node: dict[str, Any]) -> str:
    """Short type/discriminator tag shown under the node name."""
    if _is_process(node):
        return _additional_type(node) or "LabProcess"
    types = _types(node)
    for preferred in ("MolecularEntity", "Table", "File", "Sample"):
        if preferred in types:
            tag = preferred
            break
    else:
        tag = next(iter(sorted(types)), "Entity")
    add = _additional_type(node)
    return f"{tag} · {add}" if add and add != tag else tag


def _escape(text: str) -> str:
    """Make a crate-controlled string safe to interpolate into a Mermaid label.

    Mermaid node labels are quoted strings whose contents are rendered as HTML
    by mermaid.js, so unescaped crate data (entity names/descriptions) is a
    stored-XSS vector — a name like ``<img src=x onerror=alert(1)>`` would run
    when the embedded diagram is opened (#169). We therefore HTML-escape the
    metacharacters (``& < > "``) so the text renders inert, collapse newlines so
    the label stays single-line, and strip surrounding whitespace.

    The intentional markup the writers add around a label (``<br/>``,
    ``<small>``) is composed *after* this call, so it stays live; only the
    dynamic text is neutralised. ``"`` becomes ``&quot;`` (not ``'``) so a label
    can never break out of its quoted Mermaid string either.
    """
    escaped = html.escape(text, quote=True)
    return escaped.replace("\n", " ").replace("\r", " ").strip()


def _mermaid_node(mid: str, node: dict[str, Any]) -> str:
    label = f"{_escape(_name(node))}<br/><small>{_escape(_tag(node))}</small>"
    cls = _node_class(node)
    if cls == "proc":
        return f'{mid}{{{{"{label}"}}}}'
    if cls == "mat":
        return f'{mid}(["{label}"])'
    if cls == "data":
        return f'{mid}["{label}"]'
    return f'{mid}[/"{label}"/]'


def _sanitize_id(raw: str, counter: dict[str, int]) -> str:
    base = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_") or "n"
    if base[0].isdigit():
        base = "n" + base
    if base not in counter:
        counter[base] = 0
        return base
    counter[base] += 1
    return f"{base}_{counter[base]}"


def render_provenance_mermaid(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    direction: str = "LR",
    include_annotations: bool = True,
    fenced: bool = False,
) -> str:
    """Render the LabProcess derivation chain as a Mermaid flowchart.

    Args:
        metadata: A parsed ``ro-crate-metadata.json`` dict (with a ``@graph``
            key), the ``@graph`` list directly, or the dict returned by
            ``crate.metadata.generate()``.
        direction: Mermaid flow direction (``LR``, ``TD``, ``RL``, ``BT``).
        include_annotations: When True, also draw the ``about`` edges that
            connect the compound/cell line through the condition table and the
            ``derivesFrom`` sample-lineage edges (dotted). When False, only the
            ``object``/``result`` derivation edges are drawn.
        fenced: When True, wrap the output in a ```` ```mermaid ```` code block
            for direct embedding in Markdown.

    Returns:
        The Mermaid source as a string.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes: dict[str, dict[str, Any]] = {
        n["@id"]: n for n in graph if isinstance(n, dict) and "@id" in n
    }

    # Collect derivation edges first; the set of touched ids defines the
    # provenance subgraph we actually draw (everything else is excluded).
    solid: list[tuple[str, str, str]] = []  # (src_id, dst_id, label)
    dotted: list[tuple[str, str, str]] = []
    touched: set[str] = set()

    for nid, node in nodes.items():
        if not _is_process(node):
            continue
        touched.add(nid)
        for src in _refs(node, _INPUT_KEYS):
            solid.append((src, nid, "object"))
            touched.add(src)
        for dst in _refs(node, _OUTPUT_KEYS):
            solid.append((nid, dst, "result"))
            touched.add(dst)

    if include_annotations:
        # `about` edges only from the produced data tables (the condition
        # table), never from Datasets — keeps Assay→process structural edges out.
        for nid in list(touched):
            node = nodes.get(nid)
            if node is None or _node_class(node) != "data":
                continue
            for tgt in _refs(node, _ABOUT_KEYS):
                if tgt in nodes:
                    dotted.append((nid, tgt, "about"))
                    touched.add(tgt)
        # `derivesFrom` lineage between samples already on the chain.
        for nid in list(touched):
            node = nodes.get(nid)
            if node is None:
                continue
            for src in _refs(node, _LINEAGE_KEYS):
                if src in touched:
                    dotted.append((nid, src, "derivesFrom"))

    # Assign Mermaid ids only to nodes we draw.
    counter: dict[str, int] = {}
    mid: dict[str, str] = {}
    for nid in touched:
        if nid in nodes:
            mid[nid] = _sanitize_id(nid, counter)

    lines: list[str] = [f"flowchart {direction}"]

    # Node declarations grouped by class for readable, deterministic output.
    by_class: dict[str, list[str]] = {"proc": [], "mat": [], "data": [], "ctx": []}
    for nid in sorted(mid):
        node = nodes[nid]
        lines.append(f"  {_mermaid_node(mid[nid], node)}")
        by_class[_node_class(node)].append(mid[nid])

    def _edge(src: str, dst: str, label: str, dotted_edge: bool) -> str | None:
        if src not in mid or dst not in mid:
            return None
        arrow = f'-. "{label}" .->' if dotted_edge else f'-- "{label}" -->'
        return f"  {mid[src]} {arrow} {mid[dst]}"

    edge_lines: list[str] = []
    for src, dst, label in solid:
        rendered = _edge(src, dst, label, dotted_edge=False)
        if rendered:
            edge_lines.append(rendered)
    for src, dst, label in dotted:
        rendered = _edge(src, dst, label, dotted_edge=True)
        if rendered:
            edge_lines.append(rendered)
    lines.extend(edge_lines)

    # Styling.
    lines.append("  classDef proc fill:#dbeafe,stroke:#1e40af,color:#1e3a8a;")
    lines.append("  classDef mat fill:#dcfce7,stroke:#166534,color:#14532d;")
    lines.append("  classDef data fill:#fef9c3,stroke:#854d0e,color:#713f12;")
    lines.append("  classDef ctx fill:#f3e8ff,stroke:#6b21a8,color:#581c87;")
    for cls, ids in by_class.items():
        if ids:
            lines.append(f"  class {','.join(sorted(ids))} {cls};")

    out = "\n".join(lines)
    if fenced:
        out = f"```mermaid\n{out}\n```"
    return out


# Relation key sets for the full crate graph. Each tuple is
# (json_keys, label, reversed): reversed=True means the referenced node points
# INTO the holder (process inputs), so the drawn edge is ref --> holder.
_HASPART_KEYS = ("hasPart", "has_part", "http://schema.org/hasPart")
_MENTIONS_KEYS = (
    "mentions", "aop", "keyEvent", "key_event", "key_events", "organism",
    "anatomy", "chemicals", "biologicalModels", "biological_models", "cell_lines",
    "http://schema.org/mentions",
)
_AUTHOR_KEYS = (
    "author", "creator", "http://schema.org/author", "http://schema.org/creator",
)
_PROTOCOL_KEYS = ("executesLabProtocol", "https://bioschemas.org/executesLabProtocol")
_ABOUT_GRAPH_KEYS = _ABOUT_KEYS + ("labProcesses",)
_SAMPLETYPE_KEYS = ("sampleType",)
_TABLESCHEMA_KEYS = ("tableSchema", "http://www.w3.org/ns/csvw#tableSchema")
_COLUMNS_KEYS = ("columns", "column", "http://www.w3.org/ns/csvw#column")
_VALUEURL_KEYS = ("valueUrl", "http://www.w3.org/ns/csvw#valueUrl")
_CONFORMSTO_KEYS = ("conformsTo", "http://purl.org/dc/terms/conformsTo")
_CITATION_KEYS = ("citation", "funder", "publisher")
_MEASTECH_KEYS = ("measurementTechnique", "measurementMethod", "intendedUse")
_PARAM_KEYS = (
    "parameter", "parameterValue", "additionalProperty",
    "http://schema.org/additionalProperty",
)

# (keys, label, reversed). Primary edges tell the structural + derivation story;
# secondary edges (CSVW internals, conformsTo, citation/funder) add detail under
# --all-edges. Orphan reachability always uses BOTH sets so it reflects the true
# crate connectivity regardless of what is drawn.
_PRIMARY_RELATIONS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (_HASPART_KEYS, "hasPart", False),
    (_OUTPUT_KEYS, "result", False),
    (_INPUT_KEYS, "input", True),
    (_LINEAGE_KEYS, "derivesFrom", False),
    (_ABOUT_GRAPH_KEYS, "about", False),
    (_MENTIONS_KEYS, "mentions", False),
    (_AUTHOR_KEYS, "author", False),
    (_PROTOCOL_KEYS, "executes", False),
    (_SAMPLETYPE_KEYS, "sampleType", False),
)
_SECONDARY_RELATIONS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (_TABLESCHEMA_KEYS, "tableSchema", False),
    (_COLUMNS_KEYS, "column", False),
    (_VALUEURL_KEYS, "valueUrl", False),
    (_CONFORMSTO_KEYS, "conformsTo", False),
    (_CITATION_KEYS, "citation", False),
    (_MEASTECH_KEYS, "measurementTechnique", False),
    (_PARAM_KEYS, "parameter", False),
)


def _entity_layer(node: dict[str, Any]) -> int:
    """Classify a node into a paper layer (1 packaging / 2 ISA / 3 ISA-Tox).

    Caller handles the root short-circuit (root → 1) before this is reached.
    """
    types = _types(node)
    if types & _CSVW_TABLE_TYPES:  # File + csvw:Table → domain table node
        return 3
    add = _additional_type(node)
    if add in _DOMAIN_ADDTYPES:
        return 3
    if add in _STRUCT_ADDTYPES:  # incl. PropertyValue subtypes — stay structural
        return 2
    if types & _DOMAIN_TYPES:
        return 3
    if types & _STRUCT_TYPES:
        return 2
    return 1


# Functional entity categories — orthogonal to the layer. The node FILL/SHAPE
# encodes this (what the entity *is*); the enclosing subgraph encodes the layer.
# Each entry: (fill, stroke, mermaid-shape-open, shape-close).
_CATEGORY_STYLE: dict[str, tuple[str, str, str, str]] = {
    "container": ("#e0e7ff", "#4f46e5", "[[", "]]"),   # Dataset (Investigation/Study/Assay)
    "process": ("#dbeafe", "#2563eb", "{{", "}}"),     # LabProcess
    "protocol": ("#cffafe", "#0891b2", "[/", "\\]"),   # LabProtocol
    "material": ("#dcfce7", "#16a34a", "([", "])"),    # Sample
    "chemical": ("#fef3c7", "#d97706", "(", ")"),      # MolecularEntity
    "data": ("#fef9c3", "#ca8a04", "[(", ")]"),        # File / csvw:Table
    "annotation": ("#f3e8ff", "#9333ea", "(", ")"),    # DefinedTerm/PropertyValue/AOP/KeyEvent/CSVW
    "agent": ("#fce7f3", "#db2777", "(", ")"),         # Person / Organization
    "publication": ("#ffe4e6", "#e11d48", "(", ")"),   # ScholarlyArticle
}
_DEFAULT_CATEGORY_STYLE = ("#e5e7eb", "#9ca3af", "(", ")")

# Very light per-layer wash for the enclosing subgraph box (subtle, hue-distinct).
_LAYER_BOX_STYLE: dict[int, tuple[str, str]] = {
    1: ("#f8fafc", "#cbd5e1"),
    2: ("#f4fdf7", "#bbf7d0"),
    3: ("#fcf7ff", "#e9d5ff"),
}


def _entity_category(node: dict[str, Any]) -> str:
    """Functional category of an in-crate entity (drives node colour/shape)."""
    if _is_process(node):
        return "process"
    types = _types(node)
    if "LabProtocol" in types:
        return "protocol"
    if "Dataset" in types:
        return "container"
    if types & {"File", "MediaObject", "Table"}:
        return "data"
    if "MolecularEntity" in types:
        return "chemical"
    if "Sample" in types:
        return "material"
    if types & {"Person", "Organization"}:
        return "agent"
    if "ScholarlyArticle" in types:
        return "publication"
    return "annotation"


def _find_root_id(nodes: dict[str, Any], graph: list[dict[str, Any]]) -> str | None:
    """The Root Data Entity id: what the metadata descriptor's ``about`` points
    at, else ``./`` / ``""``."""
    for node in graph:
        nid = str(node.get("@id", ""))
        if nid.rsplit("/", 1)[-1] == "ro-crate-metadata.json":
            for ref in _refs(node, _ABOUT_KEYS):
                if ref in nodes:
                    return ref
    for candidate in ("./", ""):
        if candidate in nodes:
            return candidate
    return None


def _all_relations(all_edges: bool) -> tuple[tuple[tuple[str, ...], str, bool], ...]:
    return _PRIMARY_RELATIONS + _SECONDARY_RELATIONS if all_edges else _PRIMARY_RELATIONS


def _extract_edges(
    nodes: dict[str, Any], relations: tuple[tuple[tuple[str, ...], str, bool], ...]
) -> list[dict[str, str]]:
    """All (src, dst, label) edges over ``relations``; endpoints may be off-graph
    references (resolved to status later)."""
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for nid, node in nodes.items():
        for keys, label, reverse in relations:
            for ref in _refs(node, keys):
                src, dst = (ref, nid) if reverse else (nid, ref)
                key = (src, dst, label)
                if src != dst and key not in seen:
                    seen.add(key)
                    edges.append({"src": src, "dst": dst, "label": label})
    return edges


def _reachable_from(root: str | None, edges: list[dict[str, str]]) -> set[str]:
    """Undirected reachability from the root over the edge set."""
    if root is None:
        return set()
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["src"], set()).add(e["dst"])
        adj.setdefault(e["dst"], set()).add(e["src"])
    seen = {root}
    stack = [root]
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, ()):  # noqa: SIM118
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def build_crate_graph(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    layer: str | int = "all",
    all_edges: bool = False,
) -> dict[str, Any]:
    """Turn a serialized RO-Crate ``@graph`` into a deterministic graph model.

    Classifies every node into a paper layer, marks each referenced ``@id`` as
    ``in_crate`` / ``external`` (resolvable URI, not a node) / ``dangling`` (not a
    node and not resolvable), flags orphans (in-crate nodes not connected to the
    Root Data Entity), and applies a cumulative ``--layer`` filter.

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.
        layer: Cumulative depth — ``1``/``crate``, ``2``/``isa``,
            ``3``/``isa-tox``/``all``. Nodes above the depth are dropped.
        all_edges: Include secondary edges (CSVW internals, conformsTo, citation).

    Returns:
        ``{"nodes": [...], "edges": [...], "hidden_count": int, "counts": {...},
        "root": str|None}``. Each node: ``{id, label, type, layer, status,
        identifier_backed, orphan}``.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    raw = {n["@id"]: n for n in graph if isinstance(n, dict) and "@id" in n}
    root_id = _find_root_id(raw, list(graph))

    # In-crate nodes = everything except plumbing descriptors.
    nodes = {
        nid: n
        for nid, n in raw.items()
        if str(nid).rsplit("/", 1)[-1] not in _EXCLUDED_IDS and nid not in _EXCLUDED_IDS
    }

    depth = normalize_layer(layer)

    # Edges for connectivity/status use ALL relations; drawn edges may be a subset.
    full_edges = _extract_edges(nodes, _all_relations(all_edges=True))
    draw_edges = _extract_edges(nodes, _all_relations(all_edges))
    reachable = _reachable_from(root_id, full_edges)

    # Referenced ids that are NOT in-crate nodes → external or dangling stubs.
    referenced = {e["src"] for e in full_edges} | {e["dst"] for e in full_edges}
    stub_ids = {r for r in referenced if r not in nodes}

    def _layer_of(nid: str) -> int:
        return 1 if nid == root_id else _entity_layer(nodes[nid])

    model_nodes: dict[str, dict[str, Any]] = {}
    for nid, node in nodes.items():
        lyr = _layer_of(nid)
        is_root = nid == root_id
        id_backed = _is_uri(nid) or bool(node.get("identifier") or node.get("url"))
        raw_label = _name(node)
        label = "Crate root" if is_root and raw_label in (nid, "./", "") else raw_label
        model_nodes[nid] = {
            "id": nid,
            "label": _escape(label),
            "type": _tag(node),
            "category": _entity_category(node),
            "layer": lyr,
            "status": "in_crate",
            "identifier_backed": id_backed,
            "orphan": (not is_root) and (nid not in reachable),
        }
    for sid in stub_ids:
        external = _is_uri(sid)
        model_nodes[sid] = {
            "id": sid,
            "label": _escape(_external_label(sid)),
            "type": "external" if external else "unresolved",
            "category": None,
            "layer": None,
            "status": "external" if external else "dangling",
            "identifier_backed": external,
            "orphan": False,
        }

    # Cumulative layer filter: drop in-crate nodes deeper than `depth`; drop
    # stubs that lose their only connection; drop edges touching dropped nodes.
    kept = {
        nid
        for nid, n in model_nodes.items()
        if n["layer"] is None or n["layer"] <= depth
    }
    visible_edges = [
        e for e in draw_edges if e["src"] in kept and e["dst"] in kept
    ]
    connected_stubs = {e["src"] for e in visible_edges} | {e["dst"] for e in visible_edges}
    final_nodes = [
        n
        for nid, n in model_nodes.items()
        if nid in kept and (n["layer"] is not None or nid in connected_stubs)
    ]
    final_ids = {n["id"] for n in final_nodes}
    visible_edges = [
        e for e in visible_edges if e["src"] in final_ids and e["dst"] in final_ids
    ]

    hidden_count = sum(
        1 for n in model_nodes.values() if n["layer"] is not None and n["layer"] > depth
    )
    counts = {
        "layer1": sum(1 for n in model_nodes.values() if n["layer"] == 1),
        "layer2": sum(1 for n in model_nodes.values() if n["layer"] == 2),
        "layer3": sum(1 for n in model_nodes.values() if n["layer"] == 3),
        "external": sum(1 for n in model_nodes.values() if n["status"] == "external"),
        "dangling": sum(1 for n in model_nodes.values() if n["status"] == "dangling"),
        "orphan": sum(1 for n in model_nodes.values() if n["orphan"]),
    }

    return {
        "nodes": final_nodes,
        "edges": visible_edges,
        "hidden_count": hidden_count,
        "counts": counts,
        "root": root_id,
    }


def _external_label(uri: str) -> str:
    """A compact, human-readable label for an off-graph reference."""
    if not _is_uri(uri):
        return uri  # dangling local ref — show as-is
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    host = uri.split("://", 1)[-1].split("/", 1)[0].replace("www.", "")
    return f"{tail} ({host})" if tail and tail != host else host


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem; }}
  body {{ background: #fff; color: #111; }}
  h1 {{ font-size: 1.1rem; font-weight: 600; margin: 0 0 1rem; }}
  #graph {{ overflow: auto; }}
  #graph svg {{ max-width: 100%; height: auto; }}
  .err {{ color: #b91c1c; white-space: pre-wrap; font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div id="graph"><p class="err">Rendering… (needs network access for mermaid.js)</p></div>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  // securityLevel 'strict' (the default) sandboxes labels and disables click
  // handlers so crate-controlled node text can't execute as HTML/JS (#169).
  mermaid.initialize({{ startOnLoad: false, securityLevel: 'strict' }});
  const src = {source};
  try {{
    const {{ svg }} = await mermaid.render('provenance-dag', src);
    document.getElementById('graph').innerHTML = svg;
  }} catch (e) {{
    document.getElementById('graph').innerHTML =
      '<pre class="err">' + String(e) + '\\n\\n' + src.replace(/[&<>]/g, c =>
        ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c])) + '</pre>';
  }}
</script>
</body>
</html>
"""


def render_mermaid_html(mermaid: str, *, title: str = "Provenance DAG") -> str:
    """Wrap Mermaid source in a self-contained HTML page that renders it.

    The diagram is rendered client-side by mermaid.js (loaded from a CDN). The
    source is embedded as a JS string literal (via ``json.dumps``) so the
    ``<br/>`` / ``<small>`` markup inside node labels reaches mermaid intact
    instead of being parsed away by the HTML document.

    Args:
        mermaid: Mermaid ``flowchart`` source (e.g. from
            :func:`render_provenance_mermaid`).
        title: Page title / heading.

    Returns:
        A complete HTML document as a string.
    """
    return _HTML_TEMPLATE.format(title=title, source=json.dumps(mermaid))


def _crate_node_label(n: dict[str, Any]) -> str:
    # ``label`` is already escaped (see build_crate_graph); the ``id`` fallback
    # is crate-controlled too, so escape it before it reaches the HTML label.
    name = n["label"] or _escape(n["id"])
    if n["status"] == "external":
        return f"🔗 {name}<br/><small>outside crate</small>"
    if n["status"] == "dangling":
        return f"⚠ {name}<br/><small>unresolved ref</small>"
    icon = "⚠ " if n["orphan"] else ("🔗 " if n["identifier_backed"] else "")
    suffix = " · orphan" if n["orphan"] else ""
    return f"{icon}{name}<br/><small>{_escape(n['type'])}{suffix}</small>"


def _crate_node_shape(mid: str, n: dict[str, Any]) -> str:
    """Node decl — shape encodes the functional CATEGORY (not the layer)."""
    label = _crate_node_label(n)
    if n["status"] in ("external", "dangling"):
        return f'{mid}[/"{label}"/]'  # off-graph references: parallelogram
    _f, _s, open_, close = _CATEGORY_STYLE.get(n["category"], _DEFAULT_CATEGORY_STYLE)
    return f'{mid}{open_}"{label}"{close}'


def _crate_node_class(n: dict[str, Any]) -> str:
    """Fill class — the functional CATEGORY (in-crate) or status (stubs)."""
    if n["status"] == "external":
        return "external"
    if n["status"] == "dangling":
        return "dangling"
    return f"cat_{n['category']}"


def render_crate_graph(
    metadata: dict[str, Any] | list[dict[str, Any]],
    *,
    layer: str | int = "all",
    direction: str = "TD",
    include_legend: bool = True,
    all_edges: bool = False,
) -> str:
    """Render the full crate entity-graph as a layered Mermaid flowchart (#130).

    Nodes are grouped into the three paper layers (Packaging / Structural /
    Domain) as subgraphs, with off-graph references collected in an "Outside the
    crate" group so the in-crate vs external-identifier distinction is explicit.
    Identifier-backed entities carry a 🔗; dangling references and orphans carry a
    ⚠. A legend explains the encoding.

    Args:
        metadata: Parsed crate document, ``@graph`` list, or ``generate()`` output.
        layer: Cumulative depth filter — ``crate``/``isa``/``isa-tox`` or 1/2/3.
        direction: Mermaid flow direction (``TD``/``LR``/...).
        include_legend: Append a legend subgraph explaining colours/markers.
        all_edges: Also draw secondary edges (CSVW internals, conformsTo, …).

    Returns:
        The Mermaid source as a string.
    """
    model = build_crate_graph(metadata, layer=layer, all_edges=all_edges)
    depth = normalize_layer(layer)

    # Declutter: draw only nodes that carry a visible edge, are orphans (the #130
    # problem we want surfaced), or are the root. Structural annotation nodes that
    # connect only via secondary edges (parameters, CSVW columns) stay in the
    # model but are hidden until --all-edges brings their edges into view.
    edge_ids = {e["src"] for e in model["edges"]} | {e["dst"] for e in model["edges"]}
    drawn = edge_ids | {model["root"]} | {n["id"] for n in model["nodes"] if n["orphan"]}
    nodes = [n for n in model["nodes"] if n["id"] in drawn]

    counter: dict[str, int] = {}
    mid = {n["id"]: _sanitize_id(n["id"], counter) for n in nodes}

    lines: list[str] = [f"flowchart {direction}"]
    c = model["counts"]
    summary = (
        f"%% crate graph — layer≤{depth} · "
        f"L1:{c['layer1']} L2:{c['layer2']} L3:{c['layer3']} · "
        f"external:{c['external']} dangling:{c['dangling']} orphan:{c['orphan']}"
    )
    if model["hidden_count"]:
        summary += f" · {model['hidden_count']} hidden by --layer"
    lines.append(summary)

    # Layer subgraphs (only non-empty ones). The box is the LAYER; the nodes
    # inside are coloured by functional category, not by layer.
    box_styles: list[str] = []
    for lyr in (1, 2, 3):
        group = [n for n in nodes if n["status"] == "in_crate" and n["layer"] == lyr]
        if not group:
            continue
        marks = {1: "①", 2: "②", 3: "③"}[lyr]
        lines.append(f'  subgraph layer{lyr}_g["{marks} {_LAYER_NAMES[lyr]}"]')
        for n in sorted(group, key=lambda x: mid[x["id"]]):
            lines.append(f"    {_crate_node_shape(mid[n['id']], n)}")
        lines.append("  end")
        fill, stroke = _LAYER_BOX_STYLE[lyr]
        box_styles.append(f"  style layer{lyr}_g fill:{fill},stroke:{stroke};")

    # Off-graph references — the "outside the crate" group.
    outside = [n for n in nodes if n["status"] in ("external", "dangling")]
    if outside:
        lines.append('  subgraph outside_g["⤴ Outside the crate (🔗 resolvable · ⚠ unresolved)"]')
        for n in sorted(outside, key=lambda x: mid[x["id"]]):
            lines.append(f"    {_crate_node_shape(mid[n['id']], n)}")
        lines.append("  end")

    # Edges.
    for e in model["edges"]:
        if e["src"] in mid and e["dst"] in mid:
            lines.append(f'  {mid[e["src"]]} -- "{_escape(e["label"])}" --> {mid[e["dst"]]}')

    if include_legend:
        lines.extend(_crate_legend_lines())

    # Category classDefs (node fill = what the entity IS).
    for cat, (fill, stroke, _o, _c) in _CATEGORY_STYLE.items():
        lines.append(f"  classDef cat_{cat} fill:{fill},stroke:{stroke},color:#1f2937;")
    # Status classDefs for off-graph references + the orphan stroke overlay.
    lines.append(
        "  classDef external fill:#f8fafc,stroke:#64748b,"
        "color:#334155,stroke-dasharray:4 3;"
    )
    lines.append(
        "  classDef dangling fill:#fee2e2,stroke:#b91c1c,"
        "color:#7f1d1d,stroke-width:2px;"
    )
    lines.append("  classDef orphan stroke:#ea580c,stroke-width:3px,stroke-dasharray:2 2;")
    for n in nodes:
        lines.append(f"  class {mid[n['id']]} {_crate_node_class(n)};")
        if n["orphan"]:
            lines.append(f"  class {mid[n['id']]} orphan;")
    # Subtle per-layer box wash (emitted last so it isn't overridden).
    lines.extend(box_styles)

    return "\n".join(lines)


# Legend exemplars: (mermaid id, label, category-or-status class). Node fill =
# functional category; the enclosing layer box carries the (subtle) layer tint.
_LEGEND_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("lg_container", "Dataset (Inv/Study/Assay)", "cat_container"),
    ("lg_process", "LabProcess", "cat_process"),
    ("lg_protocol", "LabProtocol", "cat_protocol"),
    ("lg_material", "Sample", "cat_material"),
    ("lg_chemical", "MolecularEntity", "cat_chemical"),
    ("lg_data", "File / table", "cat_data"),
    ("lg_annotation", "Term / param / AOP", "cat_annotation"),
    ("lg_agent", "Person / Org", "cat_agent"),
    ("lg_external", "🔗 Outside crate (resolvable)", "external"),
    ("lg_dangling", "⚠ Dangling reference", "dangling"),
)


def _crate_legend_lines() -> list[str]:
    """A compact legend: node colour = entity type; boxes = the three layers."""
    lines = ['  subgraph legend_g["Legend — node colour = entity type · box = layer"]']
    lines.append("    direction LR")
    for mid_, label, cls in _LEGEND_ITEMS:
        shape = _CATEGORY_STYLE.get(cls.replace("cat_", ""))
        if cls in ("external", "dangling"):
            lines.append(f'    {mid_}[/"{label}"/]:::{cls}')
        elif shape:
            open_, close = shape[2], shape[3]
            lines.append(f'    {mid_}{open_}"{label}"{close}:::{cls}')
    lines.append("  end")
    return lines


def render_provenance_mermaid_from_file(
    path: str | Path, **kwargs: Any
) -> str:
    """Read a ``ro-crate-metadata.json`` from disk and render its DAG.

    Args:
        path: Path to a ``ro-crate-metadata.json`` (or any JSON with a
            ``@graph``).
        **kwargs: Forwarded to :func:`render_provenance_mermaid`.

    Returns:
        The Mermaid source as a string.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return render_provenance_mermaid(doc, **kwargs)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Render the LabProcess provenance DAG of an RO-Crate as Mermaid."
    )
    parser.add_argument("metadata", type=Path, help="Path to ro-crate-metadata.json")
    parser.add_argument("--direction", default="LR", help="Flow direction (LR/TD/RL/BT)")
    parser.add_argument(
        "--no-annotations",
        action="store_true",
        help="Draw only object/result edges (omit about/derivesFrom)",
    )
    parser.add_argument(
        "--fenced", action="store_true", help="Wrap output in a ```mermaid block"
    )
    args = parser.parse_args(argv)
    print(
        render_provenance_mermaid_from_file(
            args.metadata,
            direction=args.direction,
            include_annotations=not args.no_annotations,
            fenced=args.fenced,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
