/*
 * The wire format the explorer's data island carries (#694).
 *
 * `build_explorer_payload` is the readable model — ids everywhere, one dict per
 * edge — and that is what every Python consumer reads. What the PAGE carries is
 * a compacted encoding of the same thing, because a mean `@id` in a real crate
 * is 53 characters and the model repeats each one once per edge endpoint and
 * once per view or lane membership: some 1,800 copies of strings already sitting in
 * `nodes`, on a report that ships inside the crate and is opened from disk, so
 * no transfer encoding ever squeezes them.
 *
 * This is the other half of `_compact` in `entity_explorer.py`, and the two are
 * inverses — a round-trip test runs THIS file over what Python produced, so the
 * pair is checked rather than each against its own mirror.
 *
 * Pure: no DOM, no React, no payload of its own. It runs before anything else
 * touches the data, and everything downstream sees the model as Python built it.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.PayloadCodec = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /**
   * Restore a compacted payload to the model Python built.
   *
   * @param {Object} d the parsed data island.
   * @returns {Object} the same object, with ids and names put back.
   */
  function expand(d) {
    var ids = d.nodes.map(function (n) { return n.id; });
    d.nodes.forEach(function (n) {
      // `name` is the crate's own wording and `label` is the badged form; they
      // are equal on almost every entity, so equality is encoded as absence.
      if (n.name === undefined) n.name = n.label;
    });
    d.edges = d.edges.map(function (e) {
      return { src: ids[e[0]], dst: ids[e[1]], label: e[2] };
    });
    [d.views, d.lanes || []].forEach(function (group) {
      group.forEach(function (v) {
        v.members = v.members.map(function (i) { return ids[i]; });
      });
    });
    return d;
  }

  return { expand: expand };
}));
