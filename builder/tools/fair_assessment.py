"""Tool that assesses FAIR maturity from CrateState metadata.

Checks basic FAIR indicators (metadata presence, entity IDs, license, context)
and computes DSM level from fair/dsm_indicators.yaml check results.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from builder.state import CrateState, FAIRReport, MITReport

# The graph primitives and the tri-state Verdict are shared with the Bridge2AI
# AI-readiness instrument (builder/tools/air_assessment.py): both read one assembled
# @graph and both answer in the same auditable shape, and two implementations of one
# question is how two axes come to disagree about one crate. They keep their private
# names here so every call site below reads unchanged.
from builder.tools.assessment_graph import (
    OPEN_MEDIA_TYPES as _OPEN_MEDIA_TYPES,
)
from builder.tools.assessment_graph import (
    Graph,
    Verdict,
)
from builder.tools.assessment_graph import (
    as_verdict as _as_verdict,
)
from builder.tools.assessment_graph import (
    columns as _columns,
)
from builder.tools.assessment_graph import (
    is_external_iri as _is_external_iri,
)
from builder.tools.assessment_graph import (
    needs_graph as _needs_graph,
)
from builder.tools.assessment_graph import (
    node_types as _node_types,
)
from builder.tools.assessment_graph import (
    nodes as _nodes,
)
from builder.tools.assessment_graph import (
    ref_id as _ref_id,
)

logger = logging.getLogger(__name__)

# Path to the FAIR YAML files
FAIR_INDICATORS_PATH = Path(__file__).resolve().parent.parent.parent / "fair" / "indicators.yaml"
DSM_INDICATORS_PATH = Path(__file__).resolve().parent.parent.parent / "fair" / "dsm_indicators.yaml"

# The DSM ladder starts at 1. Level 0 states the pre-FAIRification condition in the
# negative ("Dataset(s) are NOT Identifiable..."), so failing its indicators is the
# desired outcome and scoring them would invert the scale.
_DSM_FIRST_LEVEL = 1


def _load_yaml(path: Path) -> dict[str, Any] | None:
    """Load and parse a YAML file safely.

    Returns the parsed content, or None on failure.
    """
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("Failed to load YAML from %s: %s", path, e)
        return None


def _root_node(graph: Graph) -> dict[str, Any]:
    """The Root Data Entity — whatever ``ro-crate-metadata.json`` says it is ``about``.

    Followed through the descriptor rather than assumed to be ``./`` so the reading
    matches the RO-Crate spec's own definition, which is what a third-party reader
    would follow.
    """
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    descriptor = by_id.get("ro-crate-metadata.json", {})
    return by_id.get(_ref_id(descriptor.get("about")) or "./", by_id.get("./", {}))


def _payload_files(graph: Graph) -> tuple[list[dict[str, Any]], set[str]]:
    """The crate's *data*: File nodes the root claims, transitively through ``hasPart``.

    "The data" cannot mean "every node". The ISA backbone always mints a Study
    ``Dataset`` into the root's ``hasPart`` — a crate with two empty entities and no
    payload whatsoever still has one — so any indicator answered by "``hasPart``
    resolves" is answered by the builder's own scaffolding, not by data. That is the
    #670 defect in miniature: an indicator about the data, satisfied by the metadata.

    Returns the File nodes and every ``@id`` walked, so a caller can also ask whether
    the references resolve.
    """
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    queue = [_root_node(graph)]
    while queue:
        parts = queue.pop().get("hasPart")
        for ref in parts if isinstance(parts, list) else [parts] if parts else []:
            part_id = _ref_id(ref)
            if part_id in seen:
                continue
            seen.add(part_id)
            child = by_id.get(part_id)
            if child is None:
                continue
            if "File" in _node_types(child):
                found.append(child)
            if "Dataset" in _node_types(child):
                queue.append(child)
    return found, seen


def _root_pid(graph: Graph) -> str:
    """The persistent identifier the crate claims for itself, or ``""``.

    A DOI, or any absolute IRI. A bare accession like ``S-VHPS22`` does not qualify:
    it is unique inside BioStudies and ambiguous outside it, and the whole point of
    the indicator is identification that survives leaving the source repository.
    """
    root = _root_node(graph)
    for value in (root.get("identifier"), root.get("@id")):
        text = _ref_id(value) or str(value or "")
        if text.startswith(("http://", "https://", "10.")) or "doi" in text.lower():
            return text
    return ""


# ---------------------------------------------------------------------------
# Shared JSON-LD shape primitives, and the crate populations the DSM asks about
#
# Any property in a JSON-LD document may be written as a scalar, as a list, or as a
# node object, and all three spellings are legal for one crate. A predicate that reads
# ``node.get(key)`` straight therefore scores a crate on its serialisation habit
# rather than on its content: ``str(["text/csv"]).split(";")[0]`` is ``"['text/csv']"``,
# which is in no set, so a legal crate reads as having declared no format at all. Both
# rounds of #670's adversarial review caught proposals mishandling list-valued
# ``tableSchema``, ``columns``, ``valueUrl`` and ``datatype``, so the normalisation
# lives here, once, and every check below reads through it rather than re-deriving it.
# ---------------------------------------------------------------------------


def _values(node: dict[str, Any], key: str) -> list[Any]:
    """Every value of *key*, whether it was written as a scalar or as a list."""
    raw = node.get(key)
    if raw is None:
        return []
    return list(raw) if isinstance(raw, list) else [raw]


def _ids(value: Any) -> list[str]:
    """Every ``@id`` a property value carries: scalar, node object, or a list of either.

    :func:`_ref_id` stringifies a list into its Python repr, so a one-element list
    defeats every comparison built on it and leaks ``[{'@id': …}]`` into published
    evidence.
    """
    items = value if isinstance(value, list) else [value]
    return [rid for item in items if (rid := _ref_id(item))]


def _ref_ids(node: dict[str, Any], *keys: str) -> list[str]:
    """The ``@id``s *node* points at through *keys*, list- and node-object-safe."""
    return [rid for key in keys for rid in _ids(node.get(key))]


def _any_external(value: Any) -> bool:
    """True when any ``@id`` a (possibly list-valued) property carries is an absolute IRI."""
    return any(rid.startswith(("http://", "https://")) for rid in _ids(value))


def _text(value: Any) -> str:
    """A property's literal text, normalised — scalar or list, one reading.

    Comparing one raw value against another is how a list-valued ``name`` silently
    stops matching a scalar ``description``.
    """
    items = value if isinstance(value, list) else [value]
    words = " ".join(str(i) for i in items if isinstance(i, str | int | float))
    return " ".join(words.split()).strip().lower().rstrip(".")


def _outgoing_refs(
    node: dict[str, Any], keys: tuple[str, ...] | None = None
) -> Iterator[str]:
    """Every node this one *references*, as ``{"@id": …}`` — never a bare literal.

    A reference in JSON-LD is a node object. A bare string that happens to equal
    another node's ``@id`` is a literal, and walking it as an edge would let a crate
    grow its own graph by writing a matching string into a text field. ``@type``'s
    values are class names, not references.
    """
    for key, value in node.items():
        if key in ("@id", "@type", "@context"):
            continue
        if keys is not None and key not in keys:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and (rid := str(item.get("@id") or "")):
                yield rid


def _has_role(node: dict[str, Any], role: str) -> bool:
    """Whether *node* declares the ISA role *role*, list- or scalar-valued."""
    return any(_ref_id(v) == role or str(v) == role for v in _values(node, "additionalType"))


def _model_datasets(graph: Graph) -> list[dict[str, Any]]:
    """The Datasets the crate's own model defines — every Dataset but the root.

    The root is excluded because it is packaging: RO-Crate requires it, so an edge that
    starts there is the serialiser talking, not the model.
    """
    root_id = str(_root_node(graph).get("@id") or "./")
    return [
        n
        for n in _nodes(graph)
        if "Dataset" in _node_types(n) and str(n.get("@id")) != root_id
    ]


def _matches_slot_type(node: dict[str, Any], slot: str) -> bool:
    """Whether *node* is this crate's realisation of the MIT crate slot *slot*.

    Delegates to ``mit_assessment._node_matches_slot_type``, the canonical D16 matcher,
    rather than comparing ``additionalType`` as a raw string: the canonical path routes
    the value through ``_local``, so the legal CURIE spelling ``isa:CellLine`` still
    matches. A hand-rolled equality test does not, and #670's second review measured
    what that costs — rewriting every ``additionalType: "CellLine"`` to ``"isa:CellLine"``
    evaporated the whole cell-line population and flipped 39 of 42 failing crates to
    True on DSM-3-C4.

    The one thing added on top is the list spelling: ``_additional_type_of`` reads a
    scalar or a node object but returns ``None`` for ``["CellLine"]``, and a subject
    that drops out of the population makes the indicator *easier*, so the list form is
    folded in here rather than left to inflate the score.
    """
    from builder.tools.mit_assessment import _SLOT_TYPE_MATCH, _local, _node_matches_slot_type

    if _node_matches_slot_type(node, slot):
        return True
    rule = _SLOT_TYPE_MATCH.get(slot)
    if rule is None:
        return False
    bases, additional = rule
    if additional is None or not (bases & {_local(t) for t in _node_types(node)}):
        return False
    return any(
        _local(_ref_id(v) or str(v)) == additional for v in _values(node, "additionalType")
    )


# Media types that carry data a machine can parse into records without a human in the
# loop — the SECOND star of the 5-star Open Data scheme ("structured data … e.g. Excel
# instead of an image scan of a table"). Deliberately wider than ``_OPEN_MEDIA_TYPES``,
# which is the THIRD star and is DSM-3-R5's question: a spreadsheet is machine readable
# but proprietary; a PDF or a README is open but is a rendering, not data. DSM-3-R5 is
# scored on the INTERSECTION of the two, so the two rungs cannot contradict each other
# about one crate.
_MACHINE_READABLE_MEDIA_TYPES = frozenset(
    {
        "text/csv", "text/tab-separated-values", "text/turtle",
        "application/json", "application/ld+json",
        "application/xml", "text/xml",
        "application/x-hdf5", "application/x-netcdf", "application/parquet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
)

# Media types that do not identify the format at all: a machine handed one of these
# still has to guess. ``application/octet-stream`` is the IANA name for "unknown bytes".
_OPAQUE_MEDIA_TYPES = frozenset(
    {
        "", "application/octet-stream", "binary/octet-stream", "application/x-binary",
        "application/unknown", "unknown",
    }
)

# Payload that holds fields — delimited text and spreadsheets alike.
_TABULAR_MEDIA_TYPES = frozenset(
    {
        "text/csv", "text/tab-separated-values",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
)
_TABULAR_SUFFIXES = (".csv", ".tsv", ".tab", ".xls", ".xlsx", ".xlsm", ".ods")
_DELIMITED_MEDIA_TYPES = frozenset({"text/csv", "text/tab-separated-values"})
_DELIMITED_SUFFIXES = (".csv", ".tsv", ".tab")

# How much longer than its derived header a delimited file can be while still holding
# no record: a UTF-8 BOM (3 bytes) plus the CRLF RFC 4180 mandates where the derivation
# assumed LF (1 byte). Both are read off the specifications, not off a corpus.
_ENCODING_SLACK = 4


def _media_type(node: dict[str, Any]) -> str:
    """``encodingFormat`` as a bare lowercase media type, list- and node-object-safe.

    Only the first non-empty declaration is read, so ``["application/octet-stream",
    "text/csv"]`` reads as octet-stream. That is the deflationary choice, and it is
    stated here rather than left implicit.
    """
    for value in _values(node, "encodingFormat"):
        text = _ref_id(value) if isinstance(value, dict) else str(value or "")
        text = text.split(";")[0].strip().lower()
        if text:
            return text
    return ""


def _is_tabular(node: dict[str, Any]) -> bool:
    """A payload file that holds fields — delimited text or a spreadsheet.

    Read from the media type *or* the suffix *or* a ``csvw:Table`` typing, so a crate
    cannot shrink a denominator by declining to declare its data tabular.
    """
    return (
        _media_type(node) in _TABULAR_MEDIA_TYPES
        or str(node.get("@id") or "").lower().endswith(_TABULAR_SUFFIXES)
        or "Table" in _node_types(node)
    )


def _minted_by_the_builder(node: dict[str, Any]) -> bool:
    """Whether the crate declares this payload file as one the tool wrote for itself.

    ``_synth_condition_table`` and the process-result templates write header-only CSVs
    into ``data/`` on every build and stamp ``AUTOGENERATED`` on their ``name``
    (``_crate_mapping.AUTOGENERATED_MARKER``, written unconditionally at mapping time
    and never cleared afterwards), so "the crate contains a CSV" is a fact about the
    assembler, not about the deposit.

    The marker is a **naming convention** — matched on ``name``, shared with
    ``air_assessment`` and ``provenance_dag._AUTOGENERATED_MARKER`` — not a typed flag.
    Two residuals follow and are load-bearing for the checks that read this: a scaffold
    whose name was edited reads as deposited, and a builder-minted condition table that
    ``populate_condition_table`` later filled with the depositor's own plate map still
    reads as minted.
    """
    from builder.tools._crate_mapping import AUTOGENERATED_MARKER

    return str(node.get("name") or "").upper().startswith(AUTOGENERATED_MARKER)


def _deposited_files(graph: Graph) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(payload files the crate received, payload files the builder minted itself).

    The split every DSM check about *the data* reads, so that none of them is answered
    by the assembler's own templates. See :func:`_minted_by_the_builder` for what the
    marker is and what it cannot see.
    """
    files, _walked = _payload_files(graph)
    minted = [f for f in files if _minted_by_the_builder(f)]
    minted_ids = {id(f) for f in minted}
    return [f for f in files if id(f) not in minted_ids], minted


# XSD/CSVW datatypes a machine can compute with rather than merely store. The CSVW
# default is ``string``, so a string-ish type declares nothing about the value.
_COMPUTABLE_DATATYPES = frozenset(
    {
        "decimal", "double", "float", "integer", "int", "long", "short", "byte",
        "nonnegativeinteger", "positiveinteger", "nonpositiveinteger", "negativeinteger",
        "unsignedint", "unsignedlong", "unsignedshort", "unsignedbyte",
        "boolean", "date", "datetime", "datestamp", "time", "duration",
        "gyear", "gmonth", "gday", "gyearmonth", "gmonthday", "anyuri", "hexbinary",
        "base64binary",
    }
)
_OPAQUE_DATATYPES = frozenset(
    {
        "string", "normalizedstring", "token", "name", "language", "anyatomictype",
        "any", "json", "html", "xml",
    }
)
_KNOWN_DATATYPES = _COMPUTABLE_DATATYPES | _OPAQUE_DATATYPES


def _datatype_names(field: dict[str, Any]) -> list[str]:
    """Every declared CSVW datatype as a bare lowercase local name.

    Unwraps the list form, the ``{"base": …}`` object form and every IRI/CURIE spelling
    of one datatype, so a notation cannot be mistaken for a semantics. All declared
    values are returned, never just the first, so ``["string", "double"]`` cannot
    smuggle a computable type past a quantifier that means "every declared type".

    The local-name reduction is what makes the whitelist notation-blind, and it is also
    its weakness: a custom IRI whose local name collides (``https://ex.org/mytype#double``,
    ``foo:double``) is credited. That is inflation by naming, narrower than the
    blacklist hole it replaces; matching full XSD/CSVW IRIs plus their canonical
    prefixes is the tighter construction if it is ever needed.
    """
    out: list[str] = []
    for declared in _values(field, "datatype"):
        if isinstance(declared, dict):
            declared = declared.get("base") or declared.get("@id")
        text = str(declared or "").strip()
        if text:
            out.append(text.rsplit("#", 1)[-1].rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower())
    return out


def _declares_a_datatype(field: dict[str, Any]) -> bool:
    """At least one datatype, and every one declared is one CSVW/XSD defines."""
    names = _datatype_names(field)
    return bool(names) and all(n in _KNOWN_DATATYPES for n in names)


def _datatype_is_computable(field: dict[str, Any]) -> bool:
    """Every declared datatype is one a machine can compute with."""
    names = _datatype_names(field)
    return bool(names) and all(n in _COMPUTABLE_DATATYPES for n in names)


def _schema_fields(graph: Graph) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """(table ``@id`` → its resolved ``csvw:Column`` nodes, table ``@id`` → unresolved refs).

    ``tableSchema`` and ``columns`` are both normalised, so a one-element list does not
    read as "no schema", and an unresolved reference is reported rather than silently
    dropped — "no schema" and "a schema whose columns do not resolve" are different
    findings and an evidence string must not conflate them.
    """
    by_id = {str(n.get("@id")): n for n in _nodes(graph) if n.get("@id")}
    resolved: dict[str, list[dict[str, Any]]] = {}
    dangling: dict[str, list[str]] = {}
    for node in _nodes(graph):
        schema_ids = _ref_ids(node, "tableSchema")
        if not schema_ids:
            continue
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for schema_id in schema_ids:
            schema = by_id.get(schema_id)
            if schema is None:
                missing.append(schema_id)
                continue
            for ref in _ref_ids(schema, "columns", "column"):
                if ref in by_id:
                    found.append(by_id[ref])
                else:
                    missing.append(ref)
        resolved[str(node.get("@id"))] = found
        dangling[str(node.get("@id"))] = missing
    return resolved, dangling


def _prescribed_column_ids(graph: Graph) -> set[str]:
    """Every ``csvw:Column`` a ``csvw:Schema`` in this crate lists — the *prescribed* fields.

    A column nothing declares is prescribed by nothing.
    """
    out: set[str] = set()
    for node in _nodes(graph):
        if "Schema" in _node_types(node):
            out.update(_ref_ids(node, "columns", "column"))
    return out


def _column_is_described(column: dict[str, Any], prescribed: set[str]) -> bool:
    """A CSVW column description: listed by a schema, named, and typed."""
    return (
        str(column.get("@id")) in prescribed
        and bool(_values(column, "titles") or _values(column, "name"))
        and _declares_a_datatype(column)
    )


def _check_root_global_id(state: CrateState) -> bool:
    """Check that the crate has a globally unique identifier (accession or session_id)."""
    return bool(state.metadata.accession or state.session_id)


def _check_every_entity_has_id(state: CrateState, graph: Graph = None) -> bool | None:
    """RDA-F1-02D — *the data* is identified by a globally unique identifier.

    Asks it of the data. The old check was ``all(bool(e.entity_id) …)``, which is
    vacuously true over any entity the builder holds and never asks whether there is
    data to identify at all: a crate of two empty entities passed it.

    A File is globally identified when its own ``@id`` or ``contentUrl`` is an
    absolute IRI, or when the root carries a PID the crate-relative paths compose
    against. On this corpus that is false everywhere, because nothing writes a DOI to
    the root — the same root cause as RDA-F1-01M, and the two flip together when it is
    fixed. False for a stated reason is a finding; true for no reason is not.
    """
    if _needs_graph(graph):
        return None
    files, _ = _payload_files(graph)
    if not files:
        return False
    if _root_pid(graph):
        return True
    return all(
        _is_external_iri(f.get("@id"))
        or _is_external_iri(f.get("contentUrl"))
        or _is_external_iri(f.get("identifier"))
        for f in files
    )


def _check_pid_form(state: CrateState, graph: Graph = None) -> bool | None:
    """RDA-F1-01M — metadata is identified by a persistent identifier.

    Reads the assembled root, so the answer is one a reader can reproduce from the
    crate alone. With no graph it answers "not assessed" rather than falling back to
    ``state.metadata.accession``: a mix of read-from-crate and guessed-from-session
    verdicts in one column is exactly the ambiguity #670 exists to remove.
    """
    if _needs_graph(graph):
        return None
    return bool(_root_pid(graph))


def _check_rich_metadata(state: CrateState) -> bool:
    """Check that rich metadata is provided (title + description)."""
    return bool(state.metadata.title and state.metadata.description)


def _check_metadata_refs_data(state: CrateState, graph: Graph = None) -> bool | None:
    """RDA-F3-01M — metadata includes the identifier for *the data*.

    The descriptor must name the root, the root must claim at least one payload File,
    and every reference walked must resolve to a node in the graph. The old check was
    ``len(state.list_entities()) > 0``.

    Note the ISA backbone trap: "the root's ``hasPart`` is non-empty and resolves" is
    *also* a tautology, because assembly always mints a Study ``Dataset`` there. It is
    payload that has to be named — see :func:`_payload_files`.
    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    descriptor = by_id.get("ro-crate-metadata.json", {})
    root_id = _ref_id(descriptor.get("about"))
    if not root_id or root_id not in by_id:
        return False
    files, walked = _payload_files(graph)
    if not files:
        return False
    return all(part_id in by_id for part_id in walked)


_RO_CRATE_PROFILE_PREFIX = "https://w3id.org/ro/crate/"


def _check_jsonld_context(state: CrateState, graph: Graph = None) -> Verdict | None:
    """RDA-I1-01M / I1-02M — metadata in a standardised, machine-understandable
    knowledge representation.

    The crate's own descriptor answers this: ``ro-crate-metadata.json`` declaring
    ``conformsTo`` an RO-Crate version IRI *is* the statement "this file is JSON-LD
    following RO-Crate". The old check was ``len(state.list_entities()) > 0``.

    An empty crate still passes, and honestly so — the serialisation really is
    standardised whether or not anything was put in it. What changes is that the
    answer now comes from the crate rather than from a session object no reader has,
    which is the half of #670 that is about reproducibility rather than rigour.

    Kept distinct from :func:`_check_conforms_to_profile`, which asks the *community
    standard* question (a domain profile beyond bare RO-Crate). Two indicators sharing
    one check can never disagree, so the two pairs read different edges.
    """
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        if node.get("@id") != "ro-crate-metadata.json":
            continue
        conforms = node.get("conformsTo")
        for ref in conforms if isinstance(conforms, list) else [conforms]:
            if _ref_id(ref).startswith(_RO_CRATE_PROFILE_PREFIX):
                return Verdict(True, f"descriptor conformsTo {_ref_id(ref)}")
        return Verdict(False, "descriptor declares no RO-Crate profile")
    return Verdict(False, "no ro-crate-metadata.json descriptor in the crate")


def _check_fair_vocabularies(state: CrateState, graph: Graph = None) -> Verdict | None:
    """RDA-I2-01M — metadata uses FAIR-compliant vocabularies.

    Counts the crate's term-bearing nodes and asks how many are actually bound to a
    resolvable vocabulary: a ``csvw:Column`` with an absolute-IRI ``propertyUrl``, a
    ``DefinedTerm`` whose ``@id`` or ``inDefinedTermSet`` is one, or a
    ``PropertyValue`` with an absolute-IRI ``propertyID``.

    The old check was ``len(state.list_entities()) > 0``, i.e. it credited a crate for
    having any entity at all. A crate that annotates nothing with a controlled term
    now scores 0 of 0 and fails, which is the point.

    **The bound ratio is the informative output, not the boolean.** A majority is a
    stated convention and nothing more; it is deliberately *not* fitted to the crates
    on hand, whose bound ratios run 0.56-0.87 with a median of 0.70, so any sharper
    cut would be reading a threshold off a corpus that is essentially one deposit.
    Read the evidence string, and see #670 for why the number beats the verdict.
    """
    if _needs_graph(graph):
        return None
    total = bound = 0
    for node in _nodes(graph):
        types = _node_types(node)
        if "Column" in types:
            total, bound = total + 1, bound + _is_external_iri(node.get("propertyUrl"))
        elif "DefinedTerm" in types:
            total += 1
            bound += _is_external_iri(node.get("@id")) or _is_external_iri(
                node.get("inDefinedTermSet")
            )
        elif "PropertyValue" in types:
            total, bound = total + 1, bound + _is_external_iri(node.get("propertyID"))
    if not total:
        return Verdict(False, "no term-bearing node in the crate to bind")
    return Verdict(
        bound / total > 0.5,
        f"{bound} of {total} term-bearing nodes resolve to an external vocabulary "
        f"({bound / total:.0%})",
    )


def _check_qualified_refs(state: CrateState) -> bool:
    """Check that metadata includes references to other metadata."""
    entity_ids = {e.entity_id for e in state.list_entities()}
    for entity in state.list_entities():
        for field_value in entity.fields.values():
            if isinstance(field_value, str) and field_value in entity_ids:
                return True
            if isinstance(field_value, list):
                for item in field_value:
                    if isinstance(item, str) and item in entity_ids:
                        return True
    return False


# The root attributes a reader needs to reuse a dataset without asking anybody.
_REUSE_ATTRIBUTES = (
    "name",
    "description",
    "license",
    "identifier",
    "datePublished",
    "author",
    "creator",
    "publisher",
    "conformsTo",
    "keywords",
    "citation",
    "contactPoint",
)
# The four RO-Crate 1.2 requires on the Root Data Entity. Anchoring the check here
# rather than on "any N of the twelve" is what keeps it a reading of the spec instead
# of a threshold read off whichever crates happened to be on disk.
_ROOT_REQUIRED = ("name", "description", "datePublished", "license")
_REUSE_BEYOND_REQUIRED = 2
# The entities a reuser has to be able to identify: what was dosed, what it was dosed
# on, and how. A name alone does not let anyone re-run or re-analyse the work.
_REUSE_SUBJECT_TYPES = frozenset(
    {"MolecularEntity", "Sample", "CellLineSample", "LabProtocol"}
)


def _check_reuse_attributes(state: CrateState, graph: Graph = None) -> Verdict | None:
    """RDA-R1-01M — a plurality of accurate and relevant attributes, to allow reuse.

    Two limbs, because "plurality" is about the description, not the count of things
    described. The root must carry all four attributes RO-Crate requires of it
    (``_ROOT_REQUIRED``) plus ``_REUSE_BEYOND_REQUIRED`` more from
    ``_REUSE_ATTRIBUTES`` — "plurality" being more than the bare minimum the spec
    already mandates. And a majority of the crate's subjects must carry a name *plus*
    something that identifies them: a description, an identifier, or an
    ``additionalProperty``. Nobody can reuse a chemical given only a common name.

    The old check was ``len(state.list_entities()) >= 2``. That is the flagship #670
    absurdity: two entities whose ``fields`` are ``{}`` have zero attributes between
    them and passed an indicator about the plurality of attributes.

    Placeholders do not count. ``_DEFAULT_ROOT_NAME`` is the constant
    ``_apply_root_name`` falls back to when nothing supplied a title, and
    ``LICENCE_NOT_STATED_ID`` is what assembly writes when nobody declared a licence;
    crediting either would move the same tautology one level down.
    """
    if _needs_graph(graph):
        return None
    from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID
    from builder.tools.builder import _DEFAULT_ROOT_NAME

    root = _root_node(graph)
    present = []
    for attribute in _REUSE_ATTRIBUTES:
        value = root.get(attribute)
        if not value:
            continue
        if attribute in ("name", "description") and str(value) == _DEFAULT_ROOT_NAME:
            continue
        if attribute == "license" and _ref_id(value) == LICENCE_NOT_STATED_ID:
            continue
        present.append(attribute)

    subjects = [n for n in _nodes(graph) if _node_types(n) & _REUSE_SUBJECT_TYPES]
    described = [
        n
        for n in subjects
        if n.get("name")
        and (n.get("description") or n.get("identifier") or n.get("additionalProperty"))
    ]
    missing = [a for a in _ROOT_REQUIRED if a not in present]
    beyond = len(present) - (len(_ROOT_REQUIRED) - len(missing))
    enough_subjects = bool(subjects) and len(described) / len(subjects) > 0.5
    evidence = (
        f"root carries {len(present)} of {len(_REUSE_ATTRIBUTES)} reuse attributes"
        + (f", missing required {missing}" if missing else f", {beyond} beyond the four required")
        + f"; {len(described)} of {len(subjects)} subjects are described beyond a name"
    )
    return Verdict(
        not missing and beyond >= _REUSE_BEYOND_REQUIRED and enough_subjects, evidence
    )


def _effective_license(state: CrateState, graph: Graph = None) -> str:
    """The licence the crate actually asserts, or ``""``.

    Read from the assembled Root Data Entity, because that is the licence a reader
    gets. The three RDA R1.1 indicators and DSM-3-C7 used to read
    ``entity.fields["license"]`` — a field nothing populates and which never reaches
    the crate. `_read_declared_licence` (#535) writes the deposit's own declaration to
    ``state.metadata.license`` and ``_crate_mapping`` assembles it onto the root, so a
    crate could carry CC-BY in its JSON, render it on the report's study card, and
    score false on every licence indicator. The crate was right; the instrument was
    pointed at the wrong object.

    ``LICENCE_NOT_STATED_ID`` is what assembly writes when nobody declared one.
    Counting it would invert the depositor's own statement in the direction that
    suppresses reuse — the defect #535 exists to prevent — so it reads as absent.

    Falls back to ``state.metadata.license`` when no graph was supplied, then to an
    entity field, so a caller holding only state still gets an answer where one
    honestly exists.
    """
    from builder.tools._crate_mapping import LICENCE_NOT_STATED_ID

    def _usable(value: Any) -> str:
        text = _ref_id(value) or str(value or "")
        return "" if text == LICENCE_NOT_STATED_ID else text

    if not _needs_graph(graph):
        for node in _nodes(graph):
            if node.get("@id") == "./" or "Dataset" in _node_types(node):
                if found := _usable(node.get("license")):
                    return found
        return ""
    if found := _usable(state.metadata.license):
        return found
    for entity in state.list_entities():
        if found := _usable(entity.fields.get("license")):
            return found
    return ""


def _check_license_present(state: CrateState, graph: Graph = None) -> bool:
    """RDA-R1.1-01M — metadata includes information about the reuse licence."""
    return bool(_effective_license(state, graph))


def _check_license_standard(state: CrateState, graph: Graph = None) -> bool:
    """RDA-R1.1-02M — the licence is a *standard* reuse licence, not bespoke terms."""
    lic = _effective_license(state, graph).lower()
    return bool(lic) and any(
        token in lic
        for token in ("creativecommons", "cc-", "cc0", "spdx.org", "opensource.org",
                      "apache", "mit", "bsd", "gpl", "opendatacommons", "odbl")
    )


def _check_license_machine(state: CrateState, graph: Graph = None) -> bool:
    """RDA-R1.1-03M — the licence is machine-understandable, i.e. a resolvable IRI.

    A bare label is a real declaration and #535 returns it verbatim rather than
    inventing a version (D5) — but "CC-BY" does not say which version, so it is not
    machine-understandable and this indicator is the one that says so.
    """
    return _effective_license(state, graph).startswith(("http://", "https://"))


def _check_provenance(state: CrateState, graph: Graph = None) -> Verdict | None:
    """RDA-R1.2-01M — provenance information according to community-specific standards.

    RO-Crate's community standard for provenance is a ``CreateAction`` naming the
    ``instrument`` that produced the crate and pointing its ``result`` at the root.
    The old check read ``entity._provenance.created_by``, a session-only attribute
    that never reaches the crate: it recorded who told *us* about an entity, which is
    not the same claim, and no reader could reproduce it.

    An empty crate still passes. The provenance of the packaging act is genuinely
    recorded; what is absent is data for it to be about, and that absence is caught by
    the indicators that ask about data.
    """
    if _needs_graph(graph):
        return None
    root_id = str(_root_node(graph).get("@id") or "./")
    for node in _nodes(graph):
        if "CreateAction" not in _node_types(node):
            continue
        if node.get("instrument") and _ref_id(node.get("result")) == root_id:
            return Verdict(True, f"CreateAction {node.get('@id')} records the build")
    return Verdict(False, "no CreateAction records how the crate was produced")


def _check_conforms_to_profile(state: CrateState, graph: Graph = None) -> Verdict | None:
    """RDA-R1.3-01M / R1.3-02M — compliance with a (machine-understandable) community
    standard.

    The root must declare ``conformsTo`` a *domain* profile — something beyond the
    bare RO-Crate packaging IRI, which :func:`_check_jsonld_context` already covers.
    The old check inferred the standard from the presence of ISA-shaped entity types
    in session state, which is a guess about what the crate probably conforms to
    rather than a reading of what it declares.
    """
    if _needs_graph(graph):
        return None
    conforms = _root_node(graph).get("conformsTo")
    declared = [
        _ref_id(ref)
        for ref in (conforms if isinstance(conforms, list) else [conforms])
        if _is_external_iri(ref)
    ]
    domain = [iri for iri in declared if not iri.startswith(_RO_CRATE_PROFILE_PREFIX)]
    if domain:
        return Verdict(True, f"root conformsTo {', '.join(domain)}")
    return Verdict(
        False,
        "root declares no domain profile"
        + (f" (only {', '.join(declared)})" if declared else ""),
    )


def _mit_has_coverage(report: MITReport) -> bool:
    """True when a MIT report records actual (non-empty, non-zero) coverage."""
    return report.module_scores != {} and report.overall_score > 0


def _check_mit_coverage_indicator(state: CrateState) -> bool:
    """Check that MIT coverage is tracked (report present).

    Reads ``state.mit_assessment`` — the back-compat path. On the report/export
    path that field is never populated, so callers pass the freshly-computed report
    to :func:`assess_fair_maturity` instead (``mit=``), which is scored against the
    assembled ``@graph`` (#311).
    """
    return _mit_has_coverage(state.mit_assessment)


# DSM indicator checks
def _check_unique_id(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-C0 — Each Dataset purposed for sharing and re-use is assigned a unique
    identifier.

    Asks the published quantifier, over the published granularity. "Each Dataset
    purposed for sharing" is the Root Data Entity plus every ``Dataset`` it gathers
    through ``hasPart`` — what a reader receives as separately citable units. Two
    limbs, and both come from the sentence:

    *Each.* Every one of those Datasets must carry an identifier **assigned** to it: a
    non-empty ``identifier``, or an absolute-IRI ``@id``. A crate-relative ``@id``
    (``./``, ``assays/a1/``) is a location inside this ZIP, not an assignment, and
    counting it would make the limb a property of JSON-LD flattening rather than of the
    crate. This is the limb :func:`_check_every_entity_has_id` cannot express: it
    short-circuits on the root PID, so a crate whose second Dataset is unidentified
    passes it.

    *Unique.* Not "distinct" — flattening already forces every ``@id`` apart, so testing
    distinctness tests the serialiser. The only content left is *globally* unique, which
    is also what both cross-references in this indicator's own row say (``rda_ref:
    RDA-F1-02D``, ``fairsfair_ref: FsF-F1-01D``, both "globally unique"). Evidenced by a
    root PID the crate-relative paths compose against, or by every Dataset carrying an
    absolute IRI of its own. :func:`_root_pid` is shared with RDA-F1-01M/F1-02D on
    purpose: the two axes may weigh identification differently, but they must not
    disagree about whether this crate is globally identified.

    The old check was ``bool(state.session_id or state.metadata.accession)`` — a session
    handle no reader receives, true of every build that ran.

    **Reachable, and false today.** No crate on hand reaches it: 62 of 62 roots carry a
    minted slug or a bare accession, none an IRI. It is not *unreachable*.
    ``_populate_isa_backbone`` copies ``inv.fields["identifier"]`` onto the root verbatim
    and ``readers.existing_crate`` copies an ingested root ``identifier`` into
    ``metadata.accession``, so an Investigation — or an input crate — declaring a DOI
    produces a root PID with no code change (measured: True).

    It gates Level 1, so until something mints one the DSM ladder reads 0 for every
    crate, and :func:`dsm_ceiling` reports DSM-1-C0 as the blocker. That is the finding
    RDA-F1-01M and RDA-F1-02D already publish; what changes is that the two axes stop
    contradicting each other, where before the DSM awarded Level 2 on the strength of a
    ``session_id``. The route out is one the tool owns: 15 of these crates carry the real
    BioStudies accession ``S-VHPS22``, and emitting it as
    ``https://www.ebi.ac.uk/biostudies/studies/S-VHPS22`` — the identifier it already
    has, written so it resolves — would satisfy this with no depositor action.

    **Stated limitations, all measured.** :func:`_root_pid` accepts any text containing
    ``doi`` or starting with ``10.``, so ``10.happy`` reads as a persistent identifier;
    the fix belongs there, shared with the two RDA indicators, not in a second copy
    here. The *each* limb accepts any non-empty identifier string, so it discriminates
    nothing on this corpus and exists to stop the check collapsing into
    :func:`_check_every_entity_has_id`. A universal quantifier over Datasets is
    evadable by demotion: retyping an unidentified child ``Dataset`` to ``File``, or
    dropping it from ``hasPart``, removes it from the denominator. And the root enters
    the population on the descriptor's word alone, so a root typed ``Person`` is still
    counted as a Dataset offered for sharing — DSM-1-R3
    (:func:`_check_general_schema`) is the indicator that catches that.
    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    root = _root_node(graph)
    shared: list[dict[str, Any]] = [root] if root else []
    seen = {str(root.get("@id"))} if root else set()
    queue = list(shared)
    while queue:
        for part_id in _ref_ids(queue.pop(), "hasPart"):
            if part_id in seen:
                continue
            seen.add(part_id)
            child = by_id.get(part_id)
            if child is not None and "Dataset" in _node_types(child):
                shared.append(child)
                queue.append(child)
    if not shared:
        return Verdict(False, "the crate offers no Dataset to identify")

    def _assigned(node: dict[str, Any]) -> str:
        for item in _values(node, "identifier"):
            text = (_ref_id(item) if isinstance(item, dict) else str(item or "")).strip()
            if text:
                return text
        return str(node.get("@id")) if _is_external_iri(node.get("@id")) else ""

    assigned = [_assigned(d) for d in shared]
    named = [a for a in assigned if a]
    pid = _root_pid(graph)
    globally = bool(pid) or (bool(named) and all(_is_external_iri(a) for a in named))
    return Verdict(
        len(named) == len(shared) and globally,
        f"{len(named)} of {len(shared)} Datasets offered for sharing carry an assigned "
        f"identifier; the crate's own persistent identifier is "
        f"{pid or 'absent, so none of them is identified outside this crate'}",
    )


def _check_study_summary(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-C1 — Dataset Descriptor(s) includes Descriptive Study/Project-Level summary
    information.

    The **prose rung** of the study-design question. Its questionnaire item ladders
    DSM-1-C1 → DSM-2-C1 → DSM-3-C1 → DSM-4-C1: a summary here, a domain model of
    concepts at Level 2, reporting guidelines at Level 3, a semantic model at Level 4.
    So this rung is satisfied by a *sentence*, by the model's own design, and asking for
    structure here would answer DSM-2-C1's question instead.

    Two limbs, both from the indicator's own words. *Summary information* is information
    the descriptor does not already state as identity, so a description that merely
    repeats the title states none — that is a structural fact of the assembly fallback
    chain (``_populate_root_and_conformance`` falls back description → title → name),
    and it is read off the crate rather than out of a tool constant, so a reader with
    only the JSON reproduces it. *Study/Project-Level* is the granularity column: in an
    ISA crate the project is the root Investigation and the studies are the Study
    Datasets it defines, so a study that is named and never described is not summarised,
    however good the investigation's abstract.

    The old check was ``bool(state.metadata.title and state.metadata.description)``.
    ``state.metadata.title`` is ``None`` on 29 of 32 real sessions because the pipeline
    never writes it, while the assembled root has carried a real abstract all along —
    the #535 defect, an instrument pointed at the wrong object.

    **What the first limb actually tests, stated plainly.** It is string inequality and
    nothing more: setting the root description to the title plus one word defeats it, and
    it was measured doing so on 4 of the 6 crates that fail this limb. Nor does it judge
    the prose — 13 of the 51 crates that pass do so on the generated stub
    "Drafted Investigation description.", which is content-free. No reader-side anchor
    exists to catch that: ``AUTOGENERATED_MARKER`` is only ever prefixed to generated
    *names*, never to a root description, and no crate in the corpus carries per-field
    provenance that would flag a value as machine-drafted. So this indicator can say a
    summary is *present and distinct*; it cannot say it is *informative*.

    This check raises the indicator on crates that always met it, so it must not land
    without the honest DSM-1-C0 (:func:`_check_unique_id`) in the same change — on its
    own the pair moves the published ladder upward, which is the direction #670 exists
    to prevent.

    No attribution is read. Whether the crate names an author is credit, not summary
    information, and it is asked by RDA-R1-01M.
    """
    if _needs_graph(graph):
        return None
    root = _root_node(graph)
    summary, label = _text(root.get("description")), _text(root.get("name"))
    if not summary or summary == label:
        return Verdict(
            False,
            "the descriptor states no project-level summary beyond the project's own title",
        )
    studies = [d for d in _model_datasets(graph) if _has_role(d, "Study")]
    if not studies:
        return Verdict(False, "the descriptor defines no study to summarise")
    unsummarised = [d for d in studies if not _text(d.get("description"))]
    if unsummarised:
        return Verdict(
            False,
            f"{len(unsummarised)} of {len(studies)} studies are named but never summarised",
        )
    return Verdict(True, f"the project and all {len(studies)} of its studies carry a summary")


def _check_dataset_metadata(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-C2 — Dataset Descriptor(s) includes Identifying & Descriptive Dataset-Level
    metadata.

    Two demands in one sentence — *identifying* and *descriptive* — asked at the
    granularity the indicator names, Dataset-Level. So every Dataset the root gathers
    for sharing must carry a name that identifies it and a description that describes
    it, and there must be data underneath for those Datasets to be about.

    The old check was ``len(state.list_entities()) > 0``: a crate of two empty entities
    passed an indicator about the metadata of its datasets.

    **Why universal rather than a majority.** DSM-1-C2's own text does not contain the
    word "Each"; it is an unquantified plural ("Dataset Descriptor(s) includes …
    Dataset-Level metadata"). This module reads an unquantified plural as universal
    wherever it appears — here, in DSM-2-C6, DSM-2-C7 and DSM-3-C4 — because the
    alternative is a ratio, and any ratio would be read off two deposits. The floor for
    a single Dataset is RO-Crate's own: the Root Data Entity MUST carry ``name`` and
    ``description``, and this indicator's plural extends that floor to the Datasets the
    root gathers.

    **The scaffolding trap, twice over.** The ISA backbone always mints a Study
    ``Dataset`` into the root's ``hasPart``, so "there is a Dataset" is the builder
    talking; and ``_apply_root_name`` always names the root, falling back to
    ``_DEFAULT_ROOT_NAME``, so "the root has a name" is too. Hence the constant is not
    counted as a name, and the payload limb reads *deposited* files only.

    Every text comparison goes through :func:`_text`, so a root whose ``name`` is written
    as ``["ISA-Tox RO-Crate"]`` is recognised as the placeholder it is; reading
    ``node.get`` straight would score the crate on its serialisation habit.

    The Dataset set is keyed by ``@id``, root included, so a crate whose ``hasPart``
    contains a cycle back to the root cannot count the root twice.
    """
    if _needs_graph(graph):
        return None
    from builder.tools.builder import _DEFAULT_ROOT_NAME

    deposited, minted = _deposited_files(graph)
    if not deposited:
        return Verdict(
            False,
            f"the crate gathers no deposited data to describe ({len(minted)} file(s), "
            "all minted by the tool)"
            if minted
            else "the crate gathers no data to describe",
        )
    _files, walked = _payload_files(graph)
    by_id = {str(n.get("@id")): n for n in _nodes(graph) if n.get("@id")}
    datasets: dict[str, dict[str, Any]] = {
        i: by_id[i] for i in walked if i in by_id and "Dataset" in _node_types(by_id[i])
    }
    root = _root_node(graph)
    datasets[str(root.get("@id") or "./")] = root
    described = [
        n
        for n in datasets.values()
        if _text(n.get("name"))
        and _text(n.get("description"))
        and _text(n.get("name")) != _text(_DEFAULT_ROOT_NAME)
    ]
    return Verdict(
        len(described) == len(datasets),
        f"{len(described)} of {len(datasets)} shared Datasets carry both an identifying "
        f"name and a description of their own, over {len(deposited)} deposited data file(s)",
    )


def _check_access_info(state: CrateState) -> bool:
    """Dataset Descriptor contains access information.

    A self-contained RO-Crate carries its access information intrinsically: the
    data is reached through the crate, and the descriptor states how — a resolvable
    identity, explicit reuse terms, included data files, or a known location.
    Reading *only* the incidental build-time ``output_path`` / ``input_path`` (unset
    on the report and fixture paths) collapsed the whole DSM ladder at L1 even for
    complete crates (#311). Credit crate content instead; a crate with none of these
    genuinely lacks access information and still fails.
    """
    md = state.metadata
    has_location = bool(md.output_path or md.input_path)
    has_identity = bool(md.accession or state.session_id)
    has_license = any(e.fields.get("license") for e in state.list_entities())
    has_data = bool(state.list_entities("File"))
    return has_location or has_identity or has_license or has_data


def _check_has_descriptor(state: CrateState) -> bool:
    """Dataset metadata is a formally identifiable Dataset Descriptor."""
    return bool(state.metadata.title)


def _check_context_fields(state: CrateState) -> bool:
    """Contextual metadata reported at summary level."""
    return len(state.list_entities()) > 0 or bool(state.metadata.title)


def _check_dataset_hierarchy(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-R2 — Data intended for sharing and reuse have a purposely defined
    representation as Datasets.

    Asks it of the data the crate actually received. The old check was
    ``len(state.list_entities()) > 0``: a session holding two empty entities and no
    payload "had a purposely defined representation as Datasets".

    Two limbs, and the anchor for each.

    *Deposited data.* The builder writes header-only CSVs into ``data/`` on every build
    and marks them ``AUTOGENERATED`` in their own ``name``, so a crate that received
    nothing still carries CSVs. Deposited data means the files the crate did not write
    for itself (:func:`_deposited_files`), and that filter alone is what fails the empty
    crate.

    *Below the root.* Only a Dataset **below** the root can evidence that the data was
    *purposely organised* rather than merely gathered, because :func:`_payload_files`
    already **defines** the payload as the Files the root reaches through ``hasPart``:
    given that definition, "some Dataset holds a deposited file" is entailed by the
    population itself. Measured, a root-inclusive variant returns True on 61 of 62 crates
    and False only on the empty one, i.e. it is the deposited-file guard restated.

    **The root exclusion has no specification sentence behind it**, only the published
    word "purposely", and it is the limb that produces both real failures on this
    corpus. That trade is stated rather than hidden: dropping the exclusion costs the two
    failures and leaves the check answering the guard, so it is kept, and a reader should
    know it is a reading and not a citation.

    The candidate Datasets are restricted to the ``@id``s :func:`_payload_files` walked
    from the root — the same restriction :func:`_check_data_structured` applies for the
    same reason. Scanning every ``Dataset`` node in the graph instead makes the check one
    unreachable node deep: appending a single ``{"@id": "#ghost", "@type": "Dataset",
    "hasPart": [<any deposited file>]}`` outside the root's ``hasPart`` flipped both real
    failures to True.

    **What the corpus can and cannot show.** The only real discrimination here is between
    two builds of the same two deposits — 22 of 23 S-VHPS22 builds put deposited files
    under the named Study and ``_v4`` does not; S-VHPS26 ``_v1`` does and ``_v2`` does
    not. Same input, different LLM run. So this measures builder nondeterminism on the
    crates on hand; its power against an unseen deposit is unmeasured.

    The verdict is existential because the statement it displaces is a universal negative
    — DSM-0-R2, the only other option in this single-select question, reads "No
    representation of Data purposed for sharing and re-use is available". Its negation is
    "at least one", not "most", so no ratio is fitted; the ratio is reported as evidence
    instead.
    """
    if _needs_graph(graph):
        return None
    files, minted = _deposited_files(graph)
    deposited = {str(f.get("@id")) for f in files}
    if not deposited:
        return Verdict(
            False,
            "the crate carries no deposited data file to represent"
            + (f" ({len(minted)} payload files, all builder-generated)" if minted else ""),
        )
    by_id = {str(n.get("@id")): n for n in _nodes(graph) if n.get("@id")}
    _payload, walked = _payload_files(graph)
    root_id = str(_root_node(graph).get("@id") or "./")
    defined: list[dict[str, Any]] = []
    held: set[str] = set()
    for node in _nodes(graph):
        node_id = str(node.get("@id"))
        if node_id == root_id or node_id not in walked:
            continue
        if "Dataset" not in _node_types(node):
            continue
        seen: set[str] = set()
        queue, mine = [node], set()
        while queue:
            for part_id in _ref_ids(queue.pop(), "hasPart"):
                if part_id in seen:
                    continue
                seen.add(part_id)
                child = by_id.get(part_id)
                if child is None:
                    continue
                if part_id in deposited:
                    mine.add(part_id)
                if "Dataset" in _node_types(child):
                    queue.append(child)
        if mine:
            defined.append(node)
            held |= mine
    names = ", ".join(str(d.get("name") or d.get("@id")) for d in defined[:3])
    return Verdict(
        bool(defined),
        f"{len(held)} of {len(deposited)} deposited data files sit under {len(defined)} "
        f"Dataset(s) the crate defines below the root"
        + (
            f" ({names})"
            if defined
            else f"; the root holds all {len(deposited)} directly, and no Dataset below "
            "it claims any"
        ),
    )


def _check_general_schema(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-R3 — A representation of the Dataset Descriptor conforming to a relevant
    General Purpose Metadata Schema is available.

    Packaging, and it stays packaging: an empty crate passes, honestly and by design
    (``_DSM_ALLOWED`` in ``test_fair_metrics_can_fail``). The old check was
    ``len(state.list_entities()) > 0``, which measured the session; this reads the
    descriptor a reader holds.

    Its own ladder says what it is asking. The R3 question runs L1 general-purpose schema
    → L2 ``generic_model`` (the schema also describes the local Dataset Model and its
    structural metadata) → L3 ``domain_standard`` → L4 semantic. So L1 asks only: *is the
    descriptor written in a general-purpose metadata schema at all?*

    Deliberately **not** ``conformsTo``. DSM-1-R4 (``descriptor_machine_readable`` →
    :func:`_check_jsonld_context`) already reads that IRI, and a predicate that added a
    limb to it would be a strict superset of R4 on the same rung — a second
    implementation of one question, which is how two axes come to disagree about one
    crate. R4's question is the *format*; this one is the *schema*. They are independent
    here: strip ``conformsTo`` and this still passes, type the root ``Person`` and R4
    still passes. If DSM-1-R4 is ever given a body that is not a ``conformsTo`` test, the
    two must be re-diffed.

    Conformance to the general-purpose schema is therefore checked structurally, against
    schema.org — the schema RO-Crate is a profile of:

    * the Dataset Descriptor exists and is *about* a node the crate defines (RO-Crate's
      own definition of a descriptor; one whose ``about`` dangles is not one), and
    * that node is typed ``Dataset``, the general-purpose class, and
    * it uses the general-purpose descriptive slots the schema requires of it —
      ``_ROOT_REQUIRED``, the four RO-Crate 1.2 mandates on the Root Data Entity.

    The third limb is about the *slots*, not their contents: ``license`` pointing at
    ``#licence-not-stated`` still demonstrates that the crate states its terms in
    schema.org's vocabulary. Whether those values are substantive is RDA-R1-01M's
    question (:func:`_check_reuse_attributes`, which excludes exactly those placeholders
    and demands two attributes beyond the required four), and the two disagree on 22 of
    the 62 crates measured. Same four names, two different questions: is the slot used,
    and is the value worth anything.

    RO-Crate requires exactly one ``about``; a descriptor declaring several is tolerated
    here by selecting the Dataset-typed one, which is the generous reading. Failing a
    multi-valued ``about`` outright would be the stricter one.
    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    descriptor = by_id.get("ro-crate-metadata.json")
    if descriptor is None:
        return Verdict(False, "the crate carries no ro-crate-metadata.json descriptor")
    targets = _ids(descriptor.get("about"))
    if not targets:
        return Verdict(
            False,
            "the descriptor does not say what it is about, so it is not a Dataset Descriptor",
        )
    about = next((t for t in targets if "Dataset" in _node_types(by_id.get(t, {}))), targets[0])
    described = by_id.get(about)
    if described is None:
        return Verdict(
            False,
            f"the descriptor is about {about}, which the crate does not define, so it "
            "describes no Dataset",
        )
    if "Dataset" not in _node_types(described):
        return Verdict(
            False,
            f"the descriptor describes {about}, typed {described.get('@type')!r} rather "
            "than the general-purpose class Dataset",
        )
    missing = [slot for slot in _ROOT_REQUIRED if not described.get(slot)]
    if missing:
        return Verdict(
            False,
            f"the Dataset Descriptor omits the general-purpose schema's required "
            f"{', '.join(missing)} on {about}",
        )
    return Verdict(
        True,
        f"the Dataset Descriptor describes {about} as a schema.org Dataset carrying "
        f"{', '.join(_ROOT_REQUIRED)}",
    )


def _check_descriptor_machine_readable(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-R4 — Dataset Descriptor is available in Machine Readable Format.

    The published model answers this itself: the indicator's ``rda_ref`` column
    (``fair/dsm_indicators.yaml``, DSM-1-R4) names RDA-I1-02M, which
    :func:`_check_jsonld_context` already implements. So it delegates to that function
    rather than getting a look-alike of its own — two implementations of one question is
    how two axes come to disagree about one crate, which is why
    :func:`_check_standard_license` and :func:`_check_domain_standard` are written the
    same way.

    The old check was ``len(state.list_entities()) > 0``. Replacing it changes no verdict
    on any crate; what it changes is where the verdict comes from, which is the
    reproducibility half of #670 and the only half this indicator has.

    **Stated plainly: this cannot fail for a crate this builder produces, and this
    migration must not be counted among the checks #670 made able to fail.** A serialised
    RO-Crate descriptor is JSON-LD declaring an RO-Crate profile by construction. It
    fails only for a foreign or damaged crate — no descriptor node, or one that declares
    no profile — and that is the honest extent of it. Inventing a stricter reading would
    answer a different question than the one printed in the model, and would contradict
    ``_DSM_ALLOWED``, which pins this as a packaging indicator an empty crate may
    honestly meet.

    **Shared blast radius.** Because this delegates, a change to
    :func:`_check_jsonld_context` moves RDA-I1-01M, RDA-I1-02M and DSM-1-R4 together.
    That is the trade being bought, and it is pinned by test — as *equality of verdict*
    over a crate, not as ``DSM_CHECKS["descriptor_machine_readable"] is
    _check_jsonld_context``, which a delegating wrapper makes false.
    """
    return _check_jsonld_context(state, graph)


def _check_data_machine_readable(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-1-R5 — Dataset(s) available in Machine Readable Format.

    Asks it of the data, not of the session. The old check was
    ``len(state.list_entities()) > 0``.

    **How this differs from the same sentence at Level 2.** DSM-2-R5 carries identical
    published text, and the four rungs of *this* question (``fair/dsm_indicators.yaml``
    lists DSM-0/1/3/4-R5 and **not** DSM-2-R5) read as the 5-star Open Data ladder: a
    rendering a human must read (L0), structured data software can parse even in a
    proprietary container (L1, here), a non-proprietary format (L3,
    :func:`_check_non_proprietary_format`), terms that resolve (L4). So Level 1 asks
    *machine readable*, which an ``.xlsx`` satisfies and a ``.docx`` does not, and
    deliberately does not ask "open" — that is L3's job. DSM-2-R5
    (:func:`_check_data_structured`) is told apart by its level's own ``level_scope``:
    Data Object level here, Project level there.

    The scaffolding trap: the builder writes header-only ``text/csv`` templates into
    ``data/`` on every build, so "the crate contains a CSV" is a fact about
    ``_synth_condition_table``. Those are excluded by :func:`_deposited_files`.

    Existential because the option it displaces, DSM-0-R5, is the universal negative
    "Dataset(s) are NOT available in a Machine Readable Format"; its negation is "at
    least one". A majority reading was measured and rejected: it fails 38 of 62 crates
    with a cut sitting within 0.06 of this corpus's own median — a threshold read off two
    deposits.

    **No real crate on hand fails this.** Every deposit in the corpus ships an ``.xlsx``
    or a ``.csv``. The failing evidence is a constructed docx-only deposit; what the
    migration buys on real crates is that the verdict is now reproducible from the
    published crate and no longer credits the builder's own templates.
    """
    if _needs_graph(graph):
        return None
    deposited, minted = _deposited_files(graph)
    if not deposited:
        return Verdict(
            False,
            "the crate carries no deposited data file"
            + (f" ({len(minted)} payload files, all builder-generated)" if minted else ""),
        )
    formats = [_media_type(f) for f in deposited]
    readable = [f for f in formats if f in _MACHINE_READABLE_MEDIA_TYPES]
    undeclared = sum(1 for f in formats if not f)
    other = sorted({f for f in formats if f and f not in _MACHINE_READABLE_MEDIA_TYPES})
    return Verdict(
        bool(readable),
        f"{len(readable)} of {len(deposited)} deposited files declare a machine-readable "
        "data format"
        + (f"; {undeclared} declare no format at all" if undeclared else "")
        + (f"; renderings/opaque: {', '.join(other[:3])}" if other else ""),
    )


# The properties by which a descriptor relates one Dataset to another. ``hasPart`` is
# how an RO-Crate states that a deposit is composed of several datasets; the rest are
# how it points at a dataset it does not contain.
_RELATING_PROPERTIES = (
    "hasPart",
    "mentions",
    "about",
    "isPartOf",
    "sameAs",
    "isBasedOn",
    "citation",
)


def _check_cross_dataset_refs(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-2-C5 — Dataset Descriptor includes reference to related Datasets and if
    applicable the relevant joining Dataset Fields.

    The old check delegated to ``_check_qualified_refs(state)`` — "some entity field
    repeats another entity's id" — which says nothing about *Datasets* and is invisible
    to a reader holding only the crate.

    **A Dataset counts only when it stands as a dataset in its own right**, which means
    it *directly* holds a File the builder did not mint, or it is a Dataset outside this
    crate named by an absolute IRI. Attribution is deliberately one-hop: an earlier draft
    recursed, so a single file two levels down counted for every ancestor and the
    always-minted Study answered the indicator by itself the moment the backbone minted
    one Assay beneath it. One-hop attribution gives each File exactly one Dataset, and
    that is what makes "related Datasets" plural in fact and not just in the text.

    "Related" is read as the descriptor's own relating properties rather than as
    containment alone, so a crate that cites a dataset it does not carry earns it. **That
    external limb requires the cited dataset to exist as a ``Dataset``-typed node in the
    ``@graph``**: a bare ``isBasedOn: {"@id": "https://doi.org/…"}`` with no node behind
    it does not count, and a stub typed ``CreativeWork`` does not either. On the corpus
    the limb contributes nothing — no crate references a Dataset outside itself — but the
    ISA decomposition of one deposit into Study and Assays is a real statement that these
    datasets relate, and it is the same statement the joining-fields limb presupposes:
    you join datasets you have both of.

    The ``AUTOGENERATED`` name is the crate's own declaration that the builder wrote the
    file, and that is the question here — not whether the table has rows, which is what
    made the same marker unsound for :func:`_check_standard_field_metadata`. Its
    provenance is exact: ``_crate_mapping`` writes ``_autogenerated_name("Condition
    table")`` unconditionally at mapping time and ``populate_condition_table`` never
    clears it, so **a Dataset whose only file is the depositor's own plate map, copied
    verbatim into a minted condition table, is judged to hold no deposited data.** That
    costs nothing on this corpus (0 crates) and it matters on exactly one crate in the
    other direction, where it is right: every Assay in ``svhps22_real_input_crate_v4``
    holds nothing but 26-37 byte result stubs and a 129-byte empty condition table while
    all 72 depositor files hang off the root.

    Second limb, the model's own "if applicable": a column whose ``valueUrl`` names an
    in-crate entity is a declared join, and a declared join that dangles is worse than no
    join. Nothing dangles on this corpus, so the limb only ever reports "n of n" today.

    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph)}
    root = _root_node(graph)

    def holds_deposited_data(node: dict[str, Any]) -> bool:
        return any(
            (child := by_id.get(rid)) is not None
            and "File" in _node_types(child)
            and not _minted_by_the_builder(child)
            for rid in _ids(node.get("hasPart"))
        )

    related: dict[str, dict[str, Any]] = {}
    queue, walked = [root], {str(root.get("@id") or "./")}
    while queue:
        for rid in _outgoing_refs(queue.pop(), _RELATING_PROPERTIES):
            if rid in walked:
                continue
            walked.add(rid)
            child = by_id.get(rid)
            if child is None or "Dataset" not in _node_types(child):
                continue
            related[rid] = child
            queue.append(child)
    standing = {
        rid
        for rid, dataset in related.items()
        if holds_deposited_data(dataset) or rid.startswith(("http://", "https://"))
    }
    joins = [
        c
        for c in _columns(graph)
        if _ids(c.get("valueUrl")) and not _any_external(c.get("valueUrl"))
    ]
    dangling = [c for c in joins if any(r not in by_id for r in _ids(c.get("valueUrl")))]
    return Verdict(
        len(standing) >= 2 and not dangling,
        f"{len(standing)} of the {len(related)} Datasets the descriptor relates stand as "
        f"datasets in their own right; {len(joins) - len(dangling)} of {len(joins)} "
        "declared joining fields resolve in-crate",
    )


def _check_field_level_metadata(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-2-C6 — Dataset Descriptor includes Field-level Metadata as prescribed by a
    locally defined Dataset Model.

    Two demands in one sentence: the field metadata must be *prescribed by a model*, and
    the descriptor must *include* it — for the data, not for a corner of it.

    *Prescribed*: every ``csvw:Column`` describing a table must be listed by a
    ``csvw:Schema`` in this crate and carry what a CSVW column description consists of, a
    title and a ``datatype`` (:func:`_column_is_described`). A column nothing declares is
    prescribed by nothing.

    *Included*: **every** deposited data file that holds fields must have them described.
    Not a majority — "includes" is read here exactly as DSM-2-C7 reads the same verb one
    row down, and as this change reads every unquantified plural in the model. An earlier
    draft put a 0.5 cut here and it failed the sensitivity test: moving it +0.1 flipped 30
    of 62 crates, and the two modes it separated (0.333 and 0.571) were two builder
    versions, not two quality tiers. There is no number here to move now.

    **The denominator cannot be shrunk by not declaring data tabular.** Counting only
    files whose ``encodingFormat`` is CSV/TSV meant shipping the same data as
    spreadsheets *raised* the score: measured, a crate with one schematised CSV plus
    twenty undescribed ``.xlsx`` files scored "1 of 1 tabular data files … (100%)" and
    passed. The denominator is every **deposited** payload file that holds fields —
    delimited text *and* spreadsheets, by media type or by suffix (:func:`_is_tabular`) —
    so the same crate now reads "1 of 21 … (5%)" and fails. Builder-minted templates stay
    out of it: a schema the tool wrote for a table the tool generated is not the
    descriptor including field metadata for the deposit.

    **What this measures on the corpus.** Coverage is 0% on all 61 crates that hold
    deposited tabular data — the deposit's own CSVs and spreadsheets carry no schema at
    all, and every CSVW schema in these crates belongs to a table the builder minted. The
    coverage ratio stays in the evidence because it, not the boolean, is what tells a
    depositor how far off they are.

    Residuals, both stated: tabularity is inferred from media type or suffix, so an
    extensionless delimited deposit escapes the denominator and a formatted ``.xlsx``
    report wrongly enters it; and ``AUTOGENERATED`` is a name prefix, so a renamed
    template re-enters both halves.

    The old check was ``len(state.list_entities()) > 1``, which counted session objects
    and never looked at a field.
    """
    if _needs_graph(graph):
        return None
    deposited, _minted = _deposited_files(graph)
    tabular = [f for f in deposited if _is_tabular(f)]
    if not tabular:
        return Verdict(
            False, "the crate deposits no tabular data whose fields could be described"
        )
    prescribed = _prescribed_column_ids(graph)
    resolved, _dangling = _schema_fields(graph)
    described = [
        t
        for t in tabular
        if (cols := resolved.get(str(t.get("@id"))))
        and all(_column_is_described(c, prescribed) for c in cols)
    ]
    described_ids = {str(t.get("@id")) for t in described}
    undescribed = sorted(
        str(t.get("name") or t.get("@id"))
        for t in tabular
        if str(t.get("@id")) not in described_ids
    )
    covered = len(described) / len(tabular)
    return Verdict(
        len(described) == len(tabular),
        f"{len(described)} of {len(tabular)} deposited tabular data files have their "
        f"fields prescribed by a schema that names and types every column ({covered:.0%})"
        + (f"; undescribed: {', '.join(undescribed[:3])}" if undescribed else ""),
    )


def _check_value_level_metadata(state: CrateState) -> bool:
    """Descriptor includes value-level metadata."""
    return any(len(e.fields) >= 2 for e in state.list_entities())


def _check_generic_model(state: CrateState) -> bool:
    """Descriptor formally represents the dataset model extending a generic model."""
    return len(state.list_entities()) > 0


def _check_data_structured(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-2-R5 — Dataset(s) available in Machine Readable Format.

    Word for word DSM-1-R5, and the workbook leaves DSM-2-R5 out of the question ladder
    the other four options of that sentence form (``fair/dsm_indicators.yaml``: that
    single-select question lists DSM-0/1/3/4-R5 only). The two rows are told apart by
    their level's own ``level_scope``: Level 1 is "Data Object level", Level 2 "Project
    level". That is the whole difference and it is what this reads. DSM-1-R5 asks whether
    *a* data object is machine readable; this asks it of the project — **every** Dataset
    the local model defines must be backed by data, and **no deposited file may leave a
    machine guessing what it is**.

    **What the format limb detects.** Not unreadability — identification. A file typed
    ``application/octet-stream`` (the IANA name for "unknown bytes") or carrying no
    ``encodingFormat`` at all tells a machine nothing, and that is the failure this limb
    names. It deliberately does *not* demand a machine-readable data format of every
    payload file: a crate is allowed to carry a README and a protocol PDF beside its
    data, and a correctly typed ``.prism`` passes this rung. Requiring open formats here
    would score Level 3's question at Level 2 (:func:`_check_non_proprietary_format` is
    DSM-3-R5) and make the ladder incoherent.

    **Coordination with DSM-1-R5, in writing.** DSM-1-R5 stays existential and Data
    Object level ("at least one deposited file is in a machine-readable data format").
    This row stays universal and Project level ("every model Dataset is backed, every
    deposited file is identified"). If DSM-1-R5 is ever made project-scoped or
    open-format-scoped, the two identically-worded rows collapse into one claim scored
    twice and one of them must be scoped ``na``.

    Both limbs read one walk. :func:`_payload_files` walks ``hasPart`` from the root, and
    the backing test is confined to the ``@id``s that walk reached, so a model Dataset
    unreachable from the root cannot count as backed by files outside the denominator.
    The walk is still not fully symmetric: a floating Dataset can count as backed by
    *borrowing* an already-walked file. The direction is neutral-to-harder, because the
    floater also enters the denominator. The format limb reads deposited files only:
    failing a deposit because the builder omitted ``encodingFormat`` from one of its own
    templates would be scoring the assembler (measured: 30 minted files in the corpus
    carry no media type).

    Builder scaffolding is in the denominator where it belongs: the minted Study
    ``Dataset`` counts, so the empty crate reads "0 of 1 Datasets are backed by data"
    rather than passing because ``hasPart`` resolves.

    Measured: 19 of 62 crates pass. Of the 43 failures, 36 fail on the format limb alone,
    1 on unbacked model Datasets alone, and 6 on both — so the format limb carries 42 of
    the 43, and ``application/octet-stream`` appears on a *deposited* file in 41 of 62
    crates.

    The old check was ``len(state.list_entities()) > 0``.
    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph) if n.get("@id")}
    root_id = str(_root_node(graph).get("@id") or "./")
    model = [
        n
        for n in _nodes(graph)
        if "Dataset" in _node_types(n) and str(n.get("@id")) != root_id
    ]
    if not model:
        return Verdict(False, "the crate defines no Dataset besides the root")

    _payload, walked = _payload_files(graph)

    def backing(dataset: dict[str, Any]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        queue, found = [dataset], []
        while queue:
            for ident in _ref_ids(queue.pop(), "hasPart"):
                if ident in seen or ident not in walked:
                    continue
                seen.add(ident)
                child = by_id.get(ident)
                if child is None:
                    continue
                if "File" in _node_types(child):
                    found.append(child)
                if "Dataset" in _node_types(child):
                    queue.append(child)
        return found

    backed = [d for d in model if backing(d)]
    deposited, _minted = _deposited_files(graph)
    identified = [f for f in deposited if _media_type(f) not in _OPAQUE_MEDIA_TYPES]
    opaque = sorted(
        {
            _media_type(f) or "(no encodingFormat)"
            for f in deposited
            if _media_type(f) in _OPAQUE_MEDIA_TYPES
        }
    )
    return Verdict(
        len(backed) == len(model) and bool(deposited) and len(identified) == len(deposited),
        f"{len(backed)} of {len(model)} Datasets in the local model are backed by data; "
        f"{len(identified)} of {len(deposited)} deposited files name the format a machine "
        "would read them with"
        + (f"; unidentified: {', '.join(opaque[:3])}" if opaque else ""),
    )


def _check_domain_model(state: CrateState) -> bool:
    """A locally-defined Domain Model describes study design."""
    return bool(state.metadata.description)


def _check_resolvable_terms(state: CrateState) -> bool:
    """Value-level metadata includes resolvable identifiers."""
    for entity in state.list_entities():
        for field, value in entity.fields.items():
            if isinstance(value, str) and ("http" in value or "doi" in value.lower()):
                return True
    return False


def _check_standard_license(state: CrateState, graph: Graph = None) -> bool:
    """DSM-3-C7 — the descriptor references a standard reuse licence.

    Answered by the RDA check for the same question, so the two instruments cannot
    return different verdicts about one crate.
    """
    return _check_license_standard(state, graph)


def _check_domain_standard(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-3-R3 — descriptor uses a community-defined metadata standard.

    Same question as RDA-R1.3-01M, so it stays the same function: two implementations
    of one question is how two axes come to disagree about one crate. It follows
    ``_check_conforms_to_profile`` onto the graph for that reason, not because the DSM
    ladder needed it.
    """
    return _check_conforms_to_profile(state, graph)


def _check_standard_field_metadata(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-3-C6 — Dataset Descriptor includes standard-compliant Field-level Metadata as
    prescribed by the adopted standard Dataset Model.

    The old check was ``len(state.list_entities()) > 1`` — **byte-identical** to
    DSM-2-C6's, so the locally-defined rung and the adopted-standard rung of one question
    were literally the same test and could never disagree. An empty crate met both. The
    published difference is the whole content of this indicator: L2 says "as prescribed
    by a **locally defined** Dataset Model", L3 "standard-compliant … as prescribed by
    the **adopted standard** Dataset Model".

    Two limbs.

    *Over data that exists.* A schema over zero rows asserts nothing — the crate says so
    itself: ``_EMPTY_CONDITION_TABLE_NOTE`` reads "Any schema-level conformance claim
    over these columns is vacuous until rows are populated" (#473). But that note is
    written only where a definite zero was measured (95 of the 274 schema-bearing tables
    here), so emptiness is established from the file instead: a delimited table holding at
    least one record is longer than its own header line by at least one record, and both
    quantities are derivable from the schema's own column ``titles``.

    **The margin, and why it is not equality.** The header is derived as
    ``",".join(titles)`` plus one LF byte, which assumes LF, while RFC 4180 mandates CRLF
    and this project's own writer (``data_content.py``, ``csv.DictWriter`` at its default
    CRLF line terminator) emits one more byte than the derivation predicts. Judging
    "populated" as ``size > header`` therefore sits at zero bytes of margin: every one of
    the 135 size-judged tables in the corpus lands at exactly ``size - header == 0``, and
    a +1 byte shift — a CRLF terminator, a UTF-8 BOM — flips 39 of 62 crates from False
    to True with no data added. So the cut is spec-anchored instead: a minimal CSV record
    over *n* columns is at least *n* bytes (*n-1* delimiters plus a terminator), and a
    table counts as populated only at ``size >= header + _ENCODING_SLACK + len(cols)``.
    ``_ENCODING_SLACK`` covers the two ways a header-only file can be longer than the
    derivation says without holding a record — a UTF-8 BOM and a CRLF terminator on the
    header line. Both bounds are read off the specifications, not off the corpus; a
    sensitivity run showed this corpus flat out to a margin of 400 bytes and the one
    genuinely populated example surviving to 439, so the margin costs nothing. A
    ``csvw:rowCount`` stated on the table would supersede the byte estimate entirely; no
    crate on hand states one.

    A table whose size or titles are not stated is counted as *not evidenced* rather than
    as populated, because "it holds data" is a positive claim. The ``#473`` note is read
    before the byte estimate, so a stale note out-votes a stated size — the conservative
    order, and the one place the two emptiness signals can mask each other.

    Explicitly **not** the ``AUTOGENERATED`` name prefix. ``_synth_condition_table``
    writes that name unconditionally and ``populate_condition_table`` never clears it, so
    keying on it would mean no populated depositor table could ever earn the indicator —
    the mirror image of the tautology. Rows are the honest provenance signal anyway: they
    are the depositor's plate map, whoever named the file.

    *In the adopted standard's vocabulary.* Compliance is not partial, so **every** column
    of such a table must declare a ``datatype`` and a ``propertyUrl`` that is an external
    IRI, read through :func:`_any_external` so that the ``{"@id": …}`` and list spellings
    ``_build_csvw_schema`` itself uses for the sibling ``valueUrl`` are not scored as
    absent. Stricter than DSM-3-C3 (``standard_field_names``), which asks only whether
    *some* field name maps to an ontology, and stricter than DSM-2-C6, which may credit a
    locally declared schema.

    Not evidence: the root always declares ``conformsTo`` both profile IRIs, so "a
    standard has been adopted" is a constant and cannot be part of the answer.

    Reachable by data alone, and measured: every one of the 274 schema-bearing tables on
    hand already satisfies the vocabulary limb, and every one is header-only, so the
    indicator turns entirely on rows. Running ``populate_condition_table`` with the
    depositor's per-well design and re-exporting takes one table from 129 bytes to 568 and
    flips this to True.

    Scope: every node carrying a ``tableSchema``, not only the payload the root gathers —
    field-level metadata in the descriptor counts wherever it is attached, and a schema
    hung off a process result outside ``hasPart`` is still in the descriptor. Inline
    schema and column objects are read in place; a ``columns`` or ``tableSchema`` given as
    a bare object or a scalar is normalised.
    """
    if _needs_graph(graph):
        return None
    from builder.tools._crate_mapping import _EMPTY_CONDITION_TABLE_NOTE

    by_id = {str(n.get("@id")): n for n in _nodes(graph)}

    def _resolve(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict) and set(value) - {"@id"}:
            return value
        node = by_id.get(_ref_id(value))
        return node if isinstance(node, dict) else None

    populated: list[dict[str, Any]] = []
    compliant: list[dict[str, Any]] = []
    vacuous = unmeasured = 0
    for node in _nodes(graph):
        if not node.get("tableSchema"):
            continue
        cols = [
            column
            for ref in _values(node, "tableSchema")
            if (schema := _resolve(ref)) is not None
            for entry in _values(schema, "columns")
            if (column := _resolve(entry)) is not None
        ]
        if not cols:
            continue
        titles = [str(c.get("titles") or c.get("name") or "") for c in cols]
        size = str(node.get("contentSize") or "").strip()
        delimited = _media_type(node) in _DELIMITED_MEDIA_TYPES or str(
            node.get("@id") or ""
        ).lower().endswith(_DELIMITED_SUFFIXES)
        header = len((",".join(titles) + "\n").encode("utf-8"))
        if _text(node.get("description")) == _text(_EMPTY_CONDITION_TABLE_NOTE):
            vacuous += 1
            continue
        if not (size.isdigit() and all(titles) and delimited):
            unmeasured += 1
            continue
        if int(size) < header + _ENCODING_SLACK + len(cols):
            vacuous += 1
            continue
        populated.append(node)
        if all(c.get("datatype") and _any_external(c.get("propertyUrl")) for c in cols):
            compliant.append(node)
    return Verdict(
        bool(compliant),
        f"{len(compliant)} of {len(populated)} data tables holding rows declare every "
        f"field against an external vocabulary"
        + (f"; {vacuous} schema(s) describe a table with no rows" if vacuous else "")
        + (f"; {unmeasured} table(s) state no size to judge" if unmeasured else ""),
    )


def _check_controlled_values(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-3-C4 — Where applicable, Dataset Field Values are standardised against
    domain-specific Controlled Terminologies and/or Ontology Terms.

    The old check credited any session field value containing an underscore or a colon.

    **On "Where applicable".** It restricts *which* values are judged; it does not
    license a pass when none exist. Scored from a crate, "nothing applicable" and
    "nothing done" are the same evidence, and reading the second as the first is the
    whole of #670. So a crate carrying no value of a kind the domain has a terminology
    for fails, with that stated as the reason. **Consequence, by design: a crate from a
    domain with no chemical and no cell-line values fails this indicator
    unconditionally.** That is the reading under which DSM-3-C4 can fail at all, and the
    indicator is scoped ``partial`` in the workbook for exactly this reason.

    **What the population is.** Every ``MolecularEntity`` and every cell-line ``Sample``
    in the crate — not only those a condition-table column happens to point at. These are
    the chemical and cell-line field values the crate publishes, however they got there;
    no claim is made that a table column licensed the population.

    "are standardised" is unquantified, so it is a claim about all of them: one bound
    compound beside a cell line named only ``H4`` has not standardised its field values.
    That sentence is the threshold anchor, which is why no ratio is fitted.

    Two scaffolding traps avoided. ``sampleType`` is not counted: the builder writes
    ``NCIT:C16403`` ("Cell Line") onto every cell line, which types the *field*, not the
    *value*. Nor is a bare accession counted, only a resolvable IRI — the reason
    :func:`_root_pid` gives, that an accession is unique inside its registry and
    ambiguous outside it.

    The type test goes through :func:`_matches_slot_type`, i.e. through
    ``mit_assessment``'s canonical D16 matcher, rather than comparing ``additionalType``
    as a raw string. A re-implementation that compared it literally was measured: writing
    the legal CURIE ``isa:CellLine`` made the entire cell-line population evaporate and
    flipped 39 of the 42 failing crates to True.

    **What a True verdict is and is not evidence of.** Measured over the corpus, 578 of
    582 ``MolecularEntity`` nodes already carry a PubChem compound IRI as their ``@id``
    while only 89 of 134 cell lines are bound. The compound limb is a near-constant of the
    assembler; a True verdict here is almost entirely a statement about cell lines. The
    evidence string reports the two limbs separately so a reader is not misled about which
    one was tested. A ``Sample`` that simply omits ``additionalType``, or spells it "Cell
    Line", still leaves the population — inherent to typing by a D16 string.
    """
    if _needs_graph(graph):
        return None

    def is_subject(node: dict[str, Any]) -> bool:
        return any(
            _matches_slot_type(node, slot) for slot in ("MolecularEntity", "CellLineSample")
        )

    def bound(node: dict[str, Any]) -> bool:
        if _is_external_iri(node.get("@id")):
            return True
        return any(
            _is_external_iri(item)
            for key in ("identifier", "sameAs", "url")
            for item in _values(node, key)
        )

    subjects = [n for n in _nodes(graph) if is_subject(n)]
    if not subjects:
        return Verdict(
            False,
            "no chemical or cell-line field value in the crate for a domain terminology "
            "to standardise",
        )
    chem = [n for n in subjects if "MolecularEntity" in _node_types(n)]
    lines = [n for n in subjects if "MolecularEntity" not in _node_types(n)]
    unbound = sorted({str(n.get("name") or n.get("@id")) for n in subjects if not bound(n)})
    return Verdict(
        not unbound,
        f"{len(chem) - len([n for n in chem if not bound(n)])} of {len(chem)} chemical "
        f"and {len(lines) - len([n for n in lines if not bound(n)])} of {len(lines)} "
        "cell-line field values resolve to a domain terminology"
        + (f"; named only in free text: {', '.join(unbound[:3])}" if unbound else ""),
    )


# Public registers that mint identifiers for the domain entities an in-vitro study
# reports — the substances tested, and the biological material they were tested on.
# Named by the reporting standards this tool already serves (OECD/ECHA substance
# identity: CAS, InChIKey, DTXSID, EC; biological resources: Cellosaurus, RRID, NCBI
# Taxonomy; ontology PURLs for everything else), plus the resolvers that front them.
# It is a list of registers, not a cut fitted to a distribution.
_REGISTER_HOSTS = (
    "pubchem.ncbi.nlm.nih.gov", "comptox.epa.gov", "cellosaurus.org",
    "www.cellosaurus.org", "identifiers.org", "n2t.net", "bioregistry.io",
    "purl.obolibrary.org", "purl.bioontology.org", "ebi.ac.uk", "www.ebi.ac.uk",
    "ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "rrid.site", "scicrunch.org",
    "www.wikidata.org",
)
_REGISTER_ACCESSIONS = (
    "DTXSID", "DTXCID", "CHEBI:", "CHEBI_", "CVCL_", "CVCL:", "RRID:", "CHEMBL",
    "UNII:", "INCHIKEY=", "NCBITAXON:", "NCBITAXON_", "UBERON:", "UBERON_", "CL:",
    "EFO:", "CAS:", "CASRN:", "ATCC:",
)
_IDENTITY_KEYS = (
    "identifier", "sameAs", "url", "termCode", "propertyID", "alternateName", "@id",
)
# The crate's own packaging: files, datasets and tables are not domain entities. Read as
# a *typing* test, never as membership of the ``hasPart`` closure — see
# :func:`_check_standard_identifiers` for what the latter costs.
_PACKAGING_TYPES = frozenset({"File", "Dataset", "Table"})


def _register_iri(value: Any) -> bool:
    """An IRI minted by a public register — not merely an absolute URL."""
    iri = _ref_id(value)
    if not iri.startswith(("http://", "https://")):
        return False
    parts = iri.split("/")
    return len(parts) > 2 and parts[2].lower() in _REGISTER_HOSTS


def _register_accession(value: Any) -> bool:
    """A bare accession from a public register (``DTXSID…``, ``CVCL_…``, ``CHEBI:…``)."""
    return isinstance(value, str) and value.strip().upper().startswith(_REGISTER_ACCESSIONS)


def _check_standard_identifiers(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-4-C4 — Values for key Domain Entities reported in the Dataset(s) are
    standardised and assigned unique Standard Identifiers.

    The subjects are the domain entities the crate's **datasets** report, gathered from
    the three independent places the crate states them: a field value that resolves to
    one (``csvw:Column.valueUrl``), a table that says what it is about
    (``csvw:Table.about``), and the process that produced the table saying what went into
    it (``LabProcess.input`` / ``object``).

    **Three sources, because one is gameable.** A denominator built from ``valueUrl``
    alone and filtered through a closed list of expected types lets a crate retype its
    process-output ``Sample`` nodes from ``["Sample", "Thing"]`` to bare ``Thing`` and
    earn the indicator by typing its cell-line values *less*. There is no type allowlist
    here at all: whatever the datasets name is a subject, and a subject that is absent,
    untyped or unexpected counts as unidentified rather than disappearing. And because
    the same entity is named in three places, no single deletion removes it from the
    denominator; removing it from all three deletes the crate's record of which
    biological material the assay used.

    **Crate parts leave the denominator by their type, never by their position.** A
    subject is dropped only when the node it resolves to is typed ``File``, ``Dataset`` or
    ``Table`` (``_PACKAGING_TYPES``). Dropping it for being named anywhere in
    :func:`_payload_files`' ``hasPart`` closure — which carries no type gate — reopened
    the same silent-vanish hole one layer down: appending the four unidentified output
    ``Sample`` nodes of ``svhps22_real_input_crate_v20`` to the root's ``hasPart``, a pure
    addition that says nothing false about identity, flipped that crate and 60 of 60
    applicable crates from False to True.

    **A register, not any URL.** Counting any absolute IRI lets a crate raise its score by
    repointing a column at a lab wiki page. An identifier here has to come from a public
    register — an IRI on a register host, or an accession such as ``DTXSID…``, ``CVCL_…``,
    ``CHEBI:…``. **Stated limitation: this is a syntax test, not a resolution test.** The
    tool is offline by design (#117), so a syntactically valid but fabricated accession
    passes; writing the constant ``CVCL_0000`` onto every crate-local node was measured
    flipping 58 of 60 crates to True. It raises the cost of inventing an identifier; it
    cannot make it impossible.

    ``all``, not a majority: the published statement quantifies over the values the
    datasets report. Measured on this corpus every crate fails, and it fails for a
    specific, fixable reason — the ``compound`` column resolves to a PubChem IRI while the
    ``cell_line`` column resolves to a locally minted output ``Sample`` carrying no
    identifier, even though the crate holds the Cellosaurus IRI for the same cells on the
    culture process's ``input``. Propagating that identifier onto the output Sample takes
    ``_v20`` from 10 of 14 to 12 of 14, so the indicator stays honest under the obvious
    builder fix rather than collapsing.

    The old check scanned ``entity.fields`` for a key named ``identifier`` / ``accession``
    / ``doi`` / ``orcid`` / ``ror``, which asks whether the *session* recorded an
    identifier for anything at all.
    """
    if _needs_graph(graph):
        return None
    by_id = {str(n.get("@id")): n for n in _nodes(graph) if n.get("@id")}
    tables = {str(n.get("@id")) for n in _nodes(graph) if "Table" in _node_types(n)}
    packaging = {i for i, n in by_id.items() if _node_types(n) & _PACKAGING_TYPES}

    subjects: set[str] = set()

    def _collect(node: dict[str, Any], key: str) -> None:
        for item in _values(node, key):
            ident = _ref_id(item)
            if ident and ident not in packaging:
                subjects.add(ident)

    for node in _nodes(graph):
        types = _node_types(node)
        if "Column" in types:
            _collect(node, "valueUrl")
        if "Table" in types:
            _collect(node, "about")
        produced = {_ref_id(o) for o in _values(node, "output") + _values(node, "result")}
        if produced & tables:
            _collect(node, "input")
            _collect(node, "object")

    if not subjects:
        return Verdict(
            False,
            "the crate's datasets report no domain entity: no field value resolves to "
            "one, no table says what it is about, and no process names what it used",
        )

    def _identified(ident: str) -> bool:
        if _register_iri(ident) or _register_accession(ident):
            return True
        node = by_id.get(ident)
        if node is None:
            return False
        for key in _IDENTITY_KEYS:
            for item in _values(node, key):
                if _register_iri(item) or _register_accession(item):
                    return True
                named = by_id.get(_ref_id(item))
                if named is None:
                    continue
                if _register_iri(named.get("propertyID")):
                    return True
                if any(_register_accession(v) for v in _values(named, "value")):
                    return True
        return False

    identified = {s for s in subjects if _identified(s)}
    missing = sorted(subjects - identified)
    return Verdict(
        len(identified) == len(subjects),
        f"{len(identified)} of {len(subjects)} domain entities reported by the crate's "
        f"datasets carry an identifier from a public register"
        + (f"; unidentified: {', '.join(missing[:2])}" if missing else ""),
    )


def _check_linked_data(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-4-R2 — Dataset(s) are standardised to a defined Semantic Data Model and
    represented using Linked Data Representations suitable for data sharing and re-use.

    Asked of the *deposit's* published tabular data: every CSV/TSV the crate received
    must be described by a schema whose every field binds to an external property IRI.
    Binding is what makes the representation *linked* rather than merely declared.

    The old check was ``len(state.list_entities()) > 0``.

    **The scaffolding trap, and why the denominator is deposited files.** This builder
    mints a CSVW schema for the condition tables it writes itself, and every column of
    that template carries a ``propertyUrl`` — so those tables are the only ones in the
    corpus that are ever 100% bound. Counting them would let a deposit that ships no CSV
    of its own (all ``.xlsx``/``.prism``, which
    :func:`_check_non_proprietary_format` calls the corpus norm) score DSM-4-R2 purely off
    the builder's template: measured, a crate stripped of its three deposited CSVs scored
    "4 of 4 tabular datasets … bind to an external property IRI". So the minted tables
    leave **both** numerator and denominator, and the ratio becomes a statement about the
    deposit.

    ``all`` rather than a ratio, because the published statement is about the shareable
    Datasets, unquantified: one modelled table beside three unmodelled ones does not make
    the crate's datasets standardised.

    Non-tabular payload (a PDF protocol, a ``.prism`` blob) is outside the denominator: it
    cannot be standardised to a tabular model, and DSM-3-R5 is the indicator that asks
    about it. So is a spreadsheet, which means an ``.xlsx``-only deposit fails on the
    "no deposited tabular dataset" guard rather than on binding.

    Every JSON-LD shape is normalised before resolving — ``tableSchema``, ``columns`` and
    ``propertyUrl`` may each be a scalar, a list or a node object, and an array-wrapped
    schema must not read as "no schema" (:func:`_schema_fields`). A schema whose column
    references do not resolve in the crate is not credited.
    """
    if _needs_graph(graph):
        return None
    deposited, _minted = _deposited_files(graph)
    tables = [
        f
        for f in deposited
        if _media_type(f) in _DELIMITED_MEDIA_TYPES
        or str(f.get("@id") or "").lower().endswith(_DELIMITED_SUFFIXES)
    ]
    if not tables:
        return Verdict(False, "the crate publishes no deposited tabular dataset to standardise")
    resolved, dangling = _schema_fields(graph)
    linked = 0
    for table in tables:
        table_id = str(table.get("@id"))
        fields = resolved.get(table_id) or []
        if (
            fields
            and not dangling.get(table_id)
            and all(
                (urls := _values(f, "propertyUrl"))
                and all(_is_external_iri(u) for u in urls)
                for f in fields
            )
        ):
            linked += 1
    return Verdict(
        linked == len(tables),
        f"{linked} of {len(tables)} deposited tabular datasets are described by a schema "
        "whose every field binds to an external property IRI",
    )


def _check_semantic_model(state: CrateState) -> bool:
    """The Semantic Data Model is represented using Linked Data."""
    return len(state.list_entities()) > 0


def _check_machine_interpretable(state: CrateState, graph: Graph = None) -> Verdict | None:
    """DSM-4-R4 — A Semantic Data Model (Metadata) describing the data is represented in a
    Machine Readable and Machine Interpretable format.

    Distinct from DSM-4-R5, which this module answers with
    :func:`_check_machine_interpretable_graph`: R5 asks whether *a* resolvable term exists
    anywhere in the crate (one PropertyValue or one bound column is enough), while R4's
    granularity in ``fair/dsm_indicators.yaml`` is **Dataset Field Values** and its
    subject is the model *describing the data*. So R4 is answered over the fields that
    model declares, and only over models attached to a data file — a schema no file points
    at describes nothing.

    Two limbs, one for each adjective, both universal because the sentence is
    unquantified. *Machine readable*: every declared field carries an explicit
    ``datatype`` **and every datatype it declares is one CSVW/XSD defines** — anchored on
    CSVW, where an absent datatype defaults to ``string`` and therefore declares nothing,
    and where ``datatype: "bananas"`` declares nothing either. *Machine interpretable*:
    the model must say what the value **is** — an IRI a machine can resolve (``valueUrl``)
    or a literal it can compute with (a non-string XSD type).

    **Why a computable literal counts, and why ``propertyUrl`` does not.** R4's
    granularity is Dataset Field *Values*. ``propertyUrl`` types the field *name*, and
    that is DSM-3-C3's published question ("Dataset Field Names use standard controlled
    terms"), already scored by :func:`_check_standard_field_names`; crediting it here
    would score one question twice and — measured — would make this indicator True on 61
    of 62 crates, because the builder's own schema template binds ``propertyUrl`` on every
    column it writes. A declared ``xsd:double`` is weaker evidence than an IRI: it says
    the value is a number rather than what the number denotes. It is counted because at
    value level "this is a quantity" is a statement a machine can act on, where
    ``xsd:string`` is not — but it is the weaker half of the limb and a reader should
    treat the ratio, not the boolean, as the informative output.

    Datatypes are compared as normalised local names against a **whitelist**, never a
    blacklist (:func:`_datatype_names`): a blacklist makes the verdict flippable by
    notation alone, so ``xsd:string``, ``http://www.w3.org/2001/XMLSchema#string`` and
    ``["string", "double"]`` must all read as the same non-computable declaration. The
    stated cost of the local-name reduction is that a custom IRI whose local name collides
    is credited.

    The old check was ``len(state.list_entities()) > 0``.
    """
    if _needs_graph(graph):
        return None
    resolved, dangling = _schema_fields(graph)
    modelled = {t: f for t, f in resolved.items() if f}
    fields = [f for cols in modelled.values() for f in cols]
    if not fields:
        broken = [t for t, missing in dangling.items() if missing]
        return Verdict(
            False,
            f"{len(broken)} data file(s) declare a tableSchema whose columns do not "
            "resolve in the crate"
            if broken
            else "no data file in the crate is described by a field-level model",
        )
    readable = [f for f in fields if _declares_a_datatype(f)]
    interpretable = [
        f
        for f in fields
        if any(_is_external_iri(v) for v in _values(f, "valueUrl"))
        or _datatype_is_computable(f)
    ]
    return Verdict(
        len(readable) == len(fields) and len(interpretable) == len(fields),
        f"of {len(fields)} fields modelled across {len(modelled)} data file(s), "
        f"{len(readable)} declare a CSVW datatype and {len(interpretable)} say what the "
        "value is — a resolvable term or a computable literal",
    )


# ---------------------------------------------------------------------------
# Graph-aware DSM checks
#
# The DSM's Level 2-4 Content and Representation indicators ask about *dataset
# fields and values* — column names, datatypes, controlled vocabularies, formats.
# None of that exists on `CrateState`: it appears only once the crate is assembled,
# as csvw:Column nodes and encodingFormat on File nodes. So these checks read the
# assembled ``@graph``, the same source `assess_mit_coverage` moved to under #311
# ("the cheaper one was wrong, not approximate"). With no graph supplied they
# return False rather than guessing — absent evidence is not evidence.
# ---------------------------------------------------------------------------

# What a DSM check may hand back: a bare tri-state, or a Verdict carrying evidence.
# Both are accepted so evidence can be enriched check by check rather than in a
# flag-day change to all forty.
CheckResult = "bool | None | Verdict"
DsmCheck = Callable[[CrateState, "Graph"], "bool | None | Verdict"]



def _check_tidy_dataset(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C2 — data structured per Tidy Data Principles.

    Tidy means one column per variable, machine-declared. The crate evidences that
    when a table's schema names several columns and each carries a ``datatype``; a
    table whose columns are untyped is not a declared structure.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    typed = [c for c in cols if c.get("datatype")]
    return Verdict(
        len(cols) >= 2 and len(typed) == len(cols),
        f"{len(typed)} of {len(cols)} declared columns carry a datatype",
    )


def _check_reference_fields(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C3 — Reference Fields that enable joining related datasets.

    A join is evidenced by a column whose ``valueUrl`` resolves to another entity
    **in this crate**: that is the foreign key. An external ontology IRI types the
    column but joins nothing.
    """
    if _needs_graph(graph):
        return None
    ids = {str(n.get("@id")) for n in _nodes(graph) if n.get("@id")}
    cols = _columns(graph)
    joins = [c for c in cols if _ref_id(c.get("valueUrl")) in ids and c.get("valueUrl")]
    names = ", ".join(str(c.get("name") or c.get("titles") or "?") for c in joins[:3])
    return Verdict(
        bool(joins),
        f"{len(joins)} of {len(cols)} columns resolve a valueUrl to an in-crate entity"
        + (f" ({names})" if joins else ""),
    )


def _check_local_data_dictionary(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-C4 — Field Values standardised against a Data Dictionary.

    Evidenced by a column that both declares a datatype and binds its values to a
    declared vocabulary (``valueUrl``) or property (``propertyUrl``).
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    bound = [c for c in cols if c.get("datatype") and (c.get("valueUrl") or c.get("propertyUrl"))]
    return Verdict(
        bool(bound),
        f"{len(bound)} of {len(cols)} columns are both typed and bound to a vocabulary",
    )


def _check_local_dataset_model(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-2-R2 — Dataset(s) standardised to a locally defined Dataset Model."""
    if _needs_graph(graph):
        return None
    tables = [n for n in _nodes(graph) if n.get("tableSchema")]
    return Verdict(bool(tables), f"{len(tables)} data table(s) declare a tableSchema")


def _check_model_documentation_human(state: CrateState, graph: Graph) -> bool | None:
    """DSM-2-R4 — the Domain Model documented in a Human Readable Format.

    A described protocol is that documentation; a bare protocol *name* is not.
    """
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        types = _node_types(node)
        if "LabProtocol" in types and str(node.get("description") or "").strip():
            return True
    return False


def _check_standard_field_names(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-3-C3 — Dataset Field Names use standard controlled terms.

    Evidenced by a column whose ``propertyUrl`` is an external ontology IRI.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    std = [c for c in cols if _is_external_iri(c.get("propertyUrl"))]
    return Verdict(
        bool(std),
        f"{len(std)} of {len(cols)} column names map to an external ontology IRI",
    )


def _check_community_domain_model(state: CrateState, graph: Graph) -> bool | None:
    """DSM-3-R1 / DSM-3-R4 — conformance to a community domain model, declared
    machine-readably as a resolvable profile IRI on the root."""
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        conforms = node.get("conformsTo")
        for entry in conforms if isinstance(conforms, list) else [conforms]:
            iri = _ref_id(entry)
            if iri.startswith("http") and "profile" in iri:
                return True
    return False


def _check_non_proprietary_format(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-3-R5 — Dataset(s) available in a non-proprietary Machine Readable Format as
    prescribed by a standard Dataset Model.

    The indicator the in-vitro corpus most often fails: GraphPad ``.prism``/``.pzf`` and
    legacy ``.xls`` need licensed software to read.

    Scored so that the L3 rung is a **subset** of the L1 rung
    (:func:`_check_data_machine_readable`) rather than an independent list: "open" is
    ``_MACHINE_READABLE_MEDIA_TYPES & _OPEN_MEDIA_TYPES``, so nothing L1 calls a rendering
    (``text/plain``, ``application/pdf``, ``image/png``) can be called an open *dataset*
    here. It also reads the same deposited-file population, so the builder's own CSVs stop
    answering it, and the media type is read list- and node-object-safely.

    Before this the published report could assert "Dataset(s) are NOT available in a
    Machine Readable Format" at Level 1 and "Dataset(s) available in non-proprietary
    Machine Readable Format" at Level 3 about one crate; the cumulative level hid it, the
    indicator table did not. Executed on a docx-only crate, the previous body returned
    True, "2 of 3 files are in an open format". Verified over all 62 corpus crates: there
    is no crate where this is True and DSM-1-R5 is not.

    The narrowing is a real behaviour change to this indicator, made as part of #670's
    DSM rewrite rather than in its own change: a deposit whose only data is a delimited
    ``.txt`` now fails where it passed. The direction is down.
    """
    if _needs_graph(graph):
        return None
    open_and_readable = _MACHINE_READABLE_MEDIA_TYPES & _OPEN_MEDIA_TYPES
    deposited, minted = _deposited_files(graph)
    if not deposited:
        return Verdict(
            False,
            "the crate carries no deposited data file"
            + (f" ({len(minted)} payload files, all builder-generated)" if minted else ""),
        )
    fmts = [_media_type(f) for f in deposited]
    open_fmts = [f for f in fmts if f in open_and_readable]
    closed = sorted({f for f in fmts if f and f not in open_and_readable})
    return Verdict(
        bool(open_fmts),
        f"{len(open_fmts)} of {len(deposited)} deposited files are data in an open format"
        + (f"; proprietary or not data: {', '.join(closed[:3])}" if closed else ""),
    )


def _check_semantic_study_design(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C1 — the Semantic Data Model includes study design elements AND the
    relationships between them: a typed process that is actually wired to both an
    input and an output."""
    if _needs_graph(graph):
        return None
    for node in _nodes(graph):
        if "LabProcess" not in _node_types(node):
            continue
        if (node.get("object") or node.get("agent")) and node.get("result"):
            return True
    return False


def _check_common_data_elements(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C3 / DSM-4-C2 — key Dataset Fields mapped to Common Data Elements.

    Stricter than DSM-3-C3's "at least one": a mapping is only a model when most of
    the declared fields carry one.
    """
    if _needs_graph(graph):
        return None
    cols = _columns(graph)
    if not cols:
        return False
    mapped = sum(1 for c in cols if _is_external_iri(c.get("propertyUrl")))
    return mapped >= max(2, len(cols) // 2)


def _check_cde_relationships(state: CrateState, graph: Graph) -> bool | None:
    """DSM-4-C5 — a pre-defined set of Common Data Elements, reported in the
    Datasets, with the relationships between them: DefinedTerm entities that
    something in the crate actually references."""
    if _needs_graph(graph):
        return None
    defined = {
        str(n.get("@id")) for n in _nodes(graph) if "DefinedTerm" in _node_types(n)
    }
    if not defined:
        return False
    for node in _nodes(graph):
        for key, value in node.items():
            if key in ("@id", "@type"):
                continue
            for item in value if isinstance(value, list) else [value]:
                if _ref_id(item) in defined and str(node.get("@id")) not in defined:
                    return True
    return False


def _check_semantic_contextual_metadata(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-4-R1 — Contextual Metadata represented by semantically defined Common
    Data Elements: a PropertyValue carrying an external ``propertyID`` IRI."""
    if _needs_graph(graph):
        return None
    pvs = [n for n in _nodes(graph) if "PropertyValue" in _node_types(n)]
    typed = [n for n in pvs if _is_external_iri(n.get("propertyID"))]
    return Verdict(
        bool(typed),
        f"{len(typed)} of {len(pvs)} PropertyValues carry an external propertyID IRI",
    )


def _check_machine_interpretable_graph(state: CrateState, graph: Graph) -> Verdict | None:
    """DSM-4-R5 — available in a Machine Readable AND Machine *Interpretable*
    format. Readable is the JSON-LD serialisation; interpretable additionally
    requires the terms to be resolvable, i.e. external IRIs in the graph."""
    if _needs_graph(graph):
        return None
    semantic = _as_verdict(_check_semantic_contextual_metadata(state, graph))
    named = _as_verdict(_check_standard_field_names(state, graph))
    return Verdict(
        semantic.value is True or named.value is True,
        "; ".join(e for e in (semantic.evidence, named.evidence) if e),
    )


def _check_min_info_guidelines(state: CrateState, graph: Graph) -> bool:
    """DSM-3-C1 — study-level metadata reported per Minimum Information Reporting
    Guidelines. The model itself cross-references RDA-R1.3-01M, which this tool
    operationalises as OECD MIT coverage (see mit_assessment).

    ``state.mit_assessment`` is a field nothing ever assigns, so this answers False on
    every build however much of the checklist the crate covers. Scoring MIT from the
    graph here instead is not the fix on its own: ``_mit_has_coverage`` is "any coverage
    at all", which an empty assembled crate already meets (measured: 1%), so the
    indicator would swap a constant False for a constant True. "In compliance with"
    needs a bar anchored to what the guidelines require, not to what this corpus
    happens to score. Tracked as #705; the report says as much in the indicator's own
    remedy rather than publishing an instruction that cannot work.
    """
    return _mit_has_coverage(state.mit_assessment)


def _state_check(fn: Callable[[CrateState], bool]) -> DsmCheck:
    """Adapt a CrateState-only check to the shared ``(state, graph)`` shape.

    One registry, one call shape. Wrapping the older checks here rather than
    keeping a second dict is what stops "which checks see the graph?" becoming a
    thing that can drift.
    """

    def _wrapped(state: CrateState, _graph: Graph) -> bool:
        return fn(state)

    # Kept reachable so a test can prove that a Bridge2AI criterion declaring it
    # shares this check really calls this function, not a look-alike of its own.
    setattr(_wrapped, "__wrapped_check__", fn)  # noqa: B010
    return _wrapped


# RDA checks that take the assembled ``@graph`` as a second argument. The rest of the
# registry is state-only; this set is the migration boundary, not a permanent design —
# see #665 for why the licence indicators moved first and what a wider move would cost.
_GRAPH_AWARE_FAIR_CHECKS: frozenset[str] = frozenset(
    {
        "license_present",
        "license_standard",
        "license_machine",
        # #670: these four asked their question of CrateState and could not fail. They
        # now read the crate a reader receives, and answer "not assessed" rather than
        # guess when no graph was supplied.
        "pid_form",
        "every_entity_has_id",
        "metadata_refs_data",
        "fair_vocabularies",
        "reuse_attributes",
        # These three could already answer honestly on an empty crate, but answered
        # from session state a reader never receives. Moving them closes the
        # reproducibility half of #670 without changing any verdict.
        "jsonld_context",
        "provenance",
        "conforms_to_profile",
    }
)

# Map check names to functions
FAIR_CHECKS: dict[str, Any] = {
    "root_global_id": _check_root_global_id,
    "every_entity_has_id": _check_every_entity_has_id,
    "pid_form": _check_pid_form,
    "rich_metadata": _check_rich_metadata,
    "metadata_refs_data": _check_metadata_refs_data,
    "jsonld_context": _check_jsonld_context,
    "fair_vocabularies": _check_fair_vocabularies,
    "qualified_refs": _check_qualified_refs,
    "reuse_attributes": _check_reuse_attributes,
    "license_present": _check_license_present,
    "license_standard": _check_license_standard,
    "license_machine": _check_license_machine,
    "provenance": _check_provenance,
    "conforms_to_profile": _check_conforms_to_profile,
    "mit_coverage": _check_mit_coverage_indicator,
}

# Every value takes ``(state, graph)``. Older CrateState-only checks are adapted
# by ``_state_check``; the Level 2-4 field/value indicators read the graph directly.
DSM_CHECKS: dict[str, DsmCheck] = {
    # --- #670: these read the assembled crate, and answer "not assessed" without one ---
    "unique_id": _check_unique_id,
    "study_summary": _check_study_summary,
    "dataset_metadata": _check_dataset_metadata,
    "dataset_hierarchy": _check_dataset_hierarchy,
    "general_schema": _check_general_schema,
    "descriptor_machine_readable": _check_descriptor_machine_readable,
    "data_machine_readable": _check_data_machine_readable,
    "cross_dataset_refs": _check_cross_dataset_refs,
    "field_level_metadata": _check_field_level_metadata,
    "data_structured": _check_data_structured,
    "standard_field_metadata": _check_standard_field_metadata,
    "controlled_values": _check_controlled_values,
    "standard_identifiers": _check_standard_identifiers,
    "linked_data": _check_linked_data,
    "machine_interpretable": _check_machine_interpretable,
    # --- still scored from CrateState: the rewrites #670 could not land honestly ---
    # Nine refuted rewrites over these eight entries — ``domain_model`` backs both
    # DSM-2-C1 and DSM-2-R1, and both proposals for it were rejected. Each resisted two
    # rounds of adversarial review, and each was refuted by an edit that carries no
    # information: deleting a declaration, retyping a node, or truncating an IRI raised
    # the score. ``test_every_dsm_check_reads_the_crate`` pins the list with the reason
    # for each; it is a burn-down, so the number may only go down.
    "access_info": _state_check(_check_access_info),
    "has_descriptor": _state_check(_check_has_descriptor),
    "context_fields": _state_check(_check_context_fields),
    "value_level_metadata": _state_check(_check_value_level_metadata),
    "generic_model": _state_check(_check_generic_model),
    "domain_model": _state_check(_check_domain_model),
    "resolvable_terms": _state_check(_check_resolvable_terms),
    "semantic_model": _state_check(_check_semantic_model),
    # --- graph-aware already, because they share an RDA check ---
    "standard_license": _check_standard_license,
    "domain_standard": _check_domain_standard,
    # DSM-4-R6 "License information is formally represented/encoded in a Machine
    # Readable Format" reuses the RDA R1.1-03M check — same question, both models.
    # It was defined but never registered here, so _compute_dsm_level skipped it
    # and handed out Level 4 for free.
    "license_machine": _check_license_machine,
    # --- graph-aware: the DSM's dataset field/value indicators (Levels 2-4) ---
    "tidy_dataset": _check_tidy_dataset,
    "reference_fields": _check_reference_fields,
    "local_data_dictionary": _check_local_data_dictionary,
    "local_dataset_model": _check_local_dataset_model,
    "model_documentation_human": _check_model_documentation_human,
    "standard_field_names": _check_standard_field_names,
    "community_domain_model": _check_community_domain_model,
    "non_proprietary_format": _check_non_proprietary_format,
    "semantic_study_design": _check_semantic_study_design,
    "common_data_elements": _check_common_data_elements,
    "cde_relationships": _check_cde_relationships,
    "semantic_contextual_metadata": _check_semantic_contextual_metadata,
    "machine_interpretable_graph": _check_machine_interpretable_graph,
    "min_info_guidelines": _check_min_info_guidelines,
}


def assess_fair_maturity(
    state: CrateState, *, mit: MITReport | None = None, graph: Graph = None
) -> FAIRReport:
    """Assess FAIR maturity from CrateState metadata.

    Checks basic FAIR indicators from fair/indicators.yaml and computes
    DSM level from fair/dsm_indicators.yaml check results.

    Args:
        state: The current CrateState to assess.
        mit: An already-computed MIT report to score the ``mit_coverage`` indicator
            (RDA-R1.3-01D) against. The report/export path computes MIT against the
            assembled ``@graph`` and passes it here, because ``state.mit_assessment``
            is never populated on that path (#311). When ``None`` the indicator falls
            back to ``state.mit_assessment`` (back-compat).

    Returns:
        A FAIRReport with indicator_results and dsm_level.
    """
    indicators_data = _load_yaml(FAIR_INDICATORS_PATH)
    dsm_data = _load_yaml(DSM_INDICATORS_PATH)

    # The mit_coverage indicator is scored from the caller-supplied report when
    # given (graph-based), else from the state's own (possibly empty) assessment.
    mit_report = mit if mit is not None else state.mit_assessment

    indicator_results: list[dict[str, Any]] = []

    # Run RDA indicator checks
    if indicators_data:
        indicators = indicators_data.get("indicators", [])
        for indicator in indicators:
            check_name = indicator.get("check", "")
            scope = indicator.get("scope", "")

            if scope == "out_of_scope":
                indicator_results.append(
                    {
                        "id": indicator.get("id", ""),
                        "dimension": indicator.get("dimension", ""),
                        "priority": indicator.get("priority", ""),
                        "text": indicator.get("text", ""),
                        "passed": None,
                        "scope": "out_of_scope",
                    }
                )
            elif check_name in FAIR_CHECKS:
                # mit_coverage is scored from the (graph-based) report, not the
                # never-populated state.mit_assessment (#311).
                if check_name == "mit_coverage":
                    passed = _mit_has_coverage(mit_report)
                elif check_name in _GRAPH_AWARE_FAIR_CHECKS:
                    # The licence lives on the assembled root, not on CrateState —
                    # see _effective_license. Passing the graph is what makes this
                    # indicator answerable from the crate a reader receives (#665),
                    # and is what lets the #670 checks ask about data rather than
                    # about how many entities the session happens to hold.
                    passed = _as_verdict(FAIR_CHECKS[check_name](state, graph)).value
                else:
                    passed = FAIR_CHECKS[check_name](state)
                indicator_results.append(
                    {
                        "id": indicator.get("id", ""),
                        "dimension": indicator.get("dimension", ""),
                        "priority": indicator.get("priority", ""),
                        "text": indicator.get("text", ""),
                        "passed": passed,
                    }
                )

    dsm_level = _compute_dsm_level(state, dsm_data, graph)

    return FAIRReport(
        indicator_results=indicator_results,
        dsm_level=dsm_level,
    )


def _assessable_indicators(
    dsm_data: dict[str, Any], level: int, answers: dict[str, Verdict] | None = None
) -> list[dict[str, Any]]:
    """The indicators at *level* that carry an answer at all.

    One definition, used by both :func:`_compute_dsm_level` and :func:`dsm_ceiling`, so
    "what counts as assessable" cannot drift between the level a crate is awarded and
    the blockers reported for the level above it.

    An indicator scoped ``na`` is not assessable *from a crate* — the published model
    carries all 83 indicators and most describe a hosting environment, a Level-0
    pre-FAIRification state, or content we do not inspect — but the published tool puts
    those to a person, so one the depositor answered counts here too. An indicator that
    *is* scoped for assessment but names a check nothing registers is a **wiring bug**,
    not a pass: it raises rather than being silently skipped, which is how DSM-4-R6 went
    unnoticed while its level was awarded for free.
    """
    out: list[dict[str, Any]] = []
    for ind in dsm_data.get("indicators", []):
        if ind.get("level") != level:
            continue
        if ind.get("scope", "na") == "na":
            if str(ind.get("id")) in (answers or {}):
                out.append(ind)
            continue
        check_name = str(ind.get("check") or "")
        if check_name not in DSM_CHECKS:
            raise KeyError(
                f"DSM indicator {ind.get('id')} is scoped {ind.get('scope')!r} but its "
                f"check {check_name!r} is not registered in DSM_CHECKS. Register it or "
                f"scope the indicator 'na' in scripts/gen_dsm_indicators.py."
            )
        out.append(ind)
    return out


def _failing_at(
    dsm_data: dict[str, Any], answers: dict[str, Verdict], level: int
) -> list[tuple[str, str, str]]:
    """``(id, the model's own text, our evidence)`` for every indicator failing at *level*.

    One definition shared by the ceiling and the blockers list, so the "what stands in
    the way" answer cannot differ between the two places the report shows it.
    """
    out: list[tuple[str, str, str]] = []
    for ind in _assessable_indicators(dsm_data, level, answers):
        verdict = answers.get(str(ind.get("id") or ""))
        if verdict is not None and verdict.value is False:
            out.append(
                (str(ind.get("id") or ""), str(ind.get("text") or ""), verdict.evidence)
            )
    return out


def dsm_verdicts(
    state: CrateState, dsm_data: dict[str, Any] | None = None, graph: Graph = None
) -> dict[str, Verdict]:
    """Every assessable indicator's verdict, with the published sheet's ladder applied.

    The single evaluation pass. The level, the grid, the ceiling and the blockers all
    read this map, so the four can never disagree about what an indicator answered.

    **The ladder is the sheet's, and it promotes.** The model's statements nest — within
    one question, "Dataset(s) are standardised to a *community* Standard Dataset Model"
    (L3) sits above "...to a *locally defined* Dataset Model" (L2) — and the published
    workbook resolves that in its validation column: ``J4`` is ``=IF(J5=1,1,H4)``, so
    meeting the higher rung satisfies the lower one. Nine such rules exist, all in the
    Representation block, and each names one source; the sheet leaves every other pair
    independent. :func:`_apply_promotion` reproduces them, so a crate scores what a
    depositor filling in the sheet by hand would score.
    """
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return {}

    verdicts: dict[str, Verdict] = {}
    for ind in dsm_data.get("indicators", []):
        ident = str(ind.get("id") or "")
        if not ident:
            continue
        if ind.get("scope", "na") == "na":
            # Not assessable from a crate — but the published tool asks a person, so a
            # depositor may answer it. The gate is deliberately on `na`: where the crate
            # can answer, the crate answers, and no answer file can overrule it.
            answer = (state.dsm_answers or {}).get(ident)
            if isinstance(answer, bool):
                verdicts[ident] = Verdict(
                    answer, "answered by the depositor; not evidenced by the crate"
                )
            continue
        check = DSM_CHECKS.get(str(ind.get("check") or ""))
        if check is None:
            raise KeyError(
                f"DSM indicator {ident} is scoped {ind.get('scope')!r} but its check "
                f"{ind.get('check')!r} is not registered in DSM_CHECKS. Register it or "
                f"scope the indicator 'na' in scripts/gen_dsm_indicators.py."
            )
        verdicts[ident] = _as_verdict(check(state, graph))

    _apply_promotion(verdicts, (dsm_data.get("scoring") or {}).get("promotion") or [])
    return verdicts


def load_dsm_answers(path: Path | str) -> dict[str, bool]:
    """Read a depositor's answers to the indicators no crate can evidence.

    A flat ``{indicator id: true/false}`` YAML file, because the depositor is
    describing their *repository* rather than one build: the same answers hold for
    every crate they deposit there, so the file lives with them and is passed in with
    ``--dsm-answers``.

    Anything that is not a published indicator id answered with a boolean is dropped
    with a warning. "yes" is not ``True``: an assessment a person will publish must not
    be able to acquire a pass from a typo.
    """
    data = _load_yaml(Path(path))
    if not isinstance(data, dict):
        return {}
    model = _load_yaml(DSM_INDICATORS_PATH) or {}
    known = {str(ind.get("id")) for ind in model.get("indicators", [])}
    answers: dict[str, bool] = {}
    for ident, value in data.items():
        if str(ident) in known and isinstance(value, bool):
            answers[str(ident)] = value
        else:
            logger.warning(
                "Ignoring DSM answer %r: %r is not a yes/no for a published indicator",
                ident,
                value,
            )
    return answers


def pre_verdicts(state: CrateState) -> dict[str, Verdict]:
    """The stored as-received verdicts — the sheet's "Pre-FAIRification" column.

    Captured once by ``AgentEngine.initialize`` and carried in the session, so a report
    rendered days later still states what the deposit looked like on arrival. Empty for
    a session written before the baseline existed, and for a run given no input to scan;
    callers render the post column alone rather than inventing a baseline.
    """
    return {
        ident: Verdict(stored.get("value"), str(stored.get("evidence", "")))
        for ident, stored in (state.pre_assessment or {}).items()
    }


def _apply_promotion(verdicts: dict[str, Verdict], rules: list[dict[str, str]]) -> None:
    """Apply the sheet's ``=IF(J{source}=1,1,H{own})`` rules to *verdicts*, in place.

    Two properties come straight from the formula and are easy to get wrong:

    * It fires over an **unanswered** target as well as a failed one. ``IF(J5=1,1,H4)``
      returns 1 whatever ``H4`` holds, so an indicator nothing measured is satisfied by
      the rung above it.
    * It does **not** fire from an unanswered source: ``IF(blank=1)`` is false.

    Promotion is monotone upward and can never manufacture a pass — every rule's source
    is itself a check that can fail, so a ``True`` here always traces back to a ``True``
    somewhere in its chain. The rules chain (``J10`` reads ``J11``, which reads ``J12``),
    so this runs to a fixed point rather than in one sweep.
    """
    for _ in range(len(rules)):
        changed = False
        for rule in rules:
            source = verdicts.get(rule["when"])
            target = verdicts.get(rule["then"])
            if source is None or source.value is not True:
                continue
            if target is not None and target.value is True:
                continue
            verdicts[rule["then"]] = Verdict(
                True,
                f"promoted by {rule['when']}: the published sheet's {rule['cell']} reads "
                f"=IF({rule['when']}=1,1,...), so the higher rung satisfies this one"
                + (
                    f" — measured: {target.evidence}"
                    if target is not None and target.evidence
                    else ""
                ),
            )
            changed = True
        if not changed:
            return


def _compute_dsm_level(
    state: CrateState,
    dsm_data: dict[str, Any] | None,
    graph: Graph = None,
    answers: dict[str, Verdict] | None = None,
) -> int:
    """Compute the FAIRplus Dataset Maturity level.

    Per the DSM model the ladder is **cumulative**: level N is awarded only when every
    assessable indicator at levels 1..N passes. Two rules keep the number honest:

    * **A level nobody assessed is never awarded.** Levels whose indicators are all
      ``na`` — Level 5 is entirely hosting/enterprise, so no crate can evidence it —
      would otherwise be handed out for free, because "no failures" and "no evidence"
      are not the same claim.
    * **Level 0 is not on the ladder.** Its indicators are *negative* statements of the
      pre-FAIRification state ("Dataset(s) are NOT Identifiable…"); failing them is the
      desired outcome, so scoring them would invert the scale.

    This number is **ours, not the model's**: no formula anywhere in the published
    workbook computes an achieved level. It is a gate over the model's indicators, kept
    because "how far up the ladder" is the question depositors ask, and the report's
    footnote says so in as many words, so it is never mistaken for the published score —
    which is the percentage grid (:func:`dsm_grid`).

    Args:
        state: The CrateState to assess.
        dsm_data: Parsed DSM indicators YAML data, or None.
        answers: an already-computed verdict map, to avoid re-running every check.

    Returns:
        The highest DSM level achieved (0-5); 0 means "level 1 not reached".
    """
    if dsm_data is None:
        return 0

    levels = sorted(
        {
            lvl
            for ind in dsm_data.get("indicators", [])
            if isinstance(lvl := ind.get("level"), int) and lvl >= _DSM_FIRST_LEVEL
        }
    )

    if answers is None:
        answers = dsm_verdicts(state, dsm_data, graph)
    max_level = 0
    for level in levels:
        answered = [
            verdict.value
            for ind in _assessable_indicators(dsm_data, level, answers)
            if (verdict := answers.get(str(ind.get("id")))) is not None
            and verdict.value is not None
        ]
        if not answered:
            break  # no evidence at this level, so nothing above it can be claimed
        if not all(answered):
            break
        max_level = level

    return max_level


def dsm_ceiling(
    state: CrateState,
    dsm_data: dict[str, Any] | None = None,
    graph: Graph = None,
    answers: dict[str, Verdict] | None = None,
) -> dict[str, Any]:
    """Where this crate stands on the derived ladder, and what caps it.

    * ``attained`` — the derived level now (:func:`_compute_dsm_level`).
    * ``ceiling`` — the highest level *any* crate can reach with this tool, because
      above it no indicator is assessable from a crate at all. Level 5 is entirely
      hosting and enterprise data-governance, so no RO-Crate can evidence it: reporting
      a level "out of 5" implies a rung that is not on the board.
    * ``blocked_by`` — the assessed indicators failing at ``attained + 1``, the concrete
      next step.

    Why the ladder stops where it does is a constant of the model rather than of a
    crate, so it is stated in the report's own footnote instead of being recomputed
    into a string here that nothing read.

    There was a fourth, ``attainable``, and it was a fiction: it was assigned inside the
    same loop as ``ceiling``, from the YAML alone, so it equalled the ceiling for every
    crate that has ever been scored while the report rendered it as a claim about *this*
    crate ("reachable: 4 once the indicators below are met").
    """
    empty: dict[str, Any] = {"attained": 0, "ceiling": 0, "blocked_by": []}
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        # The shape is constant so a caller never has to guard a key: with no model
        # to read, nothing is attained and nothing blocks.
        return empty

    levels = sorted(
        {
            lvl
            for ind in dsm_data.get("indicators", [])
            if isinstance(lvl := ind.get("level"), int) and lvl >= _DSM_FIRST_LEVEL
        }
    )

    if answers is None:
        answers = dsm_verdicts(state, dsm_data, graph)
    attained = _compute_dsm_level(state, dsm_data, graph, answers)
    ceiling = 0
    for level in levels:
        if not _assessable_indicators(dsm_data, level, answers):
            break
        ceiling = level

    failing = _failing_at(dsm_data, answers, attained + 1)

    return {"attained": attained, "ceiling": ceiling, "blocked_by": failing}


def dsm_grid(
    state: CrateState,
    dsm_data: dict[str, Any] | None = None,
    graph: Graph = None,
    *,
    answers: dict[str, Verdict] | None = None,
) -> dict[int, dict[str, dict[str, Any]]]:
    """The DSM's own **"% Complete" grid** — every level x every category, plus Total.

    This is the published instrument's *only* output: no formula anywhere in the
    workbook computes an achieved maturity level. Each cell is reproduced from the
    sheet's own definition, carried in ``fair/dsm_indicators.yaml`` under ``scoring``
    and read here rather than re-derived, so a depositor filling the sheet in by hand
    reaches the same numbers.

    Three properties of the sheet are load-bearing:

    * **Membership is the sheet's, not the indicator's level.** A level's cell carries
      lower levels forward — the Level-2 Content cell counts DSM-1-C2 and DSM-1-C3
      beside the Level-2 rows — and it is a *multiset*: DSM-4-H2 appears on two rows of
      the Level-4 Hosting cell, which divides by 3.
    * **A blank scores 0.** The sheet's validation column is entirely formulas
      (``=H{row}``), so an unanswered indicator evaluates to numeric 0 and ``COUNT``
      counts it. The instrument has no "not assessed" state.
    * **Level 0 counts zeros** (``P6`` is ``COUNTIFS(J31,0)``). Its statements describe
      the pre-FAIRification condition in the negative, so failing them is the good
      outcome.

    Because the sheet cannot say "not assessed" and this tool can, every cell carries
    two numbers: ``published_pct`` is the sheet's arithmetic and is what an external
    assessor reproduces, while ``pct`` divides by what was actually assessed and is
    ``None`` when nothing was. Reporting only the first would publish a Level-0 row
    reading 100% "escaped the pre-FAIRification state" on the strength of never having
    looked; reporting only the second would publish a number no one else can check.

    Args:
        answers: an already-computed verdict map, to avoid re-running every check.

    Returns:
        ``{level: {category: cell}}`` where *category* is ``C``/``R``/``H``/``TOTAL``
        and each cell states ``cell``, ``published_pct``, ``pct``, ``passed``,
        ``assessed`` and ``total``.
    """
    if dsm_data is None:
        dsm_data = _load_yaml(DSM_INDICATORS_PATH)
    if dsm_data is None:
        return {}
    if answers is None:
        answers = dsm_verdicts(state, dsm_data, graph)

    grid: dict[int, dict[str, dict[str, Any]]] = {}
    for spec in (dsm_data.get("scoring") or {}).get("grid") or []:
        members = spec.get("members") or []
        cell: dict[str, Any] = {
            "cell": spec["cell"],
            "published_pct": spec.get("constant"),
            "pct": None,
            "met": 0,
            "passed": 0,
            "assessed": 0,
            "total": len(members),
        }
        # The sheet's own criterion: 1 everywhere but Level 0, which counts zeros.
        wanted = spec.get("counts", 1) == 1
        for ident in members:  # iterated, not de-duplicated: see the docstring
            verdict = answers.get(ident)
            value = None if verdict is None else verdict.value
            # An unanswered indicator validates to 0 on the sheet, so it satisfies a
            # cell that counts zeros and fails one that counts ones. That is why the
            # Level-0 row can read 100% off nothing but blanks, and why `assessed`
            # rides alongside every percentage.
            if (False if value is None else value) is wanted:
                cell["met"] += 1
            if value is None:
                continue
            cell["assessed"] += 1
            if value is wanted:
                cell["passed"] += 1
        denominator = (spec.get("denominator") or {}).get("n")
        if denominator:
            cell["published_pct"] = round(cell["met"] / denominator * 100, 1)
        if cell["assessed"]:
            cell["pct"] = round(cell["passed"] / cell["assessed"] * 100, 1)
        grid.setdefault(spec["level"], {})[spec["category"]] = cell

    return grid


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
from builder.tools.registry import TOOL_REGISTRY  # noqa: E402


def _assess_fair_maturity_tool(state: CrateState) -> FAIRReport:
    """The agent-facing tool, which assembles the crate it is asked to score.

    The tool spec exposes no parameters, so a model calling this reaches
    :func:`assess_fair_maturity` with no graph — and every graph-aware indicator then
    answers "not assessed", handing the agent a different number than the report
    publishes for the same crate. Assembling here fixes that at the boundary where it
    is wrong, and leaves the assessor's own contract alone: given no graph it still
    declines to guess, which is what the #670 floor tests hold it to.
    """
    from builder.tools.mit_assessment import scoring_graph

    graph = scoring_graph(state)
    return assess_fair_maturity(state, graph=graph)


TOOL_REGISTRY.register("assess_fair_maturity", _assess_fair_maturity_tool, takes_state=True)
