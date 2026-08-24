/*
 * Where one assay's nodes go (#686).
 *
 * The generic canvas ranks by dependency, so a protocol lands in a rank to the
 * RIGHT of the step that executes it and the material chain a reader is
 * following is interrupted by something that is not material. This module
 * splits the two directions instead:
 *
 *   horizontal is the material chain; vertical is what qualifies a step.
 *
 *   spine  CellLine -> Culture -> Cultured -> Exposure -> Exposed -> Readout ...
 *              |                     |
 *   band       +-- culture protocol  +-- condition table
 *                                    +-- compounds (reagent)
 *
 * The spine itself is laid out by the SHIPPED generic module rather than by
 * geometry of its own: node size, rank gaps and the grid a wide rank of files
 * packs into are all one answer, given once, so a lane and the canvas beside it
 * cannot drift apart. What is left here is only the two-band split.
 *
 * Same contract as `entity_explorer_layout.js` — a position per id — plus one
 * addition: `null` means "this is not a lane", and the caller draws the graph on
 * the generic canvas. Declining is this module's job, not its caller's, so the
 * app never has to know which shapes are spines.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./entity_explorer_layout.js'));
  } else {
    root.AssayLaneLayout = factory(root.ExplorerLayout);
  }
}(typeof self !== 'undefined' ? self : this, function (ExplorerLayout) {
  'use strict';

  var NODE_W = ExplorerLayout.NODE_W, NODE_H = ExplorerLayout.NODE_H;
  var GAP_X = 12, GAP_Y = 12;
  // The drop from the spine's floor to the band. Wider than the gap between two
  // band tiers, so "below the chain" and "below a protocol" read as different
  // relations rather than as one column of four things.
  var BAND_DROP = 56;

  // The relations that carry material downstream — the app's own DERIVATION set.
  // `derivesFrom` is deliberately absent: a cultured sample derives from the
  // cell line the culture consumed, so the edge points back up the chain, and
  // ranking on it would put a node before its own ancestor.
  var MATERIAL = { input: 1, object: 1, result: 1, output: 1 };

  function has(edge, label) {
    return (edge.labels || []).indexOf(label) >= 0;
  }

  /* The band, and what each of its members hangs from.
   *
   * A protocol hangs from the step that executes it. A compound hangs from the
   * protocol that lists it as a reagent — the model draws `reagent` REVERSED
   * (src is the compound, dst the protocol) so the arrow points at the step that
   * consumes the material, which is also the only route a MolecularEntity has
   * into the derivation story at all (#650).
   *
   * Ties are broken by id rather than by edge order, so two builds of one
   * deposit produce the same picture.
   */
  function bandOf(visible, edges) {
    var anchor = new Map();
    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'executes')) return;
      var current = anchor.get(e.dst);
      if (current === undefined || e.src < current) anchor.set(e.dst, e.src);
    });
    var compounds = new Map();
    edges.forEach(function (e) {
      if (!visible.has(e.src) || !visible.has(e.dst)) return;
      if (!has(e, 'reagent') || !anchor.has(e.dst)) return;
      var current = compounds.get(e.src);
      if (current === undefined || e.dst < current) compounds.set(e.src, e.dst);
    });
    return { protocols: anchor, compounds: compounds };
  }

  /* Whether the spine is one story.
   *
   * Uses every edge between spine nodes, not only the material ones: an assay
   * hanging off its steps is what holds the whole-crate LabProcesses view
   * together, and that view is several assays at once. Two components mean two
   * stories, and laying them out as one spine would interleave them.
   */
  function connected(ids, edges) {
    if (ids.length < 2) return true;
    var present = new Set(ids);
    var near = new Map();
    ids.forEach(function (id) { near.set(id, []); });
    edges.forEach(function (e) {
      if (!present.has(e.src) || !present.has(e.dst) || e.src === e.dst) return;
      near.get(e.src).push(e.dst);
      near.get(e.dst).push(e.src);
    });
    var seen = new Set([ids[0]]), queue = [ids[0]];
    while (queue.length) {
      near.get(queue.pop()).forEach(function (next) {
        if (seen.has(next)) return;
        seen.add(next);
        queue.push(next);
      });
    }
    return seen.size === ids.length;
  }

  /* Deal *members* into a grid under *left*, kept inside the spine's own width.
   *
   * Twelve compounds in one row would be wider than the chain they qualify, so
   * the block wraps at whatever the spine can hold and is nudged left when it
   * would otherwise overhang — the lane's width stays the chain's width, which
   * is the property the two-band split exists to buy.
   */
  function dealt(members, left, top, span) {
    var cols = Math.max(1, Math.min(
      members.length,
      Math.floor((span + GAP_X) / (NODE_W + GAP_X))
    ));
    var width = cols * NODE_W + (cols - 1) * GAP_X;
    var start = Math.max(0, Math.min(left, span - width));
    var out = new Map();
    members.forEach(function (id, i) {
      out.set(id, {
        x: start + (i % cols) * (NODE_W + GAP_X),
        y: top + Math.floor(i / cols) * (NODE_H + GAP_Y)
      });
    });
    return out;
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
   * Position one assay's nodes as a lane, or decline the graph.
   *
   * @param {Set<string>} visible ids to place.
   * @param {Array<{src: string, dst: string, labels: Array<string>}>} edges
   * @param {Map<string, {category: string}>} nodes what each id is.
   * @returns {Map<string, {x: number, y: number}>|null} top-left corner per id,
   *   or null when the graph is not a single assay's chain.
   */
  function layout(visible, edges, nodes) {
    if (!visible || !visible.size) return null;
    edges = edges || [];

    var band = bandOf(visible, edges);
    var spine = Array.from(visible).filter(function (id) {
      return !band.protocols.has(id) && !band.compounds.has(id);
    }).sort();

    // A lane is a chain of work. Without a step there is no chain, and with two
    // components there are two chains.
    var steps = spine.filter(function (id) {
      return ((nodes && nodes.get(id)) || {}).category === 'process';
    });
    if (!steps.length) return null;
    if (!connected(spine, edges)) return null;

    var material = edges.filter(function (e) {
      if (spine.indexOf(e.src) < 0 || spine.indexOf(e.dst) < 0) return false;
      return (e.labels || []).some(function (l) { return MATERIAL[l] === 1; });
    }).map(function (e) { return { src: e.src, dst: e.dst }; });

    var pos = ExplorerLayout.layout(new Set(spine), material);
    var left = Infinity, right = -Infinity, floor = -Infinity;
    pos.forEach(function (p) {
      left = Math.min(left, p.x);
      right = Math.max(right, p.x + NODE_W);
      floor = Math.max(floor, p.y + NODE_H);
    });
    var span = right - left;

    // Tier one: a protocol directly under the step that executes it, so the
    // attachment is unambiguous without anything having to say so. Dealt rather
    // than anchored, because a step may execute several — a readout that
    // isolates RNA, makes cDNA and runs qPCR executes three — and giving each
    // its step's x would draw them all at one point.
    var top = floor + BAND_DROP;
    var deepest = top;
    group(band.protocols).forEach(function (members, step) {
      var at = pos.get(step);
      dealt(members, (at ? at.x : left) - left, top, span).forEach(function (p, id) {
        pos.set(id, { x: left + p.x, y: p.y });
        deepest = Math.max(deepest, p.y + NODE_H);
      });
    });

    // Tier two: the substances a protocol lists, under that protocol.
    group(band.compounds).forEach(function (members, protocol) {
      var at = pos.get(protocol);
      var block = dealt(members, (at ? at.x : left) - left, deepest + GAP_Y, span);
      block.forEach(function (p, id) { pos.set(id, { x: left + p.x, y: p.y }); });
    });
    return pos;
  }

  return { layout: layout, NODE_W: NODE_W, NODE_H: NODE_H, BAND_DROP: BAND_DROP };
}));
