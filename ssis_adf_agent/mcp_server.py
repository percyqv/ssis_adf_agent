"""
SSIS → ADF MCP Server.

Exposes five tools to GitHub Copilot (and any MCP-compatible client):

1. scan_ssis_packages      — discover .dtsx files (local / git / sql server)
2. analyze_ssis_package    — complexity + gap analysis of a single package
3. convert_ssis_package    — full conversion of a package to ADF JSON artifacts
4. validate_adf_artifacts  — structural validation of generated artifacts
5. deploy_to_adf           — deploy artifacts to Azure Data Factory

Run as an MCP stdio server::

    python -m ssis_adf_agent.mcp_server

Or via the installed script::

    ssis-adf-agent
"""
from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

server = Server("ssis-adf-agent")


# ---------------------------------------------------------------------------
# Tool: scan_ssis_packages
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="scan_ssis_packages",
            description=(
                "Discover all SSIS packages (.dtsx files) from a given source. "
                "Returns a JSON list of found packages with name, path, and basic metadata. "
                "source_type must be one of: 'local', 'git', 'sql'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "enum": ["local", "git", "sql"],
                        "description": "Where to find .dtsx files.",
                    },
                    "path_or_connection": {
                        "type": "string",
                        "description": (
                            "For 'local': absolute filesystem directory path. "
                            "For 'git': repository URL or local path. "
                            "For 'sql': SQL Server connection string or 'SERVER=...;DATABASE=msdb'."
                        ),
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Search subdirectories (local/git only). Default: true.",
                        "default": True,
                    },
                    "git_branch": {
                        "type": "string",
                        "description": "Branch to check out (git source only). Default: 'main'.",
                        "default": "main",
                    },
                },
                "required": ["source_type", "path_or_connection"],
            },
        ),
        types.Tool(
            name="analyze_ssis_package",
            description=(
                "Analyze a single SSIS package (.dtsx file) and return a detailed report including: "
                "complexity score (0-100), effort estimate (Low/Medium/High/Very High), "
                "component inventory, gap analysis (items needing manual work), "
                "cross-database/linked server references, CDM pattern detection, "
                "ESI reuse candidates, and dependency execution order."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package_path": {
                        "type": "string",
                        "description": "Absolute path to the .dtsx file.",
                    },
                    "esi_tables_path": {
                        "type": "string",
                        "description": (
                            "Optional path to a JSON file mapping source_system → table list for ESI reuse detection. "
                            "Format: {\"PHINEOS\": [\"TocPartyAddress\", \"TLBenefit\"]}."
                        ),
                    },
                },
                "required": ["package_path"],
            },
        ),
        types.Tool(
            name="convert_ssis_package",
            description=(
                "Convert a single SSIS package (.dtsx file) to Azure Data Factory JSON artifacts. "
                "Generates: pipeline JSON, linked service JSONs, dataset JSONs, "
                "mapping data flow JSONs, trigger JSONs, and Azure Function stubs for Script Tasks. "
                "Supports Self-Hosted IR, Key Vault secrets, Microsoft Recommended linked service format, "
                "schema remapping, ESI reuse detection, CDM pattern flagging, and cross-package dedup. "
                "Returns a summary of generated files and any warnings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package_path": {
                        "type": "string",
                        "description": "Absolute path to the .dtsx file to convert.",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "Directory to write ADF artifacts to. "
                            "Sub-folders pipeline/, linkedService/, dataset/, dataflow/, trigger/, stubs/ "
                            "will be created automatically."
                        ),
                    },
                    "generate_trigger": {
                        "type": "boolean",
                        "description": "Whether to emit a template ScheduleTrigger JSON. Default: true.",
                        "default": True,
                    },
                    "llm_translate": {
                        "type": "boolean",
                        "description": (
                            "If true, call Azure OpenAI to translate C# Script Task source code to Python "
                            "in the generated Azure Function stubs. Requires AZURE_OPENAI_ENDPOINT and "
                            "AZURE_OPENAI_API_KEY environment variables. Falls back gracefully if unavailable. "
                            "Default: false."
                        ),
                        "default": False,
                    },
                    "on_prem_ir_name": {
                        "type": "string",
                        "description": "Integration Runtime name for on-prem connections. Default: 'SelfHostedIR'.",
                        "default": "SelfHostedIR",
                    },
                    "auth_type": {
                        "type": "string",
                        "description": "Default authentication type for Azure SQL linked services. Default: 'SystemAssignedManagedIdentity'.",
                        "enum": ["SystemAssignedManagedIdentity", "SQL", "ServicePrincipal"],
                        "default": "SystemAssignedManagedIdentity",
                    },
                    "use_key_vault": {
                        "type": "boolean",
                        "description": "Use Azure Key Vault secret references for passwords/connection strings. Default: false.",
                        "default": False,
                    },
                    "kv_ls_name": {
                        "type": "string",
                        "description": "Name for the Key Vault linked service. Default: 'LS_KeyVault'.",
                        "default": "LS_KeyVault",
                    },
                    "kv_url": {
                        "type": "string",
                        "description": "Azure Key Vault base URL. Default: 'https://TODO.vault.azure.net/'.",
                        "default": "https://TODO.vault.azure.net/",
                    },
                    "esi_tables_path": {
                        "type": "string",
                        "description": (
                            "Optional path to a JSON file mapping source_system → table list for ESI reuse detection."
                        ),
                    },
                    "schema_remap_path": {
                        "type": "string",
                        "description": (
                            "Optional path to a JSON file mapping old schema prefixes to new ones for database consolidation. "
                            "Format: {\"StagingDB.dbo\": \"ConsolidatedDB.staging\"}."
                        ),
                    },
                    "shared_artifacts_dir": {
                        "type": "string",
                        "description": (
                            "Optional shared directory for cross-package linked service/dataset deduplication. "
                            "When converting multiple packages, point all to the same shared dir."
                        ),
                    },
                    "pipeline_prefix": {
                        "type": "string",
                        "description": "Prefix for pipeline names. Default: 'PL_'.",
                        "default": "PL_",
                    },
                },
                "required": ["package_path", "output_dir"],
            },
        ),
        types.Tool(
            name="validate_adf_artifacts",
            description=(
                "Validate ADF JSON artifacts in a directory for structural correctness. "
                "Checks that required fields (name, properties, activities) are present. "
                "Returns a list of validation issues found, or a success message if all artifacts are valid."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifacts_dir": {
                        "type": "string",
                        "description": "Directory containing the generated ADF JSON artifacts.",
                    },
                },
                "required": ["artifacts_dir"],
            },
        ),
        types.Tool(
            name="deploy_to_adf",
            description=(
                "Deploy ADF JSON artifacts from a local directory to an Azure Data Factory instance. "
                "Deploys in correct dependency order: linked services → datasets → data flows → pipelines → triggers. "
                "Triggers are deployed in Stopped state and must be activated manually. "
                "Uses DefaultAzureCredential (az login, managed identity, or service principal env vars)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifacts_dir": {
                        "type": "string",
                        "description": "Directory containing generated ADF JSON artifacts.",
                    },
                    "subscription_id": {
                        "type": "string",
                        "description": "Azure subscription ID.",
                    },
                    "resource_group": {
                        "type": "string",
                        "description": "Azure resource group name containing the ADF instance.",
                    },
                    "factory_name": {
                        "type": "string",
                        "description": "Name of the Azure Data Factory to deploy to.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, validate and log but do not call Azure APIs. Default: false.",
                        "default": False,
                    },
                },
                "required": ["artifacts_dir", "subscription_id", "resource_group", "factory_name"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "scan_ssis_packages":
            return await _scan(arguments)
        elif name == "analyze_ssis_package":
            return await _analyze(arguments)
        elif name == "convert_ssis_package":
            return await _convert(arguments)
        elif name == "validate_adf_artifacts":
            return await _validate(arguments)
        elif name == "deploy_to_adf":
            return await _deploy(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        tb = traceback.format_exc()
        return [types.TextContent(type="text", text=f"Error: {exc}\n\n{tb}")]


async def _scan(args: dict[str, Any]) -> list[types.TextContent]:
    source_type = args["source_type"]
    path_or_conn = args["path_or_connection"]
    recursive = args.get("recursive", True)
    branch = args.get("git_branch", "main")

    packages_info: list[dict[str, Any]] = []

    if source_type == "local":
        from .parsers.readers.local_reader import LocalReader
        reader = LocalReader()
        paths = reader.scan(path_or_conn, recursive=recursive)
        for p in paths:
            packages_info.append({"name": p.stem, "path": str(p), "source": "local"})

    elif source_type == "git":
        from .parsers.readers.git_reader import GitReader
        reader = GitReader(branch=branch)
        pkgs = reader.read_all(path_or_conn, recursive=recursive)
        for pkg in pkgs:
            packages_info.append({
                "name": pkg.name,
                "source_file": pkg.source_file,
                "protection_level": pkg.protection_level.value,
                "task_count": len(pkg.tasks),
                "connection_count": len(pkg.connection_managers),
            })

    elif source_type == "sql":
        from .parsers.readers.sql_reader import SqlServerReader
        # Expect path_or_conn to be a pyodbc-style connection string
        # Parse it first to get server/database
        import re
        server_m = re.search(r"SERVER=([^;]+)", path_or_conn, re.I)
        db_m = re.search(r"DATABASE=([^;]+)", path_or_conn, re.I)
        server = server_m.group(1) if server_m else "localhost"
        database = db_m.group(1) if db_m else "msdb"
        reader = SqlServerReader(server=server, database=database, trusted_connection=True)
        names = reader.list_packages()
        for n in names:
            packages_info.append({"name": n, "source": "msdb", "server": server})

    result = {
        "found": len(packages_info),
        "packages": packages_info,
    }
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _analyze(args: dict[str, Any]) -> list[types.TextContent]:
    from .parsers.readers.local_reader import LocalReader
    from .analyzers.complexity_scorer import score_package
    from .analyzers.gap_analyzer import analyze_gaps
    from .analyzers.dependency_graph import build_package_dependency_order
    from .analyzers.cdm_pattern_detector import detect_cdm_patterns
    from .analyzers.esi_reuse_analyzer import analyze_esi_reuse, load_esi_config

    path = Path(args["package_path"])
    reader = LocalReader()
    package = reader.read(path)

    complexity = score_package(package)
    gaps = analyze_gaps(package)

    # CDM pattern detection
    cdm_gaps = detect_cdm_patterns(package)
    gaps.extend(cdm_gaps)

    # ESI reuse detection
    esi_gaps: list = []
    esi_tables_path = args.get("esi_tables_path")
    if esi_tables_path:
        esi_config = load_esi_config(esi_tables_path)
        esi_gaps = analyze_esi_reuse(package, esi_config)
        gaps.extend(esi_gaps)

    dep_order = build_package_dependency_order(package)

    # Get task names in order
    task_by_id = {t.id: t for t in package.tasks}
    ordered_names = [task_by_id[tid].name for tid in dep_order if tid in task_by_id]

    report = {
        "package_name": package.name,
        "source_file": package.source_file,
        "complexity": complexity.model_dump(),
        "gap_count": len(gaps),
        "gaps_by_severity": {
            "manual_required": [g.model_dump() for g in gaps if g.severity == "manual_required"],
            "warning": [g.model_dump() for g in gaps if g.severity == "warning"],
            "info": [g.model_dump() for g in gaps if g.severity == "info"],
        },
        "execution_order": ordered_names,
        "connection_managers": [
            {"name": cm.name, "type": cm.type.value, "server": cm.server, "database": cm.database}
            for cm in package.connection_managers
        ],
        "parameters": [p.name for p in package.parameters],
        "variables": [v.name for v in package.variables if v.namespace.lower() == "user"],
        "event_handlers": [eh.event_name for eh in package.event_handlers],
    }

    return [types.TextContent(type="text", text=json.dumps(report, indent=2))]


async def _convert(args: dict[str, Any]) -> list[types.TextContent]:
    from .parsers.readers.local_reader import LocalReader
    from .generators.pipeline_generator import generate_pipeline
    from .generators.linked_service_generator import generate_linked_services
    from .generators.dataset_generator import generate_datasets
    from .generators.dataflow_generator import generate_data_flows
    from .generators.trigger_generator import generate_triggers
    from .analyzers.cdm_pattern_detector import detect_cdm_patterns
    from .analyzers.esi_reuse_analyzer import analyze_esi_reuse, load_esi_config

    path = Path(args["package_path"])
    output_dir = Path(args["output_dir"])
    gen_trigger = args.get("generate_trigger", True)
    llm_translate = args.get("llm_translate", False)

    # New parameters
    on_prem_ir_name = args.get("on_prem_ir_name", "SelfHostedIR")
    auth_type = args.get("auth_type", "SystemAssignedManagedIdentity")
    use_key_vault = args.get("use_key_vault", False)
    kv_ls_name = args.get("kv_ls_name", "LS_KeyVault")
    kv_url = args.get("kv_url", "https://TODO.vault.azure.net/")
    pipeline_prefix = args.get("pipeline_prefix", "PL_")
    shared_artifacts_dir = Path(args["shared_artifacts_dir"]) if args.get("shared_artifacts_dir") else None

    # Load optional config files
    schema_remap: dict[str, str] | None = None
    schema_remap_path = args.get("schema_remap_path")
    if schema_remap_path:
        schema_remap = json.loads(Path(schema_remap_path).read_text(encoding="utf-8"))

    esi_config: dict = {}
    esi_tables_path = args.get("esi_tables_path")
    if esi_tables_path:
        esi_config = load_esi_config(esi_tables_path)

    reader = LocalReader()
    package = reader.read(path)

    stubs_dir = output_dir / "stubs"

    # Run analyzers for annotations
    cdm_gaps = detect_cdm_patterns(package)
    esi_gaps = analyze_esi_reuse(package, esi_config) if esi_config else []

    # Run generators with new parameters
    linked_services = generate_linked_services(
        package, output_dir,
        on_prem_ir_name=on_prem_ir_name,
        auth_type=auth_type,
        use_key_vault=use_key_vault,
        kv_ls_name=kv_ls_name,
        kv_url=kv_url,
        shared_artifacts_dir=shared_artifacts_dir,
    )
    datasets = generate_datasets(
        package, output_dir,
        schema_remap=schema_remap,
        shared_artifacts_dir=shared_artifacts_dir,
    )
    data_flows = generate_data_flows(package, output_dir)
    pipeline = generate_pipeline(
        package, output_dir,
        stubs_dir=stubs_dir,
        llm_translate=llm_translate,
        pipeline_prefix=pipeline_prefix,
        cdm_gaps=cdm_gaps,
        esi_gaps=esi_gaps,
        schema_remap=schema_remap,
    )
    triggers = generate_triggers(package, output_dir) if gen_trigger else []

    # Find stub files
    stub_files = list(stubs_dir.rglob("*.py")) if stubs_dir.exists() else []

    # Collect warnings from pipeline activities
    conversion_warnings = [
        act["description"]
        for act in pipeline.get("properties", {}).get("activities", [])
        if "MANUAL REVIEW" in act.get("description", "") or "UNSUPPORTED" in act.get("description", "")
    ]
    conversion_warnings.extend(_data_flow_warnings(data_flows, package))

    summary = {
        "package_name": package.name,
        "output_directory": str(output_dir),
        "artifacts_generated": {
            "pipelines": 1,
            "linked_services": len(linked_services),
            "datasets": len(datasets),
            "data_flows": len(data_flows),
            "triggers": len(triggers),
            "azure_function_stubs": len(stub_files),
        },
        "manual_review_required": len(conversion_warnings),
        "cdm_patterns_flagged": len(cdm_gaps),
        "esi_reuse_candidates": len(esi_gaps),
        "warnings": conversion_warnings[:20],  # cap output size
        "files": {
            "pipeline": str(output_dir / "pipeline" / f"{pipeline['name']}.json"),
            "linked_services": [ls["name"] for ls in linked_services],
            "datasets": [ds["name"] for ds in datasets],
            "data_flows": [df["name"] for df in data_flows],
            "stubs": [str(f) for f in stub_files],
        },
    }

    return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]


def _data_flow_warnings(data_flows: list[dict[str, Any]], package: Any) -> list[str]:
    """Warnings for generated Mapping Data Flows (they are skeletons, and ADF
    Mapping Data Flows cannot reach on-premises SQL Server through a
    Self-Hosted Integration Runtime)."""
    from .converters.sql_flavor import is_azure_sql

    cms = {f"LS_{cm.id}": cm for cm in package.connection_managers}
    warnings: list[str] = []
    for df in data_flows:
        name = df.get("name", "?")
        warnings.append(
            f"[MANUAL REVIEW] Mapping Data Flow '{name}' is a skeleton: complete the output "
            "schemas, expressions and lookup/join conditions in its script before deploying."
        )
        tp = df.get("properties", {}).get("typeProperties", {})
        on_prem = sorted({
            cms[ls].name
            for item in tp.get("sources", []) + tp.get("sinks", [])
            for ls in [item.get("linkedService", {}).get("referenceName", "")]
            if ls in cms and not is_azure_sql(cms[ls])
        })
        if on_prem:
            warnings.append(
                f"[MANUAL REVIEW] Mapping Data Flow '{name}' uses on-premises connection(s) "
                f"{on_prem}. Mapping Data Flows do not run on a Self-Hosted IR: stage the data "
                "to Azure first with a Copy activity, or use a Managed VNet IR with private "
                "endpoints."
            )
    return warnings


async def _validate(args: dict[str, Any]) -> list[types.TextContent]:
    from .deployer.adf_deployer import AdfDeployer

    artifacts_dir = Path(args["artifacts_dir"])
    # Validation doesn't require Azure credentials
    deployer = AdfDeployer.__new__(AdfDeployer)
    issues = deployer.validate_artifacts(artifacts_dir)

    if not issues:
        result = {"status": "valid", "message": "All artifacts passed structural validation."}
    else:
        result = {"status": "issues_found", "issue_count": len(issues), "issues": issues}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _deploy(args: dict[str, Any]) -> list[types.TextContent]:
    from .deployer.adf_deployer import AdfDeployer

    deployer = AdfDeployer(
        subscription_id=args["subscription_id"],
        resource_group=args["resource_group"],
        factory_name=args["factory_name"],
    )
    results = deployer.deploy_all(
        Path(args["artifacts_dir"]),
        dry_run=args.get("dry_run", False),
    )

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "results": [
            {"type": r.artifact_type, "name": r.name, "success": r.success,
             "error": r.error}
            for r in results
        ],
    }
    return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import asyncio

    async def _run() -> None:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
