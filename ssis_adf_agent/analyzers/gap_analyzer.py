"""
Gap analyzer — scans an SSIS package for components that have no direct ADF equivalent
or that require manual migration effort.

Each gap item has a severity:
  - manual_required : cannot be auto-converted; human must act
  - warning         : can be partially converted but needs review
  - info            : will be converted; noting for awareness
"""
from __future__ import annotations

from ..parsers.models import (  # type: ignore[attr-defined]
    CrossDbReferenceType,
    DataFlowTask,
    ExecuteProcessTask,
    ExecuteSQLTask,
    FileSystemTask,
    ForEachLoopContainer,
    ForEachEnumeratorType,
    ForLoopContainer,
    GapItem,
    ScriptTask,
    SequenceContainer,
    SSISPackage,
    SSISTask,
    TaskType,
    ProtectionLevel,
)

# Data flow component types that ADF Mapping Data Flow cannot represent directly
_UNSUPPORTED_DF_COMPONENTS: frozenset[str] = frozenset({
    "FuzzyLookup",
    "FuzzyGrouping",
    "TermExtraction",
    "TermLookup",
    "ExportColumn",
    "ImportColumn",
    "ScriptComponent",        # needs manual Azure Function / notebook
    "SqlServerDestination",   # BCP — use Copy Activity instead
    "RecordsetDestination",   # in-memory RS — needs variable workaround
    "Cache",
    "OleDbCommand",           # per-row SQL — no Mapping Data Flow equivalent
    "SlowlyChangingDimension",  # rebuild as Lookup + Split + Alter Row + sink
    "RowSampling",            # exact N random rows — no equivalent
})

# Data flow components that map but need a review note
_REVIEW_DF_COMPONENTS: dict[str, str] = {
    "Aggregate": "Map to ADF Aggregate transformation; verify grouping columns",
    "Sort": "ADF Sort transformation is memory-bound; confirm data volume is acceptable",
    "MergeJoin": "Inputs must be sorted in ADF; add Sort transformations if needed",
    "Merge": "Inputs must be sorted in ADF Merge transformation",
    "Pivot": "Mapped to ADF Pivot; set group-by, pivot key and pivoted columns",
    "Unpivot": "Mapped to ADF Unpivot; set ungroup-by, unpivot key and unpivoted columns",
    "CopyColumn": "Mapped to Derived Column copies; verify the source of each copy",
    "PercentageSampling": (
        "Mapped to a Filter with random(); sample size is approximate and the "
        "'unselected rows' output is not produced"
    ),
}


def _is_known_df_component(component_type: str) -> bool:
    from ..converters.data_flow.classification import DEST_TYPES, SOURCE_TYPES
    from ..converters.data_flow.transformation_converter import KNOWN_TRANSFORMATION_TYPES

    return component_type in SOURCE_TYPES | DEST_TYPES | KNOWN_TRANSFORMATION_TYPES


def _walk_tasks(tasks: list[SSISTask]):  # type: ignore[type-arg]
    """Recursively yield all tasks."""
    for task in tasks:
        yield task
        if isinstance(task, (SequenceContainer, ForEachLoopContainer, ForLoopContainer)):
            yield from _walk_tasks(task.tasks)


def analyze_gaps(package: SSISPackage) -> list[GapItem]:
    """Return a list of GapItems for components that need attention."""
    gaps: list[GapItem] = []

    # Package-level: encryption warning
    if package.protection_level in (
        ProtectionLevel.ENCRYPT_ALL_WITH_PASSWORD,
        ProtectionLevel.ENCRYPT_SENSITIVE_WITH_PASSWORD,
    ):
        gaps.append(GapItem(
            task_id=package.id,
            task_name=package.name,
            task_type="Package",
            severity="warning",
            message=(
                f"Package uses ProtectionLevel={package.protection_level.value}. "
                "Sensitive values may not be readable without the password. "
                "Connection strings and credentials were likely not exported."
            ),
            recommendation=(
                "Re-export the package with ProtectionLevel=DontSaveSensitive before conversion, "
                "or provide the password to the SSIS parser."
            ),
        ))

    for task in _walk_tasks(package.tasks):
        task_gaps = _analyze_task(task)
        gaps.extend(task_gaps)

    # Event handlers
    for eh in package.event_handlers:
        for task in _walk_tasks(eh.tasks):
            task_gaps = _analyze_task(task)
            gaps.extend(task_gaps)

    return gaps


def _analyze_task(task: SSISTask) -> list[GapItem]:
    gaps: list[GapItem] = []

    if task.task_type == TaskType.SCRIPT:
        assert isinstance(task, ScriptTask)
        lang = task.script_language
        gaps.append(GapItem(
            task_id=task.id,
            task_name=task.name,
            task_type="ScriptTask",
            severity="manual_required",
            message=(
                f"Script Task uses {lang} code that cannot be automatically converted to ADF. "
                "An Azure Function stub has been generated."
            ),
            recommendation=(
                "Review the generated Azure Function stub, implement the business logic, "
                "deploy the function, and wire it as an AzureFunctionActivity in the pipeline."
            ),
        ))

    elif task.task_type == TaskType.UNKNOWN:
        gaps.append(GapItem(
            task_id=task.id,
            task_name=task.name,
            task_type="Unknown",
            severity="manual_required",
            message="Task type was not recognised and cannot be converted automatically.",
            recommendation="Identify the task type from the original SSIS package and implement a custom converter.",
        ))

    elif task.task_type == TaskType.EXECUTE_PROCESS:
        assert isinstance(task, ExecuteProcessTask)
        gaps.append(GapItem(
            task_id=task.id,
            task_name=task.name,
            task_type="ExecuteProcessTask",
            severity="manual_required",
            message=(
                f"Execute Process Task runs executable '{task.executable}' which has no ADF equivalent. "
                "ADF Custom Activity or Azure Batch is the closest option."
            ),
            recommendation=(
                "Package the executable as an Azure Batch job or Azure Container Instance "
                "and use a Custom Activity."
            ),
        ))

    elif task.task_type == TaskType.FOREACH_LOOP:
        assert isinstance(task, ForEachLoopContainer)
        if task.enumerator_type not in (
            ForEachEnumeratorType.FILE,
            ForEachEnumeratorType.ADO,
            ForEachEnumeratorType.ITEM,
        ):
            gaps.append(GapItem(
                task_id=task.id,
                task_name=task.name,
                task_type="ForEachLoop",
                severity="warning",
                message=(
                    f"ForEach enumerator type '{task.enumerator_type.value}' has no direct ADF equivalent. "
                    "It has been mapped to a ForEach Activity but the items expression needs manual review."
                ),
                recommendation="Manually populate the ForEach items array or use a Lookup Activity to feed it.",
            ))

    elif task.task_type == TaskType.FOR_LOOP:
        assert isinstance(task, ForLoopContainer)
        gaps.append(GapItem(
            task_id=task.id,
            task_name=task.name,
            task_type="ForLoop",
            severity="warning",
            message=(
                "For Loop Container has been mapped to an Until Activity. "
                "Verify that the loop exit condition translates correctly."
            ),
            recommendation=(
                f"Original expressions — Init: {task.init_expression!r}, "
                f"Eval: {task.eval_expression!r}, Assign: {task.assign_expression!r}. "
                "Update the Until activity expression and inner SetVariable activities as needed."
            ),
        ))

    elif task.task_type == TaskType.FILE_SYSTEM:
        assert isinstance(task, FileSystemTask)
        if task.operation in ("DeleteFile", "DeleteDirectory", "RemoveDirectory"):
            gaps.append(GapItem(
                task_id=task.id,
                task_name=task.name,
                task_type="FileSystemTask",
                severity="warning",
                message=f"File System Task operation '{task.operation}' has been mapped to a Web Activity calling an Azure Function.",
                recommendation="Implement the file delete logic in the generated Azure Function stub.",
            ))

    elif task.task_type == TaskType.DATA_FLOW:
        assert isinstance(task, DataFlowTask)
        for comp in task.components:
            if comp.component_type in _UNSUPPORTED_DF_COMPONENTS:
                gaps.append(GapItem(
                    task_id=comp.id,
                    task_name=f"{task.name} / {comp.name}",
                    task_type=f"DataFlow/{comp.component_type}",
                    severity="manual_required",
                    message=(
                        f"Data Flow component '{comp.component_type}' is not supported in ADF Mapping Data Flows. "
                        "Manual implementation required."
                    ),
                    recommendation=(
                        "Use an Azure Databricks notebook, U-SQL, or Azure Function "
                        "to implement the equivalent transformation."
                    ),
                ))
            elif comp.component_type in _REVIEW_DF_COMPONENTS:
                gaps.append(GapItem(
                    task_id=comp.id,
                    task_name=f"{task.name} / {comp.name}",
                    task_type=f"DataFlow/{comp.component_type}",
                    severity="warning",
                    message=_REVIEW_DF_COMPONENTS[comp.component_type],
                    recommendation="Review the generated Mapping Data Flow transformation.",
                ))
            elif not _is_known_df_component(comp.component_type):
                gaps.append(GapItem(
                    task_id=comp.id,
                    task_name=f"{task.name} / {comp.name}",
                    task_type=f"DataFlow/{comp.component_type}",
                    severity="manual_required",
                    message=(
                        f"Data Flow component type '{comp.component_type}' is not recognised; "
                        "it is kept as an empty placeholder in the Mapping Data Flow."
                    ),
                    recommendation="Implement the component's logic by hand in the data flow.",
                ))

    # Cross-database and linked server references (applies to any task)
    for ref in task.cross_db_references:
        if ref.ref_type == CrossDbReferenceType.FOUR_PART:
            gaps.append(GapItem(
                task_id=task.id,
                task_name=task.name,
                task_type=f"{task.task_type.value}/LinkedServer",
                severity="manual_required",
                message=(
                    f"Linked server reference detected: {ref.raw_match}. "
                    f"Server '{ref.server_name}' → database '{ref.database_name}'. "
                    "This pattern has no direct ADF equivalent."
                ),
                recommendation=(
                    "Replace with Azure SQL external table, elastic query, or consolidate databases. "
                    "If the linked server points to a different data source, create a separate "
                    "linked service and pipeline."
                ),
            ))
        elif ref.ref_type == CrossDbReferenceType.THREE_PART:
            gaps.append(GapItem(
                task_id=task.id,
                task_name=task.name,
                task_type=f"{task.task_type.value}/CrossDatabase",
                severity="warning",
                message=(
                    f"Cross-database reference detected: {ref.raw_match}. "
                    f"Database '{ref.database_name}' → '{ref.schema_name}.{ref.table_name}'. "
                    "Verify the target database is accessible from Azure SQL."
                ),
                recommendation=(
                    "If consolidating to a single Azure SQL database, update schema references. "
                    "If keeping separate databases, ensure connectivity via linked services."
                ),
            ))
        elif ref.ref_type in (CrossDbReferenceType.OPENQUERY, CrossDbReferenceType.OPENROWSET):
            gaps.append(GapItem(
                task_id=task.id,
                task_name=task.name,
                task_type=f"{task.task_type.value}/{ref.ref_type.value}",
                severity="manual_required",
                message=(
                    f"{ref.ref_type.value.upper()} detected: {ref.raw_match}. "
                    "This pattern is not supported in Azure SQL."
                ),
                recommendation=(
                    "Replace with a linked service query or Azure Function. "
                    "Use Copy Activity to stage the remote data into Azure SQL first."
                ),
            ))

    return gaps
