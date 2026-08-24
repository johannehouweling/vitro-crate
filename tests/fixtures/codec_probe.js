/*
 * Expands a compacted explorer payload with the shipped codec and prints the
 * result, so the round trip is checked through the code the page runs rather
 * than a Python restatement of it.
 *
 * argv[2]: path to builder/writers/payload_codec.js
 * stdin:   the compacted payload
 * stdout:  the expanded payload
 */
var codec = require(process.argv[2]);
var compact = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(codec.expand(compact)));
