from rocrate.model import ContextEntity, File
from rocrate.rocrate import ROCrate

from profiles.models.isa import (
    LabProcess,
    LabProtocol,
    ParameterValue,
    Sample,
    param_id,
)
from profiles.ontology_iris import iri

# Values that state nothing. A parameter carrying one of these is not a
# measurement, it is the absence of one dressed up as data — see :func:`_pv`.
_PLACEHOLDER_VALUES = {"", "unknown", "n/a", "na", "none", "not applicable", "not recorded"}


def _pv(crate, name, value, property_id=None, unit=None):
    """ParameterValue with a unique @id, optionally the key's ontology IRI as
    propertyID, and an optional unitText — per the ISA RO-Crate profile.

    ``property_id`` is optional: parameters without an authoritative ontology
    term for their key are emitted without a propertyID rather than carrying a
    fabricated IRI (the ISA shape treats propertyID as SHOULD, not MUST).

    Returns ``None`` when *value* says nothing (missing, or a placeholder like
    ``"unknown"``), so callers can drop the parameter instead of publishing it.
    A crate that states ``Detection Instrument: unknown`` makes a claim it cannot
    support and that a reader cannot distinguish from a real answer — while the
    instrument may well be sitting unread in the source workbook. Omitting the
    parameter is both truthful and louder: the ISA shape's "MUST have at least
    one additionalProperty" then fires, which is exactly the prompt to go and
    fill it in (D5 — never fabricate)."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().casefold() in _PLACEHOLDER_VALUES:
        return None
    props: dict = {}
    if property_id:
        props["propertyID"] = {"@id": property_id}
    if unit:
        props["unitText"] = unit
    return ParameterValue(crate, param_id(name, value), name, value, properties=props)


def _pvs(*candidates):
    """Drop the parameters :func:`_pv` declined to emit."""
    return [p for p in candidates if p is not None]


class LabProcessExposure(LabProcess):
    """Exposure step (additionalType "Exposure").

    object  = the cultured cell ``Sample``(s) being exposed.
    result  = the CSVW condition table (a ``File`` that is also a ``csvw:Table``),
              recording per well the cell line / compound / concentration /
              duration. The exposed compound is NOT a process object — the base
              ISA shape allows only File/Sample/BioSample, so the compound is
              connected THROUGH the condition table (and shown at a glance on the
              Study via schema:mentions), never via schema:object.
    """

    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        duration: str | None,
        cell_seeding_density: str | None,
        microplate: str | None,
        samples: list[Sample],
        labprotocol: LabProtocol,
        name: str = "Exposure",
        result: list[Sample | File] | Sample | File | None = None,
        units: dict[str, str] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        # ISA-Tox requires an Exposure to emit a schema:result (the CSVW condition
        # table), so `result` is a first-class parameter here — symmetric with the
        # other LabProcess subtypes — rather than only reachable through
        # `properties`.
        u = units or {}
        base_properties: dict = {
            "additionalType": "Exposure",
            "parameter": _pvs(
                _pv(
                    crate,
                    "Exposure Duration",
                    duration,
                    iri("NCIT:C83280"),
                    u.get("Exposure Duration"),
                ),
                _pv(
                    crate,
                    "Cell Seeding Density",
                    cell_seeding_density,
                    iri("MSIO:0000062"),
                    u.get("Cell Seeding Density"),
                ),
                _pv(
                    crate,
                    "Microplate",
                    microplate,
                    iri("NCIT:C43377"),
                    u.get("Microplate"),
                ),
            ),
            "input": samples,
        }
        if result is not None:
            base_properties["output"] = result
        merged_properties = base_properties | (properties or {})
        super().__init__(
            crate=crate,
            name=name,
            identifier=identifier,
            labprotocol=labprotocol,
            properties=merged_properties,
            add=add,
        )


class LabProcessEndpointReadout(LabProcess):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        samples: list[Sample] | None,
        labprotocol: LabProtocol,
        result: list[File],
        detection_instrument: str | None,
        instrument_manufacturer: str | None,
        measured_entity: str | None,
        technical_replicate: str | None,
        endpoint: str | None,
        name: str = "Endpoint Readout",
        assay_kit: str | None = None,
        substrate: str | None = None,
        units: dict[str, str] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        u = units or {}
        parameter_values = _pvs(
            _pv(
                crate,
                "Detection Instrument",
                detection_instrument,
                iri("BAO:0000697"),
                u.get("Detection Instrument"),
            ),
            _pv(
                crate,
                "Instrument Manufacturer",
                instrument_manufacturer,
                iri("BAO:0002628"),
                u.get("Instrument Manufacturer"),
            ),
            _pv(
                crate,
                "Measured Entity",
                measured_entity,
                iri("BAO:0002001"),
                u.get("Measured Entity"),
            ),
            _pv(
                crate,
                "Technical replicate",
                technical_replicate,
                iri("EFO:0002090"),
                u.get("Technical replicate"),
            ),
            _pv(
                crate,
                "Endpoint",
                endpoint,
                iri("BAO:0000179"),
                u.get("Endpoint"),
            ),
        )
        if assay_kit is not None:
            parameter_values.append(
                _pv(
                    crate,
                    "Assay Kit",
                    assay_kit,
                    iri("BAO:0000248"),
                    u.get("Assay Kit"),
                )
            )
        if substrate is not None:
            parameter_values.append(
                _pv(
                    crate,
                    "Substrate",
                    substrate,
                    iri("BAO:0003063"),
                    u.get("Substrate"),
                )
            )

        merged_properties = {
            "additionalType": "EndpointReadout",
            "parameter": _pvs(*parameter_values),
            "input": samples,
            "name": name,
            "output": result,
        } | (properties or {})
        super().__init__(
            crate=crate,
            identifier=identifier,
            labprotocol=labprotocol,
            name=name,
            properties=merged_properties,
            add=add,
        )


class LabProcessCellCulture(LabProcess):
    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        cell_line: Sample,
        culture_medium: str,
        result: Sample,
        labprotocol: LabProtocol,
        properties: dict | None = None,
        add: bool = True,
    ):
        merged_properties = {
            "additionalType": "CellCulture",
            "parameter": [
                _pv(
                    crate,
                    "Culture Medium",
                    culture_medium,
                    iri("BAO:0000114"),
                ),
            ],
            "input": cell_line,
            "output": result,
            "name": name,
        } | (properties or {})
        super().__init__(
            crate=crate,
            identifier=identifier,
            labprotocol=labprotocol,
            name=name,
            properties=merged_properties,
            add=add,
        )


class LabProcessDataAnalysis(LabProcess):
    """Data analysis process: turns the EndpointReadout's raw measurements into
    reported results. Consumes raw-data File(s) as ``object`` and emits the
    processed-data File(s) as ``result`` along the derivation graph.

    Part of the Tox ISA RO-Crate Profile extension (the 4th LabProcess
    discriminator, alongside CellCulture, Exposure, and EndpointReadout).
    Parameter keys for which no authoritative ontology IRI is asserted are
    emitted without a propertyID rather than carrying a fabricated one.
    """

    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        object: list[File],  # raw-data inputs being analysed
        result: list[File],  # processed-data outputs
        labprotocol: LabProtocol,
        data_processing: str = "",
        software: str = "",
        acceptance_criteria: str | None = None,
        evaluation_criteria: str | None = None,
        name: str = "Data Analysis",
        units: dict[str, str] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        u = units or {}
        parameter_values = [
            _pv(
                crate,
                "Data Calculation and Statistics",
                data_processing or "unknown",
                unit=u.get("Data Calculation and Statistics"),
            ),
            _pv(
                crate,
                "Computational Tool",
                software or "unknown",
                unit=u.get("Computational Tool"),
            ),
        ]
        if acceptance_criteria is not None:
            parameter_values.append(_pv(crate, "Acceptance Criteria", acceptance_criteria))
        if evaluation_criteria is not None:
            parameter_values.append(_pv(crate, "Evaluation Criteria", evaluation_criteria))

        merged_properties = {
            "additionalType": "DataAnalysis",
            "parameter": _pvs(*parameter_values),
            "input": object,
            "output": result,
            "name": name,
        } | (properties or {})
        super().__init__(
            crate=crate,
            identifier=identifier,
            labprotocol=labprotocol,
            name=name,
            properties=merged_properties,
            add=add,
        )


# Toxicology-specific Sample class for the Tox ISA RO-Crate Profile


class CellLineSample(Sample):
    """The cell-based test system, modelled as a Sample carrying a categorical
    annotation via ``sampleType`` (a schema:DefinedTerm) and a cell-line identity
    via ``identifier`` (a Cellosaurus accession). Discriminated by
    ``additionalType`` = "CellLine" so intermediate derived Samples (cultured /
    exposed cells) are not constrained by the CellLineSample shape.

    Part of the Tox ISA RO-Crate Profile extension.
    """

    def __init__(
        self,
        crate: ROCrate,
        identifier: str,
        name: str,
        sample_type: ContextEntity,  # a schema:DefinedTerm node (e.g. "cell line")
        accession: str | None = None,  # Cellosaurus accession, e.g. "CVCL_0027"
        additionalProperty: ParameterValue | list[ParameterValue] | None = None,
        properties: dict | None = None,
        add: bool = True,
    ):
        default_properties: dict = {
            "additionalType": "CellLine",
            "sampleType": sample_type,
        }
        if accession:
            # schema:identifier resolving the cell line to its Cellosaurus accession
            # (distinct from the Sample's local @id).
            default_properties["identifier"] = accession
        merged_properties = default_properties | (properties or {})

        super().__init__(
            crate,
            identifier=identifier,
            name=name,
            additionalProperty=additionalProperty,
            properties=merged_properties,
            add=add,
        )
