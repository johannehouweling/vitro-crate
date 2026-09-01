/*
 * The inspector, and the words it reads the crate in — shared by both viewers.
 *
 * The entity explorer and the assay lanes draw two different pictures of one
 * crate, and a reader who clicks an entity in either wants the same answer: what
 * the crate records about it, what it links to, and the JSON-LD behind both. Two
 * copies of that panel is two panels that drift, and two copies of the
 * vocabulary — what `input` is called in the crate, which relations the model
 * draws against their own predicate — is how one page comes to say two things.
 *
 * So the panel is built here, in plain DOM, and each viewer mounts it into its
 * own `<aside>`. DOM rather than a component of either app's framework: the
 * explorer is React and the lanes are hand-built SVG, and the one thing they
 * both already have is an element to fill.
 *
 * No anchors, no link targets, nothing assigned to an element's HTML. The
 * payload carries the crate verbatim, `javascript:` URLs and all, so every value
 * here reaches the page as a text node and every reference is a button that
 * moves the selection (#169). A URL is offered as something to copy, which
 * navigates nowhere and executes nothing.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.ExplorerInspector = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function el(name, cls, text) {
    var node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  /**
   * An inspector over one crate's payload.
   *
   * @param {Object} D the expanded data island.
   * @returns {Object} the vocabulary helpers, plus `render`.
   */
  function create(D) {
    var NODE = new Map(D.nodes.map(function (n) { return [n.id, n]; }));
    var ENTITY = new Map(
      (D.document['@graph'] || []).filter(function (e) { return e && e['@id']; })
        .map(function (e) { return [e['@id'], e]; })
    );
    var OUT = new Map(), IN = new Map();
    D.edges.forEach(function (e) {
      if (!OUT.has(e.src)) OUT.set(e.src, []);
      if (!IN.has(e.dst)) IN.set(e.dst, []);
      OUT.get(e.src).push(e);
      IN.get(e.dst).push(e);
    });
    var REVERSED = new Set(D.relations_reversed || []);

    /* What an edge says it is, in the crate's own vocabulary (#688). The model's
     * labels are this codebase's words — an edge is `input` because that reads
     * well beside `result` — but the predicate the crate is serialized with is
     * `schema:object`. A reader who copies a name off a canvas has to find it in
     * the JSON-LD, so the payload carries the mapping. */
    function term(label) { return (D.relations && D.relations[label]) || label; }
    /* The same, said in the direction the ARROW runs. `input` and `reagent` are
     * drawn against their own predicate so the arrow points the way material
     * moves (#650); the bare term on such an arrow asserts the inverse triple,
     * and `←` marks the ones whose predicate runs back the other way. */
    function edgeTerm(label) { return (REVERSED.has(label) ? '← ' : '') + term(label); }
    /* What an entity KEY expands to. `input` and `object` are one predicate;
     * `studies`, `assays` and `hasPart` are another. A key the context does not
     * name expands under the crate's own @vocab. */
    function prop(key) {
      if (key.charAt(0) === '@') return key;
      return (D.properties && D.properties[key]) || (D.vocab_prefix + ':' + key);
    }
    function category(n) { return D.categories[n.category] || D.categories.ctx; }
    function layerName(n) {
      return n.layer ? D.layers[String(n.layer)] : 'Referenced, outside the crate';
    }
    function shortId(id) {
      try {
        var u = new URL(id);
        return u.hostname.replace(/^www\./, '') + u.pathname;
      } catch (err) { return decodeURIComponent(id); }
    }
    function isUrl(v) { return typeof v === 'string' && /^https?:\/\//.test(v); }

    function swatch(key) {
      var c = D.categories[key] || D.categories.ctx;
      var node = el('span', 'ex-swatch');
      node.setAttribute('aria-hidden', 'true');
      node.style.setProperty('--ex-c', c.colour);
      return node;
    }

    /* One row per predicate, not one per spelling: two aliases of one predicate
     * carry the same values, and showing them twice is what naming the predicate
     * was meant to stop. */
    function overviewRows(entity) {
      var byTerm = new Map();
      Object.keys(entity).forEach(function (key) {
        if (key === '@id') return;
        var name = prop(key);
        if (!byTerm.has(name)) {
          byTerm.set(name, { name: name, keys: [], values: [], seen: {} });
        }
        var row = byTerm.get(name);
        if (row.keys.indexOf(key) < 0) row.keys.push(key);
        (Array.isArray(entity[key]) ? entity[key] : [entity[key]]).forEach(function (v) {
          var seal = JSON.stringify(v);
          if (row.seen[seal]) return;
          row.seen[seal] = true;
          row.values.push(v);
        });
      });
      return Array.from(byTerm.values());
    }

    /**
     * Fill *container* with the panel for *opts.id*, or with the empty state.
     *
     * @param {Element} container the viewer's own `<aside>`.
     * @param {{id: ?string, onSelect: function, onClose: function}} opts
     */
    function render(container, opts) {
      var state = { container: container, opts: opts };
      var tab = container.__exTab || 'properties';

      function redraw(next) {
        container.__exTab = next || tab;
        render(state.container, state.opts);
      }
      function ref(id) {
        var target = NODE.get(id);
        if (!target) return el('span', 'ex-mono ex-muted', shortId(id));
        var button = el(
          'button', 'ex-ref' + (target.status === 'described' ? '' : ' ex-ref-outside'),
          target.label
        );
        button.type = 'button';
        button.title = id;
        button.addEventListener('click', function () { opts.onSelect(id); });
        return button;
      }
      /* A URL the crate carries, offered as something to take away rather than
       * somewhere to go: copying navigates nowhere and executes nothing, so the
       * reader gets the URL without the crate getting a way to run anything. */
      function url(value) {
        var button = el('button', 'ex-url ex-mono', value);
        button.type = 'button';
        button.title = 'Copy ' + value;
        button.addEventListener('click', function () {
          if (!navigator.clipboard) return;
          navigator.clipboard.writeText(value);
          button.classList.add('ex-url-copied');
          setTimeout(function () { button.classList.remove('ex-url-copied'); }, 1200);
        });
        return button;
      }
      function json(value, into) {
        if (value === null || value === undefined) {
          into.appendChild(el('span', 'ex-json-null', 'null'));
          return;
        }
        if (Array.isArray(value)) {
          into.appendChild(el('span', 'ex-json-punct', '['));
          var list = el('div', 'ex-json-indent');
          value.forEach(function (item, i) {
            var line = el('div');
            json(item, line);
            if (i < value.length - 1) line.appendChild(document.createTextNode(','));
            list.appendChild(line);
          });
          into.appendChild(list);
          into.appendChild(el('span', 'ex-json-punct', ']'));
          return;
        }
        if (typeof value === 'object') {
          var keys = Object.keys(value);
          if (keys.length === 1 && keys[0] === '@id') {
            into.appendChild(el('span', 'ex-json-punct', '{ '));
            into.appendChild(el('span', 'ex-json-key', '"@id"'));
            into.appendChild(document.createTextNode(': '));
            into.appendChild(ref(value['@id']));
            into.appendChild(el('span', 'ex-json-punct', ' }'));
            return;
          }
          into.appendChild(el('span', 'ex-json-punct', '{'));
          var block = el('div', 'ex-json-indent');
          keys.forEach(function (k, i) {
            var line = el('div');
            line.appendChild(el('span', 'ex-json-key', '"' + k + '"'));
            line.appendChild(document.createTextNode(': '));
            json(value[k], line);
            if (i < keys.length - 1) line.appendChild(document.createTextNode(','));
            block.appendChild(line);
          });
          into.appendChild(block);
          into.appendChild(el('span', 'ex-json-punct', '}'));
          return;
        }
        if (typeof value === 'string') {
          into.appendChild(el('span', 'ex-json-string', '"' + value + '"'));
          return;
        }
        into.appendChild(el('span', 'ex-json-number', String(value)));
      }

      /* A value as the Overview shows it: what the crate says, not how it is
       * serialized. A bare string is text — quoted and syntax-coloured it reads
       * as JSON, and there is a whole tab for that — and a lone `{"@id": …}` is
       * the reference itself. Anything with structure the panel cannot flatten
       * falls through to the JSON walker, which is honest about the shape. */
      function plain(value, into) {
        if (typeof value === 'string') {
          if (isUrl(value)) into.appendChild(url(value));
          else into.appendChild(document.createTextNode(value));
          return;
        }
        if (typeof value === 'number' || typeof value === 'boolean') {
          into.appendChild(document.createTextNode(String(value)));
          return;
        }
        if (value && typeof value === 'object' && !Array.isArray(value)
            && Object.keys(value).length === 1 && value['@id']) {
          into.appendChild(ref(value['@id']));
          return;
        }
        json(value, into);
      }

      clear(container);
      var id = opts.id;
      container.className = 'ex-side' + (id ? '' : ' ex-side-empty');
      if (!id || !NODE.has(id)) {
        var hint = el('p', 'ex-hint',
          'Select an entity to read its properties, its links and its JSON-LD.');
        container.appendChild(hint);
        return;
      }

      var n = NODE.get(id);
      var entity = ENTITY.get(id);
      var outgoing = OUT.get(id) || [], incoming = IN.get(id) || [];

      var head = el('div', 'ex-side-head');
      var title = el('div', 'ex-side-title');
      title.appendChild(swatch(n.category));
      title.appendChild(el('span', 'ex-eyebrow', category(n).label));
      var close = el('button', 'ex-close', '×');
      close.type = 'button';
      close.title = 'Clear selection';
      close.setAttribute('aria-label', 'Clear selection');
      close.addEventListener('click', function () { opts.onClose(); });
      title.appendChild(close);
      head.appendChild(title);
      head.appendChild(el('h3', 'ex-side-name', n.label));
      var idLine = el('div', 'ex-side-id ex-mono', shortId(n.id));
      idLine.title = n.id;
      head.appendChild(idLine);

      var flags = el('div', 'ex-flags');
      flags.appendChild(el('span', 'ex-tag', layerName(n)));
      if (n.orphan) flags.appendChild(el('span', 'ex-tag ex-tag-bad', 'unreachable from the root'));
      if (n.status === 'dangling') {
        flags.appendChild(el('span', 'ex-tag ex-tag-bad', 'nothing describes this id'));
      }
      if (n.status === 'external') {
        flags.appendChild(el('span', 'ex-tag', 'described outside the crate'));
      }
      if (n.status === 'described' && !n.identifier_backed) {
        flags.appendChild(el('span', 'ex-tag', 'no persistent identifier'));
      }
      head.appendChild(flags);

      var tabs = el('div', 'ex-side-tabs');
      tabs.setAttribute('role', 'tablist');
      [
        ['properties', 'Overview'],
        ['links', 'Links (' + (outgoing.length + incoming.length) + ')'],
        ['json', 'JSON-LD']
      ].forEach(function (pair) {
        var button = el('button', null, pair[1]);
        button.type = 'button';
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-selected', String(tab === pair[0]));
        button.addEventListener('click', function () { redraw(pair[0]); });
        tabs.appendChild(button);
      });
      head.appendChild(tabs);
      container.appendChild(head);

      var body = el('div', 'ex-side-body');
      if (tab === 'properties') {
        if (entity) {
          var list = el('dl', 'ex-props');
          overviewRows(entity).forEach(function (row) {
            var pair = el('div', 'ex-prop');
            var dt = el('dt', 'ex-mono', row.name);
            dt.title = 'in the crate as: ' + row.keys.join(', ');
            pair.appendChild(dt);
            var dd = el('dd');
            row.values.forEach(function (v) {
              var line = el('div');
              plain(v, line);
              dd.appendChild(line);
            });
            pair.appendChild(dd);
            list.appendChild(pair);
          });
          body.appendChild(list);
        } else {
          body.appendChild(el('p', 'ex-hint',
            'This entity is described outside the crate; the crate carries the '
            + 'reference only.'));
        }
      } else if (tab === 'links') {
        [[outgoing, 'out'], [incoming, 'in']].forEach(function (side) {
          var byRelation = new Map();
          side[0].forEach(function (e) {
            var other = side[1] === 'out' ? e.dst : e.src;
            if (!byRelation.has(e.label)) byRelation.set(e.label, []);
            byRelation.get(e.label).push(other);
          });
          byRelation.forEach(function (ids, label) {
            var group = el('div', 'ex-link-group');
            var caption = el('div', 'ex-relation ex-mono');
            caption.title = label;
            caption.appendChild(
              document.createTextNode((side[1] === 'out' ? '→ ' : '← ') + term(label) + ' ')
            );
            caption.appendChild(el('span', 'ex-muted', '(' + ids.length + ')'));
            group.appendChild(caption);
            ids.forEach(function (other) {
              var line = el('div', 'ex-link');
              var target = NODE.get(other);
              if (target) line.appendChild(swatch(target.category));
              line.appendChild(ref(other));
              group.appendChild(line);
            });
            body.appendChild(group);
          });
        });
        if (!outgoing.length && !incoming.length) {
          body.appendChild(el('p', 'ex-hint', 'Nothing links to or from this entity.'));
        }
      } else {
        var block = el('div', 'ex-json');
        json(entity || { '@id': n.id }, block);
        body.appendChild(block);
      }
      container.appendChild(body);
    }

    return {
      term: term, edgeTerm: edgeTerm, prop: prop, category: category,
      layerName: layerName, shortId: shortId, swatch: swatch, render: render
    };
  }

  return { create: create };
}));
