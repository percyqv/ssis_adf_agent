"""
Data Flow destination converter — maps SSIS Data Flow destination component types
to ADF Mapping Data Flow ``sink`` transformation JSON.
"""
from __future__ import annotations

from typing import Any

from ...parsers.models import DataFlowComponent
from ...naming import linked_service_ref

_SINK_DATASET_TYPE: dict[str, str] = {
    "OleDbDestination": "AzureSqlTable",
    "ADONetDestination": "AzureSqlTable",
    "FlatFileDestination": "DelimitedText",
    "ExcelDestination": "Excel",
    "OdbcDestination": "OdbcTable",
    "SqlServerDestination": "SqlServerTable",
    "RecordsetDestination": "AzureSqlTable",  # approximation
}

_WRITE_BEHAVIOR: dict[str, str] = {
    "OleDbDestination": "upsert",
    "ADONetDestination": "upsert",
    "FlatFileDestination": "overwrite",
    "ExcelDestination": "overwrite",
    "SqlServerDestination": "insert",
}


def convert_destination(
    component: DataFlowComponent, ls_names: dict[str, str] | None = None
) -> dict[str, Any]:
    """
    Return an ADF Mapping Data Flow ``sink`` transformation dict.

    This dict is embedded in the ``sinks`` array of the data flow JSON.
    """
    comp_type = component.component_type
    ds_type = _SINK_DATASET_TYPE.get(comp_type, "AzureSqlTable")
    write_behavior = _WRITE_BEHAVIOR.get(comp_type, "upsert")
    safe_name = component.name.replace(" ", "_")

    sink: dict[str, Any] = {
        "name": safe_name,
        "description": f"Sink from SSIS {comp_type}: {component.name}",
        "dataset": {
            "referenceName": f"DS_{safe_name}",
            "type": "DatasetReference",
        },
        "linkedService": {
            "referenceName": linked_service_ref(component.connection_id, ls_names),
            "type": "LinkedServiceReference",
        },
        "typeProperties": {
            "format": {"type": ds_type},
            "allowUpsert": write_behavior == "upsert",
        },
    }

    # Destination table name
    table = (
        component.properties.get("OpenRowset")
        or component.properties.get("TableOrViewName")
    )
    if table:
        sink["typeProperties"]["tableName"] = table

    return sink
