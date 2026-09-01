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

  // Expanded first, so everything below sees the model as Python built it: the
  // island carries ids as indices and omits a `name` that repeats its `label`
  // (#694), and no other line in this file knows that.
  var D = window.PayloadCodec.expand(
    JSON.parse(document.getElementById('ex-data').textContent)
  );
  var NODE = new Map(D.nodes.map(function (n) { return [n.id, n]; }));
  var ENTITY = new Map(
    (D.document['@graph'] || []).filter(function (e) { return e && e['@id']; })
      .map(function (e) { return [e['@id'], e]; })
  );
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
  // The panel both viewers mount, and the words both read the crate in.
  var INSPECTOR = window.ExplorerInspector.create(D);
  var edgeTerm = INSPECTOR.edgeTerm, category = INSPECTOR.category;
  var layout = window.ExplorerLayout.layout;
  var NODE_W = window.ExplorerLayout.NODE_W, NODE_H = window.ExplorerLayout.NODE_H;
  // How far the opening view is allowed to pull back. A crate's whole graph is
  // thousands of pixels tall — the all-entities view of a real deposit lays
  // out around 2300x4000 — so "fit everything" means a field of 14px slivers with
  // no readable name on it. Better to open on a legible part of the graph and
  // let the reader pan, zoom out or use the minimap, all of which say where
  // they are; an unreadable whole says nothing.
  var FIT_FLOOR = 0.32;
  // The page-wide channel the two inspectors take turns on; see `Explorer`.
  var INSPECT = 'vitro:inspect';

  // Built in one expression, not across template lines: htm drops the newline
  // and the indentation with it, which is how "entities, 177 links" became
  // "entities,177 links".
  function summary(graph, hits) {
    var text = graph.visible.size + ' of ' + DRAWABLE.size + ' entities, '
      + graph.edges.length + ' links';
    return hits ? text + ', ' + hits.size + ' matching' : text;
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
  // The wording is the payload's (`_legend_wording`), not this file's: the assay
  // lanes draw the same legend with a different renderer, and two copies of the
  // rule is how two legends over one crate come to disagree.
  function legendLabel(c) { return c.legend || c.label; }
  function legendTitle(c) { return c.legend_title || c.label; }

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
      query: p.get('q') || ''
    };
  }
  /* The hash is the page's, not this section's: the assay lanes link their own
   * state into it under their own keys, so a write here reads what is there and
   * replaces only what this section owns. Otherwise the last section a reader
   * touched would erase the other's link. */
  function writeHash(s) {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    p.set('views', Array.from(s.views).join(','));
    p.delete('select');
    p.delete('q');
    if (s.selected) p.set('select', s.selected);
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

  /* ---- side panel ----------------------------------------------------------
   *
   * Mounted, not written: `explorer_inspector.js` builds it in plain DOM and the
   * assay lanes mount the same thing, so a reader who clicks an entity gets one
   * answer whichever picture they clicked it in.
   */
  function Panel(props) {
    var host = useRef(null);
    useEffect(function () {
      if (!host.current) return;
      INSPECTOR.render(host.current, {
        id: props.selected, onSelect: props.goTo, onClose: props.onClose
      });
    }, [props.selected]);
    return html`<aside class="ex-side" ref=${host}></aside>`;
  }

  /* ---- app ----------------------------------------------------------------- */
  function Explorer() {
    var initial = useMemo(readHash, []);
    var viewState = useState(initial.views); var views = viewState[0], setViews = viewState[1];
    var selState = useState(initial.selected); var selected = selState[0], setSelected = selState[1];
    var qState = useState(initial.query); var query = qState[0], setQuery = qState[1];
    var pinnedState = useState(function () {
      return new Set(initial.selected && NODE.has(initial.selected) ? [initial.selected] : []);
    });
    var pinned = pinnedState[0], setPinned = pinnedState[1];
    var flow = useReactFlow();
    var searchRef = useRef(null);

    useEffect(function () {
      writeHash({ views: views, selected: selected, query: query });
    }, [views, selected, query]);

    /* Both sections dock their inspector to the same edge of the window, so two
     * open at once would sit on top of each other. Each says when it opens one
     * and the other puts its own away — a fact about the page, which neither app
     * can read off the other's state. */
    useEffect(function () {
      if (!selected) return;
      document.dispatchEvent(new CustomEvent(INSPECT, { detail: { owner: 'explorer' } }));
    }, [selected]);
    useEffect(function () {
      function yield_(event) {
        if (!event.detail || event.detail.owner === 'explorer') return;
        setSelected(null);
      }
      document.addEventListener(INSPECT, yield_);
      return function () { document.removeEventListener(INSPECT, yield_); };
    }, []);

    var graph = useMemo(function () { return visibleGraph(views, pinned); }, [views, pinned]);
    var positions = useMemo(function () {
      return layout(graph.visible, graph.edges);
    }, [graph]);

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
          width: NODE_W, height: NODE_H,
          selected: id === selected,
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
          // A relation the model draws reversed keeps the crate's predicate but
          // runs the other way along this arrow, so the term alone would assert
          // the inverse triple. The glyph is the one the side panel already
          // uses for an incoming property, so the two read alike.
          label: lit ? e.labels.map(edgeTerm).join(', ') : undefined,
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

    // The whole-crate view leads and is fenced off from the rest: it is the way
    // back, not a peer of "Chemicals". The others are questions about parts of
    // the crate, and reading them as one undifferentiated wall of chips is what
    // made the toolbar a wall.
    var tops = D.views.filter(function (v) { return !v.parent; });
    var whole = tops.slice(0, 1), parts = tops.slice(1);
    function Chip(props) {
      var v = props.v;
      return html`<button key=${v.key} type="button"
        class=${'ex-chip' + (props.sub ? ' ex-sub' : '')}
        aria-pressed=${views.has(v.key)} title=${v.hint}
        onClick=${function () { toggle(v.key); }}>${v.label}
        <span class="ex-chip-count">${v.count}</span></button>`;
    }

    return html`<div class="ex-shell">
      <div class="ex-toolbar">
        <div class="ex-views" role="group" aria-label="Views">
          ${whole.map(function (v) { return html`<${Chip} key=${v.key} v=${v} />`; })}
          ${parts.length ? html`<span class="ex-sep" aria-hidden="true"></span>` : null}
          ${parts.map(function (v) { return html`<${Chip} key=${v.key} v=${v} />`; })}
        </div>
      </div>
      ${D.views.some(function (v) { return v.parent && views.has(v.parent); })
        ? html`<div class="ex-flavours" role="group" aria-label="Kinds of step">
            <span class="ex-sub-h">Only</span>
            ${D.views.filter(function (v) { return v.parent && views.has(v.parent); })
              .map(function (v) { return html`<${Chip} key=${v.key} v=${v} sub />`; })}
          </div>`
        : null}
      <div class="ex-main">
        <div class="ex-canvas">
          ${/* Over the canvas, not above it: searching acts on the drawing, so
               it sits on it, and the toolbar is left as one row of what the
               reader is choosing between. Framing is not here — React Flow's
               own Controls already carry a fit button, and two of them teach
               that one of them does something else. */ null}
          <div class="ex-overlay">
            <input ref=${searchRef} type="search" class="ex-search" value=${query}
              placeholder="Search name, @id or type  ( / )"
              onInput=${function (e) { setQuery(e.target.value); }} />
          </div>
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
        <${Panel} selected=${selected} goTo=${goTo}
          onClose=${function () { setSelected(null); }} />
      </div>
      ${/* Under the drawing, not over it: a colour key explains what is on the
           canvas, and the count says how much of the crate that is. Above, they
           were two more strips between the reader and the graph. */ null}
      <div class="ex-footer">
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
        <span class="ex-count">${summary(graph, hits)}</span>
      </div>
    </div>`;
  }

  ReactDOM.createRoot(document.getElementById('ex-app'))
    .render(html`<${ReactFlowProvider}><${Explorer} /></${ReactFlowProvider}>`);
})();
