"""The patterns an identifier must match, derived from the Bioregistry's records.

The ISA-Tox shapes check that an ORCID iD, a ROR ID, a Cellosaurus accession, a
CAS number or an ontology term IRI is well-formed. They do not decide what
well-formed means: the Bioregistry publishes, per prefix, the ``pattern`` of the
local identifier and the ``uri_format`` it expands to, and
``profiles/vocabulary/identifier_namespaces.json`` vendors those records
(written by ``scripts/refresh_identifier_namespaces.py``, run by hand). This
module derives the regular expressions from them and states which namespaces are
checked (``NAMESPACE_CHECKS``, ``VALUE_CHECKS``);
``scripts/gen_identifier_shapes.py`` writes
``profiles/shapes/tox/14_identifier_namespaces.ttl`` from the two, and
``tests/test_identifier_namespace_shapes.py`` holds every ``sh:pattern`` in the
tox shapes to a derivation here. A pattern nobody can trace to a record is a
pattern somebody typed.

The record names the scheme; it is never the identity. A Person is
``https://orcid.org/0000-…``, not ``https://bioregistry.io/registry/orcid:0000-…``
(``profiles/ontology_iris``). A shape names the record it took its pattern from
with ``rdfs:seeAlso``, which is a pointer on the shape and nowhere in a crate.

Every pattern is in the dialect ``sh:pattern`` uses, SPARQL's ``REGEX``, which
Python's ``re`` reads the same way for everything that occurs here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_SNAPSHOT = Path(__file__).resolve().parent / "vocabulary" / "identifier_namespaces.json"

REGISTRY_RECORD = "https://bioregistry.io/registry/"

# The metacharacters of the SPARQL/XPath regex dialect. Only these are escaped:
# `re.escape` also escapes `#`, `/`, `:` and `-`, and `\#` is not an escape the
# XPath dialect defines, so a strict engine refuses the BAO namespace written that
# way.
_METACHARACTERS = frozenset(".\\?*+{}()[]|^$")


@lru_cache(maxsize=1)
def snapshot() -> dict[str, Any]:
    """The vendored file, read once. A missing file is an error, not an empty map:
    the shapes are pinned to it, so without it there is nothing to check them
    against."""
    return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))


def records() -> dict[str, dict[str, str]]:
    """Every namespace the shapes may cite: the Bioregistry's records, then the
    project's own decisions for namespaces the registry does not hold."""
    data = snapshot()
    return {**data.get("prefixes", {}), **data.get("decisions", {})}


def record(prefix: str) -> dict[str, str]:
    """The record for *prefix*. Raises ``KeyError`` for one the snapshot lacks,
    which means it must be added to the refresh script first."""
    found = records().get(prefix)
    if found is None:
        raise KeyError(f"no vendored identifier namespace {prefix!r}")
    return found


def prefix_of_record(iri: str) -> str | None:
    """The prefix whose record *iri* names, as a shape's ``rdfs:seeAlso`` does."""
    for prefix, entry in records().items():
        if entry.get("record") == iri:
            return prefix
    return None


def escape(text: str) -> str:
    """*text* as a literal in a SHACL regular expression."""
    return "".join("\\" + c if c in _METACHARACTERS else c for c in text)


def _body(pattern: str) -> str:
    """*pattern* without the anchors that make it a whole-string match."""
    body = pattern[1:] if pattern.startswith("^") else pattern
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    return body


def uri_prefix(prefix: str) -> str:
    """What every IRI in *prefix*'s namespace starts with: its URI format up to ``$1``."""
    return record(prefix)["uri_format"].partition("$1")[0]


def value_pattern(prefix: str) -> str:
    """The pattern of a bare identifier in *prefix*: the registry's, with its
    anchors made to bind the whole of it.

    The registry's own pattern is not always a whole-string match. NCBITaxon's is
    ``^(\\d+)|([a-zA-Z_]+)$``, an alternation whose ``^`` belongs to one branch and
    whose ``$`` to the other, so it accepts ``12 and anything``. Grouping the body
    between the anchors is what the registry means by it.
    """
    return f"^({_body(record(prefix)['pattern'])})$"


def iri_pattern(prefix: str) -> str:
    """The pattern of an IRI in *prefix*'s namespace: the URI format with the
    identifier's pattern in place of ``$1``, grouped as in ``value_pattern``."""
    head, _, tail = record(prefix)["uri_format"].partition("$1")
    return f"^{escape(head)}({_body(record(prefix)['pattern'])}){escape(tail)}$"


def namespace_pattern(prefix: str) -> str:
    """The pattern of an IRI that is in *prefix*'s namespace at all, whatever
    follows: what "identified by an ORCID iD" asks before the form is checked."""
    return f"^{escape(uri_prefix(prefix))}"


# An accession some sources write under another registry's banana as well. Only a
# pairing that exists is listed: `RRID:CVCL_0440` is an identifier people write,
# `CHEBI:CVCL_0440` is not, and a pattern for it would be a derivation of nothing.
_ALSO_WRITTEN_UNDER: dict[str, tuple[str, ...]] = {"cellosaurus": ("rrid",)}


def accession_pattern(prefix: str, *, also_under: str | None = None) -> str:
    """The pattern of an identifier written with its banana, as a Cellosaurus
    accession is (``CVCL_0440``): the banana, its peel, then the identifier.

    *also_under* names a registry whose own banana may precede it, as an RRID
    writes the same accession ``RRID:CVCL_0440``.
    """
    entry = record(prefix)
    if "banana" not in entry:
        raise KeyError(f"{prefix!r} has no banana to write its identifier with")
    lead = ""
    if also_under is not None:
        if also_under not in _ALSO_WRITTEN_UNDER.get(prefix, ()):
            raise KeyError(f"{prefix!r} identifiers are not written under {also_under!r}")
        outer = record(also_under)
        lead = f"({escape(outer['banana'] + outer['banana_peel'])})?"
    own = escape(entry["banana"] + entry["banana_peel"])
    return f"^{lead}{own}({_body(entry['pattern'])})$"


def derived_patterns(prefix: str) -> set[str]:
    """Every pattern a shape may take from *prefix*'s record."""
    patterns = {value_pattern(prefix), iri_pattern(prefix), namespace_pattern(prefix)}
    if "banana" in record(prefix):
        patterns.add(accession_pattern(prefix))
        for outer in _ALSO_WRITTEN_UNDER.get(prefix, ()):
            patterns.add(accession_pattern(prefix, also_under=outer))
    return patterns


def example_iri(prefix: str) -> str:
    """A well-formed IRI in *prefix*'s namespace: the registry's own example in its
    URI format. What a message or a fix hint shows in place of a description of
    the form somebody would have had to type."""
    entry = record(prefix)
    return entry["uri_format"].replace("$1", entry["example"])


# --- what the ISA-Tox shapes check ------------------------------------------
#
# The two tables `scripts/gen_identifier_shapes.py` writes the shapes from. Adding
# a namespace is a line here (and, for a new prefix, one in
# `scripts/refresh_identifier_namespaces.py`), then a run of the generator.

# The namespaces an entity's ``@id`` is checked in, in the order the shape lists
# them: people and organisations, chemicals, cell lines, publications, the AOP and
# its key events, then the ontologies `profiles/ontology_iris` builds term IRIs in.
# Each is given what this project calls an ``@id`` there, which is the one thing
# here the registry does not supply: a fix hint and the report name the finding's
# namespace with it, since the shape's one message cannot.
NAMESPACE_CHECKS: dict[str, str] = {
    "orcid": "ORCID iD",
    "ror": "ROR ID",
    "pubchem.compound": "PubChem compound address",
    "compoundwiki": "Compound Wiki item address",
    "cellosaurus": "Cellosaurus address",
    "doi": "DOI address",
    "aop": "AOP-Wiki pathway address",
    "aop.events": "AOP-Wiki key event address",
    "bao": "BAO term",
    "chebi": "ChEBI term",
    "cl": "Cell Ontology term",
    "efo": "EFO term",
    "go": "Gene Ontology term",
    "iao": "IAO term",
    "msio": "MSIO term",
    "ncbitaxon": "NCBI Taxonomy term",
    "ncit": "NCIT term",
    "obi": "OBI term",
    "pato": "PATO term",
    "uberon": "UBERON term",
    "uo": "Units Ontology term",
}

# What puts an ``@id`` in a namespace is its URI format up to the identifier,
# except where that would let a malformed one out of the check. A Cellosaurus
# address is the host and then the accession with its banana
# (``…/CVCL_0440``); selecting on ``…/CVCL_`` would pass over ``…/HeLa``, which is
# exactly the address to report. The ontologies' formats also end in a banana
# (``…/obo/NCIT_``), and there it must stay: ``…/obo/ncit.owl`` is the ontology,
# not a term.
_NAMESPACE_STARTS: dict[str, str] = {"cellosaurus": "https://www.cellosaurus.org/"}

# The identifier PropertyValues whose ``value`` is checked, by the ``name`` that
# states their scheme: the names the builder writes (`_crate_mapping._identifier_pv`,
# `drafters.draft_property_value`).
VALUE_CHECKS: dict[str, str] = {
    "ORCID": "orcid",
    "CAS": "cas",
    "PubChem CID": "pubchem.compound",
    "DTXSID": "comptox",
    "Compound Wiki": "compoundwiki",
    "DOI": "doi",
    "PubMedID": "pubmed",
}

# The words a finding of these shapes is recognised by. The shapes' messages are
# built from them and the tooling reads them back (`validation` for the fix hint,
# `remediation` for the report's instruction, `agent_loop` for who still needs an
# identifier), so no second copy of a message can drift from the shape that
# speaks it.
MALFORMED = "MUST have the form its registry gives it"
PERSON_NEEDS_ORCID = "MUST be identified by an ORCID iD"
ORGANIZATION_NEEDS_ROR = "MUST be identified by a ROR ID"


def namespace_start(prefix: str) -> str:
    """What an ``@id`` starts with when it is in *prefix*'s namespace."""
    return _NAMESPACE_STARTS.get(prefix) or uri_prefix(prefix)


def namespace_of(iri: object) -> str | None:
    """The checked namespace *iri* is in, or ``None``.

    This is how a finding of the one namespace shape is traced back to the
    registry it concerns: the shape's message cannot name it, the ``@id`` does.
    """
    if not isinstance(iri, str):
        return None
    for prefix in NAMESPACE_CHECKS:
        if iri.startswith(namespace_start(prefix)):
            return prefix
    return None
