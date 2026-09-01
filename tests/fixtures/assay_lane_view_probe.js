/*
 * Runs the shipped assay-lane VIEW over a graph handed in on stdin and prints
 * the whole drawing — ranks, boxes, edges — plus the generic canvas's numbers
 * for the same graph, so a test can compare the two without a second opinion
 * about geometry.
 *
 * As with `assay_lane_probe.js`, the point is that the tests measure the real
 * module the report carries rather than a Python restatement of it.
 *
 * argv[2]: path to builder/writers/assay_lane_view.js
 * stdin:   {"nodes": [{"id":…, "category":…, "type":…}, …],
 *           "edges": [{"src":…, "dst":…, "labels":[…]}, …],
 *           "reversed": [label, …]}   the payload's relations_reversed
 * stdout:  {"ranks": [{key, label, members}],
 *           "positions": {id: {x, y, w, h}},
 *           "edges": [{src, dst, label, reversed, subject}],
 *           "bandTop", "width", "height", "crossings",
 *           "generic": {"width", "height", "crossings"}}
 */
var V = require(process.argv[2]);
var G = require(require('path').join(require('path').dirname(process.argv[2]),
  'entity_explorer_layout.js'));
var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var nodes = new Map(input.nodes.map(function (n) { return [n.id, n]; }));

var drawing = V.build(new Set(nodes.keys()), input.edges, nodes, input.reversed);

/* Segments that cross, counted the same way for both layouts so the comparison
 * is about the drawing and not about how each one is measured. */
function crossings(boxes, edges) {
  var seg = edges.map(function (e) {
    var a = boxes[e.src], b = boxes[e.dst];
    if (!a || !b) return null;
    return [a.x + a.w / 2, a.y + a.h / 2, b.x + b.w / 2, b.y + b.h / 2];
  }).filter(Boolean);
  function turn(ax, ay, bx, by, cx, cy) {
    var v = (by - ay) * (cx - bx) - (bx - ax) * (cy - by);
    return v > 0 ? 1 : (v < 0 ? -1 : 0);
  }
  var n = 0;
  for (var i = 0; i < seg.length; i++) {
    for (var j = i + 1; j < seg.length; j++) {
      var p = seg[i], q = seg[j];
      if (p[0] === q[0] && p[1] === q[1]) continue;
      if (p[2] === q[2] && p[3] === q[3]) continue;
      var d1 = turn(p[0], p[1], p[2], p[3], q[0], q[1]);
      var d2 = turn(p[0], p[1], p[2], p[3], q[2], q[3]);
      var d3 = turn(q[0], q[1], q[2], q[3], p[0], p[1]);
      var d4 = turn(q[0], q[1], q[2], q[3], p[2], p[3]);
      if (d1 !== d2 && d3 !== d4) n++;
    }
  }
  return n;
}

var positions = {};
if (drawing) {
  drawing.positions.forEach(function (p, id) { positions[id] = p; });
}

/* The generic canvas over the same graph, for the size and crossing comparison.
 * Material edges only, which is what it ranks on. */
var gpos = G.layout(new Set(nodes.keys()), input.edges.map(function (e) {
  return { src: e.src, dst: e.dst };
}));
var gboxes = {}, gw = 0, gh = 0;
gpos.forEach(function (p, id) {
  gboxes[id] = { x: p.x, y: p.y, w: G.NODE_W, h: G.NODE_H };
  gw = Math.max(gw, p.x + G.NODE_W);
  gh = Math.max(gh, p.y + G.NODE_H);
});

process.stdout.write(JSON.stringify({
  ranks: drawing ? drawing.ranks : null,
  positions: positions,
  edges: drawing ? drawing.edges : null,
  bandTop: drawing ? drawing.bandTop : null,
  width: drawing ? drawing.width : null,
  height: drawing ? drawing.height : null,
  crossings: drawing ? crossings(positions, drawing.edges) : null,
  // Split, because the two halves are drawn differently: the chain is solid
  // arrows a reader follows, the band is dashed connectors that qualify a step.
  chainCrossings: drawing ? crossings(positions, drawing.edges.filter(function (e) {
    return e.label !== 'executes' && e.label !== 'reagent';
  })) : null,

  generic: {
    width: gw,
    height: gh,
    crossings: crossings(gboxes, input.edges),
    // The same edge set the lane's chain figure counts, so the two numbers
    // compare like for like: the canvas has no band, it ranks protocols and
    // compounds into the chain, which is the difference being measured.
    chainCrossings: crossings(gboxes, input.edges.filter(function (e) {
      return (e.labels || []).some(function (l) {
        return l !== 'executes' && l !== 'reagent';
      });
    }))
  }
}));
