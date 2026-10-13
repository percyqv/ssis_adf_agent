"""
Readable, ADF-safe names for generated artifacts.

Linked services are named after the SSIS connection manager
(``DW`` → ``LS_DW``, ``CRM Origen`` → ``LS_CRM_Origen``) instead of its GUID.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .parsers.models import SSISConnectionManager

# ADF object names may not contain . + ? / < > * % & : \ — keep letters, digits, _ and -
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_name(text: str) -> str:
    """Make *text* usable as (part of) an ADF artifact name."""
    cleaned = _UNSAFE.sub("_", text.strip()).strip("_")
    return cleaned or "unnamed"


def linked_service_names(
    connection_managers: Iterable[SSISConnectionManager],
) -> dict[str, str]:
    """Map connection manager id → linked service name, unique within the package."""
    names: dict[str, str] = {}
    used: set[str] = set()
    for cm in connection_managers:
        if cm.id in names:
            continue
        base = f"LS_{safe_name(cm.name or cm.id)}"
        name, n = base, 2
        while name.lower() in used:  # ADF names are case-insensitive
            name, n = f"{base}_{n}", n + 1
        used.add(name.lower())
        names[cm.id] = name
    return names


def linked_service_ref(connection_id: str | None, ls_names: dict[str, str] | None) -> str:
    """Linked service name for *connection_id* (a connection manager id).

    Unknown ids — e.g. project-level connection managers, which the parser
    reports by name — fall back to ``LS_<id>``.
    """
    if connection_id and ls_names and connection_id in ls_names:
        return ls_names[connection_id]
    return f"LS_{safe_name(connection_id or 'unknown')}"
