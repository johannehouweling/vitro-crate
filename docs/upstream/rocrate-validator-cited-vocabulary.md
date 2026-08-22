# Upstream request

**Target:** [crs4/rocrate-validator](https://github.com/crs4/rocrate-validator)
**Status:** filed as [crs4/rocrate-validator#195](https://github.com/crs4/rocrate-validator/issues/195), open and unadopted. Local tracking issue #524 holds the follow-up if it lands.

Everything below the line is the issue text.

---

## Work out the excluded namespaces from the graph instead of listing them

### What happens today

The entity checks skip a fixed set of namespaces. From
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

The same block appears in seven files, 34 filters in total:

| file | filters |
|---|---:|
| `profiles/ro-crate/1.2/must/6_contextual_entity_metadata.ttl` | 18 |
| `profiles/ro-crate/1.2/should/0_entity_metadata.ttl` | 8 |
| `profiles/ro-crate/1.2/must/4_data_entity_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/6_organization_metadata.ttl` | 2 |
| `profiles/ro-crate/1.2/should/4_data_entity_metadata.ttl` | 1 |
| `profiles/ro-crate/1.2/should/4_dataset_data_entity.ttl` | 1 |
| `profiles/ro-crate/1.1/must/4_data_entity_metadata.ttl` | 2 |

The idea is right. A crate points at terms from other vocabularies, and it should
not have to describe them.

### The problem

The list names the vocabularies the authors had in mind. Any other one is
reported as an entity with no type, no name and no description. Those findings
cannot be fixed. The terms belong to someone else, and copying their labels into
the crate duplicates data that goes stale on the ontology's next release.

`http://purl.org/` is on the list, so Dublin Core terms pass. Ontology registries
on other hosts are not, so their terms are all reported. Every field has its own
registries, so adding entries means a release each time, in seven files, in two
profile versions.

And some cases cannot be written as a namespace at all. Take these two:

```
https://orcid.org                        an identifier scheme, only pointed at
https://orcid.org/0009-0000-5074-6239    a person, described in the crate
```

Excluding the prefix hides the people. Excluding neither reports the scheme.
There is no prefix that separates them.

### What could replace it

Whether a crate cites an IRI or describes it is already visible in the graph. An
IRI is only cited when both of these hold:

1. The crate says nothing about it — it is the subject of no triple except
   `rdf:type`.
2. Nothing points at it in a way that claims something. Every triple with it as
   the object uses a property that references rather than asserts:
   `schema:propertyID`, `csvw:propertyUrl`, `schema:inDefinedTermSet`,
   `dcterms:conformsTo`.

An IRI used only as a predicate or a type passes both without a special case,
because it is never the object of anything.

This covers the current list. `http://schema.org/name` is only ever a predicate.
`https://bioschemas.org/Sample` is only ever a type. Neither has to be named.

It also keeps the findings that matter. An entity referenced by
`schema:author` or `schema:hasPart` and never described is still reported,
because something claimed it exists. That is a real gap in the crate.

And it separates the two ORCID IRIs above without anyone deciding anything.

### How involved is it

Small. It changes the SPARQL targets that already exist, and nothing else — no
Python, no API, no new settings.

In each place the namespace filters appear now, this goes in their place:

```sparql
# Skip IRIs the crate only cites. Nothing is said about them, and every
# reference to them points rather than claims.
FILTER NOT EXISTS {
    $this ?p ?o .
    FILTER(?p != rdf:type)
}
FILTER NOT EXISTS {
    ?subject ?ref $this .
    FILTER(?ref NOT IN (schema:propertyID, csvw:propertyUrl,
                        schema:inDefinedTermSet, dcterms:conformsTo))
}
```

Roughly:

- seven files to edit, replacing 34 lines with about ten;
- `csvw:` needs adding to the shared prefixes if it is not there;
- the four properties are the only judgement call, and they are spec-defined
  rather than open-ended;
- `should/6_contextual_entity_metadata.ttl` has no filters today, so it would
  gain the exclusion and stop disagreeing with its neighbours;
- worth measuring the cost of the second `NOT EXISTS` on a large graph. It scans
  incoming edges per focus node, which is the one part that could be slow.

An implementation of this rule outside SHACL, over the same JSON-LD, took about
40 lines and behaved as described: on a 293-entity crate, 211 findings at the
RECOMMENDED gate separated into 112 about the crate and 99 about vocabulary it
cites. Everything kept was a real gap: people missing an email or a job title,
organizations, cell lines, a licence, a publication. The people under
`https://orcid.org/…` kept all their findings while the scheme root was excluded.

### A smaller option

If the rule is more than you want to take on, a setting that lets a caller add
namespaces to the current list would help a lot on its own:

```python
ValidationSettings(rocrate_uri=..., excluded_namespaces=[...])
```

It would not separate the two ORCID IRIs, but it would stop each field needing a
release.
