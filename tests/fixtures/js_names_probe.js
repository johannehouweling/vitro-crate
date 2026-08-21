/*
 * Reports the names a browser script references but never defines.
 *
 * A Python test can read the file but cannot tell a call from a definition;
 * node can, because it is the thing that will run it. The script's own
 * functions live inside an IIFE and are never exported, so nothing else can
 * observe that one of them has gone — until the page throws ReferenceError in
 * front of a reader.
 *
 * argv[2]: the script to check.  argv[3...]: names the page supplies from
 * elsewhere (the vendored globals).
 * stdout:  {"free": [name, ...]} — referenced, declared nowhere.
 */
var fs = require('fs');
var vm = require('vm');
var source = fs.readFileSync(process.argv[2], 'utf8');

// Syntax first: a script that will not parse never reaches the name check.
new vm.Script(source, { filename: process.argv[2] });

var declared = new Set(process.argv.slice(3));
var ident = /[A-Za-z_$][\w$]*/g;

// `function NAME(a, b)` — the name, and the parameters it binds.
var fn = /\bfunction\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)/g;
var m;
while ((m = fn.exec(source)) !== null) {
  if (m[1]) declared.add(m[1]);
  (m[2].match(ident) || []).forEach(function (n) { declared.add(n); });
}
// `var a = …, b = …` and `var [a, b] = …`: every name a var statement binds.
// Taken greedily — over-declaring costs a false pass on one name, while
// under-declaring reports a name that is fine, and this file's whole job is to
// be believed when it says something is missing.
var vars = /\bvar\s+([^;\n]*(?:\n\s{6,}[^;\n]*)*);?/g;
while ((m = vars.exec(source)) !== null) {
  m[1].split('=')[0].split(',').forEach(function (part) {
    (part.match(ident) || []).forEach(function (n) { declared.add(n); });
  });
  // Names bound after the first `=` are still binders in a multi-declarator
  // list: `var a = 1, b = 2` binds b too.
  m[1].split(',').slice(1).forEach(function (part) {
    var head = part.split('=')[0];
    (head.match(ident) || []).forEach(function (n) { declared.add(n); });
  });
}
// `catch (err)`
var caught = /\bcatch\s*\(\s*([A-Za-z_$][\w$]*)/g;
while ((m = caught.exec(source)) !== null) declared.add(m[1]);

// A reference: called as a bare name, or used as an htm component. Prose is
// not a reference — "the entity explorer (#615)" reads as a call to anything
// scanning for `name(` — so comments come out first. Whole-line and block
// comments only: a `//` inside a string is part of a URL, not a comment.
var code = source
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n')
  .filter(function (line) { return !/^\s*(\/\/|\*)/.test(line); })
  .join('\n');
var used = new Set();
var calls = /(?:^|[^.\w$'"`])([A-Za-z_$][\w$]*)\s*\(/gm;
while ((m = calls.exec(code)) !== null) used.add(m[1]);
var components = /<\/?\$\{([A-Za-z_$][\w$]*)\}/g;
while ((m = components.exec(code)) !== null) used.add(m[1]);

var KEYWORDS = new Set([
  'function', 'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof',
  'new', 'delete', 'void', 'in', 'of', 'do', 'else', 'try', 'throw', 'case',
  'true', 'false', 'null', 'undefined', 'this'
]);
var free = [];
used.forEach(function (name) {
  if (!declared.has(name) && !KEYWORDS.has(name) && !(name in globalThis)) free.push(name);
});
process.stdout.write(JSON.stringify({ free: free.sort() }));
