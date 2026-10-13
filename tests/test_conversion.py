"""End-to-end tests for SSIS 2012+ (.dtsx format 8) packages in samples/."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ssis_adf_agent import mcp_server
from ssis_adf_agent.parsers.models import DataFlowTask, SequenceContainer
from ssis_adf_agent.parsers.ssis_parser import SSISParser, resolve_component_type

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _call(tool: str, args: dict) -> dict:
    result = asyncio.run(mcp_server.call_tool(tool, args))
    return json.loads(result[0].text)


def _convert(package: str, out: Path) -> tuple[dict, dict]:
    summary = _call(
        "convert_ssis_package",
        {"package_path": str(SAMPLES / package), "output_dir": str(out)},
    )
    pipeline = json.loads(Path(summary["files"]["pipeline"]).read_text(encoding="utf-8"))
    return summary, pipeline


def _activities(pipeline: dict) -> dict[str, dict]:
    return {a["name"]: a for a in pipeline["properties"]["activities"]}


def _deps(activity: dict) -> list[tuple[str, list[str]]]:
    return [(d["activity"], d["dependencyConditions"]) for d in activity.get("dependsOn", [])]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("class_id", "expected"),
    [
        ("Microsoft.OLEDBSource", "OleDbSource"),
        ("Microsoft.OLEDBDestination", "OleDbDestination"),
        ("Microsoft.Lookup", "Lookup"),
        ("Microsoft.DataConvert", "DataConversion"),
        ("{2C0A8BE5-1EDC-4353-A0EF-B778599C65A0}", "OleDbSource"),
        ("2c0a8be5-1edc-4353-a0ef-b778599c65a0", "OleDbSource"),
    ],
)
def test_resolve_component_type(class_id: str, expected: str) -> None:
    assert resolve_component_type(class_id) == expected


def test_managed_component_host_uses_user_component_type() -> None:
    props = {"UserComponentTypeName": "Microsoft.SqlServer.Dts.Pipeline.DataReaderSourceAdapter"}
    assert resolve_component_type("Microsoft.ManagedComponentHost", props) == "ADONetSource"
    props = {"UserComponentTypeName": "Microsoft.ScriptComponentHost"}
    assert resolve_component_type("Microsoft.ManagedComponentHost", props) == "ScriptComponent"


def test_parser_resolves_refid_constraints_and_connections() -> None:
    pkg = SSISParser().parse(SAMPLES / "CargaVentas.dtsx")
    task_ids = {t.id for t in pkg.tasks}
    assert len(pkg.constraints) == 1
    c = pkg.constraints[0]
    assert c.from_task_id in task_ids and c.to_task_id in task_ids

    df = next(t for t in pkg.tasks if isinstance(t, DataFlowTask))
    by_name = {c.name: c for c in df.components}
    assert by_name["Origen"].component_type == "OleDbSource"
    assert by_name["Destino"].component_type == "OleDbDestination"
    cm_ids = {cm.name: cm.id for cm in pkg.connection_managers}
    assert by_name["Origen"].connection_id == cm_ids["SRC_OLTP"]
    assert by_name["Destino"].connection_id == cm_ids["DW"]
    assert by_name["Origen"].properties["SqlCommand"] == "SELECT * FROM dbo.Ventas"


def test_parser_nested_container_constraints() -> None:
    pkg = SSISParser().parse(SAMPLES / "CargaClientes.dtsx")
    seq = next(t for t in pkg.tasks if isinstance(t, SequenceContainer))
    inner_ids = {t.id for t in seq.tasks}
    assert len(seq.constraints) == 1
    assert seq.constraints[0].from_task_id in inner_ids
    assert seq.constraints[0].to_task_id in inner_ids


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def test_simple_package_becomes_ordered_copy(tmp_path: Path) -> None:
    summary, pipeline = _convert("CargaVentas.dtsx", tmp_path)
    acts = _activities(pipeline)

    assert acts["Truncar Stage"]["type"] == "Script"
    copy = acts["Cargar Ventas"]
    assert copy["type"] == "Copy"
    assert _deps(copy) == [("Truncar Stage", ["Succeeded"])]

    # On-premises SQL Server: SqlSource / SqlSink / SqlServerTable
    src = copy["typeProperties"]["source"]
    assert src["type"] == "SqlSource"
    assert src["sqlReaderQuery"] == "SELECT * FROM dbo.Ventas"
    assert copy["typeProperties"]["sink"]["type"] == "SqlSink"

    ds_dir = tmp_path / "dataset"
    dst = json.loads((ds_dir / "DS_Destino.json").read_text(encoding="utf-8"))
    assert dst["properties"]["type"] == "SqlServerTable"
    assert dst["properties"]["typeProperties"] == {"schema": "stg", "table": "Ventas"}
    assert summary["artifacts_generated"]["data_flows"] == 0


def test_sequence_container_is_flattened_and_rewired(tmp_path: Path) -> None:
    summary, pipeline = _convert("CargaClientes.dtsx", tmp_path)
    acts = _activities(pipeline)

    assert "Carga" not in acts  # container itself is not an ADF activity
    # Container's incoming dependency moves to its first inner activity
    assert _deps(acts["Limpiar Stage"]) == [("Obtener Fecha Corte", ["Succeeded"])]
    assert _deps(acts["Cargar Clientes"]) == [("Limpiar Stage", ["Succeeded"])]
    # Dependency on the container moves to its last inner activity (flagged)
    err = acts["Registrar Error"]
    assert _deps(err) == [("Cargar Clientes", ["Failed"])]
    assert "MANUAL REVIEW" in err["description"]


def test_execute_sql_single_row_becomes_lookup(tmp_path: Path) -> None:
    _, pipeline = _convert("CargaClientes.dtsx", tmp_path)
    lookup = _activities(pipeline)["Obtener Fecha Corte"]
    assert lookup["type"] == "Lookup"
    assert lookup["typeProperties"]["firstRowOnly"] is True
    # Azure SQL connection -> Azure SQL source and dataset
    assert lookup["typeProperties"]["source"]["type"] == "AzureSqlSource"
    ds = json.loads((tmp_path / "dataset" / "DS_Obtener_Fecha_Corte.json").read_text("utf-8"))
    assert ds["properties"]["type"] == "AzureSqlTable"


def test_nested_data_flow_with_transform_generates_mapping_data_flow(tmp_path: Path) -> None:
    summary, pipeline = _convert("CargaClientes.dtsx", tmp_path)
    assert summary["files"]["data_flows"] == ["DF_Cargar_Clientes"]
    assert _activities(pipeline)["Cargar Clientes"]["type"] == "ExecuteDataFlow"
    assert any("on-premises connection" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_detects_dangling_dependency(tmp_path: Path) -> None:
    _convert("CargaVentas.dtsx", tmp_path)
    assert _call("validate_adf_artifacts", {"artifacts_dir": str(tmp_path)})["status"] == "valid"

    pl_file = next((tmp_path / "pipeline").glob("*.json"))
    pl = json.loads(pl_file.read_text(encoding="utf-8"))
    pl["properties"]["activities"][1]["dependsOn"] = [
        {"activity": "Does Not Exist", "dependencyConditions": ["Succeeded"]}
    ]
    pl_file.write_text(json.dumps(pl), encoding="utf-8")

    result = _call("validate_adf_artifacts", {"artifacts_dir": str(tmp_path)})
    assert result["status"] == "issues_found"
    assert any("Does Not Exist" in i["error"] for i in result["issues"])


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def test_linked_services_are_named_after_connection_managers(tmp_path: Path) -> None:
    summary, pipeline = _convert("CargaVentas.dtsx", tmp_path)
    assert sorted(summary["files"]["linked_services"]) == ["LS_DW", "LS_SRC_OLTP"]
    assert (tmp_path / "linkedService" / "LS_DW.json").exists()

    script = _activities(pipeline)["Truncar Stage"]
    assert script["linkedServiceName"]["referenceName"] == "LS_DW"
    ds = json.loads((tmp_path / "dataset" / "DS_Origen.json").read_text("utf-8"))
    assert ds["properties"]["linkedServiceName"]["referenceName"] == "LS_SRC_OLTP"

    # No GUID-based names anywhere in the output
    for f in tmp_path.rglob("*.json"):
        assert "LS_2222" not in f.read_text("utf-8") and "LS_3333" not in f.read_text("utf-8")


def test_linked_service_names_are_safe_and_unique() -> None:
    from ssis_adf_agent.naming import linked_service_names, linked_service_ref
    from ssis_adf_agent.parsers.models import ConnectionManagerType, SSISConnectionManager

    cms = [
        SSISConnectionManager(id="A", name="CRM Origen", type=ConnectionManagerType.OLEDB),
        SSISConnectionManager(id="B", name="CRM.Origen", type=ConnectionManagerType.OLEDB),
        SSISConnectionManager(id="C", name="crm_origen", type=ConnectionManagerType.OLEDB),
    ]
    names = linked_service_names(cms)
    assert names == {"A": "LS_CRM_Origen", "B": "LS_CRM_Origen_2", "C": "LS_crm_origen_3"}
    assert linked_service_ref("A", names) == "LS_CRM_Origen"
    assert linked_service_ref("ProyectoCRM", names) == "LS_ProyectoCRM"  # project-level CM
    assert linked_service_ref(None, names) == "LS_unknown"
