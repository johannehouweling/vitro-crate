/*
 * Loads the page's own script bodies the way a browser loads them, and reports
 * which globals they defined.
 *
 * The other probes `require()` the modules, which takes the CommonJS branch of
 * their UMD wrapper — the branch the report never runs. This one evaluates the
 * scripts in a context with no `module` and no `require`, so the browser branch
 * is what is exercised: a module that failed to attach itself to `window`, or
 * one loaded before the module it reads at factory time, fails here and
 * nowhere else in the suite.
 *
 * stdin:  {"scripts": [source, ...], "expect": [globalName, ...]}
 * stdout: {"defined": {name: true|false}, "sizes": {name: {NODE_W, NODE_H}}}
 */
var vm = require('vm');
var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));

var sandbox = { console: console };
sandbox.self = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
input.scripts.forEach(function (source, i) {
  vm.runInContext(source, sandbox, { filename: 'page-script-' + i + '.js' });
});

var defined = {}, sizes = {};
input.expect.forEach(function (name) {
  var value = sandbox[name];
  defined[name] = !!value;
  if (value && value.NODE_W !== undefined) {
    sizes[name] = { NODE_W: value.NODE_W, NODE_H: value.NODE_H };
  }
});
process.stdout.write(JSON.stringify({ defined: defined, sizes: sizes }));
