"""
Mapping Data Flow generator — emits ADF dataflow.json for each complex Data Flow Task.

Simple (single source → single destination, no transforms) data flows are handled
as Copy Activities; this generator only fires for multi-component flows.

Best practices applied:
  - allowSchemaDrift: true, validateSchema: false (Microsoft default for flexibility)
  - errorHandlingOption: stopOnFirstError (explicit)
  - Isolation level: READ_UNCOMMITTED for sources (Microsoft default for data flows)
  - Configurable compute settings (coreCount, computeType)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..parsers.models import DataFlowTask, SSISPackage, TaskType
from ..converters.data_flow.source_converter import convert_source
from ..converters.data_flow.destination_converter import convert_destination
from ..converters.data_flow.transformation_converter import convert_transformation
from ..naming import linked_service_names

_SOURCE_TYPES = frozenset({
    "OleDbSource", "FlatFileSource", "ExcelSource", "OdbcSource",
    "ADONetSource", "SqlServerSource",
})
_DEST_TYPES = frozenset({
    "OleDbDestination", "FlatFileDestination", "ExcelDestination",
    "OdbcDestination", "ADONetDestination", "SqlServerDestination",
    "RecordsetDestination",
})


def _walk_tasks(tasks: list[Any]):
    """Yield tasks recursively, descending into containers (Sequence/ForEach/For)."""
    for t in tasks:
        yield t
        children = getattr(t, "tasks", None)
        if children:
            yield from _walk_tasks(children)


def generate_data_flows(
    package: SSISPackage,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """
    For every complex Data Flow Task in the package, generate a Mapping Data Flow JSON.
    Files are written to *output_dir*/dataflow/.

    Returns the list of data flow dicts.
    """
    df_dir = output_dir / "dataflow"
    df_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    ls_names = linked_service_names(package.connection_managers)

    for task in _walk_tasks(package.tasks):
        if task.task_type != TaskType.DATA_FLOW:
            continue
        assert isinstance(task, DataFlowTask)

        sources_comps = [c for c in task.components if c.component_type in _SOURCE_TYPES]
        dest_comps = [c for c in task.components if c.component_type in _DEST_TYPES]
        transform_comps = [
            c for c in task.components
            if c.component_type not in _SOURCE_TYPES
            and c.component_type not in _DEST_TYPES
        ]

        # Only generate a Mapping Data Flow for tasks with transforms (or multiple sources/sinks)
        if not transform_comps and len(sources_comps) <= 1 and len(dest_comps) <= 1:
            continue  # handled as Copy Activity

        df_name = f"DF_{task.name.replace(' ', '_')}"

        sources = [convert_source(c, ls_names) for c in sources_comps]
        sinks = [convert_destination(c, ls_names) for c in dest_comps]
        transformations: list[dict[str, Any]] = []
        for comp in transform_comps:
            t = convert_transformation(comp)
            if t is not None:
                transformations.append(t)

        # Collect key columns from destination components for upsert config
        key_cols: list[str] = []
        for comp in dest_comps:
            if comp.key_columns:
                key_cols.extend(comp.key_columns)

        # Build data flow script (simplified — real scripts produced by ADF Studio)
        script = _build_dsl_script(sources, transformations, sinks, key_cols)

        df: dict[str, Any] = {
            "name": df_name,
            "properties": {
                "description": f"Mapping Data Flow for SSIS Data Flow Task: {task.name}",
                "type": "MappingDataFlow",
                "typeProperties": {
                    "sources": sources,
                    "sinks": sinks,
                    "transformations": transformations,
                    "script": script,
                    "scriptLines": script.splitlines(),
                },
                "annotations": ["ssis-adf-agent"],
            },
        }

        (df_dir / f"{df_name}.json").write_text(
            json.dumps(df, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        results.append(df)

    return results


def _build_dsl_script(
    sources: list[dict],
    transformations: list[dict],
    sinks: list[dict],
    key_columns: list[str] | None = None,
) -> str:
    """
    Build a minimal ADF Data Flow DSL script stub so the JSON is valid.
    Real expressions must be filled in by the developer.
    """
    lines: list[str] = []

    for s in sources:
        lines.append(f"source(output(/* TODO: declare output schema */),")
        lines.append(f'    allowSchemaDrift: true,')
        lines.append(f'    validateSchema: false,')
        lines.append(f"    isolationLevel: 'READ_UNCOMMITTED',")
        lines.append(f"    errorHandlingOption: 'stopOnFirstError') ~> {s['name']}")

    for t in transformations:
        prev = sources[-1]["name"] if sources else "source1"
        lines.append(f'{prev} derive(/* TODO: add expressions */) ~> {t["name"]}')

    for sk in sinks:
        prev = (
            transformations[-1]["name"] if transformations
            else (sources[-1]["name"] if sources else "source1")
        )
        has_keys = bool(key_columns)
        lines.append(f'{prev} sink(allowSchemaDrift: true,')
        lines.append(f'    validateSchema: false,')
        lines.append(f'    errorHandlingOption: \'stopOnFirstError\',')
        if has_keys:
            keys_str = ", ".join(f"'{k}'" for k in key_columns)
            lines.append(f'    keys: [{keys_str}],')
            lines.append(f'    deletable: false,')
            lines.append(f'    insertable: true,')
            lines.append(f'    updateable: true,')
            lines.append(f'    upsertable: true) ~> {sk["name"]}')
        else:
            lines.append(f'    deletable: false,')
            lines.append(f'    insertable: true,')
            lines.append(f'    updateable: false,')
            lines.append(f'    upsertable: true) ~> {sk["name"]}')

    return "\n".join(lines)
