"""
Dataset generator — emits ADF dataset JSON files for source/destination components.

Uses Microsoft Recommended format: separate `schema` and `table` properties
instead of the deprecated `tableName` property.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..converters.control_flow.execute_sql_converter import normalize_result_type
from ..converters.sql_flavor import flavor_for
from ..naming import linked_service_names, linked_service_ref
from ..parsers.models import (
    ConnectionManagerType,
    DataFlowComponent,
    DataFlowTask,
    ExecuteSQLTask,
    SSISConnectionManager,
    SSISPackage,
    TaskType,
)

_COMP_TO_DS_TYPE: dict[str, str] = {
    "OleDbSource": "AzureSqlTable",
    "OleDbDestination": "AzureSqlTable",
    "ADONetSource": "AzureSqlTable",
    "ADONetDestination": "AzureSqlTable",
    "FlatFileSource": "DelimitedText",
    "FlatFileDestination": "DelimitedText",
    "ExcelSource": "Excel",
    "ExcelDestination": "Excel",
    "OdbcSource": "OdbcTable",
    "OdbcDestination": "OdbcTable",
    "SqlServerSource": "SqlServerTable",
    "SqlServerDestination": "SqlServerTable",
}


def _parse_table_name(raw_name: str | None) -> tuple[str | None, str | None]:
    """Split a possibly-qualified table name into (schema, table).

    Handles: ``[schema].[table]``, ``schema.table``, ``table`` (defaults to dbo).
    """
    if not raw_name:
        return None, None
    # Remove surrounding brackets and whitespace
    name = raw_name.strip().strip("[]")
    if "." in name:
        parts = [p.strip().strip("[]") for p in name.split(".", 1)]
        return parts[0], parts[1]
    return "dbo", name


def _build_dataset(
    name: str,
    ds_type: str,
    linked_service_name: str,
    table_name: str | None = None,
    file_path: str | None = None,
    description: str = "",
    schema_remap: dict[str, str] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "linkedServiceName": {
            "referenceName": linked_service_name,
            "type": "LinkedServiceReference",
        },
        "description": description,
        "annotations": ["ssis-adf-agent"],
        "type": ds_type,
        "typeProperties": {},
        "schema": [],
    }

    if ds_type in ("AzureSqlTable", "SqlServerTable", "OdbcTable"):
        schema_part, table_part = _parse_table_name(table_name)

        # Apply schema remapping if configured
        if schema_remap and schema_part:
            remap_key = schema_part.lower()
            if remap_key in schema_remap:
                schema_part = schema_remap[remap_key]

        if table_part:
            props["typeProperties"]["schema"] = schema_part or "dbo"
            props["typeProperties"]["table"] = table_part

    elif ds_type == "DelimitedText":
        props["typeProperties"] = {
            "location": {
                "type": "AzureBlobStorageLocation",
                "fileName": file_path or "TODO_filename.csv",
                "folderPath": "TODO_folder",
                "container": "TODO_container",
            },
            "columnDelimiter": ",",
            "rowDelimiter": "\n",
            "firstRowAsHeader": True,
            "quoteChar": "\"",
        }

    elif ds_type == "Excel":
        props["typeProperties"] = {
            "location": {
                "type": "AzureBlobStorageLocation",
                "fileName": file_path or "TODO_file.xlsx",
                "container": "TODO_container",
            },
            "sheetIndex": 0,
            "firstRowAsHeader": True,
        }

    return {"name": name, "properties": props}


def _walk_tasks(tasks: list[Any]):
    """Yield tasks recursively, descending into containers (Sequence/ForEach/For)."""
    for t in tasks:
        yield t
        children = getattr(t, "tasks", None)
        if children:
            yield from _walk_tasks(children)


def generate_datasets(
    package: SSISPackage,
    output_dir: Path,
    *,
    schema_remap: dict[str, str] | None = None,
    shared_artifacts_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Generate ADF dataset JSON files for every Data Flow source and destination.

    When *shared_artifacts_dir* is set, checks for existing dataset JSON files
    there before creating new ones (cross-package deduplication).

    Files are written to *output_dir*/dataset/.
    Returns the list of dataset dicts.
    """
    ds_dir = output_dir / "dataset"
    ds_dir.mkdir(parents=True, exist_ok=True)

    # Build index of existing shared datasets for dedup
    existing_ds: set[str] = set()
    if shared_artifacts_dir:
        shared_ds_dir = shared_artifacts_dir / "dataset"
        if shared_ds_dir.exists():
            for f in shared_ds_dir.glob("*.json"):
                existing_ds.add(f.stem)

    conn_by_id: dict[str, SSISConnectionManager] = {cm.id: cm for cm in package.connection_managers}
    ls_names = linked_service_names(package.connection_managers)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _write(ds: dict[str, Any]) -> None:
        (ds_dir / f"{ds['name']}.json").write_text(
            json.dumps(ds, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        results.append(ds)

    for task in _walk_tasks(package.tasks):
        # Execute SQL tasks with a result set become Lookup activities, which
        # need a dataset bound to the task's connection (the query overrides it).
        if isinstance(task, ExecuteSQLTask):
            if normalize_result_type(task.result_set_type) in ("SingleRow", "FullResultSet"):
                ds_name = f"DS_{task.name.replace(' ', '_')}"
                if ds_name not in seen and ds_name not in existing_ds:
                    seen.add(ds_name)
                    _write(_build_dataset(
                        name=ds_name,
                        ds_type=flavor_for(task.connection_id, conn_by_id).dataset_type,
                        linked_service_name=linked_service_ref(task.connection_id, ls_names),
                        description=f"Dataset for SSIS Execute SQL Task (Lookup): {task.name}",
                    ))
            continue

        if task.task_type != TaskType.DATA_FLOW:
            continue
        assert isinstance(task, DataFlowTask)

        for comp in task.components:
            ds_type = _COMP_TO_DS_TYPE.get(comp.component_type)
            if ds_type is None:
                continue  # transformation — no dataset needed
            if ds_type == "AzureSqlTable":
                # OLE DB / ADO.NET: SQL Server on-prem vs Azure SQL by connection
                ds_type = flavor_for(comp.connection_id, conn_by_id).dataset_type

            ds_name = f"DS_{comp.name.replace(' ', '_')}"
            if ds_name in seen or ds_name in existing_ds:
                continue
            seen.add(ds_name)

            conn = conn_by_id.get(comp.connection_id or "")
            ls_name = linked_service_ref(comp.connection_id, ls_names)
            table = (
                comp.properties.get("OpenRowset")
                or comp.properties.get("TableOrViewName")
            )
            file_path = conn.file_path if conn else None

            _write(_build_dataset(
                name=ds_name,
                ds_type=ds_type,
                linked_service_name=ls_name,
                table_name=table,
                file_path=file_path,
                description=f"Dataset for SSIS component: {comp.name}",
                schema_remap=schema_remap,
            ))

    return results
