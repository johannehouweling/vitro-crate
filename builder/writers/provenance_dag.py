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
import re
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
_PROCESS_DISCRIMINATORS = frozenset({"CellCulture", "Exposure", "EndpointReadout", "DataAnalysis"})


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
        "Investigation",
        "Study",
        "Assay",
        "ParameterValue",
        "FactorValue",
        "CharacteristicValue",
        "Component",
    }
)
# Local names only — `_short` strips any CURIE prefix (csvw:Table → Table), so
# the canonical compact form, the expanded IRI, and a non-standard prefix all
# reduce to the same token before these checks.
_CSVW_TABLE_TYPES = frozenset({"Table"})
_DOMAIN_TYPES = frozenset(
    {
        "MolecularEntity",
        "Schema",
        "Column",
        "AdverseOutcomePathway",
        "KeyEvent",
        "KeyEventRelationship",
    }
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
    "1": 1,
    "crate": 1,
    "rocrate": 1,
    "ro-crate": 1,
    "packaging": 1,
    "2": 2,
    "isa": 2,
    "structural": 2,
    "structure": 2,
    "3": 3,
    "isa-tox": 3,
    "isatox": 3,
    "tox": 3,
    "domain": 3,
    "all": 3,
}


def normalize_layer(value: str | int | None) -> int:
    """Resolve a ``--layer`` value (number or synonym) to a cumulative depth 1-3.

    ``crate``→1, ``isa``→2, ``isa-tox``/``tox``/``all``→3. ``None``→3 (show all).
    """
    if value is None:
        return 3
    key = str(value).strip().lower()
    if key not in _LAYER_SYNONYMS:
        raise ValueError(f"Unknown layer {value!r}. Use 1/2/3 or crate/isa/isa-tox (or 'all').")
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
    nodes = _graph_nodes(metadata)

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


# ---------------------------------------------------------------------------
# Inline-SVG provenance chain (#85) — a self-contained, offline diagram of the
# derivation chain for embedding in the maturity report. Unlike
# ``render_provenance_mermaid`` (which needs mermaid.js to render), this emits a
# finished ``<svg>`` element: no script, no external assets, prints as-is.
# ---------------------------------------------------------------------------
_SVG_NODE_W = 138
_SVG_NODE_H = 48
_SVG_COL_DX = 160  # left-edge spacing between columns
_SVG_ROW_DY = 96  # top-edge spacing between rows
_SVG_X0 = 18
_SVG_Y0 = 22
_SVG_FOLD = 11  # folded-corner size on the "File" node shape
# _node_class() bucket → (node CSS class, tag CSS class).
_SVG_CLASS = {
    "proc": ("n-process", "tag-process"),
    "mat": ("n-material", "tag-material"),
    "data": ("n-data", "tag-data"),
    "ctx": ("n-ctx", "tag-ctx"),
    # Contextual entities never appear in the derivation chain (ISA forbids a
    # MolecularEntity as a process object, and people are not consumed by a
    # process), but the chemicals and people diagrams reuse this geometry so
    # every view in the report reads as one system.
    "chem": ("n-chem", "tag-chem"),
    "agent": ("n-agent", "tag-agent"),
    "org": ("n-org", "tag-org"),
    "container": ("n-container", "tag-container"),
}


def _svg_trunc(text: str, limit: int = 18) -> str:
    """Single-line ellipsised label (the SVG has no text wrapping)."""
    text = text.replace("\n", " ").replace("\r", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _svg_node_shape(cls: str, x: int, y: int, variant: str = "") -> str:
    """The node's outline path/polygon for style bucket *cls* at ``(x, y)``.

    ``variant`` appends an extra CSS class to the outline (used by the chemicals
    diagram to mark a compound the crate never links to a process), so state is
    carried by the stylesheet rather than by inline attributes.
    """
    w, h = _SVG_NODE_W, _SVG_NODE_H
    extra = f" {variant}" if variant else ""
    if cls == "proc":  # hexagon (chevron ends read as "a step")
        pts = (
            f"{x},{y + h // 2} {x + 13},{y} {x + w - 13},{y} "
            f"{x + w},{y + h // 2} {x + w - 13},{y + h} {x + 13},{y + h}"
        )
        return f'<polygon class="n n-process{extra}" points="{pts}"/>'
    if cls == "mat":  # stadium (rounded ends) — a sample/material
        return (
            f'<rect class="n n-material{extra}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{h // 2}" ry="{h // 2}"/>'
        )
    if cls == "data":  # document with a folded corner — a File/Table
        f = _SVG_FOLD
        body = (
            f"M{x + 4},{y} H{x + w - f} L{x + w},{y + f} V{y + h - 4} "
            f"Q{x + w},{y + h} {x + w - 4},{y + h} H{x + 4} "
            f"Q{x},{y + h} {x},{y + h - 4} V{y + 4} Q{x},{y} {x + 4},{y} Z"
        )
        fold = f"M{x + w - f},{y} V{y + f} H{x + w} Z"
        return f'<path class="n n-data{extra}" d="{body}"/><path class="fold" d="{fold}"/>'
    if cls == "container":  # barred block — an ISA container (Investigation/Study/Assay)
        bar = 7
        return (
            f'<rect class="n n-container{extra}" x="{x}" y="{y}" width="{w}" '
            f'height="{h}" rx="3" ry="3"/>'
            f'<path class="bars" d="M{x + bar},{y} V{y + h} M{x + w - bar},{y} V{y + h}"/>'
        )
    if cls == "agent":  # pill — a person (no materials share this view)
        return (
            f'<rect class="n n-agent{extra}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{h // 2}" ry="{h // 2}"/>'
        )
    if cls == "org":  # square-shouldered block — an institution
        return (
            f'<rect class="n n-org{extra}" x="{x}" y="{y}" width="{w}" height="{h}" '
            'rx="3" ry="3"/>'
        )
    if cls == "chem":  # octagon — a compound (distinct from the process hexagon)
        c = 12
        pts = (
            f"{x + c},{y} {x + w - c},{y} {x + w},{y + c} {x + w},{y + h - c} "
            f"{x + w - c},{y + h} {x + c},{y + h} {x},{y + h - c} {x},{y + c}"
        )
        return f'<polygon class="n n-chem{extra}" points="{pts}"/>'
    # ctx — a plain rounded rectangle for anything off the material/data axis.
    return (
        f'<rect class="n n-ctx{extra}" x="{x}" y="{y}" width="{w}" height="{h}" rx="8" ry="8"/>'
    )


def render_provenance_svg(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> str:
    """Render the LabProcess derivation chain as a self-contained inline ``<svg>``.

    Reads the same derivation subgraph as :func:`render_provenance_mermaid` — each
    ``LabProcess`` and the entities it consumes (``object``/``input``) and produces
    (``result``/``output``) — and lays it out left-to-right by longest-path depth,
    so a receiving lab can read how a result was produced. Materials render as
    rounded "stadiums", processes as hexagons, files/tables as folded documents;
    ``object`` edges are dashed ("consumes"), ``result`` edges solid ("produces").

    Unlike the Mermaid renderer, the output is a finished SVG element (markers,
    node shapes, edge paths, labels) with **no script and no external assets**, so
    it embeds directly in the offline maturity report and prints as-is. All
    crate-controlled text (entity names/tags) is HTML-escaped (#169).

    Args:
        metadata: A parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        The ``<svg>…</svg>`` markup, or ``""`` when the crate records no
        derivation chain (no in-crate process input/output edges to draw).
    """
    nodes = _graph_nodes(metadata)

    # Directed derivation edges, both pointing "downstream": material --object-->
    # process, process --result--> data. Only edges whose endpoints are both
    # in-crate nodes are drawn (off-graph refs have nothing to attach to).
    edges: list[tuple[str, str, str]] = []  # (src, dst, kind)
    for nid, node in nodes.items():
        if not _is_process(node):
            continue
        for src in _refs(node, _INPUT_KEYS):
            if src in nodes:
                edges.append((src, nid, "object"))
        for dst in _refs(node, _OUTPUT_KEYS):
            if dst in nodes:
                edges.append((nid, dst, "result"))
    if not edges:
        return ""

    drawn = {e[0] for e in edges} | {e[1] for e in edges}

    # Predecessors/successors over the drawn subgraph.
    preds: dict[str, list[str]] = {n: [] for n in drawn}
    has_succ: dict[str, bool] = {n: False for n in drawn}
    for src, dst, _ in edges:
        preds[dst].append(src)
        has_succ[src] = True

    # Column = longest path from any source (memoised depth; back-edges → 0 so a
    # pathological cycle can't recurse forever).
    col: dict[str, int] = {}
    inprog: set[str] = set()

    def _depth(n: str) -> int:
        if n in col:
            return col[n]
        if n in inprog:
            return 0
        inprog.add(n)
        d = 0
        for p in preds[n]:
            d = max(d, _depth(p) + 1)
        inprog.discard(n)
        col[n] = d
        return d

    for n in sorted(drawn):
        _depth(n)

    # Row assignment, column by column left→right. Within a column, nodes that
    # continue the chain (have a successor) are placed first so the "spine" stays
    # on a straight row; a node inherits a placed predecessor's row when free,
    # else the lowest free row — which cleanly steps branch outputs onto new rows.
    by_col: dict[int, list[str]] = {}
    for n in drawn:
        by_col.setdefault(col[n], []).append(n)
    row: dict[str, int] = {}
    for c in sorted(by_col):
        used: set[int] = set()
        ordered = sorted(by_col[c], key=lambda n: (0 if has_succ[n] else 1, n))
        for n in ordered:
            placed_pred_rows = [row[p] for p in preds[n] if p in row]
            desired = min(placed_pred_rows) if placed_pred_rows else None
            if desired is not None and desired not in used:
                r = desired
            else:
                r = 0
                while r in used:
                    r += 1
            row[n] = r
            used.add(r)

    pos: dict[str, tuple[int, int]] = {
        n: (_SVG_X0 + col[n] * _SVG_COL_DX, _SVG_Y0 + row[n] * _SVG_ROW_DY) for n in drawn
    }
    vb_w = max(x + _SVG_NODE_W for x, _ in pos.values()) + 18
    vb_h = max(y + _SVG_NODE_H for _, y in pos.values()) + 22

    # Edges: draw results (solid) then objects (dashed), each deterministically.
    mid = _SVG_NODE_H // 2
    edge_svg: list[str] = []
    for kind in ("result", "object"):
        for src, dst, k in sorted(edges):
            if k != kind:
                continue
            x1, y1 = pos[src][0] + _SVG_NODE_W, pos[src][1] + mid
            x2, y2 = pos[dst][0], pos[dst][1] + mid
            dx = (x2 - x1) // 2 if y1 == y2 else max((x2 - x1) // 2, 30)
            path = f"M{x1},{y1} C{x1 + dx},{y1} {x2 - dx},{y2} {x2},{y2}"
            edge_svg.append(
                f'<path class="e e-{kind}" d="{path}" marker-end="url(#prov-ar-{kind})"/>'
            )

    # Nodes: shape + type tag (above) + name (inside). Labels are escaped.
    node_svg: list[str] = []
    for n in sorted(drawn, key=lambda n: (col[n], row[n], n)):
        node = nodes[n]
        cls = _node_class(node)
        node_cls, tag_cls = _SVG_CLASS[cls]
        x, y = pos[n]
        cx = x + _SVG_NODE_W // 2
        name = _escape(_svg_trunc(_name(node)))
        tag = _escape(_svg_trunc(_tag(node), 22)).upper()
        node_svg.append(
            f"<g><title>{_escape(_name(node))} — {_escape(_tag(node))}</title>"
            f"{_svg_node_shape(cls, x, y)}"
            f'<text class="tag {tag_cls}" x="{cx}" y="{y - 6}">{tag}</text>'
            f'<text class="name" x="{cx}" y="{y + 28}">{name}</text></g>'
        )

    marker = (
        '<marker id="prov-ar-{k}" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="mk-{k}"/></marker>'
    )
    return (
        f'<svg viewBox="0 0 {vb_w} {vb_h}" width="{vb_w}" height="{vb_h}" '
        'role="img" aria-label="Provenance derivation chain" class="prov">'
        "<title>Provenance derivation chain</title>"
        f"<defs>{marker.format(k='object')}{marker.format(k='result')}</defs>"
        f'<g class="edges">{"".join(edge_svg)}</g>'
        f'<g class="nodes">{"".join(node_svg)}</g></svg>'
    )


# Relation key sets for the full crate graph. Each tuple is
# (json_keys, label, reversed): reversed=True means the referenced node points
# INTO the holder (process inputs), so the drawn edge is ref --> holder.
_HASPART_KEYS = ("hasPart", "has_part", "http://schema.org/hasPart")
_MENTIONS_KEYS = (
    "mentions",
    "aop",
    "keyEvent",
    "key_event",
    "key_events",
    "organism",
    "anatomy",
    "chemicals",
    "biologicalModels",
    "biological_models",
    "cell_lines",
    "http://schema.org/mentions",
)
_AUTHOR_KEYS = (
    "author",
    "creator",
    "http://schema.org/author",
    "http://schema.org/creator",
)
_CONTRIBUTOR_KEYS = (
    "contributor",
    "maintainer",
    "http://schema.org/contributor",
    "http://schema.org/maintainer",
)
# A Person's institution. This is the ONLY edge that reaches an affiliation-only
# Organization: nothing else in the crate references it, so leaving it out of the
# relation sets below reports every ROR-backed institution as an orphan while the
# crate is in fact correct.
_AFFILIATION_KEYS = (
    "affiliation",
    "memberOf",
    "http://schema.org/affiliation",
    "https://schema.org/affiliation",
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
    "parameter",
    "parameterValue",
    "additionalProperty",
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
    (_CONTRIBUTOR_KEYS, "contributor", False),
    (_AFFILIATION_KEYS, "affiliation", False),
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
    "container": ("#e0e7ff", "#4f46e5", "[[", "]]"),  # Dataset (Investigation/Study/Assay)
    "process": ("#dbeafe", "#2563eb", "{{", "}}"),  # LabProcess
    "protocol": ("#cffafe", "#0891b2", "[/", "\\]"),  # LabProtocol
    "material": ("#dcfce7", "#16a34a", "([", "])"),  # Sample
    "chemical": ("#fef3c7", "#d97706", "(", ")"),  # MolecularEntity
    "data": ("#fef9c3", "#ca8a04", "[(", ")]"),  # File / csvw:Table
    "annotation": ("#f3e8ff", "#9333ea", "(", ")"),  # DefinedTerm/PropertyValue/AOP/KeyEvent/CSVW
    "agent": ("#fce7f3", "#db2777", "(", ")"),  # Person / Organization
    "publication": ("#ffe4e6", "#e11d48", "(", ")"),  # ScholarlyArticle
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
    raw = _graph_nodes(graph)
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
    # The generated graph artifact is registered in the exported metadata as a
    # ``File`` about the root, but it is intentionally omitted from the graph
    # visualization input to avoid drawing the diagram inside itself. Treat that
    # reserved artifact as external plumbing rather than a dangling entity.
    referenced = {e["src"] for e in full_edges} | {e["dst"] for e in full_edges}
    stub_ids = {
        r for r in referenced
        if r not in nodes and str(r).rsplit("/", 1)[-1] not in _EXCLUDED_IDS
    }

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
    kept = {nid for nid, n in model_nodes.items() if n["layer"] is None or n["layer"] <= depth}
    visible_edges = [e for e in draw_edges if e["src"] in kept and e["dst"] in kept]
    connected_stubs = {e["src"] for e in visible_edges} | {e["dst"] for e in visible_edges}
    final_nodes = [
        n
        for nid, n in model_nodes.items()
        if nid in kept and (n["layer"] is not None or nid in connected_stubs)
    ]
    final_ids = {n["id"] for n in final_nodes}
    visible_edges = [e for e in visible_edges if e["src"] in final_ids and e["dst"] in final_ids]

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


# ---------------------------------------------------------------------------
# Chemical inventory (#85) — which compounds a crate declares, how each one
# reaches the experiment, and how completely it is identified.
#
# A tox crate's compounds are the one thing a receiving lab must be able to pin
# down exactly, and they are also the easiest thing to leave dangling. ISA
# forbids a MolecularEntity as a LabProcess ``object`` (objects MUST be
# File/Sample/BioSample), so a compound is *never* wired to its Exposure
# directly: it hangs off the produced condition table
# (``table --about--> MolecularEntity``, see ``_crate_mapping._synth_condition_table``),
# off that table's ``compound`` CSVW column via ``valueUrl``, and — at a glance —
# off the Study via ``schema:mentions``. Miss that indirection and the crate
# still validates while every compound sits orphaned: fully described, but
# unreachable from the experiment that used it.
#
# This model therefore reports both halves, because either alone is misleading:
#   * the ROUTE      — which process produced which table which names which
#                      compound (or where that chain breaks), and
#   * the IDENTITY   — CAS / PubChem CID / DTXSID plus the structure fields that
#                      let someone else obtain the same substance.
# ---------------------------------------------------------------------------

_CHEMICAL_TYPES = frozenset({"MolecularEntity", "ChemicalSubstance"})

_IDENTIFIER_KEYS: tuple[str, ...] = (
    "identifier",
    "http://schema.org/identifier",
    "https://schema.org/identifier",
)
_VALUE_KEYS: tuple[str, ...] = ("value", "http://schema.org/value", "https://schema.org/value")
_PROPERTYID_KEYS: tuple[str, ...] = (
    "propertyID",
    "http://schema.org/propertyID",
    "https://schema.org/propertyID",
)

# A bare `identifier` string that is a CAS Registry Number (2-7 / 2 / 1 check
# digit) — ro-crate-py serializes some crates that way instead of minting an
# identifier PropertyValue, and dropping it would under-report identification.
_CAS_RE = re.compile(r"\d{2,7}-\d{2}-\d")

# Registry identifier schemes in the order ``_crate_mapping._MOLECULAR_IDENTIFIERS``
# mints them (CAS → PubChem CID → EPA DTXSID). ``aliases`` are matched
# case-folded against the identifier PropertyValue's ``name``; ``hosts`` against
# its ``propertyID`` *and* against the compound's own ``@id``, so a crate whose
# @id is the PubChem/CompTox page still counts as carrying that identifier even
# when it never spells the scheme out as a PropertyValue.
# ``pattern`` guards the URL fallback below: a host match alone would turn ANY
# PubChem page (a /bioassay/, a /patent/) into a "CID", inflating the
# identification score with a value that identifies nothing.
_CHEM_ID_SCHEMES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...], "re.Pattern[str] | None"], ...
] = (
    ("CAS", "CAS", ("cas", "casrn", "cas rn", "cas number", "cas registry number"), (), _CAS_RE),
    (
        "PubChem CID",
        "CID",
        ("pubchem cid", "pubchem", "cid"),
        ("pubchem.ncbi.nlm.nih.gov/compound",),
        re.compile(r"\d+"),
    ),
    (
        "DTXSID",
        "DTXSID",
        ("dtxsid", "dsstox", "dsstox substance id"),
        ("comptox.epa.gov/dashboard/chemical",),
        re.compile(r"DTXSID\w+", re.IGNORECASE),
    ),
)

# Structure/characterisation fields carried directly on the compound. The
# compact aliases are the ``profiles.context`` terms; the schema.org IRIs cover
# an expanded document.
_CHEM_STRUCTURE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "InChIKey",
        ("inchikey", "inChIKey", "http://schema.org/inChIKey", "https://schema.org/inChIKey"),
    ),
    ("SMILES", ("smiles", "http://schema.org/smiles", "https://schema.org/smiles")),
    (
        "Formula",
        (
            "formula",
            "molecularFormula",
            "molecular_formula",
            "http://schema.org/molecularFormula",
            "https://schema.org/molecularFormula",
        ),
    ),
    (
        "Mass",
        (
            "mass",
            "molecularWeight",
            "molecular_weight",
            "http://schema.org/molecularWeight",
            "https://schema.org/molecularWeight",
        ),
    ),
)

# The coverage matrix columns: (full name, column header). Registry identifiers
# first (they are what makes a compound orderable), then structure.
CHEM_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (scheme, short) for scheme, short, *_rest in _CHEM_ID_SCHEMES
) + tuple((label, label) for label, _keys in _CHEM_STRUCTURE_FIELDS)

# The less canonical ways an entity can still be referenced. They are scanned
# last (a canonical route is preferred when both exist) but they MUST be scanned:
# the panel states outright that "nothing in the crate references this compound",
# and the topology strip a few lines below it reaches the same node over the
# fuller `_PRIMARY_RELATIONS`/`_SECONDARY_RELATIONS` vocabulary. Omitting them
# let the two halves of one section contradict each other.
_EXTRA_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_OUTPUT_KEYS, "result"),
    (_LINEAGE_KEYS, "derivesFrom"),
    (_HASPART_KEYS, "hasPart"),
    (_PARAM_KEYS, "parameter"),
)

# Relations by which some other entity can point AT a compound, most canonical
# first — the order decides which route is drawn when a compound is reachable
# more than one way. ``about`` is the condition table's link, ``valueUrl`` the
# per-well column link, ``mentions`` (which subsumes the ``chemicals`` alias) the
# Study-level one; ``input``/``parameter`` are non-conformant but real, and a
# crate that uses them should see them rather than be told it has no route.
_CHEM_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_ABOUT_KEYS, "about"),
    (_VALUEURL_KEYS, "valueUrl"),
    (_MENTIONS_KEYS, "mentions"),
    (_INPUT_KEYS, "input"),
) + _EXTRA_LINK_RELATIONS
# Containment relations walked *upward* from a referrer to the entity a process
# actually produced: a `compound` column is owned by a csvw:Schema, which is the
# tableSchema of the condition table, which is the Exposure's result.
_CHEM_CONTAINMENT_KEYS: tuple[tuple[str, ...], ...] = (_TABLESCHEMA_KEYS, _COLUMNS_KEYS)
_CHEM_ANCHOR_HOPS = 3


def _graph_nodes(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index a serialized document by ``@id``, skipping malformed entries.

    ``@id`` MUST be a string, but a hand-edited or machine-mangled crate can
    carry a number or an object there. Indexing on it unguarded raises
    ``TypeError`` out of the report writer, and because the report is built
    inside ``export_crate`` that costs the crate its entire maturity report over
    one bad node. Skipping the node degrades one row instead.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    return {
        n["@id"]: n
        for n in graph
        if isinstance(n, dict) and isinstance(n.get("@id"), str)
    }


def _is_chemical(node: dict[str, Any]) -> bool:
    return bool(_types(node) & _CHEMICAL_TYPES)


def _literal(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First non-empty literal across ``keys``, unwrapping a list / ``@value``."""
    for key in keys:
        value = node.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("@value")
        if value not in (None, "", []):
            return str(value)
    return None


def _registry_identifiers(
    node: dict[str, Any],
    nodes: dict[str, Any],
    schemes: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], Any], ...],
) -> dict[str, str]:
    """Persistent identifiers an entity carries, as ``{scheme: value}``.

    Reads the ``identifier`` PropertyValues that ``_crate_mapping`` mints —
    matching the scheme by the PropertyValue's ``name`` or its ``propertyID``
    host — then falls back to the entity's own ``@id`` when that is itself a
    registry URL (an ORCID, a ROR, a PubChem compound page). The @id fallback
    matters: it is the *most* actionable form of the identifier, and a crate that
    uses it without also minting a PropertyValue is better identified, not worse.
    """
    found: dict[str, str] = {}

    def _scheme_of(name: str | None, url: str | None) -> str | None:
        key = (name or "").strip().casefold().replace("_", " ")
        for scheme, _short, aliases, hosts, _pat in schemes:
            if key and key in aliases:
                return scheme
            if url and any(host in url for host in hosts):
                return scheme
        return None

    def _from_url(url: str) -> tuple[str, str] | None:
        """``(scheme, value)`` when *url* is a registry URL whose tail actually
        looks like that registry's identifier."""
        for scheme, _short, _aliases, hosts, pattern in schemes:
            if not hosts or not any(host in url for host in hosts):
                continue
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if not tail:
                return None
            if pattern is None:
                return (scheme, tail)
            match = pattern.fullmatch(tail)
            # A host match alone is not an identifier: /bioassay/1234 lives on the
            # PubChem host and identifies no compound.
            return (scheme, match.group(0)) if match else None
        return None

    for key in _IDENTIFIER_KEYS:
        for item in _as_list(node.get(key)):
            if not isinstance(item, dict):
                continue
            pv = nodes.get(item["@id"], item) if isinstance(item.get("@id"), str) else item
            prop_id = _first(pv, _PROPERTYID_KEYS)
            if isinstance(prop_id, dict):
                prop_id = prop_id.get("@id")
            scheme = _scheme_of(
                _literal(pv, _NAME_KEYS), prop_id if isinstance(prop_id, str) else None
            )
            value = _literal(pv, _VALUE_KEYS)
            if scheme and value and scheme not in found:
                found[scheme] = value
                continue
            # The identifier node may BE the registry URL (an ORCID or a ROR
            # given as {"@id": "https://orcid.org/…"}) with no separate value.
            own = pv.get("@id")
            if isinstance(own, str) and (hit := _from_url(own)) and hit[0] not in found:
                found[hit[0]] = hit[1]

    for candidate in (_literal(node, _URL_KEYS), node.get("@id")):
        if not isinstance(candidate, str):
            continue
        hit = _from_url(candidate)
        if hit and hit[0] not in found:
            found[hit[0]] = hit[1]
    return found


def _chem_identifiers(node: dict[str, Any], nodes: dict[str, Any]) -> dict[str, str]:
    """Registry identifiers a compound carries: ``{scheme: value}``.

    :func:`_registry_identifiers` plus the bare-string CAS fallback — some crates
    serialize ``identifier`` as the literal Registry Number rather than as a
    PropertyValue, and dropping it would under-report identification.
    """
    found = _registry_identifiers(node, nodes, _CHEM_ID_SCHEMES)
    if "CAS" not in found:
        for key in _IDENTIFIER_KEYS:
            for item in _as_list(node.get(key)):
                if isinstance(item, str) and _CAS_RE.fullmatch(item.strip()):
                    found["CAS"] = item.strip()
                    return found
    return found


def _referrers_to(
    nodes: dict[str, Any],
    member_ids: set[str],
    relations: tuple[tuple[tuple[str, ...], str], ...],
) -> dict[str, list[tuple[str, str]]]:
    """``member id -> [(referrer id, relation label)]``, deterministically ordered.

    References *between* members are ignored: they are not a route into the
    experiment, and counting them would let a cluster of mutually-referencing
    entities mask the fact that none of them reaches a process.
    """
    order = {label: i for i, (_keys, label) in enumerate(relations)}
    out: dict[str, set[tuple[str, str]]] = {}
    for nid, node in nodes.items():
        if nid in member_ids or str(nid).rsplit("/", 1)[-1] in _EXCLUDED_IDS:
            continue
        for keys, label in relations:
            for ref in _refs(node, keys):
                if ref in member_ids:
                    out.setdefault(ref, set()).add((nid, label))
    return {mid: sorted(pairs, key=lambda p: (order[p[1]], p[0])) for mid, pairs in out.items()}


def _route_resolver(
    nodes: dict[str, Any],
    member_ids: set[str],
    relations: tuple[tuple[tuple[str, ...], str], ...],
) -> Any:
    """Build ``resolve(member_id) -> route | None`` for a routed inventory.

    A *route* is how a reader gets from a ``LabProcess`` to the entity:
    ``{process, via, link, edge}``. Some referrers are not themselves produced by
    a process — a CSVW column is owned by a schema which is the ``tableSchema`` of
    the table a process actually produced — so the referrer is walked up the
    containment chain to the nearest process-produced ancestor before giving up.

    ``process`` is ``None`` when the entity is referenced in-crate but by nothing
    a process produced; the whole route is ``None`` when nothing references it.
    """
    referrers = _referrers_to(nodes, member_ids, relations)

    producers: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        if _is_process(node):
            for dst in _refs(node, _OUTPUT_KEYS):
                producers.setdefault(dst, []).append(nid)
        for keys in _CHEM_CONTAINMENT_KEYS:
            for ref in _refs(node, keys):
                parents.setdefault(ref, []).append(nid)

    def _anchor_of(ref_id: str) -> str | None:
        """Nearest ancestor of *ref_id* (itself included) that a process produced."""
        seen: set[str] = set()
        frontier = [ref_id]
        for _ in range(_CHEM_ANCHOR_HOPS + 1):
            nxt: list[str] = []
            for cur in frontier:
                if cur in seen:
                    continue
                seen.add(cur)
                if cur in producers:
                    return cur
                nxt.extend(parents.get(cur, ()))
            if not nxt:
                break
            frontier = sorted(nxt)
        return None

    def _resolve(mid: str) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for rid, label in referrers.get(mid, []):
            if _is_process(nodes[rid]):
                return {"process": rid, "via": rid, "link": rid, "edge": label}
            anchor = _anchor_of(rid)
            if anchor is not None:
                return {
                    "process": sorted(producers[anchor])[0],
                    "via": anchor,
                    "link": rid,
                    "edge": label,
                }
            if fallback is None:
                fallback = {"process": None, "via": rid, "link": rid, "edge": label}
        return fallback

    return _resolve


_ROUTE_STATE_ORDER = {"wired": 0, "mentioned": 1, "unlinked": 2}


def _route_state(route: dict[str, Any] | None) -> str:
    if route is None:
        return "unlinked"
    return "wired" if route["process"] else "mentioned"


def _route_bands(
    members: list[dict[str, Any]], nodes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Group routed members into the diagram's bands.

    Members sharing a ``(process, via)`` pair travel together — the relation is
    *not* part of the key, so a table that reaches some of its members by
    ``about`` and others by the column's ``valueUrl`` still draws one band; the
    band's edge label then names both mechanisms rather than the diagram
    repeating the same process and table twice.
    """
    banded: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = {}
    for member in members:
        route = member["route"] or {}
        banded.setdefault(
            (member["state"], route.get("process"), route.get("via")), []
        ).append(member)
    return [
        {
            "state": state,
            "edge": " · ".join(sorted({m["route"]["edge"] for m in group if m["route"]})),
            "process": _chem_node_brief(proc, nodes[proc]) if proc else None,
            "via": _chem_node_brief(via, nodes[via]) if via else None,
            "members": group,
        }
        for (state, proc, via), group in sorted(
            banded.items(),
            key=lambda kv: (_ROUTE_STATE_ORDER[kv[0][0]], kv[0][1] or "", kv[0][2] or ""),
        )
    ]


def _route_counts(members: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(members),
        "wired": sum(1 for m in members if m["state"] == "wired"),
        "mentioned": sum(1 for m in members if m["state"] == "mentioned"),
        "unlinked": sum(1 for m in members if m["state"] == "unlinked"),
        "fields_met": sum(m["met"] for m in members),
        "fields_total": sum(m["total"] for m in members),
    }


_EMPTY_ROUTE_COUNTS = {
    "total": 0,
    "wired": 0,
    "mentioned": 0,
    "unlinked": 0,
    "fields_met": 0,
    "fields_total": 0,
}


def _chem_node_brief(nid: str, node: dict[str, Any]) -> dict[str, str]:
    """The minimal description a drawn node needs.

    ``name``/``tag`` are the RAW crate strings and ``label`` the escaped name.
    Both are carried because the two consumers escape at different points: the
    HTML matrix interpolates ``label`` directly, while the SVG must ellipsise
    *before* escaping — truncating an escaped string can cut a character
    reference (``&amp;``) in half.
    """
    return {"id": nid, "name": _name(node), "label": _escape(_name(node)), "tag": _tag(node)}


def build_chemical_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's compounds: their route into the experiment + their identity.

    For every ``MolecularEntity`` the crate declares, resolve how it is reachable
    from a ``LabProcess`` — normally ``process --result--> table --about--> compound``,
    or via the condition table's ``compound`` column ``valueUrl`` — and how many
    of the identification fields (CAS / PubChem CID / DTXSID / InChIKey / SMILES /
    formula / mass) it actually carries.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. All crate-controlled text on the returned nodes is HTML-escaped
    (#169) so it can be interpolated straight into the report.

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        ``{"chemicals": [...], "groups": [...], "counts": {...}}``.

        Each chemical: ``{id, label, tag, resolvable, identifiers, structure,
        met, total, state, route}`` where ``state`` is ``"wired"`` (reachable
        from a process), ``"mentioned"`` (referenced in-crate but by nothing a
        process produced) or ``"unlinked"`` (referenced by nothing at all).

        ``groups`` are the diagram's route bands — compounds sharing the same
        ``(process, via)`` pair — ordered wired → mentioned → unlinked. ``counts``
        carries ``total`` / ``wired`` / ``mentioned`` / ``unlinked`` plus
        ``fields_met`` / ``fields_total`` for the identification coverage.
    """
    nodes = _graph_nodes(metadata)
    chem_ids = {nid for nid, n in nodes.items() if _is_chemical(n)}
    if not chem_ids:
        return {"chemicals": [], "groups": [], "counts": dict(_EMPTY_ROUTE_COUNTS)}

    resolve = _route_resolver(nodes, chem_ids, _CHEM_LINK_RELATIONS)

    chemicals: list[dict[str, Any]] = []
    for cid in sorted(chem_ids, key=lambda c: (_name(nodes[c]).casefold(), c)):
        node = nodes[cid]
        ids = _chem_identifiers(node, nodes)
        fields: dict[str, bool] = {
            scheme: scheme in ids for scheme, *_rest in _CHEM_ID_SCHEMES
        }
        fields.update(
            {label: _literal(node, keys) is not None for label, keys in _CHEM_STRUCTURE_FIELDS}
        )
        route = resolve(cid)
        chemicals.append(
            {
                **_chem_node_brief(cid, node),
                "resolvable": _is_uri(cid),
                "identifiers": ids,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": len(fields),
                "state": _route_state(route),
                "route": route,
            }
        )

    return {
        "chemicals": chemicals,
        "groups": _route_bands(chemicals, nodes),
        "counts": _route_counts(chemicals),
    }


# ---------------------------------------------------------------------------
# Cell lines (#85) — the biological test system, and whether it is pinned down.
#
# A cell line is the other half of "what was tested", and it fails the same two
# ways a compound does. It is *unreachable* when the CellCulture consumes a
# freshly minted generic Sample instead of the declared CellLineSample — the line
# is then described in the crate and used by nothing, exactly the shape the
# compound view exposes. And it is *unidentified* when it carries a name but no
# Cellosaurus RRID: "CHO-K1" names a family of divergent stocks, RRID CVCL_0214
# names one, and passage/organ/tissue are what let another lab reproduce the
# culture rather than merely recognise it.
# ---------------------------------------------------------------------------

_CELLLINE_TYPES = frozenset({"CellLine", "CellLineSample"})
_CELL_LINE_TERM_HINT = "cell line"

# Cellosaurus accessions, with or without the RRID: prefix (RRID:CVCL_0214).
_RRID_RE = re.compile(r"(?:RRID[:\s]*)?CVCL[_:][A-Z0-9]+", re.IGNORECASE)

_ADDPROP_KEYS: tuple[str, ...] = (
    "additionalProperty",
    "http://schema.org/additionalProperty",
    "https://schema.org/additionalProperty",
)
_URL_KEYS: tuple[str, ...] = ("url", "http://schema.org/url", "https://schema.org/url")

# ISA Sample Characteristics that make a culture reproducible, matched
# case-folded against each additionalProperty PropertyValue's ``name`` (the
# shape ``_crate_mapping._CELL_LINE_CHARACTERISTICS`` mints).
_CELLLINE_CHARACTERISTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Organ", ("organ",)),
    ("Tissue", ("tissue",)),
    ("Passage", ("passage", "passage number")),
)

CELLLINE_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Cellosaurus RRID", "RRID"),
    ("Typed as a cell line", "Type"),
) + tuple((label, label) for label, _keys in _CELLLINE_CHARACTERISTICS)

# How something can point at a cell line. ``input`` is canonical and first: the
# CellCulture consumes the line (``_INPUT_KEYS`` already covers the ``cell_line``
# alias). ``derivesFrom`` catches the cultured Sample that descends from it, and
# ``mentions`` the Study-level ``cell_lines``/``biologicalModels`` alias.
_CELLLINE_LINK_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_INPUT_KEYS, "input"),
    (_ABOUT_KEYS, "about"),
    (_VALUEURL_KEYS, "valueUrl"),
    (_MENTIONS_KEYS, "mentions"),
) + _EXTRA_LINK_RELATIONS


def _is_cellline(node: dict[str, Any], nodes: dict[str, Any]) -> bool:
    """True for a cell-line entity.

    Accepts the canonical ISA-Tox shape (a ``Sample`` with
    ``additionalType: CellLine``), a bare ``CellLine`` type, and a ``Sample``
    whose ``sampleType`` resolves to the shared "cell line" ``DefinedTerm`` —
    the last so a crate that types its line only by term still appears.
    """
    if _types(node) & _CELLLINE_TYPES or _additional_type(node) in _CELLLINE_TYPES:
        return True
    if "Sample" not in _types(node):
        return False
    return any(
        _CELL_LINE_TERM_HINT in (_literal(nodes[ref], _NAME_KEYS) or "").casefold()
        for ref in _refs(node, _SAMPLETYPE_KEYS)
        if ref in nodes
    )


def _cellline_rrid(node: dict[str, Any], nodes: dict[str, Any]) -> str | None:
    """The Cellosaurus accession, from an ``identifier``, a ``url`` or the ``@id``."""
    candidates: list[str] = []
    for key in _IDENTIFIER_KEYS:
        for item in _as_list(node.get(key)):
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                pv = nodes.get(item["@id"], item) if "@id" in item else item
                candidates.extend(
                    v for v in (_literal(pv, _VALUE_KEYS), pv.get("@id")) if isinstance(v, str)
                )
    candidates.extend(
        v for v in (_literal(node, _URL_KEYS), node.get("@id")) if isinstance(v, str)
    )
    for cand in candidates:
        match = _RRID_RE.search(cand)
        if match:
            return match.group(0)
    return None


def _cellline_characteristics(node: dict[str, Any], nodes: dict[str, Any]) -> dict[str, bool]:
    """Which reproducibility characteristics (organ / tissue / passage) are recorded.

    Read from the ISA ``additionalProperty`` PropertyValues the builder mints,
    falling back to a bare literal on the entity so an externally-authored crate
    that never promoted them still scores.
    """
    present: set[str] = set()
    for key in _ADDPROP_KEYS:
        for item in _as_list(node.get(key)):
            if not isinstance(item, dict):
                continue
            pv = nodes.get(item["@id"], item) if "@id" in item else item
            if not isinstance(pv, dict) or _literal(pv, _VALUE_KEYS) is None:
                continue
            name = (_literal(pv, _NAME_KEYS) or "").strip().casefold()
            for label, aliases in _CELLLINE_CHARACTERISTICS:
                if name in aliases:
                    present.add(label)
    for label, aliases in _CELLLINE_CHARACTERISTICS:
        if label not in present and _literal(node, aliases) is not None:
            present.add(label)
    return {label: label in present for label, _aliases in _CELLLINE_CHARACTERISTICS}


def build_cellline_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's cell lines: their route into the experiment + their identity.

    Resolves, for every cell-line entity, how it is reachable from a
    ``LabProcess`` — canonically the ``CellCulture`` that consumes it as
    ``input``/``cell_line``, or the cultured ``Sample`` that ``derivesFrom`` it —
    and whether it carries a Cellosaurus RRID, an ontology-backed ``sampleType``,
    and the organ / tissue / passage characteristics another lab needs to
    reproduce the culture.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Returns:
        ``{"celllines": [...], "groups": [...], "counts": {...}}``, shaped exactly
        like :func:`build_chemical_inventory` so both feed the same renderer —
        ``state`` is ``"wired"`` / ``"mentioned"`` / ``"unlinked"``.
    """
    nodes = _graph_nodes(metadata)
    line_ids = {nid for nid, n in nodes.items() if _is_cellline(n, nodes)}
    if not line_ids:
        return {"celllines": [], "groups": [], "counts": dict(_EMPTY_ROUTE_COUNTS)}

    resolve = _route_resolver(nodes, line_ids, _CELLLINE_LINK_RELATIONS)

    lines: list[dict[str, Any]] = []
    for lid in sorted(line_ids, key=lambda c: (_name(nodes[c]).casefold(), c)):
        node = nodes[lid]
        rrid = _cellline_rrid(node, nodes)
        typed = bool(
            _additional_type(node) in _CELLLINE_TYPES
            or _types(node) & _CELLLINE_TYPES
            or any(ref in nodes for ref in _refs(node, _SAMPLETYPE_KEYS))
        )
        fields: dict[str, bool] = {
            "Cellosaurus RRID": rrid is not None,
            "Typed as a cell line": typed,
        }
        fields.update(_cellline_characteristics(node, nodes))
        route = resolve(lid)
        lines.append(
            {
                **_chem_node_brief(lid, node),
                "resolvable": _is_uri(lid),
                "rrid": rrid,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": len(fields),
                "state": _route_state(route),
                "route": route,
            }
        )

    return {
        "celllines": lines,
        "groups": _route_bands(lines, nodes),
        "counts": _route_counts(lines),
    }


# --- chemicals diagram -----------------------------------------------------
# Reuses the derivation chain's node geometry and shapes (see _SVG_NODE_W/H and
# _svg_node_shape) so the two diagrams in the maturity report read as one system;
# only the row pitch is tighter, because a compound band is a list rather than a
# chain.
_CHEM_COL_DX = 182
_CHEM_ROW_DY = 66
_CHEM_X0 = 16
_CHEM_Y0 = 24
_CHEM_BAND_GAP = 26
# Length of the dashed "route stops here" stub. Must leave the ✗ glyph inside the
# inter-column gap (_CHEM_COL_DX - _SVG_NODE_W = 44px), or a stub drawn next to a
# populated column lands on top of that column's node.
_CHEM_BREAK_DX = 30
# Every compound in a band is drawn by name (see _band_nodes). The former
# 3-named-plus-aggregate cap is gone: it hid exactly the detail the band exists
# to show.

# Arrowhead element ids — one per diagram (see _svg_link).
_CHEM_MARKER = "chem-ar-link"
_CELLLINE_MARKER = "cell-ar-link"
_ISA_MARKER = "isa-ar-link"
_PEOPLE_MARKER = "people-ar-link"


def _band_nodes(members: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """The entries drawn for one band: **every** member, named.

    This used to draw a head of :data:`_CHEM_MAX_NAMED` and fold the tail into a
    single "and N more" node, on the reasoning that the diagram answers *how do
    these connect* and a verbatim list is the matrix's job. In practice the
    aggregate hid the thing the picture is for: a crate with 22 compounds
    rendered as "3 named + 19 more", so which compounds were unwired — all of
    them, as it turned out — could not be read off the diagram at all. The band
    grows a row per compound; the SVG canvas is sized from the accumulated band
    height, so it scales with the list.
    """
    return list(members)


def _svg_place(
    out: list[str], brief: dict[str, str], cls: str, x: int, y: int, variant: str = ""
) -> None:
    """Append one drawn node: shape, type tag above it, ellipsised name inside.

    ``brief`` carries RAW crate text (see :func:`_chem_node_brief`); it is
    ellipsised first and escaped after, so truncation can never cut a character
    reference in half (#169).
    """
    cx = x + _SVG_NODE_W // 2
    tag_cls = _SVG_CLASS[cls][1]
    vcls = f" {variant}" if variant else ""
    out.append(
        f"<g><title>{_escape(brief['name'])} — {_escape(brief['tag'])}</title>"
        f"{_svg_node_shape(cls, x, y, variant)}"
        f'<text class="tag {tag_cls}{vcls}" x="{cx}" y="{y - 6}">'
        f"{_escape(_svg_trunc(brief['tag'], 22).upper())}</text>"
        f'<text class="name" x="{cx}" y="{y + 28}">'
        f"{_escape(_svg_trunc(brief['name']))}</text></g>"
    )


def _svg_link(
    out: list[str], x1: int, y1: int, x2: int, y2: int, label: str = "", *, marker: str
) -> None:
    """Append a labelled bezier edge between two node edges.

    ``marker`` is the arrowhead's element id, which every diagram must namespace
    for itself: several of these SVGs share one HTML document, ``url(#…)``
    resolves to the first matching id in that document, and the panels that hold
    them are ``display:none`` until their tab is selected — so a shared id points
    the arrowheads of one diagram at a marker inside a hidden subtree.
    """
    dx = (x2 - x1) // 2 if y1 == y2 else max((x2 - x1) // 2, 30)
    out.append(
        f'<path class="e e-link" d="M{x1},{y1} C{x1 + dx},{y1} {x2 - dx},{y2} {x2},{y2}" '
        f'marker-end="url(#{marker})"/>'
    )
    if label:
        out.append(
            f'<text class="elabel" x="{(x1 + x2) // 2}" y="{(y1 + y2) // 2 - 5}">'
            f"{_escape(label)}</text>"
        )


def _svg_break(out: list[str], x_end: int, y: int, *, leading: bool = True) -> None:
    """Append the dashed "the route stops here" stub ending in ✗.

    Drawn rather than omitted: an absent edge and an absent *hop* look identical
    once a diagram simply leaves things out, and the whole point of these views is
    to make a missing link legible.
    """
    if leading:
        out.append(
            f'<path class="e e-break" d="M{x_end - _CHEM_BREAK_DX},{y} H{x_end}"/>'
            f'<text class="brk" x="{x_end - _CHEM_BREAK_DX - 3}" y="{y + 4}">✗</text>'
        )
    else:
        out.append(
            f'<path class="e e-break" d="M{x_end},{y} H{x_end + _CHEM_BREAK_DX}"/>'
            f'<text class="brk start" x="{x_end + _CHEM_BREAK_DX + 3}" y="{y + 4}">✗</text>'
        )


def _svg_document(
    body_edges: list[str],
    body_nodes: list[str],
    w: int,
    h: int,
    aria: str,
    *,
    marker: str,
) -> str:
    """Wrap drawn edges/nodes in the finished, self-contained ``<svg>`` element."""
    return (
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-label="{_escape(aria)}" class="prov view">'
        f"<title>{_escape(aria)}</title>"
        f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="mk-link"/></marker></defs>'
        f'<g class="edges">{"".join(body_edges)}</g>'
        f'<g class="nodes">{"".join(body_nodes)}</g></svg>'
    )


def render_chemicals_svg(inventory: dict[str, Any]) -> str:
    """Draw the compound routes as a self-contained inline ``<svg>``.

    One band per route: ``process --result--> table --about--> compound``, with
    the compounds of that route stacked on the right. A band whose compounds are
    only *mentioned* loses its process column, and an unlinked band loses both —
    in each case the missing hop is drawn as a dashed stub ending in ✗ rather
    than silently omitted, so "described but unreachable" looks different from
    "properly wired" at a glance.

    Like :func:`render_provenance_svg` this emits finished SVG — no script, no
    external assets — so it embeds in the offline maturity report and prints
    as-is. All labels arrive pre-escaped from :func:`build_chemical_inventory`.

    Args:
        inventory: The result of :func:`build_chemical_inventory`.

    Returns:
        The ``<svg>…</svg>`` markup, or ``""`` when the crate declares no
        compounds.
    """
    return _render_routed_svg(
        inventory,
        node_cls="chem",
        more_tag="MolecularEntity",
        noun="compounds",
        marker=_CHEM_MARKER,
    )


def render_celllines_svg(inventory: dict[str, Any]) -> str:
    """Draw the cell-line routes as a self-contained inline ``<svg>``.

    Same bands as the compound view, because the failure is the same: the
    canonical route is ``CellCulture --input--> cell line`` (or the cultured
    ``Sample --derivesFrom--> cell line``), and when the culture consumes a
    freshly minted generic Sample instead, the declared line is left with no
    inbound edge at all.

    Args:
        inventory: The result of :func:`build_cellline_inventory`.

    Returns:
        The ``<svg>…</svg>`` markup, or ``""`` when the crate declares no lines.
    """
    return _render_routed_svg(
        inventory,
        # A cell line IS a Sample, so it reuses the derivation chain's material
        # stadium — the same entity keeps the same shape across every view.
        node_cls="mat",
        more_tag="Sample · CellLine",
        noun="cell lines",
        marker=_CELLLINE_MARKER,
    )


def _render_routed_svg(
    inventory: dict[str, Any],
    *,
    node_cls: str,
    more_tag: str,
    noun: str,
    marker: str,
) -> str:
    """Draw a routed inventory's bands: ``process → via → members``.

    Shared by the compound and cell-line views — both answer "can a reader get
    from a process to this entity", and drawing them identically is the point:
    a break in one reads exactly like a break in the other.
    """
    groups = inventory.get("groups") or []
    if not groups:
        return ""

    def _hops(group: dict[str, Any]) -> list[dict[str, str]]:
        """The band's left-hand nodes, rightmost last.

        Two hops for the indirect route a compound takes
        (``process --result--> table --about--> compound``); one when a process
        references the member itself (a ``CellCulture`` consuming its cell line)
        — drawing that process in both columns with a ``result`` edge between
        them would depict a step the crate does not contain.
        """
        process, via = group["process"], group["via"]
        if via is None:
            return []
        if process is not None and process["id"] != via["id"]:
            return [process, via]
        return [via]

    # Drop the leading columns no band uses, so a crate whose members are all
    # unlinked doesn't render a diagram that is mostly empty gutter. The reserved
    # pad keeps room for the dashed ✗ stub once a left column is gone.
    widest = max((len(_hops(g)) for g in groups), default=0)
    first_col = 2 - widest
    x0 = _CHEM_X0 + (_CHEM_BREAK_DX + 10 if first_col else 0)
    col_x = [x0 + (i - first_col) * _CHEM_COL_DX for i in range(3)]

    edges: list[str] = []
    nodes_svg: list[str] = []
    mid = _SVG_NODE_H // 2
    band_y = _CHEM_Y0

    for group in groups:
        members = group["members"]
        drawn = _band_nodes(members)
        span = (len(drawn) - 1) * _CHEM_ROW_DY
        centre_y = band_y + span // 2
        variant = "" if group["state"] == "wired" else "unwired"

        for i, member in enumerate(drawn):
            y = band_y + i * _CHEM_ROW_DY
            # ``None`` (the old "and N more" aggregate) is no longer produced by
            # _band_nodes; the branch stays so a caller that reintroduces a cap
            # still renders rather than crashing on a missing key.
            brief = (
                {"id": "", "name": "more", "tag": more_tag}
                if member is None
                else {"id": member["id"], "name": member["name"], "tag": member["tag"]}
            )
            _svg_place(nodes_svg, brief, node_cls, col_x[2], y, variant)
            if group["via"] is not None:
                _svg_link(
                    edges,
                    col_x[1] + _SVG_NODE_W,
                    centre_y + mid,
                    col_x[2],
                    y + mid,
                    group["edge"] if i == 0 else "",
                    marker=marker,
                )
            else:  # nothing in the crate points at these entities at all
                _svg_break(edges, col_x[2], y + mid)

        hops = _hops(group)
        for offset, hop in enumerate(reversed(hops)):  # rightmost hop first
            col = 1 - offset
            _svg_place(nodes_svg, hop, _node_class_for_brief(hop), col_x[col], centre_y)
            if offset:  # link this hop to the one on its right
                _svg_link(
                    edges, col_x[col] + _SVG_NODE_W, centre_y + mid, col_x[col + 1],
                    centre_y + mid, "result", marker=marker,
                )
        if hops and group["process"] is None:
            # Named in the crate, but produced by no process.
            _svg_break(edges, col_x[2 - len(hops)], centre_y + mid)

        band_y += span + _SVG_NODE_H + _CHEM_BAND_GAP

    counts = inventory.get("counts", {})
    return _svg_document(
        edges,
        nodes_svg,
        col_x[2] + _SVG_NODE_W + 16,
        band_y - _CHEM_BAND_GAP + 18,
        f"Routes: {counts.get('wired', 0)} of {counts.get('total', 0)} {noun} "
        "reachable from a process",
        marker=marker,
    )


# ---------------------------------------------------------------------------
# People & organisations (#85) — who the crate credits, and whether that credit
# is machine-actionable.
#
# Attribution is the part of a crate humans read and machines usually cannot.
# A name string credits nobody a registry can resolve: without an ORCID the
# author is unfindable, without a ROR the institution is unfindable, and without
# an ``affiliation`` edge the two are never connected. The failure is quiet —
# every profile still passes, because a bare `name` satisfies the shapes.
#
# The pattern also produces a specific, common defect this view is built to
# expose: the same institution appearing twice, once as a resolvable ROR node and
# once as a locally-minted `#Organization_…` node that nothing references. The
# duplicate is invisible in a list of names and obvious in a picture where one
# box has inbound edges and the other has none.
# ---------------------------------------------------------------------------

_PERSON_TYPES = frozenset({"Person"})
_ORG_TYPES = frozenset(
    {
        "Organization",
        "ResearchOrganization",
        "EducationalOrganization",
        "GovernmentOrganization",
        "Corporation",
        "NGO",
        "Consortium",
        "Project",
        "FundingAgency",
    }
)

# Persistent-identifier schemes for agents, resolved the same way compounds are
# (PropertyValue name / propertyID host / the entity's own @id).
_AGENT_ID_SCHEMES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...], "re.Pattern[str] | None"], ...
] = (
    (
        "ORCID",
        "ORCID",
        ("orcid", "orcid id", "orcid.org"),
        ("orcid.org/",),
        re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]"),
    ),
    ("ROR", "ROR", ("ror", "ror id"), ("ror.org/",), re.compile(r"0[0-9a-z]{6}[0-9]{2}")),
    ("ISNI", "ISNI", ("isni",), ("isni.org/isni/",), re.compile(r"[\d ]{15,20}[\dXx]")),
    ("GRID", "GRID", ("grid", "grid id"), ("grid.ac/institutes/",), re.compile(r"grid\.\d+\.\w+")),
)
# Which of those count as "identified" for each kind, most preferred first.
_PID_FOR_KIND: dict[str, tuple[str, ...]] = {
    "person": ("ORCID", "ISNI"),
    "org": ("ROR", "GRID", "ISNI"),
}

# Ways an entity can credit an agent, most canonical first.
_CREDIT_RELATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_AUTHOR_KEYS, "author"),
    (("contributor", "http://schema.org/contributor"), "contributor"),
    (("publisher", "http://schema.org/publisher"), "publisher"),
    (("funder", "http://schema.org/funder"), "funder"),
    (("maintainer", "http://schema.org/maintainer"), "maintainer"),
    (("sponsor", "provider", "sourceOrganization"), "provider"),
)

_NAMEPART_KEYS: tuple[str, ...] = (
    "givenName",
    "familyName",
    "http://schema.org/givenName",
    "http://schema.org/familyName",
)

# The agent matrix columns: (full name, header). ``Affiliation`` is n/a for an
# organisation and scored as such — never as a miss.
AGENT_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Persistent identifier", "PID"),
    ("Name", "Name"),
    ("Affiliation", "Affiliation"),
    # "Linked", not "Credited": an organisation is normally reached through a
    # person's affiliation rather than credited directly, and that is a correct
    # crate — the column asks whether anything connects the agent at all.
    ("Linked in crate", "Linked"),
)


def _agent_kind(node: dict[str, Any]) -> str | None:
    """``"person"`` / ``"org"`` for an agent node, else ``None``."""
    types = _types(node)
    if types & _PERSON_TYPES:
        return "person"
    if types & _ORG_TYPES:
        return "org"
    return None


def build_people_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's attribution: who is credited, by what, and how resolvably.

    Resolves, for every ``Person`` and ``Organization``: the persistent
    identifier it carries (ORCID / ROR / ISNI / GRID, from an ``identifier``
    PropertyValue or from the entity's own ``@id``), whether a person declares an
    ``affiliation``, and which entity credits it (``author`` / ``contributor`` /
    ``publisher`` / ``funder`` / …). The Root Data Entity is preferred as a
    person's credit source when several apply, so the common case — the crate
    itself crediting its authors — is the one the diagram draws.

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Args:
        metadata: Parsed ``ro-crate-metadata.json`` dict, the ``@graph`` list, or
            the ``crate.metadata.generate()`` document.

    Returns:
        ``{"agents": [...], "groups": [...], "counts": {...}}``.

        Each agent: ``{id, name, label, tag, kind, pid, pid_scheme, resolvable,
        affiliations, fields, met, total, state}`` where ``state`` is
        ``"credited"`` (something in the crate credits it), ``"affiliated"`` (an
        organisation reached only through a person's affiliation) or
        ``"unattached"`` (nothing references it — the duplicate-institution
        signature).

        ``groups`` are the diagram's bands: a credit source, the people it
        credits, and those people's organisations.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = _graph_nodes(metadata)
    kinds = {nid: _agent_kind(n) for nid, n in nodes.items()}
    agent_ids = {nid for nid, kind in kinds.items() if kind}
    empty_counts = {
        "people": 0,
        "orgs": 0,
        "total": 0,
        "credited": 0,
        "unattached": 0,
        "pid_backed": 0,
        "fields_met": 0,
        "fields_total": 0,
    }
    if not agent_ids:
        return {"agents": [], "groups": [], "counts": empty_counts}

    root_id = _find_root_id(nodes, [n for n in graph if isinstance(n, dict)])

    # Who credits whom. An agent crediting another agent (a ScholarlyArticle's
    # author list is fine, but Person --author--> Person is not attribution)
    # is kept: the source is drawn, and a self-reference is skipped.
    rel_order = {label: i for i, (_keys, label) in enumerate(_CREDIT_RELATIONS)}
    credits: dict[str, set[tuple[str, str]]] = {}
    for nid, node in nodes.items():
        if str(nid).rsplit("/", 1)[-1] in _EXCLUDED_IDS:
            continue
        for keys, label in _CREDIT_RELATIONS:
            for ref in _refs(node, keys):
                if ref in agent_ids and ref != nid:
                    credits.setdefault(ref, set()).add((nid, label))

    # Person → organisation(s), and the inverse. EVERY affiliation is recorded,
    # not just the first: a co-affiliated researcher is ordinary, and stopping at
    # one leaves the second institution referenced by nothing — which this view
    # then reports as an unattached duplicate and advises the reader to delete.
    affiliation: dict[str, list[str]] = {}
    affiliates: dict[str, list[str]] = {}
    for nid in sorted(agent_ids):
        if kinds[nid] != "person":
            continue
        orgs = [ref for ref in _refs(nodes[nid], _AFFILIATION_KEYS) if kinds.get(ref) == "org"]
        if orgs:
            affiliation[nid] = orgs
            for ref in orgs:
                affiliates.setdefault(ref, []).append(nid)

    def _credit_source(aid: str) -> tuple[str, str] | None:
        """Best credit source: the crate root when it credits this agent (that is
        the attribution a reader is looking for), else the most canonical other."""
        found = credits.get(aid)
        if not found:
            return None
        rooted = [p for p in found if p[0] == root_id]
        return sorted(rooted or found, key=lambda p: (rel_order[p[1]], p[0]))[0]

    # The @id tiebreak is load-bearing: `agent_ids` is a SET, so two agents with
    # the same display name would otherwise be ordered by the per-process string
    # hash seed — and same-named agents are exactly the duplicate-entity case
    # this view exists to surface. The embedded artifact must be byte-stable.
    def _agent_order(aid: str) -> tuple[bool, str, str]:
        return (kinds[aid] != "person", _name(nodes[aid]).casefold(), aid)

    agents: list[dict[str, Any]] = []
    for aid in sorted(agent_ids, key=_agent_order):
        node = nodes[aid]
        kind = kinds[aid]
        assert kind is not None
        ids = _registry_identifiers(node, nodes, _AGENT_ID_SCHEMES)
        pid_scheme = next((s for s in _PID_FOR_KIND[kind] if s in ids), None)
        source = _credit_source(aid)
        if source is not None:
            state = "credited"
        elif kind == "org" and affiliates.get(aid):
            state = "affiliated"
        else:
            state = "unattached"
        fields: dict[str, bool | None] = {
            "Persistent identifier": pid_scheme is not None,
            "Name": _literal(node, _NAME_KEYS) is not None,
            # An organisation has no affiliation of its own — n/a, not a miss.
            "Affiliation": (aid in affiliation) if kind == "person" else None,
            "Linked in crate": state != "unattached",
        }
        agents.append(
            {
                **_chem_node_brief(aid, node),
                "kind": kind,
                "pid": ids.get(pid_scheme) if pid_scheme else None,
                "pid_scheme": pid_scheme,
                "identifiers": ids,
                "resolvable": _is_uri(aid),
                "affiliations": affiliation.get(aid, []),
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": sum(1 for ok in fields.values() if ok is not None),
                "state": state,
                "source": source[0] if source else None,
                "edge": source[1] if source else "",
            }
        )
    by_id = {a["id"]: a for a in agents}

    # Bands: one per credit source, holding the people it credits plus those
    # people's organisations. Organisations credited directly (a publisher or
    # funder) join their crediting source's band with no person in between;
    # everything nothing references falls into a trailing unattached band.
    banded: dict[tuple[str | None, str], dict[str, list[dict[str, Any]]]] = {}
    for agent in agents:
        if agent["state"] == "unattached":
            continue
        if agent["kind"] == "person":
            key = (agent["source"], agent["edge"])
            banded.setdefault(key, {"persons": [], "orgs": []})["persons"].append(agent)
        elif agent["state"] == "credited":
            key = (agent["source"], agent["edge"])
            banded.setdefault(key, {"persons": [], "orgs": []})["orgs"].append(agent)
    # Affiliation organisations ride in the band(s) of the people that name them.
    for key, band in banded.items():
        seen = {o["id"] for o in band["orgs"]}
        for person in band["persons"]:
            for org_id in person["affiliations"]:
                if org_id not in seen and org_id in by_id:
                    seen.add(org_id)
                    band["orgs"].append(by_id[org_id])

    groups = [
        {
            "source": _chem_node_brief(src, nodes[src]) if src else None,
            "edge": edge,
            "persons": band["persons"],
            "orgs": band["orgs"],
            "state": "credited",
        }
        for (src, edge), band in sorted(
            banded.items(), key=lambda kv: (kv[0][0] != root_id, kv[0][0] or "", kv[0][1])
        )
    ]
    # Anything no band picked up — the unattached, plus an organisation whose only
    # affiliate is itself unattached (counted and listed, but previously drawn
    # nowhere). The trailing band mixes states, so the diagram marks each node
    # from its OWN state rather than the band's.
    placed = {a["id"] for band in banded.values() for a in band["persons"] + band["orgs"]}
    loose = [a for a in agents if a["id"] not in placed]
    if loose:
        groups.append(
            {
                "source": None,
                "edge": "",
                "persons": [a for a in loose if a["kind"] == "person"],
                "orgs": [a for a in loose if a["kind"] == "org"],
                "state": "unattached",
            }
        )

    counts = {
        "people": sum(1 for a in agents if a["kind"] == "person"),
        "orgs": sum(1 for a in agents if a["kind"] == "org"),
        "total": len(agents),
        "credited": sum(1 for a in agents if a["state"] != "unattached"),
        "unattached": sum(1 for a in agents if a["state"] == "unattached"),
        "pid_backed": sum(1 for a in agents if a["pid_scheme"]),
        "fields_met": sum(a["met"] for a in agents),
        "fields_total": sum(a["total"] for a in agents),
    }
    return {"agents": agents, "groups": groups, "counts": counts}


def render_people_svg(inventory: dict[str, Any]) -> str:
    """Draw the attribution chain as a self-contained inline ``<svg>``.

    One band per credit source: ``crate root --author--> Person --affiliation-->
    Organization``. A person who declares no affiliation gets a dashed ✗ stub
    where the institution should be; an agent nothing references at all sits in a
    trailing band with the stub on its left. That trailing band is where a
    duplicated institution shows up — the ROR-backed copy carries edges, the
    locally-minted one carries none.

    Args:
        inventory: The result of :func:`build_people_inventory`.

    Returns:
        The ``<svg>…</svg>`` markup, or ``""`` when the crate credits nobody.
    """
    groups = inventory.get("groups") or []
    if not groups:
        return ""

    has_source = any(g["source"] for g in groups)
    first_col = 0 if has_source else 1
    x0 = _CHEM_X0 + (_CHEM_BREAK_DX + 10 if first_col else 0)
    col_x = [x0 + (i - first_col) * _CHEM_COL_DX for i in range(3)]

    edges: list[str] = []
    nodes_svg: list[str] = []
    mid = _SVG_NODE_H // 2
    band_y = _CHEM_Y0

    def _row_y(row: int) -> int:
        return band_y + row * _CHEM_ROW_DY

    for group in groups:
        # Every agent is drawn — no "+N more" aggregate. This view exists so a
        # person can CHECK the attribution metadata entity by entity, and an
        # elided tail is exactly where a duplicated institution or a
        # missing-ORCID author would hide.
        drawn_people = group["persons"]
        drawn_orgs = group["orgs"]
        unattached = group["state"] == "unattached"
        person_rows = {p["id"]: i for i, p in enumerate(drawn_people)}

        # An organisation takes the row of its first affiliate so the affiliation
        # edge stays horizontal; collisions step down to the next free row.
        org_rows: dict[str, int] = {}
        used: set[int] = set()
        for org in drawn_orgs:
            wanted = [
                person_rows[p["id"]] for p in drawn_people if org["id"] in p["affiliations"]
            ]
            row = min(wanted) if wanted else 0
            while row in used:
                row += 1
            org_rows[org["id"]] = row
            used.add(row)

        rows = max(len(drawn_people), max(used, default=-1) + 1, 1)
        centre = _row_y(0) + ((rows - 1) * _CHEM_ROW_DY) // 2

        if group["source"] is not None:
            _svg_place(
                nodes_svg,
                group["source"],
                _node_class_for_brief(group["source"]),
                col_x[0],
                centre,
            )

        for i, person in enumerate(drawn_people):
            y = _row_y(i)
            brief = {"id": person["id"], "name": person["name"], "tag": person["tag"]}
            loose_person = person["state"] == "unattached"
            _svg_place(
                nodes_svg, brief, "agent", col_x[1], y, "unwired" if loose_person else ""
            )
            if group["source"] is not None:
                _svg_link(
                    edges,
                    col_x[0] + _SVG_NODE_W,
                    centre + mid,
                    col_x[1],
                    y + mid,
                    group["edge"] if i == 0 else "",
                    marker=_PEOPLE_MARKER,
                )
            elif loose_person:
                _svg_break(edges, col_x[1], y + mid)
            linked = [o for o in person["affiliations"] if o in org_rows]
            for j, org_id in enumerate(linked):
                _svg_link(
                    edges,
                    col_x[1] + _SVG_NODE_W,
                    y + mid,
                    col_x[2],
                    _row_y(org_rows[org_id]) + mid,
                    "affiliation" if i == 0 and j == 0 else "",
                    marker=_PEOPLE_MARKER,
                )
            if not linked and not loose_person:
                # Credited, but the institution behind the person is missing.
                _svg_break(edges, col_x[1] + _SVG_NODE_W, y + mid, leading=False)

        for org in drawn_orgs:
            y = _row_y(org_rows[org["id"]])
            brief = {"id": org["id"], "name": org["name"], "tag": org["tag"]}
            loose_org = org["state"] == "unattached"
            _svg_place(nodes_svg, brief, "org", col_x[2], y, "unwired" if loose_org else "")
            if loose_org:
                _svg_break(edges, col_x[2], y + mid)
            elif not unattached and group["source"] is not None and not any(
                org["id"] in p["affiliations"] for p in drawn_people
            ):
                # Credited straight from the source (a publisher or a funder),
                # with no person in between.
                _svg_link(
                    edges, col_x[0] + _SVG_NODE_W, centre + mid, col_x[2], y + mid,
                    group["edge"], marker=_PEOPLE_MARKER,
                )

        band_y += (rows - 1) * _CHEM_ROW_DY + _SVG_NODE_H + _CHEM_BAND_GAP

    counts = inventory.get("counts", {})
    return _svg_document(
        edges,
        nodes_svg,
        col_x[2] + _SVG_NODE_W + 16,
        band_y - _CHEM_BAND_GAP + 18,
        f"Attribution: {counts.get('pid_backed', 0)} of {counts.get('total', 0)} agents "
        "carry a persistent identifier",
        marker=_PEOPLE_MARKER,
    )


# ---------------------------------------------------------------------------
# ISA structure (#85) — the Investigation / Study / Assay backbone.
#
# ISA is the skeleton every other view hangs off: the Investigation states what
# was asked, each Study a coherent set of work toward it, each Assay one
# measurement campaign whose LabProcesses the other views trace. The skeleton is
# expressed purely as ``hasPart`` between Datasets that differ only by
# ``additionalType`` — so it is invisible in the JSON and easy to break in ways
# that still validate: a Study nobody lists as a part (present in the crate,
# outside the hierarchy), an Assay with no process attached to it, or a level
# with no ``identifier``, which is what makes an ISA node citable at all.
# ---------------------------------------------------------------------------

_ISA_LEVELS: tuple[str, ...] = ("Investigation", "Study", "Assay")
_ISA_CHILD_LEVEL = {"Investigation": "Study", "Study": "Assay"}
_DESCRIPTION_KEYS: tuple[str, ...] = (
    "description",
    "http://schema.org/description",
    "https://schema.org/description",
)

ISA_COVERAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Identifier", "Identifier"),
    ("Description", "Description"),
    ("Listed by its parent", "In parent"),
    ("Contains the next level", "Contains"),
)


def _isa_level(node: dict[str, Any], is_root: bool) -> str | None:
    """``Investigation`` / ``Study`` / ``Assay`` for an ISA container, else None.

    The Root Data Entity is the Investigation whether or not it says so — a crate
    whose root omits ``additionalType`` is still an investigation-level record,
    and reporting it as "not an ISA node" would hide the whole hierarchy.
    """
    add = _additional_type(node)
    if add in _ISA_LEVELS:
        return add
    return "Investigation" if is_root and "Dataset" in _types(node) else None


def build_isa_inventory(
    metadata: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Model the crate's ISA backbone: Investigation → Study → Assay.

    Resolves each container's level, the parent that lists it under ``hasPart``,
    the children it lists in turn, and — for an Assay — the ``LabProcess``es it is
    ``about``. Reports per node whether it carries an ``identifier`` and a
    ``description``, whether a parent lists it, and whether it contains the level
    below (an Assay's "level below" is its processes).

    Pure and cheap: one pass over the serialized ``@graph``, no validation and no
    network. Crate-controlled text is HTML-escaped in ``label`` (#169).

    Returns:
        ``{"nodes": [...], "counts": {...}}``. Each node carries ``level``,
        ``parent``, ``children``, ``processes``, ``fields``, ``met``, ``total``
        and ``state`` — ``"linked"`` when a parent lists it (or it is the
        Investigation), ``"detached"`` when nothing does.
    """
    graph = metadata.get("@graph", []) if isinstance(metadata, dict) else metadata
    nodes = _graph_nodes(metadata)
    root_id = _find_root_id(nodes, [n for n in graph if isinstance(n, dict)])
    levels = {
        nid: lvl
        for nid, n in nodes.items()
        if (lvl := _isa_level(n, nid == root_id)) is not None
    }
    if not levels:
        return {
            "nodes": [],
            "counts": {
                "total": 0,
                "investigations": 0,
                "studies": 0,
                "assays": 0,
                "processes": 0,
                "detached": 0,
                "fields_met": 0,
                "fields_total": 0,
            },
        }

    # hasPart, restricted to ISA containers — the parent/child skeleton. A file
    # part is not a structural child and must not make an Assay look populated.
    children: dict[str, list[str]] = {}
    parent: dict[str, str] = {}
    for nid in sorted(levels):
        kids = [ref for ref in _refs(nodes[nid], _HASPART_KEYS) if ref in levels]
        if kids:
            children[nid] = kids
        for kid in kids:
            parent.setdefault(kid, nid)

    processes: dict[str, list[str]] = {}
    for nid in sorted(levels):
        if levels[nid] != "Assay":
            continue
        procs = [
            ref
            for ref in _refs(nodes[nid], _ABOUT_GRAPH_KEYS)
            if ref in nodes and _is_process(nodes[ref])
        ]
        if procs:
            processes[nid] = procs

    def _order(nid: str) -> tuple[int, str, str]:
        return (_ISA_LEVELS.index(levels[nid]), _name(nodes[nid]).casefold(), nid)

    out: list[dict[str, Any]] = []
    for nid in sorted(levels, key=_order):
        node = nodes[nid]
        level = levels[nid]
        is_root = nid == root_id
        kids = children.get(nid, [])
        procs = processes.get(nid, [])
        contains = bool(procs) if level == "Assay" else bool(kids)
        fields: dict[str, bool | None] = {
            "Identifier": _literal(node, _IDENTIFIER_KEYS) is not None
            or bool(_refs(node, _IDENTIFIER_KEYS))
            or _is_uri(nid),
            "Description": _literal(node, _DESCRIPTION_KEYS) is not None,
            # The Investigation is the top of the hierarchy — it has no parent to
            # be listed by, which is not a defect.
            "Listed by its parent": None if level == "Investigation" else nid in parent,
            "Contains the next level": contains,
        }
        out.append(
            {
                **_chem_node_brief(nid, node),
                "level": level,
                "parent": parent.get(nid),
                "children": kids,
                "processes": procs,
                "fields": fields,
                "met": sum(1 for ok in fields.values() if ok),
                "total": sum(1 for ok in fields.values() if ok is not None),
                "state": "linked" if (is_root or nid in parent) else "detached",
            }
        )

    counts = {
        "total": len(out),
        "investigations": sum(1 for n in out if n["level"] == "Investigation"),
        "studies": sum(1 for n in out if n["level"] == "Study"),
        "assays": sum(1 for n in out if n["level"] == "Assay"),
        "processes": len({p for n in out for p in n["processes"]}),
        "detached": sum(1 for n in out if n["state"] == "detached"),
        "fields_met": sum(n["met"] for n in out),
        "fields_total": sum(n["total"] for n in out),
    }
    return {"nodes": out, "counts": counts}


def render_isa_svg(inventory: dict[str, Any]) -> str:
    """Draw the ISA backbone as a self-contained inline ``<svg>``.

    One column per level (Investigation → Study → Assay), each node on its own
    row so nothing is elided, with ``hasPart`` edges between them. A container no
    parent lists gets the dashed ✗ stub on its left — present in the crate but
    outside the hierarchy, the failure that still validates.

    An Assay's LabProcess count rides in its type tag rather than as a fourth
    column: the processes themselves are what the Provenance view draws, and
    repeating them here would make the skeleton unreadable.

    Args:
        inventory: The result of :func:`build_isa_inventory`.

    Returns:
        The ``<svg>…</svg>`` markup, or ``""`` when the crate has no ISA nodes.
    """
    nodes = inventory.get("nodes") or []
    if not nodes:
        return ""
    by_id = {n["id"]: n for n in nodes}

    # Rows: depth-first over the hierarchy so a parent sits with its children,
    # then anything detached, so no node is ever dropped for lack of a parent.
    row_of: dict[str, int] = {}
    order: list[str] = []

    def _walk(nid: str) -> None:
        if nid in row_of:
            return
        row_of[nid] = len(order)
        order.append(nid)
        for kid in by_id[nid]["children"]:
            if kid in by_id:
                _walk(kid)

    for n in nodes:
        if n["level"] == "Investigation":
            _walk(n["id"])
    for n in nodes:
        _walk(n["id"])

    x0 = _CHEM_X0 + _CHEM_BREAK_DX + 10
    col_x = [x0 + i * _CHEM_COL_DX for i in range(3)]
    mid = _SVG_NODE_H // 2
    edges: list[str] = []
    nodes_svg: list[str] = []

    def _y(nid: str) -> int:
        return _CHEM_Y0 + row_of[nid] * _CHEM_ROW_DY

    for n in nodes:
        col = _ISA_LEVELS.index(n["level"])
        y = _y(n["id"])
        variant = "unwired" if n["state"] == "detached" else ""
        tag = n["tag"]
        if n["level"] == "Assay":
            tag = f"{tag} · {len(n['processes'])} proc"
        _svg_place(
            nodes_svg,
            {"id": n["id"], "name": n["name"], "tag": tag},
            "container",
            col_x[col],
            y,
            variant,
        )
        if n["state"] == "detached":
            _svg_break(edges, col_x[col], y + mid)
        for j, kid in enumerate(n["children"]):
            if kid not in by_id:
                continue
            _svg_link(
                edges,
                col_x[col] + _SVG_NODE_W,
                y + mid,
                col_x[_ISA_LEVELS.index(by_id[kid]["level"])],
                _y(kid) + mid,
                "hasPart" if col == 0 and j == 0 else "",
                marker=_ISA_MARKER,
            )

    counts = inventory.get("counts", {})
    return _svg_document(
        edges,
        nodes_svg,
        col_x[2] + _SVG_NODE_W + 16,
        _CHEM_Y0 + (len(order) - 1) * _CHEM_ROW_DY + _SVG_NODE_H + 18,
        f"ISA structure: {counts.get('investigations', 0)} investigation, "
        f"{counts.get('studies', 0)} studies, {counts.get('assays', 0)} assays",
        marker=_ISA_MARKER,
    )


def _node_class_for_brief(brief: dict[str, str]) -> str:
    """Style bucket for a drawn route hop, from the type tag already computed.

    The tag is the only thing the band carries (the node itself is not re-read),
    and it is enough: a condition table tags as ``Table``/``File``, an
    Investigation/Study/Assay as ``Dataset``.
    """
    tag = brief.get("tag", "")
    head = tag.split(" · ", 1)[0]
    if head in _PROCESS_DISCRIMINATORS or head == "LabProcess":
        return "proc"
    # An agent can also be a credit source (a ScholarlyArticle's authors, an
    # organisation that funds another). Keep its own shape, or the same entity
    # renders with two different outlines in one diagram.
    if head == "Person":
        return "agent"
    if head in _ORG_TYPES:
        return "org"
    if head in ("Table", "File", "MediaObject"):
        return "data"
    if head == "Sample":
        return "mat"
    return "ctx"


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
        "  classDef external fill:#f8fafc,stroke:#64748b,color:#334155,stroke-dasharray:4 3;"
    )
    lines.append("  classDef dangling fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d,stroke-width:2px;")
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


def render_provenance_mermaid_from_file(path: str | Path, **kwargs: Any) -> str:
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
    parser.add_argument("--fenced", action="store_true", help="Wrap output in a ```mermaid block")
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
