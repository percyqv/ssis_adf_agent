"""
Data Flow transformation converter — maps SSIS Data Flow transformation component
types to ADF Mapping Data Flow transformation JSON snippets.

Each function returns a dict that goes into the ``transformations`` array of the
Mapping Data Flow JSON.
"""
from __future__ import annotations

import re
from typing import Any

from ...parsers.models import DataFlowComponent


def convert_transformation(component: DataFlowComponent) -> dict[str, Any] | None:
    """
    Dispatch to the right transformation builder based on component_type.
    Returns None only for Multicast (branching needs no ADF transformation);
    unknown types get a placeholder flagged for manual review.
    """
    builder = _DISPATCH.get(component.component_type)
    if builder is None:
        return _generic(component)
    return builder(component)


def _dispatch_table() -> dict[str, Any]:
    return {
        "DerivedColumn": _derived_column,
        "Lookup": _lookup,
        "ConditionalSplit": _conditional_split,
        "Aggregate": _aggregate,
        "Sort": _sort,
        "UnionAll": _union_all,
        "Merge": _merge,
        "MergeJoin": _merge_join,
        "DataConversion": _data_conversion,
        "RowCount": _row_count,
        "Multicast": _multicast,
        "CharacterMap": _character_map,
        "ScriptComponent": _script_component,
        "CopyColumn": _copy_column,
        "Pivot": _pivot,
        "Unpivot": _unpivot,
        "PercentageSampling": _percentage_sampling,
        "RowSampling": _row_sampling,
        "OleDbCommand": _ole_db_command,
        "SlowlyChangingDimension": _slowly_changing_dimension,
        "FuzzyLookup": _unsupported,
        "FuzzyGrouping": _unsupported,
        "TermExtraction": _unsupported,
        "TermLookup": _unsupported,
        "ExportColumn": _unsupported,
        "ImportColumn": _unsupported,
        "Cache": _unsupported,
        "RecordsetDestination": _unsupported,
    }


def _base(component: DataFlowComponent, transform_type: str) -> dict[str, Any]:
    return {
        "name": component.name.replace(" ", "_"),
        "description": f"SSIS {component.component_type}: {component.name}",
        "type": transform_type,
        "typeProperties": {},
    }


def _derived_column(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "DerivedColumn")
    # Try to build column expressions from properties
    columns: list[dict] = []
    for col in component.output_columns:
        # Expression may be stored in properties keyed by column name
        expr = component.properties.get(col.name) or f"/* TODO: expression for {col.name} */"
        columns.append({"name": col.name, "expression": expr})
    t["typeProperties"]["columns"] = columns
    return t


def _lookup(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Lookup")
    ref_conn = component.connection_id or "unknown"
    t["typeProperties"] = {
        "lookupTable": {
            "referenceName": f"DS_{component.name.replace(' ', '_')}_lookup",
            "type": "DatasetReference",
        },
        "existsOrNotExists": "exists",
        "matchMultipleRows": False,
        "conditions": [],  # TODO: populate join conditions from component.properties
    }
    return t


def _conditional_split(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "ConditionalSplit")
    conditions: list[dict] = []
    # Each output beyond the first is a named condition
    for i, out_col in enumerate(component.output_columns):
        conditions.append({
            "name": out_col.name,
            "expression": f"/* TODO: split condition {i} */",
        })
    t["typeProperties"]["conditions"] = conditions
    return t


def _aggregate(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Aggregate")
    t["typeProperties"] = {
        "groupBy": [],     # TODO: map from component.properties
        "aggregations": [],
    }
    return t


def _sort(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Sort")
    t["typeProperties"] = {
        "sortConditions": [],  # TODO: map from component.properties
        "caseSensitive": False,
    }
    return t


def _union_all(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Union")
    t["typeProperties"] = {}
    return t


def _merge(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Union")
    t["description"] += " [SSIS Merge — inputs must be pre-sorted]"
    return t


def _merge_join(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Join")
    join_type = component.properties.get("JoinType") or "inner"
    t["typeProperties"] = {
        "joinType": str(join_type).lower(),
        "conditions": [],   # TODO: extract from component.properties
    }
    return t


def _data_conversion(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Cast")
    columns: list[dict] = []
    for col in component.output_columns:
        columns.append({
            "name": col.name,
            "type": col.data_type.value,
            "length": col.length or None,
            "scale": col.scale or None,
        })
    t["typeProperties"]["columns"] = columns
    return t


def _row_count(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "SetVariable")
    var_name = component.properties.get("VariableName") or "RowCount"
    var_name_short = var_name.split("::")[-1]
    t["typeProperties"] = {
        "variableName": var_name_short,
        "value": "/* populated by Mapping Data Flow rowCount() */",
    }
    return t


def _multicast(component: DataFlowComponent) -> dict[str, Any]:
    # Multicast fans out the stream; in ADF you simply reference the same stream
    # in multiple downstream branches — no explicit transformation needed.
    return None  # type: ignore[return-value]


def _character_map(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "DerivedColumn")
    cols = []
    for col in component.output_columns:
        op = component.properties.get("MapFlags") or "upper"
        cols.append({"name": col.name, "expression": f"{op}({col.name})"})
    t["typeProperties"]["columns"] = cols
    return t


def _label(component: DataFlowComponent) -> str:
    """Original SSIS name and type, so a review note can be traced back to the .dtsx."""
    return f"SSIS {component.component_type} '{component.name}'"


def _review(component: DataFlowComponent, transform_type: str, note: str) -> dict[str, Any]:
    t = _base(component, transform_type)
    t["description"] = f"[MANUAL REVIEW] {_label(component)}: {note}"
    return t


def _script_component(component: DataFlowComponent) -> dict[str, Any]:
    t = _review(
        component, "ExternalCall",
        "mapped to an ExternalCall transformation. Implement the logic in an Azure "
        "Function / Databricks.",
    )
    t["typeProperties"] = {"functionName": f"TODO_{component.name.replace(' ', '_')}"}
    return t


def _copy_column(component: DataFlowComponent) -> dict[str, Any]:
    """Copy Column adds duplicates of input columns -> Derived Column ``new = old``."""
    ins, outs = component.input_columns, component.output_columns
    if ins and len(ins) == len(outs):
        columns = [{"name": o.name, "expression": i.name} for i, o in zip(ins, outs)]
        note = "mapped to Derived Column copies, pairing input and output columns in order; "
        note += "verify each copy reads the intended source column."
    else:
        columns = [
            {"name": o.name, "expression": f"/* TODO: source column for {o.name} */"}
            for o in outs
        ]
        note = "mapped to Derived Column; set the source column of each copy."
    t = _review(component, "DerivedColumn", note)
    t["typeProperties"]["columns"] = columns
    return t


def _pivot(component: DataFlowComponent) -> dict[str, Any]:
    t = _review(
        component, "Pivot",
        "mapped to a Pivot transformation; set the group-by columns, the pivot key and "
        "the pivoted value columns (SSIS PivotUsage 1/2/3).",
    )
    t["typeProperties"] = {"groupBy": [], "pivotKey": None, "pivotedColumns": []}
    return t


def _unpivot(component: DataFlowComponent) -> dict[str, Any]:
    t = _review(
        component, "Unpivot",
        "mapped to an Unpivot transformation; set the ungroup-by columns, the unpivot "
        "key (SSIS PivotKeyValue) and the unpivoted columns.",
    )
    t["typeProperties"] = {"ungroupBy": [], "unpivotKey": None, "unpivotedColumns": []}
    return t


def _percentage_sampling(component: DataFlowComponent) -> dict[str, Any]:
    raw = component.properties.get("SamplingValue")
    seed = component.properties.get("SamplingSeed") or "1"
    try:
        fraction = float(raw) / 100
        condition = f"random({seed}) < {fraction:g}"
    except (TypeError, ValueError):
        condition = f"random({seed}) < /* TODO: sampling percentage / 100 */"
    t = _review(
        component, "Filter",
        "mapped to a Filter with random(); the sample size is approximate and SSIS's "
        "'unselected rows' output is not produced (add a second Filter with the negated "
        "condition if it was used).",
    )
    t["typeProperties"] = {"condition": condition}
    return t


def _row_sampling(component: DataFlowComponent) -> dict[str, Any]:
    rows = component.properties.get("SamplingValue") or "N"
    t = _review(
        component, "Filter",
        f"SSIS picks exactly {rows} random rows; Mapping Data Flows have no equivalent. "
        "Use a Window/Rank over random(1) and keep rank <= N, or drop the step.",
    )
    t["typeProperties"] = {"condition": f"/* TODO: keep {rows} random rows */"}
    return t


def _ole_db_command(component: DataFlowComponent) -> dict[str, Any]:
    sql = (component.properties.get("SqlCommand") or "").strip() or "(not found in package)"
    return _review(
        component, "AlterRow",
        "runs a SQL statement once per row; Mapping Data Flows cannot do that. Either "
        "land the rows in a staging table and run the statement set-based in a "
        "Script / Stored Procedure activity after the data flow, or, for a simple "
        "UPDATE/DELETE, use Alter Row + a sink with update/delete enabled. "
        f"Original SQL: {sql}",
    )


def _slowly_changing_dimension(component: DataFlowComponent) -> dict[str, Any]:
    where = component.properties.get("CurrentRowWhere")
    note = (
        "has no single ADF equivalent. Rebuild it as Lookup on the dimension (business "
        "key) -> Conditional Split (new / changed / unchanged) -> Derived Column "
        "(validity dates, current flag) -> Alter Row -> sink with upsert, or start from "
        "the ADF template gallery's SCD Type 1/2 data flows."
    )
    if where:
        note += f" SSIS current-row filter: {where}"
    return _review(component, "AlterRow", note)


def _unsupported(component: DataFlowComponent) -> dict[str, Any]:
    t = _base(component, "Wait")
    t["description"] = (
        f"[UNSUPPORTED — {component.component_type}] {_label(component)}: manual "
        "implementation required. This component has no ADF Mapping Data Flow equivalent."
    )
    return t


def _generic(component: DataFlowComponent) -> dict[str, Any]:
    return {
        "name": component.name.replace(" ", "_"),
        "description": (
            f"[MANUAL REVIEW] {_label(component)}: unknown component type, nothing was "
            "converted. Implement its logic by hand."
        ),
        "type": "DerivedColumn",
        "typeProperties": {"columns": []},
    }


# Built after all builders are defined.
_DISPATCH: dict[str, Any] = _dispatch_table()

# Component types this module knows how to handle (fully, partially or as an
# explicit "unsupported" placeholder). Anything else is reported as unrecognised.
KNOWN_TRANSFORMATION_TYPES: frozenset[str] = frozenset(_DISPATCH)
