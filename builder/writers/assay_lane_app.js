/*
 * The assay lanes section: one assay, drawn as the chain it is.
 *
 * The lane used to be a layout the entity explorer could switch to. It is a
 * section of its own now, because the two answer different questions and the
 * explorer's canvas was the wrong instrument for this one: a lane has a fixed
 * left-to-right order and nine named columns, so it wants a flat drawing a
 * reader can scan, not a pan-and-zoom viewport that has to be framed first.
 *
 * Where each box goes is `assay_lane_view.js` — pure geometry, no DOM, run by
 * the same test suite over the same graphs. This file is only the drawing and
 * the reading of it: the SVG, the assay chips, the two folds, and the inspector.
 *
 * It reads the entity explorer's own data island. One crate document on a page
 * that ships inside the crate is already the accepted cost of a self-contained
 * report; a second copy of it for a second section would be another.
 *
 * No anchors, no markup built from strings, nothing assigned to an element's
 * HTML — same rule the explorer keeps (#169). The crate is untrusted text, and
 * every value here reaches the page as a text node.
 */
(function () {
  'use strict';

  var root = document.getElementById('lane-svg');
  var island = document.getElementById('ex-data');
  if (!root || !island || !window.AssayLaneView || !window.PayloadCodec) return;

  var D = window.PayloadCodec.expand(JSON.parse(island.textContent));
  var LANES = D.lanes || [];
  if (!LANES.length) return;

  var NODE = new Map(D.nodes.map(function (n) { return [n.id, n]; }));
  // The panel the entity explorer mounts, and the words it reads the crate in.
  // Both come from the section above, which the report emits first for the same
  // reason it holds the data island.
  var INSPECTOR = window.ExplorerInspector.create(D);
  var edgeTerm = INSPECTOR.edgeTerm, category = INSPECTOR.category;

  /* ---- state --------------------------------------------------------------
   *
   * Four things, all of them the reader's: which assay, whether the protocol
   * band is shown, which folded file columns are open, and what is selected.
   */
  var cur = 0, showBand = true, unfolded = {}, selected = null, fit = false;

  /* ---- the state a reader can link to ---------------------------------------
   *
   * Under this section's own keys, in the page's one hash: the entity explorer
   * writes `views`, `select` and `q` there, so each side reads what is present
   * and replaces only what it owns. A lane is worth linking to for the same
   * reason a view is — it is what one reader wants to show another.
   */
  function readHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var key = p.get('lane');
    LANES.forEach(function (lane, i) { if (lane.key === key) cur = i; });
    var folds = (p.get('fold') || '').split(',').filter(Boolean);
    showBand = folds.indexOf('-band') < 0;
    fit = folds.indexOf('fit') >= 0;
    folds.forEach(function (f) { if (f !== 'fit' && f !== '-band') unfolded[f] = true; });
    selected = p.get('pick') || null;
  }
  function writeHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    p.set('lane', LANES[cur].key);
    var folds = Object.keys(unfolded).filter(function (k) { return unfolded[k]; }).sort();
    if (!showBand) folds.unshift('-band');
    if (fit) folds.push('fit');
    p.delete('fold');
    p.delete('pick');
    if (folds.length) p.set('fold', folds.join(','));
    if (selected) p.set('pick', selected);
    history.replaceState(null, '', '#' + p.toString());
  }
  // Which relations the model draws against their own predicate — the
  // geometry orients its arrows by them, and the payload is the only copy.
  var REVERSED = new Set(D.relations_reversed || []);

  // Read off the empty root the section already carries rather than written out
  // here: the page must not carry a URL-shaped literal it never fetches, and an
  // SVG namespace belongs on an SVG element.
  var NS = root.namespaceURI;
  var STACK = '\u0000stack:';

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    for (var k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) {
      node.setAttribute(k, String(attrs[k]));
    }
    return node;
  }
  function tag(name, cls, text) {
    var node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }

  /* A name across two lines, breaking on words and giving up on the last one.
   * A step's name is a sentence ("10-minute radiolabeled T3/T4 uptake
   * exposure") and a box is 150px wide; one truncated line loses the endpoint,
   * which is the half that says what the step did. */
  function wrap(text, cap, max) {
    var words = String(text).split(/\s+/), out = [], line = '';
    for (var i = 0; i < words.length; i++) {
      if (!line) { line = words[i]; continue; }
      if ((line + ' ' + words[i]).length <= cap) { line += ' ' + words[i]; continue; }
      out.push(line);
      line = words[i];
      if (out.length === max - 1) break;
    }
    if (line && out.length < max) out.push(line);
    var used = out.join(' ').length;
    if (used < String(text).length) {
      var rest = String(text).slice(used).trim();
      if (rest) out[out.length - 1] = trunc(out[out.length - 1] + ' ' + rest, cap);
    }
    return out.slice(0, max);
  }

  /* ---- the graph one lane draws -------------------------------------------
   *
   * The payload's edges, merged per pair the way the explorer merges them, so
   * two relations between the same two entities are one arrow with two names.
   */
  function edgesWithin(visible) {
    var merged = new Map();
    D.edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      var key = e.src + ' ' + e.dst;
      if (!merged.has(key)) merged.set(key, { src: e.src, dst: e.dst, labels: [] });
      merged.get(key).labels.push(e.label);
    });
    return Array.from(merged.values());
  }

  /* Which columns collapse into a stack.
   *
   * A readout that wrote forty files is the normal case, and forty boxes down
   * one column is a lane no one can read across. A column of files folds; a
   * column of anything else does not, because the other columns are the chain
   * itself and hiding a step would hide the finding.
   */
  function foldable(drawing) {
    return drawing.ranks.filter(function (r) {
      return r.members.length > 1 && r.members.every(function (id) {
        var n = NODE.get(id);
        return n && n.category === 'data';
      });
    }).map(function (r) { return r.key; });
  }

  /* One lane, laid out, with the reader's two folds applied.
   *
   * Both folds are done by handing the geometry a smaller graph and asking
   * again, rather than by editing its answer: a fold changes which column is
   * tallest, and the chain is centred on that.
   */
  function build() {
    var lane = LANES[cur];
    var visible = new Set(lane.members);
    var nodes = NODE;
    var drawing = window.AssayLaneView.build(visible, edgesWithin(visible), nodes, REVERSED);
    if (!drawing) return null;

    if (!showBand) {
      drawing.band.forEach(function (h) { visible.delete(h.id); });
      drawing = window.AssayLaneView.build(visible, edgesWithin(visible), nodes, REVERSED);
      if (!drawing) return null;
    }

    var folds = foldable(drawing).filter(function (key) { return !unfolded[key]; });
    var stacks = new Map();
    if (folds.length) {
      nodes = new Map(NODE);
      var edges = edgesWithin(visible);
      folds.forEach(function (key) {
        var rank = drawing.ranks.filter(function (r) { return r.key === key; })[0];
        var id = STACK + key;
        var word = rank.label.toLowerCase();
        stacks.set(id, rank.members.slice());
        nodes.set(id, {
          id: id, label: rank.members.length + ' ' + word + ' files',
          name: rank.members.length + ' ' + word + ' files',
          type: 'File', category: 'data', layer: null,
          status: 'described', residence: 'carried', orphan: false
        });
        var members = new Set(rank.members);
        members.forEach(function (m) { visible.delete(m); });
        visible.add(id);
        edges = edges.map(function (e) {
          return {
            src: members.has(e.src) ? id : e.src,
            dst: members.has(e.dst) ? id : e.dst,
            labels: e.labels
          };
        }).filter(function (e) { return e.src !== e.dst; });
      });
      var deduped = new Map();
      edges.forEach(function (e) {
        var key = e.src + ' ' + e.dst;
        if (!deduped.has(key)) deduped.set(key, { src: e.src, dst: e.dst, labels: [] });
        e.labels.forEach(function (l) {
          if (deduped.get(key).labels.indexOf(l) < 0) deduped.get(key).labels.push(l);
        });
      });
      var refolded = window.AssayLaneView.build(visible, Array.from(deduped.values()),
        nodes, REVERSED);
      if (refolded) drawing = refolded;
    }
    return { lane: lane, drawing: drawing, nodes: nodes, stacks: stacks,
      foldable: foldable(drawing) };
  }

  /* ---- the drawing --------------------------------------------------------- */

  var RANK_FONT = 9, MARGIN = 16;
  var NODE_H = window.AssayLaneView.NODE_H;

  function colourOf(node) { return category(node).colour; }

  function drawNode(svg, state, id, box, onPick) {
    var node = state.nodes.get(id);
    if (!node) return;
    var colour = colourOf(node);
    var stack = state.stacks.get(id);
    var group = el('g', {
      class: 'lane-node' + (selected === id ? ' is-sel' : ''),
      tabindex: '0', role: 'button', 'aria-label': node.label
    });
    if (stack) {
      // Two ghosts behind the box: a stack of files reads as a stack before it
      // reads as a count, and the count is what it says.
      group.appendChild(el('rect', { x: box.x + 6, y: box.y - 6, width: box.w, height: box.h,
        rx: 5, fill: 'none', stroke: colour, 'stroke-width': 1.1, opacity: 0.38 }));
      group.appendChild(el('rect', { x: box.x + 3, y: box.y - 3, width: box.w, height: box.h,
        rx: 5, fill: 'none', stroke: colour, 'stroke-width': 1.1, opacity: 0.66 }));
    }
    var carried = node.residence === 'carried';
    var rect = el('rect', {
      class: 'lane-box', x: box.x, y: box.y, width: box.w, height: box.h,
      rx: node.category === 'chemical' ? 4 : 6,
      fill: carried ? colour : 'var(--surface)',
      'fill-opacity': carried ? 0.13 : null,
      stroke: colour,
      'stroke-width': node.category === 'process' ? 1.9 : 1.35,
      'stroke-dasharray': node.status !== 'described' ? '2 2.5'
        : (node.orphan ? '5 3' : null)
    });
    group.appendChild(rect);

    /* Name, and under it the crate's own type — the caption the explorer's
     * nodes carry, so a box means the same thing in both pictures. Only where
     * there is height for a second line: a protocol box is short and a compound
     * shorter still, and both sit under a column already headed LABPROTOCOL. */
    var tagged = box.h >= NODE_H;
    var size = box.h < 24 ? 8.4 : (box.h < 30 ? 9.3 : 10.2);
    var cap = box.h < 24 ? 15 : 24;
    var name = el('text', {
      class: 'lane-name' + (node.category === 'process' ? ' is-step' : ''),
      x: box.x + 8, y: box.y + (tagged ? 15 : box.h / 2 + 3.4), 'font-size': size
    });
    name.textContent = trunc(node.label, cap);
    group.appendChild(name);
    if (tagged && node.type) {
      var tag = el('text', { class: 'lane-tag', x: box.x + 8, y: box.y + 26,
        'font-size': 6.6 });
      tag.textContent = trunc(node.type.toUpperCase(), 30);
      group.appendChild(tag);
    }

    function pick() { onPick(id); }
    group.addEventListener('click', pick);
    group.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); pick(); }
    });
    svg.appendChild(group);
  }

  function edgePath(a, b) {
    var x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x - 2, y2 = b.y + b.h / 2;
    if (Math.abs(y1 - y2) < 1) return 'M' + x1 + ' ' + y1 + ' L' + x2 + ' ' + y2;
    return 'M' + x1 + ' ' + y1 + ' C' + (x1 + 22) + ' ' + y1 + ','
      + (x2 - 22) + ' ' + y2 + ',' + x2 + ' ' + y2;
  }

  function render() {
    writeHash();
    var state = build();
    var note = document.getElementById('lane-note');
    var svg = root;
    clear(svg);
    if (!state) {
      note.hidden = false;
      svg.hidden = true;
      paintChips(null);
      document.getElementById('lane-count').textContent = '';
      inspect(null);
      return;
    }
    note.hidden = true;
    svg.hidden = false;
    var drawing = state.drawing, positions = drawing.positions;
    var bandIds = new Set(drawing.band.map(function (h) { return h.id; }));

    svg.setAttribute('viewBox', '0 0 ' + drawing.width + ' ' + drawing.height);
    // Fitted, the viewBox does the scaling and the chain stops scrolling; at
    // rest the boxes are drawn at reading size and the viewer scrolls instead.
    // A nine-column chain of a real deposit is 1800px wide, and neither answer
    // is right for every reader.
    svg.setAttribute('width', fit ? '100%' : String(drawing.width));
    svg.removeAttribute('height');
    if (!fit) svg.setAttribute('height', String(drawing.height));
    svg.setAttribute('aria-label', 'Process lane for ' + state.lane.label);
    var defs = el('defs');
    var marker = el('marker', { id: 'lane-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5,
      markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' });
    marker.appendChild(el('path', { d: 'M0 0 L10 5 L0 10 z', fill: 'currentColor' }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // Column headings. A rank is always the same step, so naming the columns
    // once turns nine positions into nine words — and an empty column then
    // says, where the reader is already looking, that the deposit recorded no
    // such step. That gap is the finding a maturity report is for.
    var folds = new Set(state.foldable);
    drawing.ranks.forEach(function (rank) {
      var open = folds.has(rank.key) && unfolded[rank.key];
      var group = el('g', folds.has(rank.key)
        ? { class: 'lane-rank-btn', tabindex: '0', role: 'button',
          'aria-label': rank.label + ' files, ' + (open ? 'unfolded' : 'folded')
            + ' — activate to toggle' }
        : {});
      var text = el('text', { class: 'lane-rank' + (rank.members.length ? '' : ' is-empty'),
        x: rank.x, y: 14, 'font-size': RANK_FONT });
      text.textContent = folds.has(rank.key)
        ? rank.label + (open ? '  ▴' : '  ▾') : rank.label;
      group.appendChild(text);
      if (!rank.members.length) {
        var none = el('text', { class: 'lane-rank-none', x: rank.x, y: 24, 'font-size': 8 });
        none.textContent = 'not recorded';
        group.appendChild(none);
      }
      if (folds.has(rank.key)) {
        var toggle = function () {
          unfolded[rank.key] = !unfolded[rank.key];
          selected = null;
          render();
        };
        group.addEventListener('click', toggle);
        group.addEventListener('keydown', function (event) {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
        });
      }
      svg.appendChild(group);
    });

    // The chain. Band members hang off it on their own connectors, so an edge
    // that ends below the rule is not drawn twice.
    var labels = [];
    var chain = el('g', { fill: 'none', 'marker-end': 'url(#lane-arrow)' });
    drawing.edges.forEach(function (edge) {
      if (bandIds.has(edge.src) || bandIds.has(edge.dst)) return;
      var a = positions.get(edge.src), b = positions.get(edge.dst);
      if (!a || !b) return;
      var lit = selected && (edge.src === selected || edge.dst === selected);
      chain.appendChild(el('path', {
        class: 'lane-edge', d: edgePath(a, b),
        stroke: lit ? 'var(--accent)' : 'currentColor',
        'stroke-width': lit ? 1.8 : 1.2,
        opacity: selected ? (lit ? 1 : 0.16) : 0.48
      }));
      if (lit) {
        labels.push({
          x: (a.x + a.w + b.x - 2) / 2,
          y: (a.y + a.h / 2 + b.y + b.h / 2) / 2 - 1,
          text: edgeTerm(edge.label)
        });
      }
    });
    svg.appendChild(chain);

    if (showBand && drawing.band.length) {
      svg.appendChild(el('line', { class: 'lane-band-rule', x1: MARGIN, y1: drawing.bandTop - 20,
        x2: drawing.width - MARGIN, y2: drawing.bandTop - 20 }));
      var caption = el('text', { class: 'lane-band-label', x: MARGIN, y: drawing.bandTop - 8,
        'font-size': RANK_FONT });
      caption.textContent = 'LABPROTOCOL';
      svg.appendChild(caption);

      // At rest one connector per anchor is drawn, so twelve compounds under one
      // table do not read as a comb. A selection always reveals its own.
      var seen = {};
      var hangs = el('g', { fill: 'none' });
      drawing.band.forEach(function (hang) {
        var a = positions.get(hang.anchor), b = positions.get(hang.id);
        if (!a || !b) return;
        var lit = selected && (hang.anchor === selected || hang.id === selected);
        var first = !seen[hang.anchor];
        seen[hang.anchor] = true;
        if (!lit && !first) return;
        // A bracket out of the anchor's left side, down the gap the geometry
        // chose, and back into the member's left side. Not a drop from the
        // anchor's own box: a step in the top row of a three-row rank would
        // have its line pass through the two steps below it, and a line
        // crossing a box reads as an edge to that box.
        var x = hang.x;
        var ay = a.y + a.h / 2, by = b.y + b.h / 2;
        hangs.appendChild(el('path', {
          class: 'lane-hang',
          d: 'M' + a.x + ' ' + ay + ' L' + x + ' ' + ay
            + ' L' + x + ' ' + by + ' L' + b.x + ' ' + by,
          stroke: lit ? 'var(--accent)' : colourOf(state.nodes.get(hang.id) || {}),
          'stroke-width': lit ? 1.7 : 1.1,
          opacity: selected ? (lit ? 1 : 0.15) : 0.55
        }));
        if (lit) {
          labels.push({
            x: x + 5, y: (ay + by) / 2, anchor: 'start',
            text: edgeTerm(hang.label)
          });
        }
      });
      svg.appendChild(hangs);
    }

    var order = Array.from(positions.keys());
    order.forEach(function (id) {
      if (bandIds.has(id)) return;
      drawNode(svg, state, id, positions.get(id), pickNode);
    });
    order.forEach(function (id) {
      if (!bandIds.has(id)) return;
      drawNode(svg, state, id, positions.get(id), pickNode);
    });

    // Drawn last, over everything: the label knocks the edge out behind itself
    // with a surface-coloured halo and takes the edge's own colour, so it reads
    // as part of the line rather than as a chip sitting on top of one.
    labels.forEach(function (label) {
      var text = el('text', { class: 'lane-edge-label', x: label.x, y: label.y + 3.2,
        'font-size': 8.4, 'text-anchor': label.anchor || 'middle' });
      text.textContent = label.text;
      svg.appendChild(text);
    });

    paintChips(state);
    paintLegend(state);
    var links = drawing.edges.length + drawing.band.length;
    document.getElementById('lane-count').textContent =
      positions.size + ' entities, ' + links + ' links';
    inspect(selected ? state.nodes.get(selected) : null);

    function pickNode(id) {
      if (state.stacks.has(id)) {
        unfolded[id.slice(STACK.length)] = true;
        selected = null;
        render();
        return;
      }
      selected = selected === id ? null : id;
      if (selected) announce();
      render();
    }
  }

  /* ---- the toolbar --------------------------------------------------------- */

  function paintChips(state) {
    var chips = document.getElementById('lane-chips');
    clear(chips);
    LANES.forEach(function (lane, i) {
      var button = tag('button', 'ex-chip');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(i === cur));
      button.title = 'This assay end to end — its materials, steps, protocols and compounds';
      button.appendChild(document.createTextNode(lane.label));
      button.appendChild(tag('span', 'ex-chip-count', lane.members.length));
      button.addEventListener('click', function () {
        cur = i;
        unfolded = {};
        selected = null;
        render();
      });
      chips.appendChild(button);
    });
    var openable = state ? state.foldable : [];
    var open = openable.filter(function (key) { return unfolded[key]; });
    setSwitch('lane-band', showBand, true);
    setSwitch('lane-fit', fit, true);
    setSwitch('lane-unfold', openable.length > 0 && open.length === openable.length,
      openable.length > 0);
  }

  function setSwitch(id, on, enabled) {
    var button = document.getElementById(id);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.disabled = !enabled;
  }

  /* The same legend the entity explorer draws, from the same payload and with
   * the same wording (`_legend_wording`) — the two sections colour one crate,
   * and a reader who learned the colours above must not have to learn them
   * again here. Only the categories on this lane are listed. */
  function paintLegend(state) {
    var legend = document.getElementById('lane-legend');
    clear(legend);
    var present = new Set();
    state.drawing.positions.forEach(function (_box, id) {
      var node = state.nodes.get(id);
      if (node) present.add(node.category);
    });
    Object.keys(D.categories).forEach(function (key) {
      if (!present.has(key)) return;
      var c = D.categories[key];
      var span = tag('span', 'ex-key');
      span.title = c.legend_title || c.label;
      var swatch = tag('span', 'ex-swatch');
      swatch.setAttribute('aria-hidden', 'true');
      swatch.style.setProperty('--ex-c', c.colour);
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(c.legend || c.label));
      legend.appendChild(span);
    });
  }

  function inspect(node) {
    INSPECTOR.render(document.getElementById('lane-panel'), {
      id: node ? node.id : null,
      onSelect: function (id) { selected = id; announce(); render(); },
      onClose: function () { selected = null; render(); }
    });
  }

  /* Both sections dock their inspector to the same edge of the window, so two
   * open at once would sit on top of each other. Each says when it opens one
   * and the other puts its own away — a fact about the page, which neither app
   * can read off the other's state. */
  var CHANNEL = 'vitro:inspect';
  function announce() {
    document.dispatchEvent(new CustomEvent(CHANNEL, { detail: { owner: 'lane' } }));
  }
  document.addEventListener(CHANNEL, function (event) {
    if (!event.detail || event.detail.owner === 'lane' || !selected) return;
    selected = null;
    render();
  });

  /* ---- wiring -------------------------------------------------------------
   *
   * The frame — bar, chips holder, the controls, the canvas, the legend and the
   * panel — is markup the section already carries, so a reader with no script
   * gets the section's `<noscript>` note rather than an empty box, and every id
   * this file reaches for is declared in one place.
   */
  readHash();
  document.getElementById('lane-band').addEventListener('click', function () {
    showBand = !showBand;
    selected = null;
    render();
  });
  document.getElementById('lane-fit').addEventListener('click', function () {
    fit = !fit;
    render();
  });
  document.getElementById('lane-unfold').addEventListener('click', function () {
    var state = build();
    var openable = state ? state.foldable : [];
    var open = openable.filter(function (key) { return unfolded[key]; });
    var wanted = !(openable.length > 0 && open.length === openable.length);
    unfolded = {};
    if (wanted) openable.forEach(function (key) { unfolded[key] = true; });
    selected = null;
    render();
  });

  render();
}());
