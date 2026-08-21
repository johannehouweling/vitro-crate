/*
 * Where the entity explorer's nodes go (#615, #619).
 *
 * Pure geometry: it takes a set of node ids and the edges between them and
 * returns a position per id. No DOM, no React, no payload — which is what lets
 * a test run the shipped code over a real crate's graph instead of a copy of
 * it, and what keeps `entity_explorer.js` about the canvas rather than about
 * arithmetic.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./vendor/dagre.min.js'));
  } else {
    root.ExplorerLayout = factory(root.dagre);
  }
}(typeof self !== 'undefined' ? self : this, function (dagre) {
  'use strict';

  var NODE_W = 200, NODE_H = 44;
  var GAP_X = 12, GAP_Y = 12, RANK_GAP = 90, MARGIN = 24;

  // How many leaves a rank may hold before its column becomes a block. A
  // layered layout gives every node in a rank its own row, so a crate root that
  // `hasPart` sixty files lays out sixty rows tall however small the rest of the
  // graph is — 12,100px on a real deposit, in a 620px canvas (#619). Above this
  // many, the column is packed into a grid; at or below it the column is left
  // alone, because a column IS a rank and that is worth more than the pixels.
  var RANK_CAP = 12;

  function newGraph() {
    var g = new dagre.graphlib.Graph();
    g.setGraph({
      rankdir: 'LR', nodesep: GAP_Y, ranksep: RANK_GAP, marginx: MARGIN, marginy: MARGIN
    });
    g.setDefaultEdgeLabel(function () { return {}; });
    return g;
  }

  function positionsOf(g, ids) {
    var pos = new Map();
    ids.forEach(function (id) {
      var p = g.node(id);
      pos.set(id, { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 });
    });
    return pos;
  }

  /* The ranks worth packing, and the order their members keep.
   *
   * A leaf is a node nothing on the canvas hangs off: its row carries no
   * structure, only which entity it belongs to, and the edge already says that.
   * Members keep the order the layered pass gave them, so siblings the layout
   * put together stay together in the block.
   */
  function wideLeafRanks(g, ids) {
    var byRank = new Map();
    ids.forEach(function (id) {
      var rank = g.node(id).rank;
      if (!byRank.has(rank)) byRank.set(rank, []);
      if ((g.outEdges(id) || []).length === 0) byRank.get(rank).push(id);
    });
    var blocks = [];
    Array.from(byRank.keys()).sort(function (a, b) { return a - b; }).forEach(function (rank) {
      var leaves = byRank.get(rank);
      if (leaves.length <= RANK_CAP) return;
      leaves.sort(function (a, b) { return g.node(a).y - g.node(b).y; });
      blocks.push({ rank: rank, members: leaves });
    });
    return blocks;
  }

  /* The grid a block of *n* nodes is laid out on: as near square in pixels as a
   * whole number of columns allows, which is the shape that buys the most
   * height back for the least width. */
  function blockShape(n) {
    var cols = Math.max(1, Math.round(Math.sqrt(n * (NODE_H + GAP_Y) / (NODE_W + GAP_X))));
    var rows = Math.ceil(n / cols);
    return {
      cols: cols,
      width: cols * NODE_W + (cols - 1) * GAP_X,
      height: rows * NODE_H + (rows - 1) * GAP_Y
    };
  }

  function blockKey(index, taken) {
    // A stand-in is a graph key, not an entity: it must not be an id the crate
    // could also carry. Widened rather than trusted, so nothing can shadow it.
    var key = '(block ' + index + ')';
    while (taken.has(key)) key += ' ';
    return key;
  }

  /**
   * Position every id in *visible*, with wide ranks of leaves packed into grids.
   *
   * Runs the layered pass twice. The first says which rank each node is in and
   * which of them are leaves; every rank holding more than `RANK_CAP` leaves
   * becomes a block. The second pass then lays out the graph with each block
   * standing in as ONE node the size of the grid it will hold, and its members'
   * edges redirected to that stand-in — so rank order, the gap between ranks and
   * the room a block needs are still dagre's answers rather than arithmetic here
   * trying to reproduce them. The members are dealt into the box afterwards.
   *
   * @param {Set<string>|Array<string>} visible ids to place.
   * @param {Array<{src: string, dst: string}>} edges links between them.
   * @returns {Map<string, {x: number, y: number}>} top-left corner per id.
   */
  function layout(visible, edges) {
    var ids = Array.from(visible);
    var present = new Set(ids);
    var full = newGraph();
    ids.forEach(function (id) { full.setNode(id, { width: NODE_W, height: NODE_H }); });
    edges.forEach(function (e) {
      if (present.has(e.src) && present.has(e.dst)) full.setEdge(e.src, e.dst);
    });
    dagre.layout(full);

    var blocks = wideLeafRanks(full, ids);
    if (!blocks.length) return positionsOf(full, ids);

    var blockOf = new Map();
    blocks.forEach(function (b, i) {
      b.key = blockKey(i, present);
      b.shape = blockShape(b.members.length);
      b.members.forEach(function (id) { blockOf.set(id, b.key); });
    });

    var g = newGraph();
    ids.forEach(function (id) {
      if (!blockOf.has(id)) g.setNode(id, { width: NODE_W, height: NODE_H });
    });
    blocks.forEach(function (b) {
      g.setNode(b.key, { width: b.shape.width, height: b.shape.height });
    });
    var drawn = new Set();
    edges.forEach(function (e) {
      if (!present.has(e.src) || !present.has(e.dst)) return;
      var src = blockOf.get(e.src) || e.src, dst = blockOf.get(e.dst) || e.dst;
      // Two members of one block, or a member and its own block: that link is
      // inside the box, and a self-edge is not a layout constraint.
      if (src === dst) return;
      var key = src + ' ' + dst;
      if (drawn.has(key)) return;
      drawn.add(key);
      g.setEdge(src, dst);
    });
    dagre.layout(g);

    var pos = positionsOf(g, ids.filter(function (id) { return !blockOf.has(id); }));
    blocks.forEach(function (b) {
      var box = g.node(b.key);
      var left = box.x - b.shape.width / 2, top = box.y - b.shape.height / 2;
      b.members.forEach(function (id, i) {
        pos.set(id, {
          x: left + (i % b.shape.cols) * (NODE_W + GAP_X),
          y: top + Math.floor(i / b.shape.cols) * (NODE_H + GAP_Y)
        });
      });
    });
    return pos;
  }

  return { layout: layout, NODE_W: NODE_W, NODE_H: NODE_H, RANK_CAP: RANK_CAP };
}));
