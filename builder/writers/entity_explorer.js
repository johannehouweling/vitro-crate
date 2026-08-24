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
  // A view that refines another (#624): the LabProcesses sub-row. Children
  // NARROW — a parent with active children draws their union instead of its own
  // members — so the explorer keeps one rule, "what is on is what is drawn",
  // rather than gaining a second interaction model for the sub-row.
  var VIEW_PARENT = new Map(D.views.map(function (v) { return [v.key, v.parent || null]; }));
  var CHILDREN = new Map();
  D.views.forEach(function (v) {
    if (!v.parent) return;
    if (!CHILDREN.has(v.parent)) CHILDREN.set(v.parent, []);
    CHILDREN.get(v.parent).push(v.key);
  });
  // The views that draw one assay's chain, and so want the lane layout (#686).
  // Read off the payload rather than matched on a key: which views are lanes is
  // a fact about the selection, not a naming convention the browser re-derives.
  var LANE = new Set(D.views.filter(function (v) { return v.lane; })
    .map(function (v) { return v.key; }));
  /* What an edge says it is, in the crate's own vocabulary (#688).
   *
   * The model's labels are this codebase's words — an edge is `input` because
   * that reads well beside `result` — but the predicate the crate is serialized
   * with is `schema:object`. A reader who copies a name off the canvas has to
   * find it in the JSON-LD, so the payload carries the mapping and this looks it
   * up rather than keeping a second copy of the vocabulary here.
   */
  function term(label) { return (D.relations && D.relations[label]) || label; }
  /* What an entity KEY expands to. `input` and `object` are one predicate;
   * `studies`, `assays` and `hasPart` are another. A key the context does not
   * name expands under the crate's own @vocab, which is the crate's rule rather
   * than a guess made here. */
  function prop(key) {
    if (key.charAt(0) === '@') return key;
    return (D.properties && D.properties[key]) || (D.vocab_prefix + ':' + key);
  }
  function isUrl(v) { return typeof v === 'string' && /^https?:\/\//.test(v); }
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
  var layout = window.ExplorerLayout.layout;
  var laneLayout = window.AssayLaneLayout ? window.AssayLaneLayout.layout : null;
  var NODE_W = window.ExplorerLayout.NODE_W, NODE_H = window.ExplorerLayout.NODE_H;
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

  /* A category, as colour and nothing else (#688).
   *
   * This was a per-category SVG shape, and shape was the redundant channel that
   * survived greyscale, print and colour vision deficiency — `CATEGORY_STYLES`
   * guaranteed no two categories shared one. Dropping it leaves eleven
   * categories on a colour ring the registry itself calls full at ten, so the
   * cost is real and is recorded rather than forgotten: the mitigations that
   * ship with it are the inspector naming the type in words on every entity, and
   * the legend remaining the one place the mapping is stated.
   */
  function Swatch(props) {
    var c = D.categories[props.k] || D.categories.ctx;
    return html`<span class="ex-swatch" aria-hidden="true"
      style=${{ '--ex-c': c.colour }}></span>`;
  }
  // How many type names a legend key spells out before it counts the rest. The
  // legend is one strip across the toolbar, so a bucket holding eight types
  // would push the rest of the keys off it; the full census rides on `title`.
  var LEGEND_TYPES = 2;
  function legendLabel(c) {
    var types = c.types || [];
    if (!types.length) return c.label;
    var shown = types.slice(0, LEGEND_TYPES).join(', ');
    return types.length > LEGEND_TYPES ? shown + ' +' + (types.length - LEGEND_TYPES) : shown;
  }
  function legendTitle(c) {
    var types = c.types || [];
    return types.length > LEGEND_TYPES ? c.label + ' — ' + types.join(', ') : c.label;
  }

  /* ---- the state a reader can link to -------------------------------------- */
  function readHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var keys = (p.get('views') || '').split(',').filter(function (k) { return VIEW.has(k); });
    if (!keys.length) {
      keys = D.views.filter(function (v) { return v.default; }).map(function (v) { return v.key; });
    }
    if (!keys.length) keys = [D.views[0].key];
    // A link to a flavour opens LabProcesses too: the sub-row lives under its
    // parent's chip, and a filter the reader cannot see is one they cannot undo.
    keys.concat().forEach(function (k) {
      var parent = VIEW_PARENT.get(k);
      if (parent && keys.indexOf(parent) === -1) keys.push(parent);
    });
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
      var kids = (CHILDREN.get(key) || []).filter(function (k) { return views.has(k); });
      // A child that is on is drawn by its parent's turn, so a child whose
      // parent is off draws nothing on its own — the sub-row is a filter on the
      // parent, not a ninth chip.
      if (!kids.length && VIEW_PARENT.get(key) && views.has(VIEW_PARENT.get(key))) return;
      var sources = kids.length ? kids : [key];
      sources.forEach(function (k) {
        VIEW.get(k).forEach(function (id) { visible.add(id); });
      });
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

  /* ---- node ---------------------------------------------------------------- */
  function EntityNode(props) {
    var n = props.data.n, c = category(n);
    // The colour rides in as a custom property rather than a per-category class:
    // it comes from the payload, which is generated from the one registry, so
    // there is no second palette in the stylesheet to fall out of step with it.
    var cls = ['ex-node'];
    if (n.orphan) cls.push('ex-orphan');
    if (n.status !== 'described') cls.push('ex-outside');
    // A tinted fill means the bytes are in the crate directory. Read off
    // `residence`, never off `status`: every described entity shares a status,
    // so tinting on that would paint a compound as though it were a file (#687).
    if (n.residence === 'carried') cls.push('ex-carried');
    if (props.data.hit) cls.push('ex-hit');
    if (props.data.dim) cls.push('ex-dim');
    if (props.selected) cls.push('ex-selected');
    return html`<div class=${cls.join(' ')} style=${{ '--ex-c': c.colour }}
        title=${(n.name || n.label) + ' — ' + n.id}>
      <${Handle} type="target" position=${Position.Left} />
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
    var cls = 'ex-ref' + (target.status === 'described' ? '' : ' ex-ref-outside');
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

  /* A URL the crate carries, offered as something to take away.
   *
   * NOT a link. The payload holds the crate verbatim, `javascript:` URLs and
   * all, so the explorer writes no anchor and no link target of any kind — that
   * absence is load-bearing and pinned by a test that greps this file for the
   * attribute names, which is why they are not written out even here (#169).
   * Copying navigates nowhere and executes nothing, so the reader gets the URL
   * without the crate getting a way to run anything.
   */
  function Url(props) {
    var v = props.v;
    var copied = useState(false), was = copied[0], setWas = copied[1];
    return html`<button type="button" class=${'ex-url ex-mono' + (was ? ' ex-url-copied' : '')}
      title=${'Copy ' + v}
      onClick=${function () {
        if (navigator.clipboard) {
          navigator.clipboard.writeText(v);
          setWas(true);
          setTimeout(function () { setWas(false); }, 1200);
        }
      }}>${v}</button>`;
  }

  /* One row per predicate, not one per spelling. */
  function overviewRows(entity) {
    var byTerm = new Map();
    Object.keys(entity).forEach(function (key) {
      if (key === '@id') return;
      var name = prop(key);
      if (!byTerm.has(name)) byTerm.set(name, { name: name, keys: [], values: [], seen: new Set() });
      var row = byTerm.get(name);
      if (row.keys.indexOf(key) < 0) row.keys.push(key);
      (Array.isArray(entity[key]) ? entity[key] : [entity[key]]).forEach(function (v) {
        // Two aliases of one predicate carry the same values; showing them
        // twice is what naming the predicate was meant to stop.
        var seal = JSON.stringify(v);
        if (row.seen.has(seal)) return;
        row.seen.add(seal);
        row.values.push(v);
      });
    });
    return Array.from(byTerm.values());
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
                ${n ? html`<${Swatch} k=${n.category} />` : null}
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
    if (n.status === 'described' && !n.identifier_backed) {
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
          <div class="ex-relation ex-mono"
            title=${pair[0]}>${(direction === 'out' ? '→ ' : '← ') + term(pair[0])}
            <span class="ex-muted">${'(' + pair[1].length + ')'}</span></div>
          ${pair[1].map(function (id) {
            var t = NODE.get(id);
            return html`<div key=${id} class="ex-link">
              ${t ? html`<${Swatch} k=${t.category} />` : null}
              <${Ref} id=${id} goTo=${goTo} /></div>`;
          })}
        </div>`;
      });
    }

    var tabs = [
      ['properties', 'Overview'],
      ['links', 'Links (' + (outgoing.length + incoming.length) + ')'],
      ['json', 'JSON-LD']
    ];
    return html`<aside class="ex-side">
      <div class="ex-side-head">
        <div class="ex-side-title">
          <${Swatch} k=${n.category} />
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
          ? html`<dl class="ex-props">${overviewRows(entity).map(function (row) {
                return html`<div key=${row.name} class="ex-prop">
                  <dt class="ex-mono" title=${'in the crate as: ' + row.keys.join(', ')}>${row.name}</dt>
                  <dd>${row.values.map(function (v, i) {
                    return html`<div key=${i}>${isUrl(v)
                      ? html`<${Url} v=${v} />`
                      : html`<${JsonValue} v=${v} goTo=${goTo} />`}</div>`;
                  })}</dd></div>`;
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
    /* Where the nodes go.
     *
     * One assay on its own is a chain, and a chain reads as a lane: the material
     * runs left to right and what qualifies a step hangs below it. Anything else
     * — several assays at once, the whole crate — is not one chain, so it goes
     * on the generic canvas.
     *
     * The lane may still decline a graph that is nominally an assay but does not
     * fit a spine (a characterisation run with no exposure, two exposures, an
     * assay carrying AOP entities). It returns null and the fallback is the same
     * canvas, same styling, no visible seam.
     */
    var positions = useMemo(function () {
      var lanes = Array.from(views).filter(function (k) { return LANE.has(k); });
      if (lanes.length === 1 && laneLayout) {
        var laid = laneLayout(graph.visible, graph.edges, NODE);
        if (laid) return laid;
      }
      return layout(graph.visible, graph.edges);
    }, [graph, views]);

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
          // Named only while lit, and named in the crate's vocabulary. No
          // background box: the label takes the edge's own colour and a halo
          // knocks the line out from behind the text (see `.ex-canvas
          // .react-flow__edge-text` in the stylesheet), so it reads as part of
          // the edge rather than as a card sitting on top of one.
          label: lit ? e.labels.map(term).join(', ') : undefined,
          labelShowBg: false,
          labelStyle: { fontSize: 10, fill: colour },
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
        if (next.has(key)) {
          next.delete(key);
          // A sub-row goes with the chip that opens it; a filter still running
          // behind a hidden control is one the reader cannot undo.
          (CHILDREN.get(key) || []).forEach(function (k) { next.delete(k); });
        } else {
          next.add(key);
        }
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
          ${D.views.filter(function (v) { return !v.parent; }).map(function (v) {
            return html`<button key=${v.key} type="button" class="ex-chip"
              aria-pressed=${views.has(v.key)} title=${v.hint}
              onClick=${function () { toggle(v.key); }}>${v.label}
              <span class="ex-chip-count">${v.count}</span></button>`;
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
      ${D.views.some(function (v) { return v.parent && views.has(v.parent); })
        ? html`<div class="ex-flavours" role="group" aria-label="Kinds of step">
            <span class="ex-sub-h">Only</span>
            ${D.views.filter(function (v) { return v.parent && views.has(v.parent); })
              .map(function (v) {
                return html`<button key=${v.key} type="button" class="ex-chip ex-sub"
                  aria-pressed=${views.has(v.key)} title=${v.hint}
                  onClick=${function () { toggle(v.key); }}>${v.label}
                  <span class="ex-chip-count">${v.count}</span></button>`;
              })}
          </div>`
        : null}
      <div class="ex-legend">
        ${Object.keys(D.categories).filter(function (k) { return present.has(k); })
          .map(function (k) {
            var c = D.categories[k];
            return html`<span key=${k} class="ex-key" title=${legendTitle(c)}>
              <${Swatch} k=${k} />${legendLabel(c)}</span>`;
          })}
        <span class="ex-key"><span class="ex-swatch ex-swatch-orphan"></span>unreachable from the root</span>
        <span class="ex-key"><span class="ex-swatch ex-swatch-outside"></span>outside the crate</span>
      </div>
      <div class="ex-main">
        <div class="ex-canvas">
          <${ReactFlowCanvas} nodes=${nodes} edges=${edges} nodeTypes=${nodeTypes}
            onNodeClick=${function (_e, n) {
              // Click again to clear, taking the edge labels with it. Selection
              // used to change only by choosing something else, so a reader who
              // had finished with an entity had nowhere to put the selection
              // down except the empty canvas.
              setSelected(function (was) { return was === n.id ? null : n.id; });
            }}
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
