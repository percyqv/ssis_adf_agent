"""
Execute SQL Task → ADF Stored Procedure Activity or Lookup Activity.

Mapping rules
-------------
- result_set_type == "None"       → StoredProcedure Activity (fire-and-forget) if sql looks
                                    like a stored procedure call; otherwise Script Activity.
- result_set_type == "SingleRow"  → Lookup Activity (first row only)
- result_set_type == "FullResultSet" → Lookup Activity
- result_set_type == "Xml"        → WebActivity (not supported natively; flagged as warning)

Supports optional schema remapping for database consolidation scenarios.
"""
from __future__ import annotations

import re
from typing import Any

from ...parsers.models import ExecuteSQLTask, PrecedenceConstraint, SSISConnectionManager, SSISTask
from ..base_converter import BaseConverter
from ...naming import linked_service_ref as _ls_ref
from ..sql_flavor import flavor_for

# The .dtsx stores SQLTask:ResultType as e.g. "ResultSetType_SingleRow".
_RESULT_TYPE_ALIASES = {
    "resultsettype_none": "None",
    "resultsettype_singlerow": "SingleRow",
    "resultsettype_rowset": "FullResultSet",
    "resultsettype_xml": "Xml",
    "none": "None",
    "singlerow": "SingleRow",
    "fullresultset": "FullResultSet",
    "rowset": "FullResultSet",
    "xml": "Xml",
}


def normalize_result_type(raw: str | None) -> str:
    return _RESULT_TYPE_ALIASES.get((raw or "None").strip().lower(), "None")

_PROC_PATTERN = re.compile(
    r"^\s*(exec(?:ute)?\s+|call\s+)(?:\[?[\w\s]+\]?\.)?\[?[\w\s]+\]?",
    re.IGNORECASE,
)


def _is_stored_proc_call(sql: str | None) -> bool:
    if not sql:
        return False
    return bool(_PROC_PATTERN.match(sql.strip()))


def _extract_proc_name(sql: str) -> str:
    """Best-effort extraction of a stored procedure name from a SQL string."""
    m = re.search(
        r"(?:exec(?:ute)?\s+|call\s+)(?:\[?[\w\s]+\]?\.)?\[?([\w\s]+)\]?",
        sql, re.IGNORECASE,
    )
    return m.group(1).strip() if m else "sp_unknown"


def apply_schema_remap(sql: str | None, schema_remap: dict[str, str] | None) -> str | None:
    """Apply schema remapping to SQL text.

    Replaces three-part names like ``OldDB.dbo.Table`` with ``NewDB.newschema.Table``
    based on the remap dict (key = "OldDB.dbo" → value = "NewDB.newschema").
    """
    if not sql or not schema_remap:
        return sql
    result = sql
    for old_prefix, new_prefix in schema_remap.items():
        # Match [OldDB].[dbo] or OldDB.dbo (with optional brackets)
        parts = old_prefix.split(".", 1)
        if len(parts) == 2:
            db, schema = parts
            pattern = re.compile(
                rf"\[?{re.escape(db)}\]?\.\[?{re.escape(schema)}\]?\.",
                re.IGNORECASE,
            )
            new_parts = new_prefix.split(".", 1)
            if len(new_parts) == 2:
                replacement = f"[{new_parts[0]}].[{new_parts[1]}]."
            else:
                replacement = f"[{new_prefix}]."
            result = pattern.sub(replacement, result)
    return result


class ExecuteSQLConverter(BaseConverter):
    def __init__(
        self,
        connections_by_id: dict[str, SSISConnectionManager] | None = None,
        ls_names: dict[str, str] | None = None,
    ) -> None:
        self._connections_by_id = connections_by_id or {}
        self._ls_names = ls_names or {}

    def convert(
        self,
        task: SSISTask,
        constraints: list[PrecedenceConstraint],
        task_by_id: dict[str, SSISTask],
    ) -> list[dict[str, Any]]:
        assert isinstance(task, ExecuteSQLTask)
        depends_on = self._depends_on(task, constraints, task_by_id)

        result_type = normalize_result_type(task.result_set_type)

        # Build linked service reference from connection_id placeholder
        linked_service_ref = {
            "referenceName": _ls_ref(task.connection_id, self._ls_names),
            "type": "LinkedServiceReference",
        }

        if result_type in ("SingleRow", "FullResultSet"):
            return [self._lookup_activity(task, linked_service_ref, depends_on)]
        else:
            if _is_stored_proc_call(task.sql_statement):
                return [self._stored_proc_activity(task, linked_service_ref, depends_on)]
            else:
                return [self._script_activity(task, linked_service_ref, depends_on)]

    def _lookup_activity(
        self,
        task: ExecuteSQLTask,
        ls_ref: dict,
        depends_on: list,
    ) -> dict[str, Any]:
        first_row_only = normalize_result_type(task.result_set_type) == "SingleRow"
        flavor = flavor_for(task.connection_id, self._connections_by_id)
        return {
            "name": task.name,
            "description": task.description or "",
            "type": "Lookup",
            "dependsOn": depends_on,
            "policy": {
                "timeout": f"0.{task.timeout // 3600:02d}:{(task.timeout % 3600) // 60:02d}:{task.timeout % 60:02d}"
                if task.timeout > 0 else "0.12:00:00",
                "retry": 0,
                "retryIntervalInSeconds": 30,
            },
            "typeProperties": {
                "source": {
                    "type": flavor.source_type,
                    "sqlReaderQuery": task.sql_statement or "",
                    "queryTimeout": "02:00:00",
                },
                "dataset": {
                    "referenceName": f"DS_{task.name.replace(' ', '_')}",
                    "type": "DatasetReference",
                },
                "firstRowOnly": first_row_only,
            },
        }

    def _stored_proc_activity(
        self,
        task: ExecuteSQLTask,
        ls_ref: dict,
        depends_on: list,
    ) -> dict[str, Any]:
        proc_name = _extract_proc_name(task.sql_statement or "")
        parameters = {}
        for pb in task.parameter_bindings:
            parameters[pb.get("parameter_name", f"param{len(parameters)}")] = {
                "value": f"@variables('{pb.get('variable', '')}')",
                "type": "String",
            }
        return {
            "name": task.name,
            "description": task.description or "",
            "type": "SqlServerStoredProcedure",
            "dependsOn": depends_on,
            "linkedServiceName": ls_ref,
            "typeProperties": {
                "storedProcedureName": proc_name,
                "storedProcedureParameters": parameters,
            },
        }

    def _script_activity(
        self,
        task: ExecuteSQLTask,
        ls_ref: dict,
        depends_on: list,
    ) -> dict[str, Any]:
        return {
            "name": task.name,
            "description": task.description or "",
            "type": "Script",
            "dependsOn": depends_on,
            "linkedServiceName": ls_ref,
            "typeProperties": {
                "scripts": [
                    {
                        "type": "Query",
                        "text": task.sql_statement or "",
                    }
                ],
                "logSettings": {
                    "logDestination": "ActivityOutput",
                },
            },
        }
