"""
Pick the ADF SQL connector flavour (SQL Server on-premises vs Azure SQL Database)
for a given SSIS connection manager.

The linked-service generator emits ``SqlServer`` linked services for on-premises
servers and ``AzureSqlDatabase`` for ``*.database.windows.net``. Datasets and
Copy/Lookup source/sink types must match the linked service type or deployment
fails, so every generator uses this module to decide.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..parsers.models import SSISConnectionManager

_AZURE_SQL_SUFFIX = ".database.windows.net"


@dataclass(frozen=True)
class SqlFlavor:
    dataset_type: str
    source_type: str
    sink_type: str


ON_PREM = SqlFlavor(dataset_type="SqlServerTable", source_type="SqlSource", sink_type="SqlSink")
AZURE = SqlFlavor(
    dataset_type="AzureSqlTable", source_type="AzureSqlSource", sink_type="AzureSqlSink"
)


def is_azure_sql(cm: SSISConnectionManager | None) -> bool:
    if cm is None:
        return False
    haystack = f"{cm.server or ''} {cm.connection_string or ''}".lower()
    return _AZURE_SQL_SUFFIX in haystack


def flavor_for(
    connection_id: str | None,
    connections_by_id: dict[str, SSISConnectionManager] | None,
) -> SqlFlavor:
    """Return the SQL flavour for *connection_id*.

    Unknown connections (e.g. project-level connection managers) are treated as
    on-premises, consistent with the linked-service generator's heuristic.
    """
    cm = (connections_by_id or {}).get(connection_id or "")
    return AZURE if is_azure_sql(cm) else ON_PREM
