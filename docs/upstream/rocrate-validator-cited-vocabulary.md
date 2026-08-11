# Upstream request (draft)

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** to file
**Local workaround:** `profiles/validator.py::_patch_cited_vocabulary_exemption` — delete once this ships.

Everything below the line is the issue text.

---

## A way to extend the excluded-namespaces list

The shapes exclude a fixed set of namespaces from the entity checks. From
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

The same block appears in seven shape files, 34 filters in all, at both SHOULD and
MUST severity:

| file | filters |
|---|---:|
| `profiles/ro-crate/1.2/must/6_contextual_entity_metadata.ttl` | 18 |
| `profiles/ro-crate/1.2/should/0_entity_metadata.ttl` | 8 |
| `profiles/ro-crate/1.2/must/4_data_entity_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/6_organization_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/4_data_entity_metadata.ttl` | 1 |
| `profiles/ro-crate/1.2/should/4_dataset_data_entity.ttl` | 1 |
| `profiles/ro-crate/1.1/must/4_data_entity_metadata.ttl` | 2 |

The idea behind it is right: an IRI that a crate only cites is not an entity the
crate has to describe.

The difficulty is that the set is fixed. Crates in different fields cite different
vocabularies, and any namespace not on this list is reported as an entity missing
a type, a name and a description. Those findings cannot be resolved, because the
terms are defined and maintained elsewhere. In one crate we saw around 20 such
IRIs produce 87 findings.

Adding namespaces one at a time means a release each time, and the list can never
be complete. It also has to be added in several places at once, and the 1.1 and
1.2 profiles carry their own copies.

**Could the list be made extensible?** A setting would fit well beside the options
that already exist:

```python
ValidationSettings(
    rocrate_uri=...,
    excluded_namespaces=["https://example.org/some-vocabulary/"],
)
```

Two things we noticed while looking at this:

- `profiles/ro-crate/1.2/should/6_contextual_entity_metadata.ttl` has no namespace
  filters at all, so a cited IRI is excluded from "must have a name" but still
  reported by "should be described in the same @graph". A shared list would make
  those two agree.
- Matching on a full prefix rather than a host would help. Some sites publish
  vocabulary and data under the same host, where the vocabulary is only cited but
  the data entities can genuinely be described.
