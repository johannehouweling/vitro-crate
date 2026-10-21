# ruff: noqa: E501
from __future__ import annotations

import functools
import hashlib
import re
from pathlib import Path

from rocrate.model import ContextEntity, File
from rocrate.rocrate import ROCrate


def param_id(name: str, value: str) -> str:
    """Stable, unique @id for a PropertyValue node, derived from its key+value.

    Distinct values get distinct @ids (so same-key parameters across processes no
    longer collide/merge); identical (name, value) pairs share one node."""
    digest = hashlib.sha1(f"{name}|{value}".encode("utf-8")).hexdigest()[:10]
    base = re.sub(r"[^\w.-]", "_", name or "").strip("_") or "param"
    return f"#param_{base}_{digest}"


def reader_compatible(cls):
    """Let ro-crate-py's READER construct this class, without changing our own API.

    ``ROCrate(path)`` builds its class map from every imported subclass of
    ``ContextEntity``, keys it by class NAME, and constructs the match
    positionally (``rocrate.py:294``)::

        type_map = {_.__name__: _ for _ in subclasses(ContextEntity)}
        cls = pick_type(entity, type_map, fallback=ContextEntity)
        self.add(cls(self, identifier, entity))    # (crate, identifier, properties)

    Our modelling classes are written for the WRITE path, where the third
    parameter is the thing the entity is about — ``Sample(crate, id, name)``,
    ``LabProcess(crate, id, labprotocol)``. Under the reader the properties dict
    binds to that parameter instead, and the entity is built with ``name`` set to
    a whole JSON-LD dict, which dies as ``ValueError: no @id in {...}``.

    Merely IMPORTING ``profiles.models`` arms this — no registration call is
    involved — so any crate carrying a ``Sample``, a ``LabProcess`` or a
    ``ParameterValue`` was unreadable inside this repo's own process, including
    any code that opens one with ro-crate-py inside this process. External
    consumers were never affected: without these imports ``pick_type`` falls back
    to plain ``ContextEntity`` (#544).

    The read path does not need the rich classes — it needs not to crash. So a
    third positional argument that is a ``dict`` is taken as the reader's
    ``properties`` and handed to ``ContextEntity`` directly. That test is
    unambiguous rather than merely convenient: across all twelve affected classes
    the third parameter is a ``str``, a ``list`` or a ``LabProtocol``, and not one
    of them accepts a mapping, so no legitimate write-path call can be mistaken
    for a read.

    Deliberately NOT done by reordering every signature to put ``properties``
    third: that is the more principled fix and it churns every construction site
    in the codebase, for a benefit the reader alone consumes.
    """
    original_init = cls.__init__

    @functools.wraps(original_init)
    def __init__(self, crate, identifier=None, *args, **kwargs):
        if args and isinstance(args[0], dict):
            # ro-crate-py's reader. Bypass this class's own __init__ entirely:
            # it exists to COMPOSE an entity from parts, and the reader already
            # has the finished JSON-LD.
            ContextEntity.__init__(self, crate, identifier, args[0])
            return
        original_init(self, crate, identifier, *args, **kwargs)

    cls.__init__ = __init__
    return cls


class AutoAddContextEntity(ContextEntity):
    def __init__(
        self,
        crate: ROCrate,
        identifier=None,
        properties=None,
        add: bool = True,
        *args,
        **kwargs,
    ):
        super().__init__(crate, identifier=identifier, properties=properties, *args, **kwargs)
        if add:
            crate.add(self)


class AutoAddFile(File):
    def __init__(
        self,
        crate: ROCrate,
        source=None,
        dest_path=None,
        fetch_remote: bool = False,
        validate_url: bool = False,
        properties: dict | None = None,
        record_size: bool = False,
        add: bool = True,
        *args,
        **kwargs,
    ):
        super().__init__(
            crate=crate,
            source=source,
            dest_path=dest_path,
            fetch_remote=fetch_remote,
            validate_url=validate_url,
            properties=properties,
            record_size=record_size,
            *args,
            **kwargs,
        )
        if add:
            crate.add(self)


# Lab Process
@reader_compatible
class LabProcess(AutoAddContextEntity):
    """Bioschemas LabProcess (DRAFT) mapping of an ISA-JSON Process.

    This entity uses the Bioschemas DRAFT type `https://bioschemas.org/LabProcess`.

    Properties
    ----------

    Required
    ^^^^^^^^
    - ``@id``: Text | URL
      Identifier for the process (e.g., ISA metadata filename + protocol reference, or process name).
    - ``@type``: Text
      MUST be ``bioschemas.org/LabProcess``.
    - ``name``: Text
      Human-readable name.

    Recommended
    ^^^^^^^^^^^
    - ``object``: bioschemas.org/Sample | File | list[bioschemas.org/Sample | File]
      Input(s) of the process. If multiple inputs are provided, they SHOULD be stored as a sorted list
      to establish correspondence with outputs (both lists should have the same length).
    - ``result``: bioschemas.org/Sample | File | list[bioschemas.org/Sample | File]
      Output(s) of the process. If multiple outputs are provided, they SHOULD be stored as a sorted list
      to establish correspondence with inputs (both lists should have the same length).
    - ``agent``: schema.org/Person
      The performer.
    - ``executesLabProtocol``: bioschemas.org/LabProtocol
      The protocol executed: the step's one protocol record
      (:class:`LabProtocol`), which is ``isBasedOn`` the deposited documents
      rather than being one of them.
    - ``parameterValue``: schema.org/PropertyValue
      Parameter value(s) of the experimental process, typically key-value pairs using ontology terms.
    - ``endTime``: DateTime
      End time.

    Optional
    ^^^^^^^^
    - ``disambiguatingDescription``: Text
      Comments.

    Notes
    -----
    - This class currently sets `@type` automatically to `https://bioschemas.org/LabProcess` and merges
      any additional properties you pass in.
    """

    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        labprotocol: LabProtocol | None,
        name: str | None = None,
        object: Sample | File | list[Sample | File] | None = None,
        result: Sample | File | list[Sample | File] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        default_properties: dict = {
            "@type": "LabProcess",
            "executesLabProtocol": labprotocol,
            "name": name,
            "object": object,
            "result": result,
        }
        # User properties override defaults; then whatever is absent is OMITTED,
        # never emitted as null or []. An untyped step that names no document has
        # no protocol record, and the crate says nothing rather than "null" (#783).
        # A subtype whose every parameter was declined
        # (:func:`profiles.models.tox._pv`) states no parameter rather than an
        # empty list: the shape's "MUST have at least one additionalProperty" fires
        # on the missing key just the same, and a reader meets one spelling of
        # "absent", not two (#740).
        merged_properties = {
            k: v
            for k, v in (default_properties | (properties or {})).items()
            if v is not None and v != []
        }

        super().__init__(
            crate,
            identifier=identifier,
            properties=merged_properties,
            add=add,
        )


@reader_compatible
class LabProtocol(AutoAddContextEntity):
    """Bioschemas LabProtocol mapping of an ISA-JSON Protocol: a step's protocol record.

    This entity uses the Bioschemas type `https://bioschemas.org/LabProtocol`.
    Every ISA-Tox LabProcess executes exactly one, written by the build from what
    the deposit states. No file stands behind it: a deposited document is a
    ``File`` co-typed ``LabProtocol`` that the record is ``isBasedOn`` (#783, #799).

    Properties
    ----------

    Required
    ^^^^^^^^
    - ``@id``: Text | URL
      ``<process @id>_protocol``: one record per step.
    - ``@type``: Text
      MUST be ``https://bioschemas.org/LabProtocol``.

    Recommended
    ^^^^^^^^^^^
    - ``intendedUse``: schema.org/DefinedTerm
      The protocol type as an ontology term (an ISA Warning when absent). The build
      writes it from the step's type (``_crate_mapping._STEP_PROTOCOL_TYPES``).
    - ``name``: Text
      Main title of the LabProtocol.
    - ``description``: Text
      Short description (e.g., abstract). Only what the lab stated (#670), from
      the protocol the drafter recorded for the step (#789).
    - ``url``: URL
      Where the lab published the procedure, when it stated one.

    Optional
    ^^^^^^^^
    - ``isBasedOn``: schema.org/CreativeWork
      The deposited documents the protocol follows. ISA-JSON has no equivalent,
      so the link is lost on conversion to ISA-JSON.
    - ``comment``: schema.org/Comment
      Comment.
    - ``computationalTool``: schema.org/DefinedTerm | schema.org/PropertyValue (Component) | schema.org/SoftwareApplication
      Software or tool used as part of the protocol.
    - ``labEquipment``: schema.org/DefinedTerm | schema.org/PropertyValue (Component) | Text | URL
      Equipment used to follow one or more steps in this LabProtocol.
    - ``reagent``: schema.org/BioChemEntity | schema.org/ChemicalSubstance | schema.org/DefinedTerm | schema.org/MolecularEntity | schema.org/PropertyValue (Component) | Text | URL
      Reagents used in the protocol. The range is transcribed from
      ``LabProtocol_v0.6-DRAFT``; ``ChemicalSubstance`` and ``MolecularEntity``
      were missing from it, which mattered — a test compound is a
      ``MolecularEntity``, so this is the property that carries it. The compounds
      of an exposure are ``reagent``s of the protocol record the Exposure
      executes, because the bundled ISA process shape restricts
      ``schema:object`` to File/Sample/BioSample at Violation severity (#783).
    - ``url``: URL
      Pointer to protocol resources external to the ISA-Tab / ISA-JSON.
    - ``version``: Number | Text
      Version identifier for protocol tracking.
    """

    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        intended_use: ContextEntity | None = None,
        is_based_on: list | None = None,
        reagents: list | None = None,
        description: str | None = None,
        url: str | None = None,
    ):
        properties = {
            "@type": "LabProtocol",
            "name": name,
            "description": description,
            "url": url,
            "intendedUse": intended_use,
            "isBasedOn": is_based_on,
            "reagent": reagents,
        }
        super().__init__(
            crate,
            identifier=identifier,
            properties={k: v for k, v in properties.items() if v is not None and v != []},
        )


# Parameter defines the specific settings or values used in a protocol (e.g., temperature, duration, operator).
@reader_compatible
class ParameterValue(AutoAddContextEntity):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        value: str,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {
            "@type": "PropertyValue",
            "additionalType": "ParameterValue",
            "name": name,
            "value": value,
        }
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            properties=merged_properties,
            add=add,
        )


# Factor represents an independent variable manipulated by the researcher to affect a biological system.
@reader_compatible
class FactorValue(ParameterValue):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        value: str,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {
            "additionalType": "FactorValue",
        }
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            name=name,
            value=value,
            properties=merged_properties,
            add=add,
        )


# Factor represents an independent variable manipulated by the researcher to affect a biological system.
@reader_compatible
class CharacteristicValue(ParameterValue):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        value: str,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {
            "additionalType": "CharacteristicValue",
        }
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            name=name,
            value=value,
            properties=merged_properties,
            add=add,
        )


# Factor represents an independent variable manipulated by the researcher to affect a biological system.
@reader_compatible
class Component(ParameterValue):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        value: str,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {
            "additionalType": "Component",
        }
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            name=name,
            value=value,
            properties=merged_properties,
            add=add,
        )


class NamedFile(AutoAddFile):
    def __init__(
        self,
        crate,
        source=None,
        dest_path=None,
        fetch_remote=False,
        validate_url=False,
        properties=None,
        record_size=False,
        add: bool = True,
    ):
        if isinstance(source, list):
            raise ValueError(
                "NamedFile expects a single source. Use NamedFile.from_sources(...) "
                "to create multiple instances."
            )
        default_properties = {"name": source.name} if isinstance(source, Path) else {}
        merged_properties = default_properties | (properties or {})
        super().__init__(
            crate=crate,
            source=source,
            dest_path=None,
            fetch_remote=False,
            validate_url=False,
            properties=merged_properties,
            record_size=record_size,
            add=add,
        )

    @classmethod
    def from_sources(
        cls,
        crate,
        sources: list[Path],
        dest_path=None,
        fetch_remote=False,
        validate_url=False,
        properties=None,
        record_size=False,
        add: bool = True,
    ):
        return [
            cls(
                crate=crate,
                source=source,
                dest_path=dest_path,
                fetch_remote=fetch_remote,
                validate_url=validate_url,
                properties=properties,
                record_size=record_size,
                add=add,
            )
            for source in sources
        ]


@reader_compatible
class Sample(AutoAddContextEntity):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        additionalProperty: ParameterValue | list[ParameterValue] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {
            "@type": "Sample",
            "name": name,
        }
        if additionalProperty is not None:
            default_properties = default_properties | {"additionalProperty": additionalProperty}
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            properties=merged_properties,
            add=add,
        )
