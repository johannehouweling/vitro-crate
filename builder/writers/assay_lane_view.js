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
 * Node size and the rank gap come from the shipped generic module, so a lane
 * and the canvas beside it cannot drift apart (the rule `assay_lane_layout.js`
 * set and this keeps).
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
    module.exports = factory(require('./entity_explorer_layout.js'));
  } else {
    root.AssayLaneView = factory(root.ExplorerLayout);
  }
}(typeof self !== 'undefined' ? self : this, function (ExplorerLayout) {
  'use strict';

  var NODE_W = ExplorerLayout.NODE_W, NODE_H = ExplorerLayout.NODE_H;
  var GAP_X = 58, GAP_Y = 12, MARGIN = 16, TOP = 28;
  // The drop from the spine's floor to the band. Wider than the gap inside the
  // band, so "below the chain" and "below a protocol" read as two relations
  // rather than one column of four things.
  var BAND_DROP = 48;
  var PROTO_H = 26, COMP_W = 100, COMP_H = 20;

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

  /**
   * Draw one assay as a lane, or decline the graph.
   *
   * @param {Set<string>} visible ids to place.
   * @param {Array<{src: string, dst: string, labels: Array<string>}>} edges
   * @param {Map<string, {category: string, type: string}>} nodes what each id is.
   * @param {Array<string>|Set<string>} reversed labels the model draws against
   *   their own predicate; the payload's `relations_reversed`.
   * @returns {{ranks: Array, positions: Map, edges: Array, width: number,
   *   height: number}|null} the drawing, or null when this is not a chain.
   */
  function build(visible, edges, nodes, reversed) {
    if (!visible || !visible.size) return null;
    edges = edges || [];
    var against = new Set(reversed || []);

    var placed = assign(visible, edges, nodes);
    if (!placed) return null;
    var band = bandOf(visible, edges, placed.rank);

    var members = RANKS.map(function () { return []; });
    Array.from(placed.rank.keys()).sort().forEach(function (id) {
      if (band.protocols.has(id) || band.compounds.has(id)) return;
      members[placed.rank.get(id)].push(id);
    });

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

    // Tier one: a protocol under the step that executes it, in that step's own
    // column. Several protocols under one step stack down the column; several
    // steps in one rank each keep their own row, because the rows are dealt per
    // column rather than per anchor.
    var bandTop = TOP + tallest + BAND_DROP;
    var floor = bandTop;
    var rowOf = new Map();
    group(band.protocols).forEach(function (ids, step) {
      var at = positions.get(step);
      if (!at) return;
      var y = rowOf.has(at.x) ? rowOf.get(at.x) : bandTop;
      ids.forEach(function (id) {
        positions.set(id, { x: at.x, y: y, w: NODE_W, h: PROTO_H });
        y += PROTO_H + GAP_Y;
      });
      rowOf.set(at.x, y);
      floor = Math.max(floor, y);
    });

    // Tier two: the substances a protocol lists, under that protocol, dealt
    // into the width of one column so the lane stays as wide as its chain.
    var cols = Math.max(1, Math.floor((NODE_W + GAP_X) / (COMP_W + GAP_Y)));
    group(band.compounds).forEach(function (ids, protocol) {
      var at = positions.get(protocol);
      if (!at) return;
      var base = Math.max(floor, at.y + at.h) + GAP_Y;
      ids.forEach(function (id, i) {
        positions.set(id, {
          x: at.x + (i % cols) * (COMP_W + GAP_Y),
          y: base + Math.floor(i / cols) * (COMP_H + GAP_Y),
          w: COMP_W, h: COMP_H
        });
      });
      floor = Math.max(floor, base + Math.ceil(ids.length / cols) * (COMP_H + GAP_Y));
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
        return { key: r.key, label: r.label, x: columnX[i], members: members[i] };
      }),
      positions: positions,
      edges: drawn,
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
