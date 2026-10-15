"""
Classify the components of an SSIS Data Flow Task.

Single source of truth for deciding whether a Data Flow Task becomes a plain ADF
Copy activity or an Execute Data Flow activity backed by a Mapping Data Flow.
Both the control-flow converter (which emits the activity) and the data flow
generator (which emits the ``dataflow/*.json`` it references) use this module,
so they can never disagree.

A Copy activity can only move rows from one place to another. It is used ONLY
when the flow is exactly one source and one destination with nothing else in
between. Any other component -- a known transformation, one we recognise but
cannot convert (OLE DB Command, SCD, ...), or one we do not recognise at all --
forces a Mapping Data Flow, so the component is kept and flagged for review
instead of being silently dropped.
"""
from __future__ import annotations

from ...parsers.models import DataFlowComponent, DataFlowTask

SOURCE_TYPES: frozenset[str] = frozenset({
    "OleDbSource", "FlatFileSource", "ExcelSource", "OdbcSource",
    "ADONetSource", "SqlServerSource",
})

DEST_TYPES: frozenset[str] = frozenset({
    "OleDbDestination", "FlatFileDestination", "ExcelDestination",
    "OdbcDestination", "ADONetDestination", "SqlServerDestination",
    "RecordsetDestination",
})


def is_source(component: DataFlowComponent) -> bool:
    return component.component_type in SOURCE_TYPES


def is_destination(component: DataFlowComponent) -> bool:
    return component.component_type in DEST_TYPES


def split_components(
    task: DataFlowTask,
) -> tuple[list[DataFlowComponent], list[DataFlowComponent], list[DataFlowComponent]]:
    """Return ``(sources, transformations, destinations)``.

    "Transformations" is everything that is neither a known source nor a known
    destination, including unrecognised component types.
    """
    sources = [c for c in task.components if is_source(c)]
    dests = [c for c in task.components if is_destination(c)]
    others = [c for c in task.components if not is_source(c) and not is_destination(c)]
    return sources, others, dests


def is_simple_copy(task: DataFlowTask) -> bool:
    """True only for exactly one source, one destination and no other component."""
    sources, others, dests = split_components(task)
    return len(sources) == 1 and len(dests) == 1 and not others


def needs_mapping_data_flow(task: DataFlowTask) -> bool:
    """True when the Data Flow Task must become an Execute Data Flow activity.

    An empty Data Flow Task needs neither (the converter emits a placeholder).
    """
    return bool(task.components) and not is_simple_copy(task)
