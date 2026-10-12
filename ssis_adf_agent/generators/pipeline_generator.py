"""
Pipeline generator — assembles a complete ADF pipeline.json from an SSISPackage.

Supports:
  - Configurable naming prefixes (PL_, LS_, DS_, TR_)
  - Ingestion pattern annotations (full/delta/merge)
  - CDM pattern annotations
  - ESI reuse candidate annotations
  - Schema remapping for database consolidation

Output structure::

    {
        "name": "PL_<PackageName>",
        "properties": {
            "description": "...",
            "activities": [...],
            "parameters": {...},
            "variables": {...}
        }
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..parsers.models import (
    DataFlowTask,
    ExecuteSQLTask,
    IngestionPattern,
    SSISPackage,
    SSISParameter,
    SSISVariable,
    TaskType,
)
from ..analyzers.dependency_graph import topological_sort
from ..converters.dispatcher import ConverterDispatcher

_SSIS_TO_ADF_TYPE: dict[str, str] = {
    "String": "String",
    "Int32": "Int",
    "Int16": "Int",
    "Int64": "Int",
    "Boolean": "Bool",
    "DateTime": "String",
    "Double": "Float",
    "Single": "Float",
    "Object": "String",
}


def _map_param_type(ssis_type: str) -> str:
    return _SSIS_TO_ADF_TYPE.get(ssis_type, "String")


def _collect_annotations(package: SSISPackage) -> list[str]:
    """Build pipeline annotations from detected patterns."""
    annotations = ["ssis-adf-agent", f"source-package:{package.name}"]

    # Ingestion pattern annotations
    has_delta = False
    has_merge = False
    for task in package.tasks:
        pat = IngestionPattern.UNKNOWN
        if isinstance(task, ExecuteSQLTask):
            pat = task.ingestion_pattern
        elif isinstance(task, DataFlowTask):
            pat = task.ingestion_pattern
        if pat == IngestionPattern.DELTA:
            has_delta = True
        elif pat == IngestionPattern.MERGE:
            has_merge = True

    if has_merge:
        annotations.append("ingestion-pattern:merge")
    elif has_delta:
        annotations.append("ingestion-pattern:delta")

    # Cross-DB references
    has_cross_db = any(
        len(t.cross_db_references) > 0 for t in package.tasks
    )
    if has_cross_db:
        annotations.append("has-cross-db-references")

    return annotations


def generate_pipeline(
    package: SSISPackage,
    output_dir: Path,
    stubs_dir: Path | None = None,
    llm_translate: bool = False,
    *,
    pipeline_prefix: str = "PL_",
    cdm_gaps: list | None = None,
    esi_gaps: list | None = None,
    schema_remap: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Convert an SSISPackage to a full ADF pipeline JSON and write it to *output_dir*.

    Args:
        pipeline_prefix: Prefix for pipeline name (default "PL_").
        cdm_gaps: CDM pattern gap items to annotate pipeline.
        esi_gaps: ESI reuse gap items to annotate pipeline.
        schema_remap: Schema remap config for database consolidation.

    Returns the pipeline dict.
    """
    dispatcher = ConverterDispatcher(
        stubs_dir=stubs_dir or output_dir / "stubs",
        llm_translate=llm_translate,
        connection_managers=package.connection_managers,
    )
    pipeline_name = f"{pipeline_prefix}{package.name.replace(' ', '_')}"

    # Topological task ordering
    task_by_id = {t.id: t for t in package.tasks}
    ordered_ids = topological_sort(package.tasks, package.constraints)

    # Convert tasks in dependency order
    activities: list[dict[str, Any]] = []
    for task_id in ordered_ids:
        task = task_by_id.get(task_id)
        if task is None:
            continue
        acts = dispatcher.convert_task(task, package.constraints, task_by_id)

        # Apply schema remap to SQL text in Script activities
        if schema_remap:
            from ..converters.control_flow.execute_sql_converter import apply_schema_remap
            for act in acts:
                if act.get("type") == "Script":
                    scripts = act.get("typeProperties", {}).get("scripts", [])
                    for script in scripts:
                        if "text" in script:
                            script["text"] = apply_schema_remap(script["text"], schema_remap) or script["text"]

        activities.extend(acts)

    # Flattened Sequence containers: point dependencies at their exit activities
    dispatcher.rewire_container_dependencies(activities)

    # Build parameters from SSIS package parameters
    parameters: dict[str, Any] = {}
    for p in package.parameters:
        parameters[p.name] = {
            "type": _map_param_type(p.data_type),
            **({"defaultValue": p.value} if p.value is not None else {}),
        }

    # Add implicit parameters for function URLs (referenced by File System / Send Mail converters)
    _inject_function_url_params(parameters, activities)

    # Add delta_column / key_columns as pipeline parameters when detected
    for task in package.tasks:
        if isinstance(task, ExecuteSQLTask) and task.delta_column:
            parameters.setdefault("delta_column", {"type": "String", "defaultValue": task.delta_column})
        if isinstance(task, DataFlowTask):
            for comp in task.components:
                if comp.key_columns:
                    parameters.setdefault("key_columns", {
                        "type": "String",
                        "defaultValue": ",".join(comp.key_columns),
                    })

    # Build variables from SSIS package variables (User namespace only)
    variables: dict[str, Any] = {}
    for v in package.variables:
        if v.namespace.lower() == "user":
            variables[v.name] = {
                "type": _map_param_type(v.data_type),
                **({"defaultValue": v.value} if v.value is not None else {}),
            }

    # Annotations
    annotations = _collect_annotations(package)
    if cdm_gaps:
        annotations.append("cdm-review-required")
    if esi_gaps:
        annotations.append("esi-reuse-candidate")

    pipeline: dict[str, Any] = {
        "name": pipeline_name,
        "properties": {
            "description": (
                f"Auto-generated from SSIS package: {package.source_file}. "
                "Review flagged activities before deploying."
            ),
            "activities": activities,
            "parameters": parameters,
            "variables": variables,
            "annotations": annotations,
        },
    }

    # Write to disk
    output_dir.mkdir(parents=True, exist_ok=True)
    pipeline_file = output_dir / "pipeline" / f"{pipeline_name}.json"
    pipeline_file.parent.mkdir(parents=True, exist_ok=True)
    pipeline_file.write_text(
        json.dumps(pipeline, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )

    return pipeline


def _inject_function_url_params(
    parameters: dict[str, Any],
    activities: list[dict],
) -> None:
    """Add pipeline parameters for Azure Function URLs referenced in activities."""
    needed = set()
    for act in activities:
        tp = act.get("typeProperties", {})
        url = tp.get("url", "")
        if isinstance(url, str) and url.startswith("@pipeline().parameters."):
            param_name = url.split("@pipeline().parameters.")[-1]
            needed.add(param_name)
    for name in needed:
        if name not in parameters:
            parameters[name] = {
                "type": "String",
                "defaultValue": "https://TODO.azurewebsites.net/api/" + name.replace("Url", ""),
            }
