"""Build a self-contained, searchable entity explorer for an RO-Crate.

Reuses the crate builder's own category registry (builder/writers/provenance_dag.py)
so colours, shapes and category names match the maturity report exactly.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import provenance_dag as pdag  # noqa: E402

CRATE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ro-crate-entities.html")

meta = json.loads((CRATE / "ro-crate-metadata.json").read_text())
graph = meta["@graph"]
raw_by_id = {e["@id"]: e for e in graph if "@id" in e}

model = pdag.build_crate_graph(meta, layer="all", all_edges=True)

# --- shapes: one glyph per category, mirroring the Mermaid delimiters --------
# Secondary encoding. The palette is a constant-lightness ring, which does not
# survive CVD simulation, so category identity never rests on colour alone.
SHAPES = {
    "container": "M2 3h10v8H2z M4 3v8 M10 3v8",          # [[ ]] double-walled
    "process": "M4 2h6l3 5-3 5H4L1 7z",                    # {{ }} hexagon
    "protocol": "M4 3h9l-3 8H1z",                          # [/ \] parallelogram
    "material": "M5 3h4a4 4 0 010 8H5a4 4 0 010-8z",       # ([ ]) stadium
    "chemical": "M7 2.5a4.5 4.5 0 110 9 4.5 4.5 0 010-9z",  # ( ) circle
    "data": "M2 4c0-1.1 2.2-2 5-2s5 .9 5 2v6c0 1.1-2.2 2-5 2s-5-.9-5-2z M2 4c0 1.1 2.2 2 5 2s5-.9 5-2",
    "agent": "M7 1.5a5.5 5.5 0 110 11 5.5 5.5 0 010-11z M7 4a3 3 0 110 6 3 3 0 010-6z",
    "org": "M2 3h10v8H2z",                                  # [ ] rect
    "publication": "M1 3h12l-2 8H3z",                       # [\ /] trapezoid
    "annotation": "M1 3h9l3 4-3 4H1z",                      # > ] pennant
    "ctx": "M2 3h10v8H2z",
}
CTX = pdag._CTX_CATEGORY

CATS: dict[str, dict] = {}
for key, style in pdag.CATEGORY_STYLES.items():
    CATS[key] = {"colour": style.colour, "label": style.label, "shape": SHAPES[key]}
CATS[CTX] = {"colour": "#78807f", "label": "Unclassified", "shape": SHAPES["ctx"]}

LAYERS = {
    1: {"name": "Packaging", "sub": "RO-Crate"},
    2: {"name": "Structural", "sub": "ISA"},
    3: {"name": "Domain", "sub": "ISA-Tox"},
}

# --- edges, both directions, keyed by node ----------------------------------
out_edges: dict[str, list] = {}
in_edges: dict[str, list] = {}
for e in model["edges"]:
    out_edges.setdefault(e["src"], []).append([e["dst"], e["label"]])
    in_edges.setdefault(e["dst"], []).append([e["src"], e["label"]])


def props(nid: str) -> list:
    """Raw properties of an entity, minus @id/@type, values normalised."""
    node = raw_by_id.get(nid)
    if not node:
        return []
    out = []
    for k, v in node.items():
        if k in ("@id", "@type"):
            continue
        vals = v if isinstance(v, list) else [v]
        norm = []
        for item in vals:
            if isinstance(item, dict) and "@id" in item:
                norm.append({"ref": item["@id"]})
            elif isinstance(item, dict) and "@value" in item:
                norm.append({"lit": str(item["@value"])})
            elif isinstance(item, dict):
                norm.append({"lit": json.dumps(item, ensure_ascii=False)})
            else:
                norm.append({"lit": str(item)})
        out.append([k, norm])
    return out


nodes = []
for n in model["nodes"]:
    cat = n["category"] or (CTX if n["status"] == "in_crate" else None)
    nodes.append(
        {
            "id": n["id"],
            "label": html.unescape(n["label"]),
            "type": n["type"],
            "cat": cat,
            "layer": n["layer"],
            "status": n["status"],
            "idb": n["identifier_backed"],
            "orphan": n["orphan"],
            "reach": n["reach"],
            "out": out_edges.get(n["id"], []),
            "inc": in_edges.get(n["id"], []),
            "props": props(n["id"]),
        }
    )

root_id = model["root"]
root = raw_by_id.get(root_id, {})
title = root.get("name") or "RO-Crate"

payload = {
    "title": title,
    "description": root.get("description", ""),
    "root": root_id,
    "crate": CRATE.resolve().name,
    "published": root.get("datePublished", ""),
    "conformsTo": [c.get("@id") for c in (root.get("conformsTo") or []) if isinstance(c, dict)],
    "counts": model["counts"],
    "cats": CATS,
    "layers": {str(k): v for k, v in LAYERS.items()},
    "nodes": nodes,
}

DATA = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

# ---------------------------------------------------------------------------
CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --page:#eef1f2; --surface:#fff; --surface-2:#f5f8f8; --ink:#111a1c; --ink-2:#46565a;
  --muted:#6d7b7e; --hairline:#e2e8e8; --border:rgba(17,26,28,.09); --accent:#0e7c86;
  --accent-soft:#e4f1f2; --low:#b3261e; --low-soft:#fbeceb; --track:#e6ebeb;
  color-scheme:light;
}
html,body{margin:0;height:100%}
body{background:var(--page);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  line-height:1.5;-webkit-font-smoothing:antialiased;font-size:15px}
.mono{font-family:ui-monospace,"SF Mono","Cascadia Code",Menlo,monospace}
.wrap{max-width:96rem;margin:0 auto;padding:clamp(.9rem,2.4vw,1.8rem);display:flex;flex-direction:column;
  gap:.85rem;min-height:100vh}
.eyebrow{font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:650}
h1{font-size:clamp(1.25rem,2.4vw,1.7rem);font-weight:680;margin:.2rem 0 0;letter-spacing:-.01em}
header .sub{color:var(--ink-2);font-size:.88rem;margin:.3rem 0 0}
header{padding-bottom:.9rem;border-bottom:1px solid var(--hairline)}
.hrow{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-start;justify-content:space-between}

/* composition strip */
.comp{margin-top:.9rem;display:flex;flex-direction:column;gap:.4rem;min-width:min(100%,22rem)}
.compbar{display:flex;gap:2px;height:10px}
.compbar i{display:block;border-radius:3px}
.complegend{display:flex;flex-wrap:wrap;gap:.15rem 1rem;font-size:.76rem;color:var(--muted)}
.complegend b{color:var(--ink-2);font-weight:640}

/* controls */
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.search{flex:1 1 16rem;position:relative;min-width:12rem}
.search input{width:100%;font:inherit;font-size:.9rem;padding:.5rem .7rem .5rem 2rem;border-radius:9px;
  border:1px solid var(--border);background:var(--surface);color:var(--ink)}
.search input:focus{outline:2px solid var(--accent);outline-offset:1px}
.search svg{position:absolute;left:.6rem;top:50%;transform:translateY(-50%);color:var(--muted)}
.search kbd{position:absolute;right:.5rem;top:50%;transform:translateY(-50%);font-size:.7rem;color:var(--muted);
  border:1px solid var(--border);border-radius:4px;padding:0 .3rem;background:var(--surface-2)}
.chip{font:inherit;font-size:.78rem;display:inline-flex;align-items:center;gap:.38rem;padding:.32rem .6rem;
  border-radius:999px;border:1px solid var(--border);background:var(--surface);color:var(--ink-2);cursor:pointer}
.chip:hover{border-color:var(--accent)}
.chip[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);color:var(--ink);font-weight:600}
.chip .n{color:var(--muted);font-variant-numeric:tabular-nums}
.chip[aria-pressed=true] .n{color:var(--ink-2)}
.chip.warn[aria-pressed=true]{background:var(--low-soft);border-color:var(--low);color:var(--low)}
.catbar{display:flex;flex-wrap:wrap;gap:.35rem}
.bar{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center}
.barlab{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:650;
  margin-right:.1rem}
.linky{background:none;border:0;font:inherit;font-size:.78rem;color:var(--accent);cursor:pointer;
  text-decoration:underline;text-underline-offset:2px;padding:.2rem}

/* layout */
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,25rem);gap:.85rem;align-items:start;flex:1}
@media (max-width:62rem){.split{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.panel-h{display:flex;align-items:baseline;justify-content:space-between;gap:.7rem;padding:.7rem .9rem;
  border-bottom:1px solid var(--hairline)}
.panel-h h2{font-size:.95rem;font-weight:660;margin:0}
.panel-h .meta{font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums}

/* entity list */
.list{max-height:calc(100vh - 20rem);min-height:18rem;overflow:auto}
.grp{padding:.45rem .9rem .2rem;font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  font-weight:650;background:var(--surface-2);border-bottom:1px solid var(--hairline);position:sticky;top:0;z-index:1}
.row{display:grid;grid-template-columns:1.1rem minmax(0,1fr) auto;gap:.6rem;align-items:center;width:100%;
  text-align:left;font:inherit;background:none;border:0;border-bottom:1px solid var(--hairline);
  padding:.42rem .9rem;cursor:pointer;color:inherit}
.row:hover{background:var(--surface-2)}
.row[aria-current=true]{background:var(--accent-soft)}
.row:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.row .nm{min-width:0}
.row .t1{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.88rem}
.row .t2{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.73rem;color:var(--muted)}
.row .rt{display:flex;gap:.3rem;align-items:center;flex:none}
.tag{font-size:.68rem;padding:.06rem .38rem;border-radius:5px;background:var(--surface-2);color:var(--ink-2);
  border:1px solid var(--border);white-space:nowrap}
.tag.bad{background:var(--low-soft);color:var(--low);border-color:rgba(179,38,30,.3)}
.empty{padding:2rem .9rem;text-align:center;color:var(--muted);font-size:.88rem}

/* detail */
.detail{position:sticky;top:.85rem;max-height:calc(100vh - 1.7rem);overflow:auto}
.dhead{padding:.85rem .9rem;border-bottom:1px solid var(--hairline)}
.dhead h2{font-size:1rem;font-weight:660;margin:.25rem 0 .35rem;letter-spacing:-.005em;word-break:break-word}
.did{font-size:.74rem;color:var(--muted);word-break:break-all}
.dflags{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.5rem}
.dsec{padding:.7rem .9rem;border-bottom:1px solid var(--hairline)}
.dsec:last-child{border-bottom:0}
.dsec h3{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650;margin:0 0 .45rem}
dl{margin:0;display:grid;grid-template-columns:minmax(6rem,auto) minmax(0,1fr);gap:.3rem .7rem;font-size:.82rem}
dt{color:var(--muted);word-break:break-word}
dd{margin:0;min-width:0;word-break:break-word}
.vals{display:flex;flex-direction:column;gap:.2rem}
.ref{background:none;border:0;padding:0;font:inherit;font-size:.82rem;color:var(--accent);cursor:pointer;
  text-align:left;text-decoration:underline;text-underline-offset:2px;word-break:break-all}
.ref.ext{color:var(--ink-2);text-decoration-style:dotted}
.linkrow{display:flex;align-items:center;gap:.45rem;padding:.2rem 0;font-size:.82rem}
.linkrow .pred{font-size:.7rem;color:var(--muted);flex:none;min-width:6.5rem;text-align:right}
.hint{color:var(--muted);font-size:.8rem;padding:2.5rem .9rem;text-align:center}
.glyph{flex:none;display:block}
footer{font-size:.76rem;color:var(--muted);padding-top:.3rem}
footer code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:0 .25rem}
@media print{.controls,.detail{display:none}.list{max-height:none;overflow:visible}}
"""

JS = r"""
const D = window.__CRATE__;
const $ = s => document.querySelector(s);
const el = (t, c) => { const n = document.createElement(t); if (c) n.className = c; return n; };
const byId = new Map(D.nodes.map(n => [n.id, n]));

const state = { q: '', cats: new Set(), layers: new Set(), flag: null, sel: null };

function glyph(cat, size) {
  const c = D.cats[cat] || D.cats.ctx;
  const s = size || 14;
  return `<svg class="glyph" width="${s}" height="${s}" viewBox="0 0 14 14" aria-hidden="true">
    <path d="${c.shape}" fill="${c.colour}" fill-opacity=".16" stroke="${c.colour}"
      stroke-width="1.3" stroke-linejoin="round"/></svg>`;
}
function catName(n) {
  if (n.status === 'external') return 'External reference';
  if (n.status === 'dangling') return 'Unresolved reference';
  return (D.cats[n.cat] || D.cats.ctx).label;
}
function layerName(n) {
  const l = D.layers[String(n.layer)];
  return l ? `${l.name} — ${l.sub}` : 'Referenced, outside the crate';
}
function shortId(id) {
  try { const u = new URL(id); return u.hostname.replace(/^www\./, '') + u.pathname; }
  catch { return decodeURIComponent(id); }
}

/* ---- filtering ---- */
function matches(n) {
  if (state.cats.size && !state.cats.has(n.cat || 'ctx')) return false;
  if (state.layers.size && !state.layers.has(String(n.layer))) return false;
  if (state.flag === 'orphan' && !n.orphan) return false;
  if (state.flag === 'noid' && (n.idb || n.status !== 'in_crate')) return false;
  if (state.q) {
    const q = state.q.toLowerCase();
    if (!(n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)
       || (n.type || '').toLowerCase().includes(q) || catName(n).toLowerCase().includes(q))) return false;
  }
  return true;
}

/* ---- list ---- */
function renderList() {
  const hits = D.nodes.filter(matches);
  $('#count').textContent = hits.length === D.nodes.length
    ? `${hits.length} entities`
    : `${hits.length} of ${D.nodes.length} entities`;
  const list = $('#list');
  list.innerHTML = '';
  if (!hits.length) {
    list.innerHTML = '<p class="empty">No entity matches those filters.</p>';
    return;
  }
  const order = ['1', '2', '3', 'null'];
  const groups = new Map(order.map(k => [k, []]));
  hits.forEach(n => groups.get(String(n.layer) in Object.fromEntries(order.map(o => [o, 1])) ? String(n.layer) : 'null').push(n));
  const frag = document.createDocumentFragment();
  for (const k of order) {
    const g = groups.get(k);
    if (!g || !g.length) continue;
    const h = el('div', 'grp');
    const L = D.layers[k];
    h.textContent = L ? `${L.name} · ${L.sub} · ${g.length}` : `Referenced outside the crate · ${g.length}`;
    frag.appendChild(h);
    g.sort((a, b) => a.label.localeCompare(b.label));
    for (const n of g) frag.appendChild(row(n));
  }
  list.appendChild(frag);
}

function row(n) {
  const b = el('button', 'row');
  b.type = 'button';
  b.dataset.id = n.id;
  if (state.sel === n.id) b.setAttribute('aria-current', 'true');
  const tags = [];
  if (n.orphan) tags.push('<span class="tag bad">unreachable</span>');
  if (n.status === 'external') tags.push('<span class="tag">external</span>');
  if (n.status === 'dangling') tags.push('<span class="tag bad">unresolved</span>');
  const deg = (n.out.length + n.inc.length);
  tags.push(`<span class="tag">${deg} link${deg === 1 ? '' : 's'}</span>`);
  b.innerHTML = `${glyph(n.cat)}
    <span class="nm"><span class="t1">${esc(n.label)}</span>
    <span class="t2">${esc(catName(n))} · ${esc(n.type || '—')}</span></span>
    <span class="rt">${tags.join('')}</span>`;
  b.addEventListener('click', () => select(n.id));
  return b;
}

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ---- detail ---- */
function select(id) {
  state.sel = id;
  document.querySelectorAll('.row').forEach(r => {
    if (r.dataset.id === id) r.setAttribute('aria-current', 'true');
    else r.removeAttribute('aria-current');
  });
  const n = byId.get(id);
  const d = $('#detail');
  if (!n) { d.innerHTML = '<p class="hint">Not in this crate.</p>'; return; }

  const flags = [];
  flags.push(`<span class="tag">${esc(layerName(n))}</span>`);
  if (n.orphan) flags.push('<span class="tag bad">unreachable from the root</span>');
  if (!n.idb && n.status === 'in_crate') flags.push('<span class="tag">no persistent identifier</span>');
  if (n.status === 'external') flags.push('<span class="tag">external reference</span>');

  let h = `<div class="dhead">
      <div style="display:flex;align-items:center;gap:.45rem">${glyph(n.cat, 16)}
        <span class="eyebrow">${esc(catName(n))}</span></div>
      <h2>${esc(n.label)}</h2>
      <div class="did mono">${esc(shortId(n.id))}</div>
      <div class="dflags">${flags.join('')}</div>
    </div>`;

  if (n.props.length) {
    h += '<div class="dsec"><h3>Properties</h3><dl>';
    for (const [k, vals] of n.props) {
      h += `<dt>${esc(k)}</dt><dd><div class="vals">${vals.map(v =>
        v.ref !== undefined ? refBtn(v.ref) : esc(v.lit)).join('')}</div></dd>`;
    }
    h += '</dl></div>';
  }
  h += linkSec('Links out', n.out, 'to');
  h += linkSec('Links in', n.inc, 'from');
  d.innerHTML = h;
  d.querySelectorAll('[data-goto]').forEach(x =>
    x.addEventListener('click', () => { const t = x.dataset.goto; if (byId.has(t)) { clearFilters(); select(t); scrollToRow(t); } }));
  d.scrollTop = 0;
}

function refBtn(id) {
  const t = byId.get(id);
  if (!t) return `<span class="mono" style="font-size:.78rem;color:var(--muted)">${esc(shortId(id))}</span>`;
  const cls = t.status === 'in_crate' ? 'ref' : 'ref ext';
  return `<button class="${cls}" data-goto="${esc(id)}">${esc(t.label)}</button>`;
}

function linkSec(title, links, dir) {
  if (!links.length) return '';
  let h = `<div class="dsec"><h3>${title} · ${links.length}</h3>`;
  const seen = new Map();
  links.forEach(([id, pred]) => { if (!seen.has(pred)) seen.set(pred, []); seen.get(pred).push(id); });
  for (const [pred, ids] of seen) {
    for (const id of ids) {
      const t = byId.get(id);
      h += `<div class="linkrow"><span class="pred mono">${esc(pred)}</span>
        ${t ? glyph(t.cat, 12) : ''}${refBtn(id)}</div>`;
    }
  }
  return h + '</div>';
}

function scrollToRow(id) {
  renderList();
  const r = document.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
  if (r) r.scrollIntoView({ block: 'center' });
}

function clearFilters() {
  state.q = ''; state.cats.clear(); state.layers.clear(); state.flag = null;
  $('#q').value = '';
  syncChips();
}

/* ---- chips ---- */
function syncChips() {
  document.querySelectorAll('[data-cat]').forEach(c =>
    c.setAttribute('aria-pressed', state.cats.has(c.dataset.cat)));
  document.querySelectorAll('[data-layer]').forEach(c =>
    c.setAttribute('aria-pressed', state.layers.has(c.dataset.layer)));
  document.querySelectorAll('[data-flag]').forEach(c =>
    c.setAttribute('aria-pressed', state.flag === c.dataset.flag));
  renderList();
}

function build() {
  // category chips, in registry order, counts from the data
  const counts = {};
  D.nodes.forEach(n => { const k = n.cat || 'ctx'; counts[k] = (counts[k] || 0) + 1; });
  const cb = $('#cats');
  for (const key of Object.keys(D.cats)) {
    if (!counts[key]) continue;
    const b = el('button', 'chip');
    b.type = 'button'; b.dataset.cat = key; b.setAttribute('aria-pressed', 'false');
    b.innerHTML = `${glyph(key, 12)}<span>${esc(D.cats[key].label)}</span><span class="n">${counts[key]}</span>`;
    b.addEventListener('click', () => {
      state.cats.has(key) ? state.cats.delete(key) : state.cats.add(key); syncChips();
    });
    cb.appendChild(b);
  }
  document.querySelectorAll('[data-layer]').forEach(b => b.addEventListener('click', () => {
    const k = b.dataset.layer;
    state.layers.has(k) ? state.layers.delete(k) : state.layers.add(k); syncChips();
  }));
  document.querySelectorAll('[data-flag]').forEach(b => b.addEventListener('click', () => {
    state.flag = state.flag === b.dataset.flag ? null : b.dataset.flag; syncChips();
  }));
  $('#clear').addEventListener('click', clearFilters);
  $('#q').addEventListener('input', e => { state.q = e.target.value.trim(); renderList(); });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== $('#q')) { e.preventDefault(); $('#q').focus(); }
    if (e.key === 'Escape') { if (document.activeElement === $('#q')) $('#q').blur(); clearFilters(); }
  });
  renderList();
  select(D.root);
}
build();
"""


def comp_strip() -> str:
    c = payload["counts"]
    tot = c["layer1"] + c["layer2"] + c["layer3"]
    segs, leg = [], []
    tone = {1: "#0e7c86", 2: "#4a8f96", 3: "#8fb3b6"}
    for k in (1, 2, 3):
        n = c[f"layer{k}"]
        pct = 100 * n / tot
        L = LAYERS[k]
        segs.append(f'<i style="flex:{pct};background:{tone[k]}"></i>')
        leg.append(f'<span><b>{n}</b> {L["name"]} · {L["sub"]}</span>')
    return (
        '<div class="comp"><div class="eyebrow">Composition</div>'
        f'<div class="compbar" role="img" aria-label="{tot} entities across three layers">'
        + "".join(segs)
        + '</div><div class="complegend">'
        + "".join(leg)
        + "</div></div>"
    )


def layer_chips() -> str:
    c = payload["counts"]
    out = []
    for k in (1, 2, 3):
        L = LAYERS[k]
        out.append(
            f'<button class="chip" type="button" data-layer="{k}" aria-pressed="false">'
            f'<span>{L["name"]}</span><span class="n">{c[f"layer{k}"]}</span></button>'
        )
    return "".join(out)


orphans = payload["counts"]["orphan"]
no_id = sum(1 for n in nodes if n["status"] == "in_crate" and not n["idb"])

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — entity explorer</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="hrow">
    <div>
      <div class="eyebrow">RO-Crate · entity explorer</div>
      <h1>{html.escape(title)}</h1>
      <p class="sub">{len(nodes)} entities · {len(model["edges"])} links ·
        published {html.escape(payload["published"][:10])} ·
        <span class="mono">{html.escape(payload["crate"])}</span></p>
    </div>
    {comp_strip()}
  </div>
</header>

<div class="controls">
  <label class="search">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6">
      <circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>
    <input id="q" type="search" placeholder="Search name, @id, type or category…"
      aria-label="Search entities" autocomplete="off">
    <kbd>/</kbd>
  </label>
  <div class="bar"><span class="barlab">Layer</span>{layer_chips()}</div>
  <div class="bar">
    <button class="chip warn" type="button" data-flag="orphan" aria-pressed="false">
      <span>Unreachable</span><span class="n">{orphans}</span></button>
    <button class="chip" type="button" data-flag="noid" aria-pressed="false">
      <span>No identifier</span><span class="n">{no_id}</span></button>
    <button class="linky" id="clear" type="button">Clear</button>
  </div>
</div>
<div class="catbar" id="cats"></div>

<div class="split">
  <div class="panel">
    <div class="panel-h"><h2>Entities</h2><span class="meta" id="count"></span></div>
    <div class="list" id="list"></div>
  </div>
  <div class="panel detail" id="detail"></div>
</div>

<footer>
  Shape and colour both carry the category, so nothing depends on colour alone.
  Press <code>/</code> to search, <code>Esc</code> to clear. Generated from
  <code>ro-crate-metadata.json</code> using the crate builder's own category registry.
</footer>
</div>
<script>window.__CRATE__={DATA};</script>
<script>{JS}</script>
</body>
</html>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT} — {OUT.stat().st_size / 1024:.0f} KB · {len(nodes)} entities")
