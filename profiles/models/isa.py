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
    ``read_existing_crate`` and every build→read→build round trip. External
    consumers were never affected: without these imports ``pick_type`` falls back
    to plain ``ContextEntity`` (#544).

    The read path does not need the rich classes — it needs not to crash. So a
    third positional argument that is a ``dict`` is taken as the reader's
    ``properties`` and handed to ``ContextEntity`` directly. That test is
    unambiguous rather than merely convenient: across all eleven affected classes
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
      The protocol executed.
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
        labprotocol: LabProtocol,
        name: str | None = None,
        object: Sample | File | list[Sample | File] | None = None,
        result: Sample | File | list[Sample | File] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        # Define default properties for this LabProcess
        default_properties = {"@type": "LabProcess", "executesLabProtocol": labprotocol}
        if name is not None:
            default_properties["name"] = name
        if object is not None:
            default_properties["object"] = object
        if result is not None:
            default_properties["result"] = result
        # Merge default properties with user-provided properties (user properties override defaults)
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            properties=merged_properties,
            add=add,
        )


class LabProtocol(AutoAddFile):
    """Bioschemas LabProtocol mapping of an ISA-JSON Protocol.

    This entity uses the Bioschemas type `https://bioschemas.org/LabProtocol`.

    Properties
    ----------

    Required
    ^^^^^^^^
    - ``@id``: Text | URL
      For file-based protocols, this will typically be the file entity identifier
      (often derived from ``dest_path``) or a URL if ``source`` is remote.
    - ``@type``: Text
      MUST be ``https://bioschemas.org/LabProtocol``.
    - ``intendedUse``: schema.org/DefinedTerm | Text | URL
      Protocol type as an ontology term. DECISION BY AUTHOR to ELEVATE

    Recommended
    ^^^^^^^^^^^
    - ``name``: Text
      Main title of the LabProtocol.
    - ``description``: Text
      Short description (e.g., abstract).


    Optional
    ^^^^^^^^
    - ``comment``: schema.org/Comment
      Comment.
    - ``computationalTool``: schema.org/DefinedTerm | schema.org/PropertyValue (Component) | schema.org/SoftwareApplication
      Software or tool used as part of the protocol.
    - ``labEquipment``: schema.org/DefinedTerm | schema.org/PropertyValue (Component) | Text | URL
      Equipment used to follow one or more steps in this LabProtocol.
    - ``reagent``: schema.org/BioChemEntity | schema.org/DefinedTerm | schema.org/PropertyValue (Component) | Text | URL
      Reagents used in the protocol.
    - ``url``: URL
      Pointer to protocol resources external to the ISA-Tab / ISA-JSON.
    - ``version``: Number | Text
      Version identifier for protocol tracking.

    File parameters
    --------------
    These are inherited from `rocrate.model.file.File` and control how the protocol is
    represented as a file in the crate.

    - ``source``:
      Local path/handle (e.g. a `Path`) or a URL. If provided, the file can be added/copied
      into the crate (or referenced remotely, depending on options).
    - ``dest_path``:
      Path *inside the crate* (e.g. ``"protocols/wetlab/protocol_a.md"``). If omitted,
      `ro-crate-py` may derive it from the source filename.
    - ``fetch_remote``:
      If ``source`` is a remote URL and this is True, fetch the content into the crate.
      If False, keep it as a remote reference.
    - ``validate_url``:
      If True and ``source`` is a URL, validate it (may involve a network call).
    - ``record_size``:
      If True, compute and store file size metadata.

    Notes
    -----
    - This class sets `@type` automatically to `https://bioschemas.org/LabProtocol` and merges
      any additional properties you pass in.
    """

    def __init__(
        self,
        crate: ROCrate,
        intendeduse: str,
        source=None,
        dest_path=None,
        fetch_remote: bool = False,
        validate_url: bool = False,
        properties: dict | None = None,
        record_size: bool = False,
        add: bool = True,
    ):
        if isinstance(source, list):
            raise ValueError(
                "LabProtocol expects a single source. Use LabProtocol.from_sources(...) "
                "to create multiple instances."
            )

        merged_properties = self.make_properties(intendeduse, source, properties)
        super().__init__(
            crate=crate,
            source=source,
            dest_path=dest_path,
            fetch_remote=fetch_remote,
            validate_url=validate_url,
            properties=merged_properties,
            record_size=record_size,
            add=add,
        )

    @classmethod
    def from_sources(
        cls,
        crate: ROCrate,
        intendeduse: str,
        sources: list[Path],
        dest_path=None,
        fetch_remote: bool = False,
        validate_url: bool = False,
        properties: dict | None = None,
        record_size: bool = False,
        add: bool = True,
    ):
        return [
            cls(
                crate=crate,
                intendeduse=intendeduse,
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

    def make_properties(self, intendeduse, source, properties):
        default_properties = (
            {
                "@type": "LabProtocol",
                "intendedUse": intendeduse,
                "name": source.name,
            }
            if isinstance(source, Path)
            else {
                "@type": "LabProtocol",
                "intendedUse": intendeduse,
            }
        )
        return default_properties | (properties or {})


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
