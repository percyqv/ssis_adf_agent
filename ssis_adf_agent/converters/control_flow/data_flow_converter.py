"""
Data Flow Task → ADF Copy Activity + Mapping Data Flow Activity.

For simple single-source → single-destination flows with no intermediate
transformations, a Copy Activity is generated (cheaper, faster, no Spark overhead).
For flows with transformations, a Mapping Data Flow activity is generated.
The actual Mapping Data Flow JSON is produced by generators/dataflow_generator.py.

Copy Activity patterns follow Microsoft best practices:
  - Full Load: writeBehavior=insert, tableOption=autoCreate
  - Delta/Upsert: writeBehavior=upsert, upsertSettings with keys from parsed key_columns
  - MERGE: writeBehavior=upsert with detected key columns
  - Retry policy: 2 retries, 60s interval per Microsoft recommended pattern
"""
from __future__ import annotations

from typing import Any

from ...parsers.models import (
    DataFlowComponent,
    DataFlowTask,
    IngestionPattern,
    PrecedenceConstraint,
    SSISConnectionManager,
    SSISTask,
)
from ..base_converter import BaseConverter
from ..sql_flavor import flavor_for

# Component types treated as "pure source"
_SOURCE_TYPES = frozenset({
    "OleDbSource", "FlatFileSource", "ExcelSource", "OdbcSource",
    "ADONetSource", "SqlServerSource",
})

# Component types treated as "pure destination"
_DEST_TYPES = frozenset({
    "OleDbDestination", "FlatFileDestination", "ExcelDestination",
    "OdbcDestination", "ADONetDestination", "SqlServerDestination",
    "RecordsetDestination",
})

# Component types that are pure transformations
_TRANSFORM_TYPES = frozenset({
    "Lookup", "DerivedColumn", "ConditionalSplit", "Multicast", "UnionAll",
    "Aggregate", "Sort", "MergeJoin", "Merge", "DataConversion",
    "CharacterMap", "RowCount", "FuzzyLookup", "FuzzyGrouping",
    "TermExtraction", "TermLookup", "ScriptComponent", "Cache",
})


def _is_simple_copy(task: DataFlowTask) -> bool:
    """True if the data flow has exactly one source and one destination, no transforms."""
    sources = [c for c in task.components if c.component_type in _SOURCE_TYPES]
    dests = [c for c in task.components if c.component_type in _DEST_TYPES]
    transforms = [c for c in task.components if c.component_type in _TRANSFORM_TYPES]
    return len(sources) == 1 and len(dests) == 1 and len(transforms) == 0


def _source_query(src: DataFlowComponent | None) -> str | None:
    """SQL text of an OLE DB / ADO.NET source in "SQL command" mode, if any.

    OLE DB Source AccessMode: 0=table, 1=table from variable, 2=SQL command,
    3=SQL command from variable.
    """
    if src is None:
        return None
    sql = (src.properties.get("SqlCommand") or "").strip()
    mode = str(src.properties.get("AccessMode") or "")
    if sql and mode in ("", "2"):
        return sql
    if mode == "3":
        var = src.properties.get("SqlCommandVariable") or "TODO"
        return f"/* TODO: SSIS read this query from variable {var}; map it to a pipeline parameter */"
    return None


class DataFlowConverter(BaseConverter):
    def __init__(
        self, connections_by_id: dict[str, SSISConnectionManager] | None = None
    ) -> None:
        self._connections_by_id = connections_by_id or {}

    def convert(
        self,
        task: SSISTask,
        constraints: list[PrecedenceConstraint],
        task_by_id: dict[str, SSISTask],
    ) -> list[dict[str, Any]]:
        assert isinstance(task, DataFlowTask)
        depends_on = self._depends_on(task, constraints, task_by_id)
        safe_name = task.name.replace(" ", "_")

        if _is_simple_copy(task):
            return [self._copy_activity(task, safe_name, depends_on)]
        else:
            return [self._mapping_dataflow_activity(task, safe_name, depends_on)]

    def _copy_activity(
        self, task: DataFlowTask, safe_name: str, depends_on: list
    ) -> dict[str, Any]:
        src = next((c for c in task.components if c.component_type in _SOURCE_TYPES), None)
        dst = next((c for c in task.components if c.component_type in _DEST_TYPES), None)

        src_flavor = flavor_for(src.connection_id if src else None, self._connections_by_id)
        dst_flavor = flavor_for(dst.connection_id if dst else None, self._connections_by_id)

        src_ds = f"DS_{src.name.replace(' ', '_')}" if src else f"DS_src_{safe_name}"
        dst_ds = f"DS_{dst.name.replace(' ', '_')}" if dst else f"DS_dst_{safe_name}"

        # Determine sink pattern based on ingestion pattern
        ingestion = task.ingestion_pattern

        # Collect key columns from destination component
        key_cols: list[str] = []
        if dst and dst.key_columns:
            key_cols = dst.key_columns

        sink: dict[str, Any]
        if ingestion == IngestionPattern.MERGE or (
            ingestion == IngestionPattern.DELTA and key_cols
        ):
            # Upsert pattern with native temp table
            sink = {
                "type": dst_flavor.sink_type,
                "writeBehavior": "upsert",
                "upsertSettings": {
                    "useTempDB": True,
                    "keys": key_cols or ["TODO_KEY_COLUMN"],
                },
                "sqlWriterUseTableLock": False,
            }
        elif ingestion == IngestionPattern.DELTA:
            # Delta without detected keys — upsert with placeholder
            sink = {
                "type": dst_flavor.sink_type,
                "writeBehavior": "upsert",
                "upsertSettings": {
                    "useTempDB": True,
                    "keys": ["TODO_KEY_COLUMN"],
                },
                "sqlWriterUseTableLock": False,
            }
        else:
            # Full load — insert with auto-create
            sink = {
                "type": dst_flavor.sink_type,
                "writeBehavior": "insert",
                "tableOption": "autoCreate",
                "writeBatchSize": 100000,
                "sqlWriterUseTableLock": False,
            }

        source: dict[str, Any] = {
            "type": src_flavor.source_type,
            "queryTimeout": "02:00:00",
            "isolationLevel": "ReadUncommitted",
        }
        query = _source_query(src)
        if query:
            source["sqlReaderQuery"] = query

        return {
            "name": task.name,
            "description": task.description or "",
            "type": "Copy",
            "dependsOn": depends_on,
            "policy": {
                "timeout": "01:00:00",
                "retry": 2,
                "retryIntervalInSeconds": 60,
                "secureOutput": False,
                "secureInput": False,
            },
            "typeProperties": {
                "source": source,
                "sink": sink,
                "enableStaging": False,
                "translator": {"type": "TabularTranslator", "typeConversion": True},
            },
            "inputs": [{"referenceName": src_ds, "type": "DatasetReference"}],
            "outputs": [{"referenceName": dst_ds, "type": "DatasetReference"}],
        }

    def _mapping_dataflow_activity(
        self, task: DataFlowTask, safe_name: str, depends_on: list
    ) -> dict[str, Any]:
        return {
            "name": task.name,
            "description": task.description or "",
            "type": "ExecuteDataFlow",
            "dependsOn": depends_on,
            "policy": {
                "timeout": "1.00:00:00",
                "retry": 2,
                "retryIntervalInSeconds": 60,
                "secureOutput": False,
                "secureInput": False,
            },
            "typeProperties": {
                "dataflow": {
                    "referenceName": f"DF_{safe_name}",
                    "type": "DataFlowReference",
                },
                "compute": {
                    "coreCount": 8,
                    "computeType": "General",
                },
                "traceLevel": "None",
            },
        }
