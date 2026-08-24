/*
 * Runs the shipped assay-lane layout over a graph handed in on stdin and prints
 * what it computed. As with `layout_probe.js`, the tests measure the real module
 * the report carries rather than a Python restatement of its geometry.
 *
 * argv[2]: path to builder/writers/assay_lane_layout.js
 * stdin:   {"nodes": [{"id":…, "category":…, "type":…}, …],
 *           "edges": [{"src":…, "dst":…, "labels":[…]}, …]}
 * stdout:  {"positions": {id: {x, y}} | null, "nodeW": n, "nodeH": n}
 *
 * `positions: null` is the module declining the graph — the caller falls back
 * to the generic canvas, so a null here is a result, not a failure.
 */
var L = require(process.argv[2]);
var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var nodes = new Map(input.nodes.map(function (n) { return [n.id, n]; }));
var pos = L.layout(new Set(nodes.keys()), input.edges, nodes);
var positions = null;
if (pos) {
  positions = {};
  pos.forEach(function (p, id) { positions[id] = p; });
}
process.stdout.write(JSON.stringify({
  positions: positions, nodeW: L.NODE_W, nodeH: L.NODE_H
}));
