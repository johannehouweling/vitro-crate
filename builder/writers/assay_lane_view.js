/*
 * One assay, drawn as the chain it is (#686).
 *
 * `assay_lane_layout.js` re-ranked the generic canvas: it handed the spine to
 * the layered pass and then hung a band underneath. That inherited two defects
 * the layered pass cannot avoid here. Sibling steps land in ONE rank sharing an
 * x, so their satellites were dealt into the same cell and drew on top of each
 * other — normal since #678 gave every cell line its own CellCulture. And a
 * spine that is not one connected component was declined outright, so a
 * characterisation assay with no Exposure fell back to the canvas the lane
 * exists to improve on.
 *
 * This module ranks by what a node IS in the ISA-Tox chain instead:
 *
 *   cellline  culture  cultured  exposure  exposed  readout  raw  analysis  processed
 *      o    ->   [ ]  ->   o   ->   [ ]  ->   o  ->  [ ]  ->  o  ->  [ ]  ->    o
 *                 |                  |                |
 *   band          +- protocol        +- protocol      +- protocol
 *                                    +- compounds (reagent of that protocol)
 *
 * Two consequences the old module had to work for, and this one gets for free:
 *
 *   * A rank is a COLUMN. Two CellCultures stack; they cannot coincide, because
 *     a rank deals its members down its own column and nothing else is in it.
 *   * A missing step is an EMPTY column, not a broken graph. An assay that ran
 *     no exposure still draws, and the gap is visible in the place the reader
 *     is already looking — which is the whole point of a maturity report.
 *
 * Geometry is this module's own. It used to be the generic canvas's, so that a
 * lane and the graph beside it could not drift apart; the lane is now a section
 * of its own, drawn as flat SVG at a scale chosen for a nine-column chain, and
 * borrowing a dagre canvas's 200x44 box would only make the chain too wide to
 * read across.
 *
 * Edges carry `reversed` and `subject`. The model draws some relations against
 * the predicate so the arrow points the way material moves — deliberate — but a
 * renderer that labels such an arrow with the bare term asserts the inverse of
 * the triple the crate holds. WHICH relations those are is the payload's answer
 * (`relations_reversed`, derived from the relation tables), passed in rather
 * than restated here: a second copy in the browser is the drift this codebase
 * already refuses for the vocabulary itself.
 *
 * Same contract as its predecessor: `null` means "not a lane", and the caller
 * draws the graph on the generic canvas.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.AssayLaneView = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Two lines: the entity's name and, under it, the type the explorer's own
  // nodes are captioned with.
  var NODE_W = 152, NODE_H = 34;
  var GAP_X = 58, GAP_Y = 11, MARGIN = 16, TOP = 28;
  // The drop from the spine's floor to the band. Wider than the gap inside the
  // band, so "below the chain" and "below a protocol" read as two relations
  // rather than one column of four things.
  var BAND_DROP = 42;
  // A protocol is a filename and runs long, so its box is wider than a chain
  // box and sits a little left of it; a compound is one word and three fit
  // across the space one chain box leaves.
  var PROTO_W = 164, PROTO_H = 26, PROTO_DX = -7;
  var COMP_W = 100, COMP_H = 19, COMP_COLS = 3, COMP_DX = -16, COMP_DROP = 34;

  /* The chain, in the order ISA-Tox states it.
   *
   * `of` is the process additionalType that owns a step rank; a material rank
   * is named by the step it comes out of, which is why the material ranks carry
   * `from` rather than a type of their own — a cultured sample is a plain
   * `Sample` in the crate and only its position in the chain says more.
   */
  var RANKS = [
    { key: 'cellline',  label: 'CELL LINE', kind: 'material', seed: 'CellLineSample' },
    { key: 'culture',   label: 'CULTURE',   kind: 'process',  of: 'CellCulture' },
    { key: 'cultured',  label: 'CULTURED',  kind: 'material', from: 'CellCulture' },
    { key: 'exposure',  label: 'EXPOSURE',  kind: 'process',  of: 'Exposure' },
    { key: 'exposed',   label: 'EXPOSED',   kind: 'material', from: 'Exposure' },
    { key: 'readout',   label: 'READOUT',   kind: 'process',  of: 'EndpointReadout' },
    { key: 'raw',       label: 'RAW',       kind: 'material', from: 'EndpointReadout' },
    { key: 'analysis',  label: 'ANALYSIS',  kind: 'process',  of: 'DataAnalysis' },
    { key: 'processed', label: 'PROCESSED', kind: 'material', from: 'DataAnalysis' }
  ];


  function has(edge, label) {
    return (edge.labels || []).indexOf(label) >= 0;
  }

  /* A node's additionalType, which the payload appends to its type after a
   * middle dot ("Sample · CellLineSample"). Absent for a plain entity. */
  function addType(node) {
    var parts = String((node && node.type) || '').split('·');
    return parts.length > 1 ? parts[parts.length - 1].trim() : parts[0].trim();
  }

  function indexOfRank(key) {
    for (var i = 0; i < RANKS.length; i++) if (RANKS[i].key === key) return i;
    return -1;
  }

  /* Which rank each visible id belongs to.
   *
   * Steps go by their additionalType. Materials go by the step they came OUT
   * of, because that is what distinguishes a cultured sample from an exposed
   * one when the crate types both as `Sample`. A material no step produced is a
   * seed — the cell line an assay starts from — and anything left over has no
   * place on the chain and is not drawn.
   */
  function assign(visible, edges, nodes) {
    var rank = new Map();
    var stepType = new Map();

    visible.forEach(function (id) {
      var node = nodes && nodes.get(id);
      if (!node || node.category !== 'process') return;
      var at = addType(node);
      var i = indexOfRank(RANKS.filter(function (r) { return r.of === at; })
        .map(function (r) { return r.key; })[0]);
      if (i >= 0) { rank.set(id, i); stepType.set(id, at); }
    });
    // Without a single recognised step there is no chain to draw.
    if (!stepType.size) return null;

    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'result') || !stepType.has(e.src)) return;
      var i = indexOfRank(RANKS.filter(function (r) { return r.from === stepType.get(e.src); })
        .map(function (r) { return r.key; })[0]);
      if (i >= 0) rank.set(e.dst, i);
    });

    // A material a recognised step consumes and nothing produced starts the
    // chain. Its rank is the one before the step that took it in, so a deposit
    // that hands a File straight to an analysis still reads left to right.
    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'input') || rank.has(e.src) || !rank.has(e.dst)) return;
      rank.set(e.src, Math.max(0, rank.get(e.dst) - 1));
    });

    return { rank: rank, stepType: stepType };
  }

  /* The band, and what each member hangs from — unchanged in meaning from
   * `assay_lane_layout.js`: a protocol hangs from the step that executes it, a
   * compound from the protocol that lists it as a reagent, and `reagent` is
   * drawn reversed so the arrow points at the work that consumes the material
   * (#650). Ties break by id so two builds of one deposit draw alike. */
  function bandOf(visible, edges, rank) {
    var protocols = new Map();
    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'executes') || !rank.has(e.src)) return;
      var current = protocols.get(e.dst);
      if (current === undefined || e.src < current) protocols.set(e.dst, e.src);
    });
    var compounds = new Map();
    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'reagent') || !protocols.has(e.dst)) return;
      var current = compounds.get(e.src);
      if (current === undefined || e.dst < current) compounds.set(e.src, e.dst);
    });
    return { protocols: protocols, compounds: compounds };
  }

  function group(pairs) {
    var out = new Map();
    Array.from(pairs.keys()).sort().forEach(function (id) {
      var key = pairs.get(id);
      if (!out.has(key)) out.set(key, []);
      out.get(key).push(id);
    });
    return out;
  }

  /* Which row of its column each node takes.
   *
   * A rank sorted by id is a rank sorted by nothing a reader can see: an assay
   * that cultures three lines draws three parallel tracks, and ids that happen
   * to sort in a different order from their cultures' braid them together. Every
   * crossing then says "these two are related" and none of them are.
   *
   * So a rank takes its order from the rank before it — each node sits opposite
   * whatever it came from, ties broken by id so two builds of one deposit draw
   * alike. Where the chain genuinely fans (three samples into one exposure and
   * out again) nothing determines the order and the id decides; that crossing is
   * the deposit's, not the drawing's.
   */
  function rows(members, edges) {
    var beside = new Map();
    edges.forEach(function (e) {
      if (!beside.has(e.src)) beside.set(e.src, []);
      if (!beside.has(e.dst)) beside.set(e.dst, []);
      beside.get(e.src).push(e.dst);
      beside.get(e.dst).push(e.src);
    });
    var row = new Map();
    var previous = null;
    return members.map(function (ids) {
      var ordered = ids.slice().sort();
      if (previous) {
        var anchor = new Map();
        var left = new Set(previous);
        ordered.forEach(function (id) {
          var seen = (beside.get(id) || []).filter(function (n) { return left.has(n); });
          if (!seen.length) return;
          var total = 0;
          seen.forEach(function (n) { total += row.get(n); });
          anchor.set(id, total / seen.length);
        });
        ordered.sort(function (a, b) {
          var x = anchor.has(a) ? anchor.get(a) : Infinity;
          var y = anchor.has(b) ? anchor.get(b) : Infinity;
          if (x !== y) return x - y;
          return a < b ? -1 : (a > b ? 1 : 0);
        });
      }
      ordered.forEach(function (id, i) { row.set(id, i); });
      // The last NON-EMPTY rank, so a deposit that ran no exposure still has its
      // exposed-sample column ordered against the samples it actually follows.
      if (ordered.length) previous = ordered;
      return ordered;
    });
  }

  /**
   * Draw one assay as a lane, or decline the graph.
   *
   * @param {Set<string>} visible ids to place.
   * @param {Array<{src: string, dst: string, labels: Array<string>}>} edges
   * @param {Map<string, {category: string, type: string}>} nodes what each id is.
   * @param {Array<string>|Set<string>} reversed labels the model draws against
   *   their own predicate; the payload's `relations_reversed`.
   * @returns {{ranks: Array, positions: Map, edges: Array, band: Object,
   *   bandTop: number, width: number, height: number}|null} the drawing, or
   *   null when this is not a chain.
   */
  function build(visible, edges, nodes, reversed) {
    if (!visible || !visible.size) return null;
    edges = edges || [];
    var against = new Set(reversed || []);

    var placed = assign(visible, edges, nodes);
    if (!placed) return null;
    var band = bandOf(visible, edges, placed.rank);

    var unordered = RANKS.map(function () { return []; });
    Array.from(placed.rank.keys()).sort().forEach(function (id) {
      if (band.protocols.has(id) || band.compounds.has(id)) return;
      unordered[placed.rank.get(id)].push(id);
    });
    var members = rows(unordered, edges);

    // Every rank is a column at a fixed x; the tallest decides the spine's
    // height and the shorter ones centre against it, so the chain reads as one
    // horizontal line rather than as a staircase.
    var tallest = 0;
    members.forEach(function (ids) {
      tallest = Math.max(tallest, ids.length * NODE_H + Math.max(0, ids.length - 1) * GAP_Y);
    });
    var middle = TOP + tallest / 2;

    var positions = new Map();
    var columnX = [];
    members.forEach(function (ids, i) {
      var x = MARGIN + i * (NODE_W + GAP_X);
      columnX.push(x);
      var height = ids.length * NODE_H + Math.max(0, ids.length - 1) * GAP_Y;
      var y = middle - height / 2;
      ids.forEach(function (id) {
        positions.set(id, { x: x, y: y, w: NODE_W, h: NODE_H });
        y += NODE_H + GAP_Y;
      });
    });

    /* Tier one: a protocol under the step that executes it, in that step's own
     * column and in that step's own ROW of the band.
     *
     * Steps that share a rank share a column, so their protocols do too, and
     * the nth protocol down belongs to the nth step from the top.
     */
    var bandTop = TOP + tallest + BAND_DROP;
    var floor = bandTop;
    var rowOf = new Map();
    var hangs = [];
    var byStep = group(band.protocols);
    var steps = [];
    members.forEach(function (ids, rank) {
      ids.forEach(function (id) { if (byStep.has(id)) steps.push(id); });
    });
    steps.forEach(function (step) {
      var at = positions.get(step);
      var y = rowOf.has(at.x) ? rowOf.get(at.x) : bandTop;
      byStep.get(step).forEach(function (id) {
        positions.set(id, { x: at.x + PROTO_DX, y: y, w: PROTO_W, h: PROTO_H });
        hangs.push({ id: id, anchor: step, label: 'executes', tier: 1, column: at.x });
        y += PROTO_H + GAP_Y;
      });
      rowOf.set(at.x, y);
      floor = Math.max(floor, y);
    });

    // Tier two: the substances a protocol lists, under that protocol, three
    // across — a compound is one word, and a single file down a column would
    // make the band taller than the chain it annotates.
    group(band.compounds).forEach(function (ids, protocol) {
      var at = positions.get(protocol);
      if (!at) return;
      var base = Math.max(floor, at.y + at.h + COMP_DROP);
      ids.forEach(function (id, i) {
        positions.set(id, {
          x: at.x + COMP_DX + (i % COMP_COLS) * (COMP_W + 7),
          y: base + Math.floor(i / COMP_COLS) * (COMP_H + 6),
          w: COMP_W, h: COMP_H
        });
        hangs.push({
          id: id, anchor: protocol, label: 'reagent', tier: 2, column: at.x - PROTO_DX
        });
      });
      floor = Math.max(floor, base + Math.ceil(ids.length / COMP_COLS) * (COMP_H + 6));
    });

    /* Where each connector runs down.
     *
     * NOT down the anchor's own box: a step in the top row of a three-row rank
     * would have its line pass straight through the two steps below it, and a
     * line crossing a box reads as an edge to that box. It runs down the gap to
     * the LEFT of the column instead, where nothing is drawn — one vertical per
     * anchor, spread across the gap so anchors sharing a column stay apart, and
     * the renderer brackets it into both boxes' sides.
     */
    var perColumn = new Map();
    hangs.forEach(function (hang) {
      if (!perColumn.has(hang.column)) perColumn.set(hang.column, []);
      var seen = perColumn.get(hang.column);
      if (seen.indexOf(hang.anchor) < 0) seen.push(hang.anchor);
    });
    hangs.forEach(function (hang) {
      var seen = perColumn.get(hang.column);
      var slot = seen.indexOf(hang.anchor) + 1;
      hang.x = hang.column - GAP_X + (GAP_X * slot) / (seen.length + 1);
    });

    // Only edges between two drawn nodes, each said in the direction the crate
    // states it. `subject` is the end the triple is asserted FROM, so a label
    // can be oriented without the renderer re-deriving which relations reverse.
    var drawn = [];
    edges.forEach(function (e) {
      if (!positions.has(e.src) || !positions.has(e.dst)) return;
      (e.labels || []).forEach(function (label) {
        var back = against.has(label);
        drawn.push({
          src: e.src, dst: e.dst, label: label, reversed: back,
          subject: back ? e.dst : e.src
        });
      });
    });

    var width = 0, height = 0;
    positions.forEach(function (p) {
      width = Math.max(width, p.x + p.w);
      height = Math.max(height, p.y + p.h);
    });

    return {
      ranks: RANKS.map(function (r, i) {
        return {
          key: r.key, label: r.label, kind: r.kind, x: columnX[i], members: members[i]
        };
      }),
      positions: positions,
      edges: drawn,
      // What hangs BELOW the chain, and off what — named rather than inferred
      // from a y coordinate, because the section lets a reader put the band
      // away and "everything under `bandTop`" stops being true the moment the
      // drawing changes shape. Each entry carries the relation that puts it
      // there, so the connector can be labelled without re-deriving it.
      band: hangs,
      // Where the chain ends and the band begins. The chain is what the generic
      // canvas draws too, so it is the half the two layouts can be compared on;
      // the band is information the canvas scatters into ranks instead.
      bandTop: bandTop,
      width: width + MARGIN,
      height: height + MARGIN
    };
  }

  return { build: build, NODE_W: NODE_W, NODE_H: NODE_H, RANKS: RANKS };
}));
