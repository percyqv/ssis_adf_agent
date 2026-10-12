"""
Converter dispatcher — routes SSIS tasks to the right converter based on TaskType.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..parsers.models import PrecedenceConstraint, SSISConnectionManager, SSISTask, TaskType
from .base_converter import BaseConverter
from .control_flow.execute_sql_converter import ExecuteSQLConverter
from .control_flow.execute_package_converter import ExecutePackageConverter
from .control_flow.file_system_converter import FileSystemConverter
from .control_flow.script_task_converter import ScriptTaskConverter
from .control_flow.foreach_converter import ForEachConverter
from .control_flow.for_loop_converter import ForLoopConverter
from .control_flow.data_flow_converter import DataFlowConverter


class ConverterDispatcher:
    """
    Selects and applies the appropriate converter for each SSIS task type.

    Usage::

        dispatcher = ConverterDispatcher(stubs_dir=Path("output/stubs"))
        activities = dispatcher.convert_task(task, constraints, task_by_id)
    """

    def __init__(
        self,
        stubs_dir: Path | None = None,
        llm_translate: bool = False,
        connection_managers: list[SSISConnectionManager] | None = None,
    ) -> None:
        connections_by_id = {cm.id: cm for cm in (connection_managers or [])}
        script_converter = ScriptTaskConverter(stubs_output_dir=stubs_dir, llm_translate=llm_translate)

        # Pass self to loop converters so they can recursively convert inner tasks
        foreach_converter = ForEachConverter(child_converter=self)
        for_loop_converter = ForLoopConverter(child_converter=self)

        self._registry: dict[TaskType, BaseConverter] = {
            TaskType.EXECUTE_SQL: ExecuteSQLConverter(connections_by_id=connections_by_id),
            TaskType.EXECUTE_PACKAGE: ExecutePackageConverter(),
            TaskType.FILE_SYSTEM: FileSystemConverter(),
            TaskType.FTP: _FTPConverter(),
            TaskType.SEND_MAIL: _SendMailConverter(),
            TaskType.SCRIPT: script_converter,
            TaskType.EXECUTE_PROCESS: _ExecuteProcessConverter(),
            TaskType.DATA_FLOW: DataFlowConverter(connections_by_id=connections_by_id),
            TaskType.SEQUENCE: _SequenceConverter(child_converter=self),
            TaskType.FOREACH_LOOP: foreach_converter,
            TaskType.FOR_LOOP: for_loop_converter,
        }
        self._fallback = _FallbackConverter()
        # Sequence containers are flattened (ADF has no equivalent activity).
        # container name -> names of its "exit" activities, used to rewire
        # dependencies that pointed at the container itself.
        self.container_exits: dict[str, list[str]] = {}

    def convert_task(
        self,
        task: SSISTask,
        constraints: list[PrecedenceConstraint],
        task_by_id: dict[str, SSISTask],
    ) -> list[dict[str, Any]]:
        converter = self._registry.get(task.task_type, self._fallback)
        return converter.convert(task, constraints, task_by_id)

    def rewire_container_dependencies(self, activities: list[dict[str, Any]]) -> None:
        """Replace ``dependsOn`` entries that target a flattened Sequence container
        with entries targeting that container's exit activities (in place,
        recursively, including activities nested in ForEach / If / Until)."""
        if not self.container_exits:
            return
        for act in activities:
            new_deps: list[dict[str, Any]] = []
            review_notes: list[str] = []
            for dep in act.get("dependsOn", []) or []:
                exits = self._resolve_exits(dep.get("activity", ""))
                if exits is None:
                    new_deps.append(dep)
                    continue
                conds = dep.get("dependencyConditions", ["Succeeded"])
                for exit_name in exits:
                    new_deps.append({"activity": exit_name, "dependencyConditions": list(conds)})
                if any(c != "Succeeded" for c in conds):
                    review_notes.append(
                        f"[MANUAL REVIEW] Depended on SSIS container '{dep.get('activity')}' "
                        f"with condition {conds}; it was rewired to the container's last "
                        f"activities {exits}. ADF requires ALL listed dependencies to match "
                        "and a failure earlier in the container leaves them 'Skipped', so "
                        "verify the error path."
                    )
            if "dependsOn" in act:
                act["dependsOn"] = _dedupe_deps(new_deps)
            if review_notes:
                desc = act.get("description") or ""
                act["description"] = (desc + " " + " ".join(review_notes)).strip()
            tp = act.get("typeProperties", {}) or {}
            for key in ("activities", "ifTrueActivities", "ifFalseActivities"):
                if isinstance(tp.get(key), list):
                    self.rewire_container_dependencies(tp[key])

    def _resolve_exits(self, name: str, _depth: int = 0) -> list[str] | None:
        if name not in self.container_exits or _depth > 50:
            return None
        resolved: list[str] = []
        for exit_name in self.container_exits[name]:
            nested = self._resolve_exits(exit_name, _depth + 1)
            resolved.extend(nested if nested is not None else [exit_name])
        return resolved


def _dedupe_deps(deps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for d in deps:
        key = d["activity"]
        if key in seen:
            merged = seen[key]["dependencyConditions"]
            for c in d.get("dependencyConditions", []):
                if c not in merged:
                    merged.append(c)
        else:
            seen[key] = {"activity": key, "dependencyConditions": list(d.get("dependencyConditions", []))}
    return list(seen.values())


# ---------------------------------------------------------------------------
# Inline minimal converters for types that don't warrant standalone files
# ---------------------------------------------------------------------------

class _FTPConverter(BaseConverter):
    def convert(self, task, constraints, task_by_id):  # type: ignore[override]
        from ..parsers.models import FTPTask
        assert isinstance(task, FTPTask)
        depends_on = self._depends_on(task, constraints, task_by_id)
        return [{
            "name": task.name,
            "description": task.description or "",
            "type": "Copy",
            "dependsOn": depends_on,
            "typeProperties": {
                "source": {
                    "type": "BinarySource",
                    "storeSettings": {"type": "FtpReadSettings", "recursive": False},
                },
                "sink": {
                    "type": "BinarySink",
                    "storeSettings": {"type": "AzureBlobStorageWriteSettings"},
                },
                "enableStaging": False,
            },
            "inputs": [{"referenceName": f"DS_FTP_{task.name.replace(' ', '_')}", "type": "DatasetReference"}],
            "outputs": [{"referenceName": f"DS_Blob_{task.name.replace(' ', '_')}", "type": "DatasetReference"}],
        }]


class _SendMailConverter(BaseConverter):
    def convert(self, task, constraints, task_by_id):  # type: ignore[override]
        from ..parsers.models import SendMailTask
        assert isinstance(task, SendMailTask)
        depends_on = self._depends_on(task, constraints, task_by_id)
        return [{
            "name": task.name,
            "description": (
                "[MANUAL REVIEW] Send Mail has been mapped to a Web Activity. "
                "Configure a Logic App or Azure Communication Services endpoint."
            ),
            "type": "WebActivity",
            "dependsOn": depends_on,
            "typeProperties": {
                "method": "POST",
                "url": "@pipeline().parameters.SendMailFunctionUrl",
                "body": {
                    "to": task.to or "",
                    "cc": task.cc or "",
                    "from": task.from_address or "",
                    "subject": task.subject or "",
                    "message": task.message_source or "",
                },
                "authentication": {"type": "Anonymous"},
            },
        }]


class _ExecuteProcessConverter(BaseConverter):
    def convert(self, task, constraints, task_by_id):  # type: ignore[override]
        from ..parsers.models import ExecuteProcessTask
        assert isinstance(task, ExecuteProcessTask)
        depends_on = self._depends_on(task, constraints, task_by_id)
        return [{
            "name": task.name,
            "description": (
                f"[MANUAL REVIEW] Execute Process Task (executable: {task.executable!r}) "
                "mapped to Azure Batch Custom Activity. Configure batch pool and application."
            ),
            "type": "Custom",
            "dependsOn": depends_on,
            "linkedServiceName": {
                "referenceName": "LS_AzureBatch",
                "type": "LinkedServiceReference",
            },
            "typeProperties": {
                "command": f"{task.executable or 'TODO'} {task.arguments or ''}".strip(),
                "resourceLinkedService": {
                    "referenceName": "LS_AzureStorage",
                    "type": "LinkedServiceReference",
                },
                "folderPath": "custom-activity",
            },
        }]


class _SequenceConverter(BaseConverter):
    def __init__(self, child_converter: ConverterDispatcher) -> None:
        self._child = child_converter

    def convert(self, task, constraints, task_by_id):  # type: ignore[override]
        from ..parsers.models import SequenceContainer
        assert isinstance(task, SequenceContainer)
        # Dependencies of the container itself become dependencies of its
        # entry activities (the inner tasks that have no inner predecessor).
        container_deps = self._depends_on(task, constraints, task_by_id)
        inner_by_id = {t.id: t for t in task.tasks}
        has_pred = {c.to_task_id for c in task.constraints if c.from_task_id in inner_by_id}
        has_succ = {c.from_task_id for c in task.constraints if c.to_task_id in inner_by_id}

        activities: list[dict[str, Any]] = []
        exits: list[str] = []
        for t in task.tasks:
            if t.disabled:
                continue
            acts = self._child.convert_task(t, task.constraints, inner_by_id)
            if t.id not in has_pred:
                for a in acts:
                    if not a.get("dependsOn"):
                        a["dependsOn"] = [dict(d) for d in container_deps]
            if t.id not in has_succ:
                exits.append(t.name)
            activities.extend(acts)

        self._child.container_exits[task.name] = exits
        return activities


class _FallbackConverter(BaseConverter):
    def convert(self, task, constraints, task_by_id):  # type: ignore[override]
        depends_on = self._depends_on(task, constraints, task_by_id)
        return [{
            "name": task.name,
            "description": (
                f"[UNSUPPORTED — TaskType: {task.task_type.value}] "
                "Manual implementation required. Original task could not be converted."
            ),
            "type": "Wait",
            "dependsOn": depends_on,
            "typeProperties": {"waitTimeInSeconds": 1},
        }]
