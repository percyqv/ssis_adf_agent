"""Data Flow Tasks whose middle component is not a plain source/destination.

Regression tests for a bug where components such as OLE DB Command, Slowly
Changing Dimension, Pivot, etc. were not counted as transformations: a
``source -> component -> destination`` flow became a plain Copy activity and the
component was silently dropped from the conversion.
"""
# ruff: noqa: E501  (the inline .dtsx template is easier to read unwrapped)
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ssis_adf_agent import mcp_server
from ssis_adf_agent.converters.control_flow.data_flow_converter import DataFlowConverter
from ssis_adf_agent.converters.data_flow.classification import needs_mapping_data_flow
from ssis_adf_agent.converters.data_flow.transformation_converter import (
    convert_transformation,
)
from ssis_adf_agent.generators.dataflow_generator import generate_data_flows
from ssis_adf_agent.parsers.models import (
    DataFlowColumn,
    DataFlowComponent,
    DataFlowTask,
    SSISPackage,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

# (componentClassID as written in the .dtsx, component type the parser resolves)
MIDDLE_COMPONENTS = [
    ("Microsoft.OLEDBCommand", "OleDbCommand"),
    ("Microsoft.SCD", "SlowlyChangingDimension"),
    ("Microsoft.Pivot", "Pivot"),
    ("Microsoft.UnPivot", "Unpivot"),
    ("Microsoft.CopyMap", "CopyColumn"),
    ("Microsoft.PctSampling", "PercentageSampling"),
    ("Microsoft.RowSampling", "RowSampling"),
    ("{7D910C2B-4EB3-48F8-B892-5BCDF74E1DF9}", "ExportColumn"),
    ("{7BB0DB4E-B7E0-4E1E-A15B-43C58DBBCA33}", "ImportColumn"),
    # Not in any map: must not be dropped either.
    ("Microsoft.Audit", "Microsoft.Audit"),
]

_PACKAGE_TEMPLATE = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:refId="Package"
  DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="Pkg"
  DTS:DTSID="{{B1111111-1111-1111-1111-111111111111}}">
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[AZ]" DTS:CreationName="OLEDB"
      DTS:ObjectName="AZ" DTS:DTSID="{{B2222222-2222-2222-2222-222222222222}}">
      <DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Data Source=x.database.windows.net;Initial Catalog=Db;Provider=MSOLEDBSQL;"/></DTS:ObjectData>
    </DTS:ConnectionManager>
  </DTS:ConnectionManagers>
  <DTS:Executables>
    <DTS:Executable DTS:refId="Package\\Flujo" DTS:ExecutableType="Microsoft.Pipeline"
      DTS:ObjectName="Flujo" DTS:DTSID="{{B5555555-5555-5555-5555-555555555555}}">
      <DTS:ObjectData><pipeline version="1"><components>
        <component refId="Package\\Flujo\\Origen" componentClassID="Microsoft.OLEDBSource" name="Origen">
          <properties><property name="SqlCommand">SELECT 1 AS Id</property><property name="AccessMode">2</property></properties>
          <connections><connection refId="x" connectionManagerRefId="Package.ConnectionManagers[AZ]" name="c"/></connections>
        </component>
        <component refId="Package\\Flujo\\Paso Intermedio" componentClassID="{class_id}" name="Paso Intermedio">
          <properties><property name="SqlCommand">UPDATE dbo.T SET X = 1 WHERE Id = ?</property></properties>
        </component>
        <component refId="Package\\Flujo\\Destino" componentClassID="Microsoft.OLEDBDestination" name="Destino">
          <properties><property name="OpenRowset">[dbo].[T]</property><property name="AccessMode">3</property></properties>
          <connections><connection refId="y" connectionManagerRefId="Package.ConnectionManagers[AZ]" name="c"/></connections>
        </component>
      </components></pipeline></DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def _call(tool: str, args: dict) -> dict:
    result = asyncio.run(mcp_server.call_tool(tool, args))
    return json.loads(result[0].text)


def _comp(name: str, comp_type: str, **kw) -> DataFlowComponent:
    return DataFlowComponent(id=name, name=name, component_class_id=comp_type,
                             component_type=comp_type, **kw)


def _flow(*middle_types: str, sources: int = 1, dests: int = 1) -> DataFlowTask:
    comps = [_comp(f"src{i}", "OleDbSource") for i in range(sources)]
    comps += [_comp(f"mid{i}", t) for i, t in enumerate(middle_types)]
    comps += [_comp(f"dst{i}", "OleDbDestination") for i in range(dests)]
    return DataFlowTask(id="t", name="Flujo", components=comps)


def _write_package(tmp_path: Path, class_id: str) -> Path:
    path = tmp_path / "Pkg.dtsx"
    path.write_text(_PACKAGE_TEMPLATE.format(class_id=class_id), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Classification: Copy only for exactly one source + one destination, nothing else
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("_class_id", "comp_type"), MIDDLE_COMPONENTS)
def test_any_middle_component_forces_mapping_data_flow(_class_id: str, comp_type: str) -> None:
    task = _flow(comp_type)
    assert needs_mapping_data_flow(task)
    activity = DataFlowConverter().convert(task, [], {task.id: task})[0]
    assert activity["type"] == "ExecuteDataFlow"
    assert activity["typeProperties"]["dataflow"]["referenceName"] == "DF_Flujo"


def test_plain_source_to_destination_is_still_a_copy() -> None:
    task = _flow()
    assert not needs_mapping_data_flow(task)
    assert DataFlowConverter().convert(task, [], {task.id: task})[0]["type"] == "Copy"


@pytest.mark.parametrize(("sources", "dests"), [(1, 0), (0, 1), (2, 1), (1, 2)])
def test_non_one_to_one_flows_use_mapping_data_flow(sources: int, dests: int) -> None:
    assert needs_mapping_data_flow(_flow(sources=sources, dests=dests))


@pytest.mark.parametrize(
    "task",
    [
        _flow(),
        _flow("Lookup"),
        _flow("OleDbCommand"),
        _flow(sources=1, dests=0),  # used to reference a data flow that was never generated
        _flow(sources=0, dests=1),
        _flow(sources=2, dests=1),
    ],
    ids=["copy", "lookup", "oledbcommand", "source-only", "dest-only", "two-sources"],
)
def test_converter_and_generator_agree(tmp_path: Path, task: DataFlowTask) -> None:
    activity = DataFlowConverter().convert(task, [], {task.id: task})[0]
    package = SSISPackage(id="p", name="Pkg", source_file="x.dtsx", tasks=[task])
    generated = [df["name"] for df in generate_data_flows(package, tmp_path)]
    if activity["type"] == "ExecuteDataFlow":
        assert generated == [activity["typeProperties"]["dataflow"]["referenceName"]]
    else:
        assert activity["type"] == "Copy"
        assert generated == []


def test_empty_data_flow_is_a_flagged_placeholder(tmp_path: Path) -> None:
    task = DataFlowTask(id="t", name="Vacio", components=[])
    activity = DataFlowConverter().convert(task, [], {task.id: task})[0]
    assert activity["type"] == "Wait"
    assert "MANUAL REVIEW" in activity["description"]
    package = SSISPackage(id="p", name="Pkg", source_file="x.dtsx", tasks=[task])
    assert generate_data_flows(package, tmp_path) == []


# ---------------------------------------------------------------------------
# End to end: the component is kept in the Mapping Data Flow and reported
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("class_id", "comp_type"), MIDDLE_COMPONENTS)
def test_convert_keeps_and_reports_middle_component(
    tmp_path: Path, class_id: str, comp_type: str
) -> None:
    pkg = _write_package(tmp_path, class_id)
    out = tmp_path / "out"
    summary = _call("convert_ssis_package", {"package_path": str(pkg), "output_dir": str(out)})

    pipeline = json.loads(Path(summary["files"]["pipeline"]).read_text(encoding="utf-8"))
    act = pipeline["properties"]["activities"][0]
    assert act["type"] == "ExecuteDataFlow"
    assert summary["files"]["data_flows"] == ["DF_Flujo"]

    df = json.loads((out / "dataflow" / "DF_Flujo.json").read_text(encoding="utf-8"))
    transforms = df["properties"]["typeProperties"]["transformations"]
    assert [t["name"] for t in transforms] == ["Paso_Intermedio"]
    assert comp_type in transforms[0]["description"]

    # The component needs manual work, and the summary says so by name.
    assert any("Paso Intermedio" in w and comp_type in w for w in summary["warnings"]), (
        summary["warnings"]
    )

    # Every artifact the pipeline references exists.
    status = _call("validate_adf_artifacts", {"artifacts_dir": str(out)})["status"]
    assert status != "issues_found"


@pytest.mark.parametrize(("class_id", "comp_type"), MIDDLE_COMPONENTS)
def test_analyze_reports_middle_component(tmp_path: Path, class_id: str, comp_type: str) -> None:
    pkg = _write_package(tmp_path, class_id)
    result = _call("analyze_ssis_package", {"package_path": str(pkg)})
    gaps = result["gaps_by_severity"]["manual_required"] + result["gaps_by_severity"]["warning"]
    assert any(g["task_type"] == f"DataFlow/{comp_type}" for g in gaps), gaps


def test_ole_db_command_sample(tmp_path: Path) -> None:
    summary = _call(
        "convert_ssis_package",
        {"package_path": str(SAMPLES / "ActualizarClientes.dtsx"), "output_dir": str(tmp_path)},
    )
    pipeline = json.loads(Path(summary["files"]["pipeline"]).read_text(encoding="utf-8"))
    assert pipeline["properties"]["activities"][0]["type"] == "ExecuteDataFlow"

    df = json.loads((tmp_path / "dataflow" / "DF_Actualizar_Clientes.json").read_text("utf-8"))
    cmd = df["properties"]["typeProperties"]["transformations"][0]
    assert cmd["name"] == "Marcar_Procesado"
    # The original SQL is carried over so nobody has to dig it out of the .dtsx.
    assert "UPDATE dbo.Clientes SET Procesado = 1 WHERE Id = ?" in cmd["description"]


# ---------------------------------------------------------------------------
# Individual transformation mappings
# ---------------------------------------------------------------------------

def test_pivot_and_unpivot_map_to_native_transformations() -> None:
    assert convert_transformation(_comp("P", "Pivot"))["type"] == "Pivot"
    assert convert_transformation(_comp("U", "Unpivot"))["type"] == "Unpivot"


def test_copy_column_becomes_derived_column_copies() -> None:
    comp = _comp(
        "Copiar", "CopyColumn",
        input_columns=[DataFlowColumn(name="Nombre"), DataFlowColumn(name="Pais")],
        output_columns=[DataFlowColumn(name="Copy of Nombre"), DataFlowColumn(name="PaisCopia")],
    )
    t = convert_transformation(comp)
    assert t["type"] == "DerivedColumn"
    assert t["typeProperties"]["columns"] == [
        {"name": "Copy of Nombre", "expression": "Nombre"},
        {"name": "PaisCopia", "expression": "Pais"},
    ]


def test_percentage_sampling_becomes_random_filter() -> None:
    t = convert_transformation(_comp("Muestra", "PercentageSampling",
                                     properties={"SamplingValue": "10"}))
    assert t["type"] == "Filter"
    assert t["typeProperties"]["condition"] == "random(1) < 0.1"
