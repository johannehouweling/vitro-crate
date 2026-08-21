/*
 * The maturity report's interactive entity explorer (#615).
 *
 * Reads the payload that `entity_explorer.py` writes into the page and draws the
 * crate's entity graph on a React Flow canvas. Views are toggles rather than
 * tabs: what is drawn is the union of the views that are on, with the edges
 * between whatever that leaves visible, so a reader can ask "the compounds AND
 * the samples" without a view having to exist for that question.
 *
 * Plain ES2020 over the vendored UMD builds — `htm` gives the JSX-ish templates
 * without a build step, so what ships inside the crate is what is written here.
 */
(function () {
  'use strict';

  var html = htm.bind(React.createElement);
  var useState = React.useState, useMemo = React.useMemo, useEffect = React.useEffect,
      useRef = React.useRef, useCallback = React.useCallback;
  var RF = window.ReactFlow;
  var ReactFlowCanvas = RF.ReactFlow, ReactFlowProvider = RF.ReactFlowProvider,
      Background = RF.Background, MiniMap = RF.MiniMap, Controls = RF.Controls,
      Handle = RF.Handle, Position = RF.Position, useReactFlow = RF.useReactFlow,
      MarkerType = RF.MarkerType;

  var D = JSON.parse(document.getElementById('ex-data').textContent);
  var NODE = new Map(D.nodes.map(function (n) { return [n.id, n]; }));
  var ENTITY = new Map(
    (D.document['@graph'] || []).filter(function (e) { return e && e['@id']; })
      .map(function (e) { return [e['@id'], e]; })
  );
  var GRAPH_ORDER = (D.document['@graph'] || [])
    .filter(function (e) { return e && e['@id']; })
    .map(function (e) { return e['@id']; });
  var VIEW = new Map(D.views.map(function (v) { return [v.key, new Set(v.members)]; }));
  // Everything some view can put on the canvas. Not `D.nodes.length`: that also
  // counts bare references the crate never describes, which no view offers, so
  // a denominator taken from it would be one the numerator can never reach.
  var DRAWABLE = new Set();
  VIEW.forEach(function (members) { members.forEach(function (id) { DRAWABLE.add(id); }); });

  // Adjacency both ways, so the side panel can show links in as well as out.
  var OUT = new Map(), IN = new Map();
  D.edges.forEach(function (e) {
    if (!OUT.has(e.src)) OUT.set(e.src, []);
    if (!IN.has(e.dst)) IN.set(e.dst, []);
    OUT.get(e.src).push(e);
    IN.get(e.dst).push(e);
  });

  // The relations that carry a process's own inputs and outputs. A file reached
  // this way is drawn hanging off the step that made it, not off the dataset
  // that merely contains it: a crate root lists every file it holds, and drawing
  // those alongside the derivation is what made the whole-crate picture a
  // hairball on paper.
  var DERIVATION = new Set(['input', 'object', 'result', 'output']);
  var NODE_W = 200, NODE_H = 44;
  // How far the opening view is allowed to pull back. A crate's whole graph is
  // thousands of pixels tall — the researcher view of a real deposit lays out
  // around 2300x4000 — so "fit everything" means a field of 14px slivers with
  // no readable name on it. Better to open on a legible part of the graph and
  // let the reader pan, zoom out or use the minimap, all of which say where
  // they are; an unreadable whole says nothing.
  var FIT_FLOOR = 0.32;

  // Built in one expression, not across template lines: htm drops the newline
  // and the indentation with it, which is how "entities, 177 links" became
  // "entities,177 links".
  function summary(graph, hits) {
    var text = graph.visible.size + ' of ' + DRAWABLE.size + ' entities, '
      + graph.edges.length + ' links';
    return hits ? text + ', ' + hits.size + ' matching' : text;
  }

  function category(n) { return D.categories[n.category] || D.categories.ctx; }
  function layerName(n) {
    return n.layer ? D.layers[String(n.layer)] : 'Referenced, outside the crate';
  }
  function shortId(id) {
    try {
      var u = new URL(id);
      return u.hostname.replace(/^www\./, '') + u.pathname;
    } catch (err) { return decodeURIComponent(id); }
  }

  function Glyph(props) {
    var c = D.categories[props.k] || D.categories.ctx;
    var s = props.size || 14;
    return html`<svg class="ex-glyph" width=${s} height=${s} viewBox="0 0 14 14"
      aria-hidden="true"><path d=${c.glyph} fill=${c.colour} fill-opacity=".18"
      stroke=${c.colour} stroke-width="1.3" stroke-linejoin="round" /></svg>`;
  }

  /* ---- the state a reader can link to -------------------------------------- */
  function readHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var keys = (p.get('views') || '').split(',').filter(function (k) { return VIEW.has(k); });
    if (!keys.length) {
      keys = D.views.filter(function (v) { return v.default; }).map(function (v) { return v.key; });
    }
    if (!keys.length) keys = [D.views[0].key];
    return {
      views: new Set(keys), selected: p.get('select') || null,
      document: p.get('json') === '1', query: p.get('q') || ''
    };
  }
  function writeHash(s) {
    var p = new URLSearchParams();
    p.set('views', Array.from(s.views).join(','));
    if (s.selected) p.set('select', s.selected);
    if (s.document) p.set('json', '1');
    if (s.query) p.set('q', s.query);
    history.replaceState(null, '', '#' + p.toString());
  }

  /* ---- what is on the canvas ----------------------------------------------- */
  function visibleGraph(views, pinned) {
    var visible = new Set(pinned);
    views.forEach(function (key) {
      VIEW.get(key).forEach(function (id) { visible.add(id); });
    });
    var everything = views.has('all');

    // Files a visible process produced or consumed: their containment edge says
    // the same thing a second time, so it is dropped unless the reader asked to
    // see everything.
    var derived = new Set();
    if (!everything) {
      D.edges.forEach(function (e) {
        if (!DERIVATION.has(e.label)) return;
        if (!visible.has(e.src) || !visible.has(e.dst)) return;
        var src = NODE.get(e.src), dst = NODE.get(e.dst);
        if (src && src.category === 'process') derived.add(e.dst);
        if (dst && dst.category === 'process') derived.add(e.src);
      });
    }

    // One edge per pair, carrying every relation that connects them: a process
    // that both consumes and produces an entity is one arrow labelled with both,
    // rather than two drawn on top of each other.
    var merged = new Map();
    D.edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst) || e.src === e.dst) return;
      if (!everything && e.label === 'hasPart' && derived.has(e.dst)) return;
      var key = e.src + ' ' + e.dst;
      if (!merged.has(key)) merged.set(key, { src: e.src, dst: e.dst, labels: [] });
      if (merged.get(key).labels.indexOf(e.label) < 0) merged.get(key).labels.push(e.label);
    });
    return { visible: visible, edges: Array.from(merged.values()) };
  }

  function layout(visible, edges) {
    var g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', nodesep: 12, ranksep: 90, marginx: 24, marginy: 24 });
    g.setDefaultEdgeLabel(function () { return {}; });
    visible.forEach(function (id) { g.setNode(id, { width: NODE_W, height: NODE_H }); });
    edges.forEach(function (e) { g.setEdge(e.src, e.dst); });
    dagre.layout(g);
    var pos = new Map();
    visible.forEach(function (id) {
      var p = g.node(id);
      pos.set(id, { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 });
    });
    return pos;
  }

  /* ---- node ---------------------------------------------------------------- */
  function EntityNode(props) {
    var n = props.data.n, c = category(n);
    // The colour rides in as a custom property rather than a per-category class:
    // it comes from the payload, which is generated from the one registry, so
    // there is no second palette in the stylesheet to fall out of step with it.
    var cls = ['ex-node'];
    if (n.orphan) cls.push('ex-orphan');
    if (n.status !== 'in_crate') cls.push('ex-outside');
    if (props.data.hit) cls.push('ex-hit');
    if (props.data.dim) cls.push('ex-dim');
    if (props.selected) cls.push('ex-selected');
    return html`<div class=${cls.join(' ')} style=${{ '--ex-c': c.colour }}
        title=${(n.name || n.label) + ' — ' + n.id}>
      <${Handle} type="target" position=${Position.Left} />
      <${Glyph} k=${n.category} size=${16} />
      <div class="ex-node-text">
        <div class="ex-node-name">${n.label}</div>
        <div class="ex-node-tag">${n.type}</div>
      </div>
      <${Handle} type="source" position=${Position.Right} />
    </div>`;
  }
  var nodeTypes = { entity: EntityNode };

  /* ---- JSON-LD, with every reference walkable ------------------------------ */
  function Ref(props) {
    var target = NODE.get(props.id);
    if (!target) {
      return html`<span class="ex-mono ex-muted">${shortId(props.id)}</span>`;
    }
    var cls = 'ex-ref' + (target.status === 'in_crate' ? '' : ' ex-ref-outside');
    return html`<button type="button" class=${cls} title=${props.id}
      onClick=${function () { props.goTo(props.id); }}>${target.label}</button>`;
  }

  function JsonValue(props) {
    var v = props.v, goTo = props.goTo;
    if (v === null || v === undefined) return html`<span class="ex-json-null">null</span>`;
    if (Array.isArray(v)) {
      if (!v.length) return html`<span class="ex-json-punct">[]</span>`;
      return html`<span class="ex-json-punct">[</span><div class="ex-json-indent">
        ${v.map(function (x, i) {
          return html`<div key=${i}><${JsonValue} v=${x} goTo=${goTo} />${i < v.length - 1 ? ',' : ''}</div>`;
        })}</div><span class="ex-json-punct">]</span>`;
    }
    if (typeof v === 'object') {
      var keys = Object.keys(v);
      if (keys.length === 1 && keys[0] === '@id') {
        return html`<span class="ex-json-punct">{ </span><span class="ex-json-key">"@id"</span>: <${Ref}
          id=${v['@id']} goTo=${goTo} /><span class="ex-json-punct"> }</span>`;
      }
      return html`<span class="ex-json-punct">{</span><div class="ex-json-indent">
        ${keys.map(function (k, i) {
          return html`<div key=${k}><span class="ex-json-key">"${k}"</span>: <${JsonValue}
            v=${v[k]} goTo=${goTo} />${i < keys.length - 1 ? ',' : ''}</div>`;
        })}</div><span class="ex-json-punct">}</span>`;
    }
    if (typeof v === 'string') return html`<span class="ex-json-string">"${v}"</span>`;
    return html`<span class="ex-json-number">${String(v)}</span>`;
  }

  /* ---- side panel ---------------------------------------------------------- */
  function Panel(props) {
    var sel = props.selected, goTo = props.goTo;
    var tabState = useState('properties');
    var tab = tabState[0], setTab = tabState[1];
    var docRef = useRef(null);

    useEffect(function () {
      if (props.document && sel && docRef.current) {
        var key = window.CSS && CSS.escape ? CSS.escape(sel) : sel;
        var el = docRef.current.querySelector('[data-entity="' + key + '"]');
        if (el) el.scrollIntoView({ block: 'start' });
      }
    }, [props.document, sel]);

    if (props.document) {
      return html`<aside class="ex-side ex-side-document" ref=${docRef}>
        <div class="ex-side-head"><span class="ex-eyebrow">${
          'ro-crate-metadata.json, ' + GRAPH_ORDER.length + ' entities'}</span></div>
        <div class="ex-json">
          ${GRAPH_ORDER.map(function (id) {
            var n = NODE.get(id);
            return html`<div key=${id} data-entity=${id}
                class=${'ex-json-entity' + (id === sel ? ' ex-json-current' : '')}>
              <div class="ex-json-entity-head">
                ${n ? html`<${Glyph} k=${n.category} size=${12} />` : null}
                ${n ? html`<${Ref} id=${id} goTo=${goTo} />`
                    : html`<span class="ex-mono">${shortId(id)}</span>`}
                <span class="ex-muted">${n ? n.type : 'not drawn'}</span>
              </div>
              <${JsonValue} v=${ENTITY.get(id)} goTo=${goTo} />
            </div>`;
          })}
        </div>
      </aside>`;
    }

    if (!sel) {
      return html`<aside class="ex-side ex-side-empty"><p class="ex-hint">
        Select an entity to read its properties, its links and its JSON-LD.<br />
        Views combine: turn on as many as you need. Press <kbd class="ex-kbd">/</kbd> to search.
      </p></aside>`;
    }

    var n = NODE.get(sel);
    var entity = ENTITY.get(sel);
    var outgoing = OUT.get(sel) || [], incoming = IN.get(sel) || [];
    var flags = [{ text: layerName(n), bad: false }];
    if (n.orphan) flags.push({ text: 'unreachable from the root', bad: true });
    if (n.status === 'dangling') flags.push({ text: 'nothing describes this id', bad: true });
    if (n.status === 'external') flags.push({ text: 'described outside the crate', bad: false });
    if (n.status === 'in_crate' && !n.identifier_backed) {
      flags.push({ text: 'no persistent identifier', bad: false });
    }

    function links(edges, direction) {
      var byRelation = new Map();
      edges.forEach(function (e) {
        var id = direction === 'out' ? e.dst : e.src;
        if (!byRelation.has(e.label)) byRelation.set(e.label, []);
        byRelation.get(e.label).push(id);
      });
      return Array.from(byRelation.entries()).map(function (pair) {
        return html`<div key=${direction + pair[0]} class="ex-link-group">
          <div class="ex-relation ex-mono">${(direction === 'out' ? '→ ' : '← ') + pair[0]}
            <span class="ex-muted">${'(' + pair[1].length + ')'}</span></div>
          ${pair[1].map(function (id) {
            var t = NODE.get(id);
            return html`<div key=${id} class="ex-link">
              ${t ? html`<${Glyph} k=${t.category} size=${12} />` : null}
              <${Ref} id=${id} goTo=${goTo} /></div>`;
          })}
        </div>`;
      });
    }

    var tabs = [
      ['properties', 'Properties'],
      ['links', 'Links (' + (outgoing.length + incoming.length) + ')'],
      ['json', 'JSON-LD']
    ];
    return html`<aside class="ex-side">
      <div class="ex-side-head">
        <div class="ex-side-title">
          <${Glyph} k=${n.category} size=${16} />
          <span class="ex-eyebrow">${category(n).label}</span>
          <button type="button" class="ex-close" title="Clear selection"
            onClick=${props.onClose}>×</button>
        </div>
        <h3 class="ex-side-name">${n.label}</h3>
        <div class="ex-side-id ex-mono" title=${n.id}>${shortId(n.id)}</div>
        <div class="ex-flags">${flags.map(function (f) {
          return html`<span key=${f.text} class=${'ex-tag' + (f.bad ? ' ex-tag-bad' : '')}>${f.text}</span>`;
        })}</div>
        <div class="ex-side-tabs" role="tablist">${tabs.map(function (t) {
          return html`<button key=${t[0]} type="button" role="tab" aria-selected=${tab === t[0]}
            onClick=${function () { setTab(t[0]); }}>${t[1]}</button>`;
        })}</div>
      </div>
      <div class="ex-side-body">
        ${tab === 'properties' ? (entity
          ? html`<dl class="ex-props">${Object.keys(entity).filter(function (k) { return k !== '@id'; })
              .map(function (k) {
                return html`<div key=${k} class="ex-prop">
                  <dt class="ex-mono">${k}</dt>
                  <dd><${JsonValue} v=${entity[k]} goTo=${goTo} /></dd></div>`;
              })}</dl>`
          : html`<p class="ex-hint">This entity is described outside the crate; the crate
              carries the reference only.</p>`) : null}
        ${tab === 'links' ? html`${links(outgoing, 'out')}${links(incoming, 'in')}
          ${!outgoing.length && !incoming.length
            ? html`<p class="ex-hint">Nothing links to or from this entity.</p>` : null}` : null}
        ${tab === 'json' ? html`<div class="ex-json"><${JsonValue}
          v=${entity || { '@id': n.id }} goTo=${goTo} /></div>` : null}
      </div>
    </aside>`;
  }

  /* ---- app ----------------------------------------------------------------- */
  function Explorer() {
    var initial = useMemo(readHash, []);
    var viewState = useState(initial.views); var views = viewState[0], setViews = viewState[1];
    var selState = useState(initial.selected); var selected = selState[0], setSelected = selState[1];
    var docState = useState(initial.document); var showDoc = docState[0], setShowDoc = docState[1];
    var qState = useState(initial.query); var query = qState[0], setQuery = qState[1];
    var pinnedState = useState(function () {
      return new Set(initial.selected && NODE.has(initial.selected) ? [initial.selected] : []);
    });
    var pinned = pinnedState[0], setPinned = pinnedState[1];
    var flow = useReactFlow();
    var searchRef = useRef(null);

    useEffect(function () {
      writeHash({ views: views, selected: selected, document: showDoc, query: query });
    }, [views, selected, showDoc, query]);

    var graph = useMemo(function () { return visibleGraph(views, pinned); }, [views, pinned]);
    var positions = useMemo(function () { return layout(graph.visible, graph.edges); }, [graph]);

    var needle = query.trim().toLowerCase();
    var hits = useMemo(function () {
      if (!needle) return null;
      var found = new Set();
      graph.visible.forEach(function (id) {
        var n = NODE.get(id);
        var hay = (n.label + ' ' + n.id + ' ' + n.type + ' ' + category(n).label).toLowerCase();
        if (hay.indexOf(needle) >= 0) found.add(id);
      });
      return found;
    }, [needle, graph]);

    var touching = useMemo(function () {
      var set = new Set();
      if (selected) {
        graph.edges.forEach(function (e) {
          if (e.src === selected || e.dst === selected) set.add(e.src + ' ' + e.dst);
        });
      }
      return set;
    }, [selected, graph]);

    var nodes = useMemo(function () {
      return Array.from(graph.visible).map(function (id) {
        return {
          id: id, type: 'entity', position: positions.get(id),
          width: NODE_W, height: NODE_H, selected: id === selected,
          data: {
            n: NODE.get(id),
            hit: hits ? hits.has(id) : false,
            dim: hits ? !hits.has(id) : false
          }
        };
      });
    }, [graph, positions, selected, hits]);

    var edges = useMemo(function () {
      return graph.edges.map(function (e) {
        var lit = touching.has(e.src + ' ' + e.dst);
        var colour = lit ? category(NODE.get(e.src)).colour : '#b8bcc4';
        return {
          id: e.src + ' ' + e.dst, source: e.src, target: e.dst,
          label: lit ? e.labels.join(', ') : undefined,
          labelStyle: { fontSize: 10 }, labelBgStyle: { fill: '#fff', fillOpacity: 0.92 },
          labelBgPadding: [3, 1],
          style: { stroke: colour, strokeWidth: lit ? 2 : 1, opacity: hits && !lit ? 0.25 : 1 },
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: colour },
          zIndex: lit ? 10 : 0
        };
      });
    }, [graph, touching, hits]);

    // Re-frame when the visible set changes, and lean in when one entity is
    // chosen: a selection the reader cannot find on the canvas is not one.
    var shape = Array.from(graph.visible).join('|');
    useEffect(function () {
      var t = setTimeout(function () {
        flow.fitView({ padding: 0.08, duration: 300, minZoom: FIT_FLOOR });
      }, 30);
      return function () { clearTimeout(t); };
    }, [shape]);
    useEffect(function () {
      if (!selected || !positions.has(selected)) return;
      var t = setTimeout(function () {
        flow.fitView({
          nodes: [{ id: selected }], padding: 0.6, duration: 350, maxZoom: 1, minZoom: 0.6
        });
      }, 60);
      return function () { clearTimeout(t); };
    }, [selected]);

    // Following a reference out of the side panel can land on an entity no
    // active view holds; it is pinned onto the canvas rather than the click
    // silently doing nothing.
    var goTo = useCallback(function (id) {
      if (!NODE.has(id)) return;
      setPinned(function (prev) {
        if (graph.visible.has(id) || prev.has(id)) return prev;
        var next = new Set(prev); next.add(id); return next;
      });
      setSelected(id);
    }, [graph]);

    function toggle(key) {
      setViews(function (prev) {
        var next = new Set(prev);
        if (next.has(key)) next.delete(key); else next.add(key);
        if (!next.size) next.add(key);  // never leave the canvas blank
        return next;
      });
    }

    useEffect(function () {
      function onKey(e) {
        if (e.key === '/' && document.activeElement !== searchRef.current) {
          e.preventDefault(); searchRef.current.focus();
        } else if (e.key === 'Escape') {
          if (document.activeElement === searchRef.current) searchRef.current.blur();
          setQuery('');
        } else if (e.key === 'Enter' && document.activeElement === searchRef.current
                   && hits && hits.size) {
          setSelected(Array.from(hits)[0]);
        }
      }
      document.addEventListener('keydown', onKey);
      return function () { document.removeEventListener('keydown', onKey); };
    }, [hits]);

    var present = new Set();
    graph.visible.forEach(function (id) { present.add(NODE.get(id).category); });

    return html`<div class="ex-shell">
      <div class="ex-toolbar">
        <div class="ex-views" role="group" aria-label="Views">
          ${D.views.map(function (v) {
            return html`<button key=${v.key} type="button" class="ex-chip"
              aria-pressed=${views.has(v.key)} title=${v.hint}
              onClick=${function () { toggle(v.key); }}>${v.label}
              <span class="ex-chip-count">${v.members.length}</span></button>`;
          })}
        </div>
        <div class="ex-tools">
          <input ref=${searchRef} type="search" class="ex-search" value=${query}
            placeholder="Search name, @id or type  ( / )"
            onInput=${function (e) { setQuery(e.target.value); }} />
          <button type="button" class="ex-chip" aria-pressed=${showDoc}
            title="Show the crate's whole ro-crate-metadata.json in the side panel, instead of the selected entity"
            onClick=${function () { setShowDoc(!showDoc); }}>JSON</button>
          <button type="button" class="ex-chip"
            title="Zoom out until everything currently on the canvas fits on screen"
            onClick=${function () { flow.fitView({ padding: 0.08, duration: 300 }); }}>Fit</button>
          <span class="ex-count">${summary(graph, hits)}</span>
        </div>
      </div>
      <div class="ex-legend">
        ${Object.keys(D.categories).filter(function (k) { return present.has(k); })
          .map(function (k) {
            return html`<span key=${k} class="ex-key"><${Glyph} k=${k} size=${12} />
              ${D.categories[k].label}</span>`;
          })}
        <span class="ex-key"><span class="ex-swatch ex-swatch-orphan"></span>unreachable from the root</span>
        <span class="ex-key"><span class="ex-swatch ex-swatch-outside"></span>outside the crate</span>
      </div>
      <div class="ex-main">
        <div class="ex-canvas">
          <${ReactFlowCanvas} nodes=${nodes} edges=${edges} nodeTypes=${nodeTypes}
            onNodeClick=${function (_e, n) { setSelected(n.id); }}
            onPaneClick=${function () { setSelected(null); }}
            nodesConnectable=${false} minZoom=${0.05} fitView
            proOptions=${{ hideAttribution: true }}>
            <${Background} gap=${18} size=${1} color="#e3e6ea" />
            <${MiniMap} pannable zoomable nodeStrokeWidth=${0}
              nodeColor=${function (n) { return category(n.data.n).colour; }} />
            <${Controls} showInteractive=${false} />
          </${ReactFlowCanvas}>
        </div>
        <${Panel} selected=${selected} document=${showDoc} goTo=${goTo}
          onClose=${function () { setSelected(null); }} />
      </div>
    </div>`;
  }

  ReactDOM.createRoot(document.getElementById('ex-app'))
    .render(html`<${ReactFlowProvider}><${Explorer} /></${ReactFlowProvider}>`);
})();
