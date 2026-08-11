# Upstream request (draft)

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** to file
**Local workaround:** `profiles/validator.py::_patch_cited_vocabulary_exemption` — delete once this ships.

Everything below the line is the issue text.

---

## A way to extend the excluded-namespaces list

The shapes already exclude some namespaces from the entity checks. From
`profiles/ro-crate/1.2/should/0_entity_metadata.ttl`:

```sparql
# Exclude entities with non-IRI identifiers or those from specific namespaces
FILTER(!STRSTARTS(STR(?this), "http://www.w3.org/"))
FILTER(!STRSTARTS(STR(?this), "https://w3id.org/ro/crate/"))
FILTER(!STRSTARTS(STR(?this), "http://schema.org/"))
FILTER(!STRSTARTS(STR(?this), "http://purl.org/"))
FILTER(!STRSTARTS(STR(?this), "https://bioschemas.org/"))
FILTER(!STRSTARTS(STR(?this), "urn:"))
```

This is the right idea: an IRI a crate only cites is not an entity the crate has
to describe.

The problem is that the list is fixed. `http://purl.org/` is on it, so Dublin
Core is fine. `http://purl.obolibrary.org/` is a different host and is not on it,
so every OBO term a crate cites gets reported as missing a type, a name and a
description. In one crate we tested that was 87 findings from about 20 IRIs, none
of which we can fix — the terms belong to those ontologies.

Every field has its own registries, so adding entries one by one means a release
each time.

**Could we get a way to extend this list?** A setting would be ideal, next to the
options that already exist:

```python
ValidationSettings(
    rocrate_uri=...,
    cited_vocabulary_namespaces=["http://purl.obolibrary.org/obo/"],
)
```

Two notes if you take this up:

- `should/6_contextual_entity_metadata.ttl` has no namespace filters at all, so a
  cited term is excluded from "must have a name" but still reported by "should be
  described in the same @graph". A shared list would make those agree.
- Matching on the term path rather than the bare host matters. Some sites serve
  both, for example `https://aopwiki.org/ontology/KeyEvent` (a class, only cited)
  and `https://aopwiki.org/events/2266` (a specific event, which a crate can
  describe). Only the first should be excluded.
