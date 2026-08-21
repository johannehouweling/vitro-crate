/*
 * Runs the explorer's shipped layout module over a graph handed in on stdin and
 * prints the positions it computed. The tests measure the real code this way
 * rather than keeping a second copy of the geometry in Python.
 *
 * argv[2]: path to builder/writers/entity_explorer_layout.js
 * stdin:   {"nodes": [id, ...], "edges": [{"src": id, "dst": id}, ...]}
 * stdout:  {"positions": {id: {x, y}}, "nodeW": n, "nodeH": n}
 */
var L = require(process.argv[2]);
var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
var pos = L.layout(new Set(input.nodes), input.edges);
var positions = {};
pos.forEach(function (p, id) { positions[id] = p; });
process.stdout.write(JSON.stringify({
  positions: positions, nodeW: L.NODE_W, nodeH: L.NODE_H
}));
