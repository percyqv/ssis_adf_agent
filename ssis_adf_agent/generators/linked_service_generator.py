"""
Linked Service generator — emits one ADF linkedService.json per SSIS Connection Manager.

Uses Microsoft Recommended version format for Azure SQL Database linked services
(discrete server/database/authenticationType properties, TLS 1.3 support) instead
of the legacy connectionString format.

Supports:
  - Self-Hosted IR detection for on-prem connections
  - Key Vault secret references
  - SystemAssignedManagedIdentity as default auth
  - Cross-package deduplication via shared_artifacts_dir
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..parsers.models import ConnectionManagerType, SSISConnectionManager, SSISPackage


_DEFAULT_IR = "AutoResolveIntegrationRuntime"
_AZURE_SQL_SUFFIX = ".database.windows.net"


def _is_on_prem(cm: SSISConnectionManager) -> bool:
    """Heuristic: server name without Azure SQL suffix → on-premises."""
    server = cm.server or ""
    if _AZURE_SQL_SUFFIX in server.lower():
        return False
    # Check connection string for Azure SQL suffix
    cs = cm.connection_string or ""
    if _AZURE_SQL_SUFFIX in cs.lower():
        return False
    return True


def _base_ls(
    cm: SSISConnectionManager,
    ir_name: str = _DEFAULT_IR,
) -> dict[str, Any]:
    return {
        "name": f"LS_{cm.id}",
        "properties": {
            "description": f"Auto-generated from SSIS Connection Manager: {cm.name}",
            "annotations": ["ssis-adf-agent"],
            "connectVia": {
                "referenceName": ir_name,
                "type": "IntegrationRuntimeReference",
            },
            "type": "UNKNOWN",
            "typeProperties": {},
        },
    }


def _kv_secret_ref(
    kv_ls_name: str,
    secret_name: str,
) -> dict[str, Any]:
    """Build an Azure Key Vault secret reference."""
    return {
        "type": "AzureKeyVaultSecret",
        "store": {
            "referenceName": kv_ls_name,
            "type": "LinkedServiceReference",
        },
        "secretName": secret_name,
    }


def _oledb_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    on_prem = _is_on_prem(cm)

    if on_prem:
        # On-prem SQL Server: use SqlServer connector type with SHIR
        ls["properties"]["type"] = "SqlServer"
        server = cm.server or "TODO_SERVER"
        database = cm.database or "TODO_DATABASE"

        if use_key_vault:
            ls["properties"]["typeProperties"] = {
                "server": server,
                "database": database,
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "SQL",
                "userName": "TODO_USERNAME",
                "password": _kv_secret_ref(kv_ls_name, f"{cm.name}-password"),
                "pooling": False,
            }
        else:
            ls["properties"]["typeProperties"] = {
                "server": server,
                "database": database,
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "Windows",
                "userName": "TODO_DOMAIN\\\\TODO_USER",
                "password": {
                    "type": "SecureString",
                    "value": "TODO — store in Azure Key Vault",
                },
                "pooling": False,
            }
    else:
        # Azure SQL Database: Recommended version format
        ls["properties"]["type"] = "AzureSqlDatabase"
        server = cm.server or "TODO_SERVER.database.windows.net"
        database = cm.database or "TODO_DATABASE"

        if auth_type == "SystemAssignedManagedIdentity":
            ls["properties"]["typeProperties"] = {
                "server": server,
                "database": database,
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "SystemAssignedManagedIdentity",
            }
        elif use_key_vault:
            ls["properties"]["typeProperties"] = {
                "server": server,
                "database": database,
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "SQL",
                "userName": "TODO_USERNAME",
                "password": _kv_secret_ref(kv_ls_name, f"{cm.name}-password"),
            }
        else:
            ls["properties"]["typeProperties"] = {
                "server": server,
                "database": database,
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "SQL",
                "userName": "TODO_USERNAME",
                "password": {
                    "type": "SecureString",
                    "value": "TODO — store in Azure Key Vault",
                },
            }

    return ls


def _flat_file_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    ls["properties"]["type"] = "AzureBlobStorage"
    if use_key_vault:
        ls["properties"]["typeProperties"] = {
            "connectionString": _kv_secret_ref(kv_ls_name, f"{cm.name}-connectionstring"),
            "note": (
                f"Original flat file path: {cm.file_path or 'unknown'}. "
                "Replace with Azure Blob Storage or ADLS Gen2 connection string."
            ),
        }
    else:
        ls["properties"]["typeProperties"] = {
            "connectionString": "DefaultEndpointsProtocol=https;AccountName=TODO;AccountKey=TODO",
            "note": (
                f"Original flat file path: {cm.file_path or 'unknown'}. "
                "Replace with Azure Blob Storage or ADLS Gen2 connection string."
            ),
        }
    return ls


def _ftp_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    ls["properties"]["type"] = "FtpServer"
    password: Any
    if use_key_vault:
        password = _kv_secret_ref(kv_ls_name, f"{cm.name}-password")
    else:
        password = {"type": "SecureString", "value": "TODO — store in Azure Key Vault"}
    ls["properties"]["typeProperties"] = {
        "host": cm.server or "TODO_FTP_HOST",
        "port": 21,
        "enableSsl": True,
        "authenticationType": "Basic",
        "userName": cm.username or "TODO",
        "password": password,
    }
    return ls


def _http_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    ls["properties"]["type"] = "HttpServer"
    ls["properties"]["typeProperties"] = {
        "url": cm.connection_string or "https://TODO",
        "authenticationType": "Anonymous",
    }
    return ls


def _smtp_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    ls["properties"]["type"] = "AzureFunction"
    ls["properties"]["description"] = (
        f"[MANUAL REVIEW] SMTP connection '{cm.name}' has no ADF linked service. "
        "Replace with Azure Communication Services or Logic App."
    )
    ls["properties"]["typeProperties"] = {
        "functionAppUrl": "https://TODO.azurewebsites.net",
        "functionKey": {"type": "SecureString", "value": "TODO"},
    }
    return ls


_BUILDERS: dict[ConnectionManagerType, Any] = {
    ConnectionManagerType.OLEDB: _oledb_ls,
    ConnectionManagerType.ADO_NET: _oledb_ls,
    ConnectionManagerType.FLAT_FILE: _flat_file_ls,
    ConnectionManagerType.EXCEL: _flat_file_ls,
    ConnectionManagerType.FTP: _ftp_ls,
    ConnectionManagerType.HTTP: _http_ls,
    ConnectionManagerType.SMTP: _smtp_ls,
    ConnectionManagerType.ODBC: _oledb_ls,
}


def _generate_kv_linked_service(
    kv_ls_name: str,
    kv_url: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Generate an Azure Key Vault linked service."""
    ls: dict[str, Any] = {
        "name": kv_ls_name,
        "properties": {
            "description": "Azure Key Vault for storing connection secrets.",
            "annotations": ["ssis-adf-agent"],
            "type": "AzureKeyVault",
            "typeProperties": {
                "baseUrl": kv_url,
            },
        },
    }
    ls_dir = output_dir / "linkedService"
    ls_dir.mkdir(parents=True, exist_ok=True)
    (ls_dir / f"{kv_ls_name}.json").write_text(
        json.dumps(ls, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    return ls


def generate_linked_services(
    package: SSISPackage,
    output_dir: Path,
    *,
    on_prem_ir_name: str = "SelfHostedIR",
    cloud_ir_name: str = _DEFAULT_IR,
    auth_type: str = "SystemAssignedManagedIdentity",
    use_key_vault: bool = False,
    kv_ls_name: str = "LS_KeyVault",
    kv_url: str = "https://TODO.vault.azure.net/",
    shared_artifacts_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Generate one ADF linked service JSON per unique Connection Manager in *package*.

    When *shared_artifacts_dir* is set, checks for existing linked service JSON files
    there before creating new ones (cross-package deduplication by server+database).

    Files are written to *output_dir*/linkedService/.
    Returns the list of linked service dicts.
    """
    ls_dir = output_dir / "linkedService"
    ls_dir.mkdir(parents=True, exist_ok=True)

    # Build index of existing shared linked services for dedup
    existing_ls: dict[str, str] = {}  # (server|database) → ls_name
    if shared_artifacts_dir:
        shared_ls_dir = shared_artifacts_dir / "linkedService"
        if shared_ls_dir.exists():
            for f in shared_ls_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    tp = data.get("properties", {}).get("typeProperties", {})
                    key = f"{tp.get('server', '')}|{tp.get('database', '')}".lower()
                    if key != "|":
                        existing_ls[key] = data["name"]
                except Exception:
                    continue

    from ..naming import linked_service_names

    ls_names = linked_service_names(package.connection_managers)
    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    generated_kv = False

    for cm in package.connection_managers:
        if cm.id in seen_ids:
            continue
        seen_ids.add(cm.id)

        # Check for cross-package dedup
        dedup_key = f"{(cm.server or '').lower()}|{(cm.database or '').lower()}"
        if dedup_key != "|" and dedup_key in existing_ls:
            # Reuse existing linked service — don't generate a new file
            continue

        on_prem = _is_on_prem(cm)
        ir_name = on_prem_ir_name if on_prem else cloud_ir_name

        builder = _BUILDERS.get(cm.type, _generic_ls)
        ls = builder(cm, ir_name, auth_type, use_key_vault, kv_ls_name)
        ls["name"] = ls_names[cm.id]

        # Generate Key Vault linked service once if needed
        if use_key_vault and not generated_kv:
            kv_ls = _generate_kv_linked_service(kv_ls_name, kv_url, output_dir)
            results.append(kv_ls)
            generated_kv = True

        ls_name = ls["name"]
        file_path = ls_dir / f"{ls_name}.json"
        file_path.write_text(
            json.dumps(ls, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        results.append(ls)

        # Track for future dedup
        if dedup_key != "|":
            existing_ls[dedup_key] = ls_name

    return results


def _generic_ls(
    cm: SSISConnectionManager,
    ir_name: str,
    auth_type: str,
    use_key_vault: bool,
    kv_ls_name: str,
) -> dict[str, Any]:
    ls = _base_ls(cm, ir_name)
    ls["properties"]["type"] = "AzureSqlDatabase"
    ls["properties"]["description"] = (
        f"[MANUAL REVIEW] Unknown connection type '{cm.type.value}' for '{cm.name}'. "
        "Update type and typeProperties."
    )
    server = cm.server or "TODO_SERVER"
    database = cm.database or "TODO_DATABASE"
    ls["properties"]["typeProperties"] = {
        "server": server,
        "database": database,
        "encrypt": "mandatory",
        "trustServerCertificate": False,
        "authenticationType": auth_type,
    }
    return ls
