"""
SSIS .dtsx XML parser.

Parses a .dtsx file (XML) into SSISPackage and related Pydantic models.
Handles namespace variations across SQL Server 2008 through 2022.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from lxml import etree

from .models import (
    ConnectionManagerType,
    CrossDbReference,
    CrossDbReferenceType,
    DataFlowColumn,
    DataFlowComponent,
    DataFlowPath,
    DataFlowTask,
    DataType,
    EventHandler,
    ExecutePackageTask,
    ExecuteProcessTask,
    ExecuteSQLTask,
    FileSystemTask,
    ForEachLoopContainer,
    ForEachEnumeratorType,
    ForLoopContainer,
    FTPTask,
    GapItem,
    IngestionPattern,
    PrecedenceConstraint,
    PrecedenceEvalOp,
    PrecedenceValue,
    ProtectionLevel,
    ScriptTask,
    SendMailTask,
    SequenceContainer,
    SSISConnectionManager,
    SSISPackage,
    SSISParameter,
    SSISTask,
    SSISVariable,
    TaskType,
)

# ---------------------------------------------------------------------------
# XML namespace registry — covers SQL Server 2008 → 2022
# ---------------------------------------------------------------------------
DTS_NS = "www.microsoft.com/SqlServer/Dts"
DTS_NS_FULL = f"{{{DTS_NS}}}"
PIPELINE_NS = "www.microsoft.com/sqlserver/dts/pipeline"

NAMESPACES: dict[str, str] = {
    "DTS": DTS_NS,
    "pipeline": PIPELINE_NS,
    "SQLTask": "www.microsoft.com/sqlserver/dts/tasks/sqltask",
    "ExecuteSQLTask": "www.microsoft.com/sqlserver/dts/tasks/ExecuteSQLTask",
    "ScriptProject": "www.microsoft.com/SqlServer/Dts/Tasks/ScriptTask",
    "MSFTContainers": "www.microsoft.com/sqlserver/dts/containers",
}

# CLSID → component type mapping for Data Flow components
CLSID_MAP: dict[str, str] = {
    "{2C0A8BE5-1EDC-4353-A0EF-B778599C65A0}": "OleDbSource",
    "{E2568105-9550-4F71-A638-B7F7A6C346EC}": "OleDbDestination",
    "{90C7770B-DE7C-435E-880E-E718C92C0573}": "FlatFileSource",
    "{A38DDFF8-76AE-40A2-9F87-86B25E8EDC1B}": "FlatFileDestination",
    "{BCEFE59B-6819-47F7-A125-63753B33ABB7}": "ExcelSource",
    "{F0F2EBC8-8A66-4D12-8AF4-39C0FC9E8B1E}": "ExcelDestination",
    "{DFC8EDA7-6CBE-4F4C-BCFD-63E43FD23EB7}": "OdbcSource",
    "{4FAE0FD3-3B12-4EA3-9B22-5DE2E1B87B7E}": "OdbcDestination",
    "{27648839-AB06-4806-920A-5E73D2D1DD9D}": "Lookup",
    "{FB9AA693-7EBD-4B96-9E16-CB5F8D1F3B5F}": "DerivedColumn",
    "{3D632F73-C90B-400D-B13E-8B38E98BEDB5}": "ConditionalSplit",
    "{5B651BDB-7FED-4B70-84B0-D02E3E5B09DC}": "Multicast",
    "{A3DA38DA-B8DF-46BE-9AA9-9A3B1E9B5DDD}": "UnionAll",
    "{D04A9F10-FBBA-4E25-B2A9-B90A02091066}": "Aggregate",
    "{1904CD90-55D4-4893-B834-6D8B90ED36F3}": "Sort",
    "{5B2B19FB-BD61-413B-A8E7-F97E8A56F975}": "MergeJoin",
    "{1D09B7C7-D8E4-4BE3-89C3-BCD7AE25D71E}": "Merge",
    "{8BC4C51C-D1D5-4C01-98E3-14A5E2BE8ED5}": "DataConversion",
    "{48E51C9A-2CA6-4B3F-89B8-A609E7009019}": "CharacterMap",
    "{7B7E83F3-1DD8-4B22-AAA2-7F8DA32234DA}": "RowCount",
    "{2932025B-AB99-40F6-B5B8-783A73F80E24}": "ScriptComponent",
    "{9B18AD15-5D9E-4B92-B3D9-87063EB9B7D1}": "TermExtraction",
    "{9898B672-DFCE-43ff-8B4A-4A0C6978DF41}": "TermLookup",
    "{CD7D1B85-1E98-4C5C-B8D0-33B281D7D63D}": "FuzzyLookup",
    "{FD7D7A0B-F1F1-48B7-9F30-6CA52C68EAED}": "FuzzyGrouping",
    "{7D910C2B-4EB3-48F8-B892-5BCDF74E1DF9}": "ExportColumn",
    "{7BB0DB4E-B7E0-4E1E-A15B-43C58DBBCA33}": "ImportColumn",
    "{D6B8A63B-B3B0-41F7-9EBB-E06E41B0DD80}": "Cache",
    "{93FFC8EB-6CC1-4989-9F4D-9B0930FB7B77}": "RecordsetDestination",
    "{62B1106B-04A4-4A69-BCCA-A3E72F862832}": "ADONetSource",
    "{2C77430C-E219-4034-A577-CFC2CE2D3020}": "ADONetDestination",
    "{ACA08B87-CCDE-4BA4-BFBF-09AC42891D56}": "SqlServerDestination",
}

# Same map keyed without braces, upper-case (the form used for lookups).
_CLSID_LOOKUP: dict[str, str] = {k.strip("{}").upper(): v for k, v in CLSID_MAP.items()}

# SSIS 2012+ (PackageFormatVersion >= 6) stores componentClassID as a ProgID
# such as "Microsoft.OLEDBSource" instead of a GUID.
PROGID_MAP: dict[str, str] = {
    "Microsoft.OLEDBSource": "OleDbSource",
    "Microsoft.OLEDBDestination": "OleDbDestination",
    "Microsoft.FlatFileSource": "FlatFileSource",
    "Microsoft.FlatFileDestination": "FlatFileDestination",
    "Microsoft.ExcelSource": "ExcelSource",
    "Microsoft.ExcelDestination": "ExcelDestination",
    "Microsoft.SSISODBCSrc": "OdbcSource",
    "Microsoft.SSISODBCDst": "OdbcDestination",
    "Microsoft.SQLServerDestination": "SqlServerDestination",
    "Microsoft.RecordsetDestination": "RecordsetDestination",
    "Microsoft.Lookup": "Lookup",
    "Microsoft.DerivedColumn": "DerivedColumn",
    "Microsoft.ConditionalSplit": "ConditionalSplit",
    "Microsoft.Multicast": "Multicast",
    "Microsoft.UnionAll": "UnionAll",
    "Microsoft.Aggregate": "Aggregate",
    "Microsoft.Sort": "Sort",
    "Microsoft.MergeJoin": "MergeJoin",
    "Microsoft.Merge": "Merge",
    "Microsoft.DataConvert": "DataConversion",
    "Microsoft.CharacterMap": "CharacterMap",
    "Microsoft.RowCount": "RowCount",
    "Microsoft.CopyMap": "CopyColumn",
    "Microsoft.OLEDBCommand": "OleDbCommand",
    "Microsoft.SCD": "SlowlyChangingDimension",
    "Microsoft.Pivot": "Pivot",
    "Microsoft.UnPivot": "Unpivot",
    "Microsoft.PctSampling": "PercentageSampling",
    "Microsoft.RowSampling": "RowSampling",
    "Microsoft.FuzzyLookup": "FuzzyLookup",
    "Microsoft.FuzzyGrouping": "FuzzyGrouping",
    "Microsoft.TermExtraction": "TermExtraction",
    "Microsoft.TermLookup": "TermLookup",
    "Microsoft.Cache": "Cache",
}

# Managed components share componentClassID "Microsoft.ManagedComponentHost";
# the real type is in the UserComponentTypeName property.
_MANAGED_COMPONENT_MAP: dict[str, str] = {
    "ScriptComponentHost": "ScriptComponent",
    "DataReaderSourceAdapter": "ADONetSource",
    "ADONETDestination": "ADONetDestination",
    "ADONETSource": "ADONetSource",
}


def resolve_component_type(class_id: str, properties: dict[str, Any] | None = None) -> str:
    """Map a Data Flow componentClassID (GUID or ProgID) to a component type name."""
    cid = (class_id or "").strip()
    if not cid:
        return "Unknown"
    if cid in PROGID_MAP:
        return PROGID_MAP[cid]
    if cid.lower() == "microsoft.managedcomponenthost":
        user_type = str((properties or {}).get("UserComponentTypeName") or "")
        for marker, comp_type in _MANAGED_COMPONENT_MAP.items():
            if marker.lower() in user_type.lower():
                return comp_type
        return "ScriptComponent" if not user_type else user_type
    return _CLSID_LOOKUP.get(cid.strip("{}").upper(), cid)


def _local(elem: etree._Element) -> str:
    tag = elem.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _children_local(elem: etree._Element, name: str) -> list[etree._Element]:
    """Direct children with the given local name, in any (or no) namespace."""
    return [c for c in elem if _local(c) == name]


def _first_child_local(elem: etree._Element, name: str) -> etree._Element | None:
    for c in elem:
        if _local(c) == name:
            return c
    return None


def _iter_local(elem: etree._Element, name: str):
    """Descendants (including elem) with the given local name, in any namespace."""
    for e in elem.iter():
        if _local(e) == name:
            yield e


def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def _dts(local: str) -> str:
    return f"{DTS_NS_FULL}{local}"


def _prop(element: etree._Element, name: str, ns: str = DTS_NS) -> str | None:
    """Get a DTS:Property value by name."""
    tag = f"{{{ns}}}Property"
    for prop in element.findall(f".//{tag}"):
        n = prop.get(f"{{{ns}}}Name") or prop.get("Name") or prop.get("name")
        if n == name:
            return (prop.text or "").strip() or None
    return None


def _attr(element: etree._Element, name: str, ns: str = DTS_NS) -> str | None:
    """Get an attribute value trying both namespaced and bare forms."""
    val = element.get(f"{{{ns}}}{name}")
    if val is None:
        val = element.get(name)
    return val


def _clean_id(raw: str | None) -> str:
    """Strip curly braces from GUIDs; return a placeholder if None."""
    if not raw:
        return str(uuid.uuid4())
    return raw.strip("{}").upper()


# ---------------------------------------------------------------------------
# Cross-database / linked server detection regexes
# ---------------------------------------------------------------------------

# Four-part name: [server].[database].[schema].[table]
_FOUR_PART_RE = re.compile(
    r"\[?(\w+)\]?\.\[?(\w+)\]?\.\[?(\w+)\]?\.\[?(\w+)\]?",
)

# Three-part name: [database].[schema].[table]
# Exclude common false positives: sys.*, INFORMATION_SCHEMA.*, dbo.sp_*
_THREE_PART_RE = re.compile(
    r"(?<!\w)\[?(\w+)\]?\.\[?(\w+)\]?\.\[?(\w+)\]?(?!\.\[?\w)",
)

# OPENQUERY / OPENROWSET patterns
_OPENQUERY_RE = re.compile(r"\bOPENQUERY\s*\(\s*\[?(\w+)\]?", re.IGNORECASE)
_OPENROWSET_RE = re.compile(r"\bOPENROWSET\s*\(", re.IGNORECASE)

# Skip databases for three-part-name detection (system databases, common aliases)
_SKIP_DBS = frozenset({"sys", "INFORMATION_SCHEMA", "tempdb", "master", "model", "msdb"})


def _detect_cross_db_references(sql: str | None) -> list[CrossDbReference]:
    """Scan SQL text for cross-database and linked server references."""
    if not sql:
        return []

    refs: list[CrossDbReference] = []
    seen: set[str] = set()

    # OPENQUERY
    for m in _OPENQUERY_RE.finditer(sql):
        key = f"openquery:{m.group(1).upper()}"
        if key not in seen:
            seen.add(key)
            refs.append(CrossDbReference(
                ref_type=CrossDbReferenceType.OPENQUERY,
                server_name=m.group(1),
                raw_match=m.group(0),
            ))

    # OPENROWSET
    for m in _OPENROWSET_RE.finditer(sql):
        key = f"openrowset:{m.start()}"
        if key not in seen:
            seen.add(key)
            refs.append(CrossDbReference(
                ref_type=CrossDbReferenceType.OPENROWSET,
                raw_match=m.group(0),
            ))

    # Four-part names (linked server)
    for m in _FOUR_PART_RE.finditer(sql):
        server, db, schema, table = m.group(1), m.group(2), m.group(3), m.group(4)
        key = f"4part:{server}.{db}.{schema}.{table}".upper()
        if key not in seen:
            seen.add(key)
            refs.append(CrossDbReference(
                ref_type=CrossDbReferenceType.FOUR_PART,
                server_name=server,
                database_name=db,
                schema_name=schema,
                table_name=table,
                raw_match=m.group(0),
            ))

    # Three-part names (cross-database) — skip if already covered by four-part
    for m in _THREE_PART_RE.finditer(sql):
        db, schema, table = m.group(1), m.group(2), m.group(3)
        if db in _SKIP_DBS:
            continue
        # Skip if this match is part of a four-part match
        four_part_key = None
        for four_m in _FOUR_PART_RE.finditer(sql):
            if four_m.start() <= m.start() <= four_m.end():
                four_part_key = True
                break
        if four_part_key:
            continue
        key = f"3part:{db}.{schema}.{table}".upper()
        if key not in seen:
            seen.add(key)
            refs.append(CrossDbReference(
                ref_type=CrossDbReferenceType.THREE_PART,
                database_name=db,
                schema_name=schema,
                table_name=table,
                raw_match=m.group(0),
            ))

    return refs


# ---------------------------------------------------------------------------
# Delta / incremental load pattern detection
# ---------------------------------------------------------------------------

# Common delta variable patterns in SSIS
_DELTA_VAR_RE = re.compile(
    r"@\[?(?:User::)?(LastExtractDate|LastRunDate|MaxModifiedDate|IncrementalDate|"
    r"LastLoadDate|WatermarkDate|DeltaDate|StartDate|ModifiedSince)\]?",
    re.IGNORECASE,
)

# WHERE clause with > or >= on a date-like comparison
_DELTA_WHERE_RE = re.compile(
    r"WHERE\b.+?(\w+)\s*(?:>=?|>)\s*(?:@\[?(?:User::)?\w+\]?|\?)",
    re.IGNORECASE | re.DOTALL,
)

# MERGE statement detection
_MERGE_RE = re.compile(r"\bMERGE\b\s+(?:INTO\s+)?", re.IGNORECASE)


def _detect_ingestion_pattern(sql: str | None) -> tuple[IngestionPattern, str | None]:
    """Detect ingestion pattern (full/delta/merge) and delta column from SQL text."""
    if not sql:
        return IngestionPattern.UNKNOWN, None

    # Check for MERGE first (most specific)
    if _MERGE_RE.search(sql):
        return IngestionPattern.MERGE, None

    # Check for delta variable references
    if _DELTA_VAR_RE.search(sql):
        # Try to extract the actual column being compared
        m = _DELTA_WHERE_RE.search(sql)
        delta_col = m.group(1) if m else None
        return IngestionPattern.DELTA, delta_col

    # Check for delta-like WHERE patterns even without named variables
    m = _DELTA_WHERE_RE.search(sql)
    if m:
        col = m.group(1).lower()
        if any(kw in col for kw in ("modif", "date", "time", "extract", "load", "update", "change")):
            return IngestionPattern.DELTA, m.group(1)

    return IngestionPattern.UNKNOWN, None


# ---------------------------------------------------------------------------
# Key column extraction from OLE DB Destination / MERGE commands
# ---------------------------------------------------------------------------

_MERGE_ON_RE = re.compile(
    r"\bON\b\s+(.+?)\s*\bWHEN\b",
    re.IGNORECASE | re.DOTALL,
)

_COLUMN_REF_RE = re.compile(r"\b(?:target|t|src|s|source)\.\[?(\w+)\]?", re.IGNORECASE)


def _extract_key_columns_from_sql(sql: str | None) -> list[str]:
    """Extract key columns from a MERGE statement's ON clause."""
    if not sql:
        return []
    m = _MERGE_ON_RE.search(sql)
    if not m:
        return []
    on_clause = m.group(1)
    cols = _COLUMN_REF_RE.findall(on_clause)
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for c in cols:
        if c.upper() not in seen:
            seen.add(c.upper())
            result.append(c)
    return result


# Short-form CreationName values used by older SQL Server versions
_SHORT_FORM_TASK_MAP: dict[str, TaskType] = {
    "microsoft.executesqltask": TaskType.EXECUTE_SQL,
    "microsoft.pipeline": TaskType.DATA_FLOW,
    "microsoft.scripttask": TaskType.SCRIPT,
    "microsoft.filesystemtask": TaskType.FILE_SYSTEM,
    "microsoft.ftptask": TaskType.FTP,
    "microsoft.sendmailtask": TaskType.SEND_MAIL,
    "microsoft.executepackagetask": TaskType.EXECUTE_PACKAGE,
    "microsoft.executeprocesstask": TaskType.EXECUTE_PROCESS,
    "stock:sequence": TaskType.SEQUENCE,
    "stock:foreach": TaskType.FOREACH_LOOP,
    "stock:forloop": TaskType.FOR_LOOP,
}


def _extract_source_from_blob(b64_text: str, language: str) -> str | None:
    """
    Decode a base64-encoded ZIP blob (Pattern A/B) and extract C#/VB source files.

    Returns concatenated source code string, or None if extraction fails.
    """
    import base64
    import io
    import zipfile
    import logging

    ext = ".vb" if language == "VisualBasic" else ".cs"
    _EXCLUDE = {"assemblyinfo", ".designer.", "assemblyattributes"}

    try:
        raw = base64.b64decode(b64_text.strip())
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            entries = sorted(
                n for n in zf.namelist()
                if n.lower().endswith(ext)
                and not any(ex in n.lower() for ex in _EXCLUDE)
            )
            if not entries:
                return None
            parts: list[str] = []
            for entry in entries:
                code = zf.read(entry).decode("utf-8", errors="replace")
                parts.append(f"// --- {entry} ---\n{code}")
            return "\n\n".join(parts)
    except Exception:
        logging.getLogger(__name__).debug(
            "Failed to extract script source from blob", exc_info=True
        )
        return None


def _extract_source_from_script_project(
    config_elem: "etree._Element", language: str
) -> str | None:
    """
    Pattern A (SSIS 2012+): look for a ScriptProject child inside the config element,
    then find a BinaryData child and decode it.
    """
    for child in config_elem:
        local = etree.QName(child.tag).localname
        if "ScriptProject" in local:
            for sub in child:
                sub_local = etree.QName(sub.tag).localname
                if sub_local == "BinaryData" and sub.text:
                    return _extract_source_from_blob(sub.text, language)
    return None


def _resolve_task_type(class_id: str | None, dts_type: str | None) -> TaskType:
    """Map a clsid or DTS type string to a TaskType enum."""
    mapping: dict[str, TaskType] = {
        "Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask": TaskType.EXECUTE_SQL,
        "Microsoft.SqlServer.Dts.Pipeline.PipelineComponent": TaskType.DATA_FLOW,
        "Microsoft.SqlServer.Dts.Tasks.ScriptTask.ScriptTask": TaskType.SCRIPT,
        "Microsoft.SqlServer.Dts.Tasks.FileSystemTask.FileSystemTask": TaskType.FILE_SYSTEM,
        "Microsoft.SqlServer.Dts.Tasks.FtpTask.FtpTask": TaskType.FTP,
        "Microsoft.SqlServer.Dts.Tasks.SendMailTask.SendMailTask": TaskType.SEND_MAIL,
        "Microsoft.SqlServer.Dts.Tasks.ExecutePackageTask.ExecutePackageTask": TaskType.EXECUTE_PACKAGE,
        "Microsoft.SqlServer.Dts.Tasks.ExecuteProcess.ExecuteProcessTask": TaskType.EXECUTE_PROCESS,
        "Sequence": TaskType.SEQUENCE,
        "ForEachLoop": TaskType.FOREACH_LOOP,
        "ForLoop": TaskType.FOR_LOOP,
    }
    # Try class ID first
    if class_id:
        for k, v in mapping.items():
            if k.lower() in class_id.lower() or class_id.strip("{}").upper() in k.upper():
                return v
    # Fall back to DTS type string
    if dts_type:
        for k, v in mapping.items():
            if k.lower() in dts_type.lower() or dts_type.lower() in k.lower():
                return v
        # Check short-form creation names (e.g. "Microsoft.ExecuteSQLTask")
        lower = dts_type.lower()
        if lower in _SHORT_FORM_TASK_MAP:
            return _SHORT_FORM_TASK_MAP[lower]
        # Handle versioned SSIS Pipeline identifiers: "SSIS.Pipeline.2", "SSIS.Pipeline.4", etc.
        if lower.startswith("ssis.pipeline"):
            return TaskType.DATA_FLOW
    return TaskType.UNKNOWN


class SSISParser:
    """
    Parses a .dtsx XML file into an SSISPackage model.

    Usage::

        parser = SSISParser()
        package = parser.parse(Path("/path/to/package.dtsx"))
    """

    def parse(self, path: Path, raw_xml: str | None = None) -> SSISPackage:
        """Parse a .dtsx file. Accepts either a path or raw XML string."""
        if raw_xml is None:
            raw_xml = path.read_text(encoding="utf-8", errors="replace")

        root = etree.fromstring(raw_xml.encode("utf-8"))
        return self._parse_package(root, str(path), raw_xml)

    def parse_xml(self, raw_xml: str, source_identifier: str = "<memory>") -> SSISPackage:
        """Parse a .dtsx XML string directly (e.g. from SQL Server msdb)."""
        root = etree.fromstring(raw_xml.encode("utf-8"))
        return self._parse_package(root, source_identifier, raw_xml)

    # ------------------------------------------------------------------
    # Package
    # ------------------------------------------------------------------

    def _parse_package(self, root: etree._Element, source: str, raw_xml: str) -> SSISPackage:
        pkg_id = _clean_id(_attr(root, "DTSID"))
        pkg_name = _attr(root, "ObjectName") or Path(source).stem

        protection_str = _attr(root, "ProtectionLevel") or "0"
        protection_map = {
            "0": ProtectionLevel.DONT_SAVE_SENSITIVE,
            "1": ProtectionLevel.ENCRYPT_SENSITIVE_WITH_USER_KEY,
            "2": ProtectionLevel.ENCRYPT_SENSITIVE_WITH_PASSWORD,
            "3": ProtectionLevel.ENCRYPT_ALL_WITH_PASSWORD,
            "4": ProtectionLevel.ENCRYPT_ALL_WITH_USER_KEY,
            "5": ProtectionLevel.SERVER_STORAGE,
        }
        protection = protection_map.get(protection_str, ProtectionLevel.DONT_SAVE_SENSITIVE)

        # Per-package lookup tables used to resolve SSIS 2012+ refId references
        self._exec_ref_map: dict[str, str] = {}
        self._cm_ref_ids: dict[str, str] = {}
        connection_managers = self._parse_connection_managers(root)
        self._cm_lookup: dict[str, str] = dict(self._cm_ref_ids)
        for cm in connection_managers:
            self._cm_lookup[cm.id] = cm.id
            self._cm_lookup[cm.name] = cm.id
            self._cm_lookup[f"Package.ConnectionManagers[{cm.name}]"] = cm.id
        variables = self._parse_variables(root, direct_children_only=True)
        parameters = self._parse_parameters(root)
        tasks, constraints = self._parse_executables(root)
        event_handlers = self._parse_event_handlers(root)

        return SSISPackage(
            id=pkg_id,
            name=pkg_name,
            source_file=source,
            description=_attr(root, "Description") or "",
            protection_level=protection,
            creator_computer_name=_attr(root, "CreatorComputerName") or "",
            creator_name=_attr(root, "CreatorName") or "",
            creation_date=_attr(root, "CreationDate") or "",
            package_format_version=int(_attr(root, "PackageFormatVersion") or "8"),
            connection_managers=connection_managers,
            variables=variables,
            parameters=parameters,
            tasks=tasks,
            constraints=constraints,
            event_handlers=event_handlers,
            raw_xml=raw_xml,
        )

    # ------------------------------------------------------------------
    # Connection Managers
    # ------------------------------------------------------------------

    def _parse_connection_managers(self, root: etree._Element) -> list[SSISConnectionManager]:
        cms: list[SSISConnectionManager] = []
        cm_container = root.find(_dts("ConnectionManagers"))
        if cm_container is None:
            return cms

        for cm_elem in cm_container.findall(_dts("ConnectionManager")):
            cm_id = _clean_id(_attr(cm_elem, "DTSID"))
            cm_name = _attr(cm_elem, "ObjectName") or cm_id
            cm_ref = _attr(cm_elem, "refId")
            if cm_ref and hasattr(self, "_cm_ref_ids"):
                self._cm_ref_ids[cm_ref] = cm_id
            cm_type_str = _attr(cm_elem, "CreationName") or ""

            # Resolve type
            type_map = {
                "OLEDB": ConnectionManagerType.OLEDB,
                "ADO.NET": ConnectionManagerType.ADO_NET,
                "FLATFILE": ConnectionManagerType.FLAT_FILE,
                "EXCEL": ConnectionManagerType.EXCEL,
                "FTP": ConnectionManagerType.FTP,
                "HTTP": ConnectionManagerType.HTTP,
                "SMTP": ConnectionManagerType.SMTP,
                "FILE": ConnectionManagerType.FILE,
                "MULTIFILE": ConnectionManagerType.MULTIFILE,
                "ODBC": ConnectionManagerType.ODBC,
            }
            cm_type = ConnectionManagerType.UNKNOWN
            for key, val in type_map.items():
                if key.lower() in cm_type_str.lower():
                    cm_type = val
                    break

            # Extract properties from inner ObjectData element
            props: dict[str, Any] = {}
            connection_string: str | None = None
            server: str | None = None
            database: str | None = None
            file_path: str | None = None

            object_data = cm_elem.find(_dts("ObjectData"))
            if object_data is not None:
                for child in object_data:
                    local = etree.QName(child.tag).localname
                    # OLE DB / ADO.NET: ConnectionString attribute
                    cs = child.get("ConnectionString") or child.get(f"{{{DTS_NS}}}ConnectionString")
                    if cs:
                        connection_string = cs
                        # Try parsing server/database from connection string
                        m = re.search(r"(?:Data Source|Server)=([^;]+)", cs, re.I)
                        if m:
                            server = m.group(1).strip()
                        m = re.search(r"(?:Initial Catalog|Database)=([^;]+)", cs, re.I)
                        if m:
                            database = m.group(1).strip()

                    # Flat File: flat file path
                    fp = child.get(f"{{{DTS_NS}}}ConnectionString") or child.get("ConnectionString")
                    if fp and cm_type == ConnectionManagerType.FLAT_FILE:
                        file_path = fp

                    for attr_name, attr_val in child.attrib.items():
                        local_attr = etree.QName(attr_name).localname
                        props[local_attr] = attr_val

            cms.append(SSISConnectionManager(
                id=cm_id,
                name=cm_name,
                type=cm_type,
                connection_string=connection_string,
                server=server,
                database=database,
                file_path=file_path,
                properties=props,
            ))
        return cms

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------

    def _parse_variables(
        self, parent: etree._Element, direct_children_only: bool = False
    ) -> list[SSISVariable]:
        vars_: list[SSISVariable] = []
        vars_container = parent.find(_dts("Variables"))
        if vars_container is None:
            return vars_

        tag = _dts("Variable")
        elements = vars_container.findall(tag) if direct_children_only else vars_container.iter(tag)
        for var_elem in elements:
            name = _attr(var_elem, "ObjectName") or ""
            ns = _attr(var_elem, "Namespace") or "User"
            ronly = (_attr(var_elem, "ReadOnly") or "0") == "-1"
            val_elem = var_elem.find(_dts("VariableValue"))
            data_type = _attr(var_elem, "DataType") or "String"
            value: Any = val_elem.text if val_elem is not None else None
            expression = _attr(var_elem, "Expression")
            vars_.append(SSISVariable(
                name=name,
                namespace=ns,
                data_type=data_type,
                value=value,
                expression=expression,
                read_only=ronly,
            ))
        return vars_

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _parse_parameters(self, root: etree._Element) -> list[SSISParameter]:
        params: list[SSISParameter] = []
        # Support both modern (PackageParameters/PackageParameter) and
        # legacy (Parameters/Parameter) formats
        params_container = root.find(_dts("PackageParameters")) or root.find(_dts("Parameters"))
        if params_container is None:
            return params

        p_elems = params_container.findall(_dts("PackageParameter")) \
                  or params_container.findall(_dts("Parameter"))
        for p_elem in p_elems:
            # Modern format uses ObjectName; legacy uses Name
            name = _attr(p_elem, "ObjectName") or _attr(p_elem, "Name") or ""
            data_type = _attr(p_elem, "DataType") or "String"
            required = (_attr(p_elem, "Required") or "0") != "0"
            sensitive = (_attr(p_elem, "Sensitive") or "0") != "0"
            val_elem = p_elem.find(_dts("Property"))
            # Legacy format stores value as element text content
            value = (val_elem.text if val_elem is not None else None) or (p_elem.text or "").strip() or None
            params.append(SSISParameter(
                name=name,
                data_type=data_type,
                value=value,
                required=required,
                sensitive=sensitive,
            ))
        return params

    # ------------------------------------------------------------------
    # Executables (tasks + containers)
    # ------------------------------------------------------------------

    def _parse_executables(
        self, parent: etree._Element
    ) -> tuple[list[SSISTask], list[PrecedenceConstraint]]:
        tasks: list[SSISTask] = []
        constraints: list[PrecedenceConstraint] = []

        executables = parent.find(_dts("Executables"))
        if executables is None:
            return tasks, constraints

        for elem in executables.findall(_dts("Executable")):
            task = self._parse_executable(elem)
            if task:
                tasks.append(task)

        constraints = self._parse_precedence_constraints(parent, tasks)
        return tasks, constraints

    def _parse_executable(self, elem: etree._Element) -> SSISTask | None:
        exec_type = _attr(elem, "ExecutableType") or ""
        creation_name = _attr(elem, "CreationName") or exec_type
        task_type = _resolve_task_type(None, creation_name)

        task_id = _clean_id(_attr(elem, "DTSID"))
        task_name = _attr(elem, "ObjectName") or task_id
        description = _attr(elem, "Description") or ""
        ref_id = _attr(elem, "refId")
        if ref_id:
            self._exec_ref_map[ref_id] = task_id
        self._exec_ref_map[task_id] = task_id
        disabled = (_attr(elem, "Disabled") or "0") not in ("0", "")

        base_kwargs = dict(
            id=task_id, name=task_name, description=description,
            task_type=task_type, disabled=disabled,
        )

        object_data = elem.find(_dts("ObjectData"))

        if task_type == TaskType.EXECUTE_SQL:
            return self._parse_execute_sql(elem, object_data, base_kwargs)
        elif task_type == TaskType.DATA_FLOW:
            return self._parse_data_flow(elem, object_data, base_kwargs)
        elif task_type == TaskType.SCRIPT:
            return self._parse_script_task(elem, object_data, base_kwargs)
        elif task_type == TaskType.FILE_SYSTEM:
            return self._parse_file_system(elem, object_data, base_kwargs)
        elif task_type == TaskType.FTP:
            return self._parse_ftp(elem, object_data, base_kwargs)
        elif task_type == TaskType.SEND_MAIL:
            return self._parse_send_mail(elem, object_data, base_kwargs)
        elif task_type == TaskType.EXECUTE_PACKAGE:
            return self._parse_execute_package(elem, object_data, base_kwargs)
        elif task_type == TaskType.EXECUTE_PROCESS:
            return self._parse_execute_process(elem, object_data, base_kwargs)
        elif task_type == TaskType.SEQUENCE:
            return self._parse_sequence(elem, base_kwargs)
        elif task_type == TaskType.FOREACH_LOOP:
            return self._parse_foreach(elem, base_kwargs)
        elif task_type == TaskType.FOR_LOOP:
            return self._parse_for_loop(elem, base_kwargs)
        else:
            props = {etree.QName(k).localname: v for k, v in elem.attrib.items()}
            return SSISTask(**base_kwargs, properties=props)

    # ------------------------------------------------------------------
    # Individual task parsers
    # ------------------------------------------------------------------

    def _parse_execute_sql(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> ExecuteSQLTask:
        conn_id: str | None = None
        sql: str | None = None
        result_type = "None"
        timeout = 0
        result_bindings: list[dict] = []
        param_bindings: list[dict] = []

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                ns = etree.QName(child.tag).namespace or ""
                # Accept both canonicalpipeline namespace variants:
                #   - "SqlTaskData" (www.microsoft.com/sqlserver/dts/tasks/sqltask)
                #   - "ExecuteSQLTask" (www.microsoft.com/sqlserver/dts/tasks/ExecuteSQLTask)
                if local in ("SqlTaskData", "ExecuteSQLTask"):
                    # Attributes may live under either namespace or bare
                    def _get_sqla(attr: str) -> str | None:
                        return (
                            child.get(f"{{{ns}}}{attr}")
                            or child.get(f"{{{NAMESPACES['SQLTask']}}}{attr}")
                            or child.get(f"{{{NAMESPACES['ExecuteSQLTask']}}}{attr}")
                            or child.get(attr)
                        )
                    conn_id = _clean_id(_get_sqla("Connection"))
                    sql = _get_sqla("SqlStatementSource")
                    result_type = _get_sqla("ResultType") or "None"
                    timeout = int(_get_sqla("TimeOut") or "0")
                    for rb in list(child.findall(f"{{{NAMESPACES['SQLTask']}}}ResultBinding"))\
                              + list(child.findall(f"{{{NAMESPACES['ExecuteSQLTask']}}}ResultBinding")):
                        rb_ns = etree.QName(rb.tag).namespace or ""
                        result_bindings.append({
                            "variable": rb.get(f"{{{rb_ns}}}DtsVariableName") or rb.get("DtsVariableName") or "",
                            "result_name": rb.get(f"{{{rb_ns}}}ResultName") or rb.get("ResultName") or "",
                        })
                    for pb in list(child.findall(f"{{{NAMESPACES['SQLTask']}}}ParameterBinding"))\
                              + list(child.findall(f"{{{NAMESPACES['ExecuteSQLTask']}}}ParameterBinding")):
                        pb_ns = etree.QName(pb.tag).namespace or ""
                        param_bindings.append({
                            "variable": pb.get(f"{{{pb_ns}}}DtsVariableName") or pb.get("DtsVariableName") or "",
                            "direction": pb.get(f"{{{pb_ns}}}ParameterDirection") or pb.get("ParameterDirection") or "Input",
                            "data_type": pb.get(f"{{{pb_ns}}}DataType") or pb.get("DataType") or "0",
                            "parameter_name": pb.get(f"{{{pb_ns}}}ParameterName") or pb.get("ParameterName") or "",
                        })

        # Detect cross-DB references and ingestion pattern
        cross_db_refs = _detect_cross_db_references(sql)
        ingestion_pat, delta_col = _detect_ingestion_pattern(sql)

        return ExecuteSQLTask(
            **base,
            connection_id=conn_id,
            sql_statement=sql,
            result_set_type=result_type,
            timeout=timeout,
            result_bindings=result_bindings,
            parameter_bindings=param_bindings,
            cross_db_references=cross_db_refs,
            ingestion_pattern=ingestion_pat,
            delta_column=delta_col,
        )

    def _parse_script_task(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> ScriptTask:
        language = "CSharp"
        entry_point = "Main"
        ro_vars: list[str] = []
        rw_vars: list[str] = []
        source_code: str | None = None

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                if "ScriptTaskProjectConfiguration" in local or "ScriptTask" in local:
                    lang = child.get("ScriptLanguage") or child.get(
                        f"{{{DTS_NS}}}ScriptLanguage"
                    )
                    if lang:
                        language = "VisualBasic" if "VB" in lang.upper() else "CSharp"
                    ep = child.get("EntryPoint") or child.get(f"{{{DTS_NS}}}EntryPoint")
                    if ep:
                        entry_point = ep
                    ro = child.get("ReadOnlyVariables") or ""
                    rw = child.get("ReadWriteVariables") or ""
                    ro_vars = [v.strip() for v in ro.split(",") if v.strip()]
                    rw_vars = [v.strip() for v in rw.split(",") if v.strip()]

                    # Pattern B (SSIS 2008): ProjectBytes attribute on the config element
                    project_bytes = child.get("ProjectBytes")
                    if project_bytes:
                        source_code = _extract_source_from_blob(project_bytes, language)

                    # Pattern A (SSIS 2012+): BinaryData child inside ScriptProject child
                    if source_code is None:
                        source_code = _extract_source_from_script_project(child, language)

        return ScriptTask(
            **base,
            script_language=language,
            entry_point=entry_point,
            read_only_variables=ro_vars,
            read_write_variables=rw_vars,
            source_code=source_code,
        )

    def _parse_file_system(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> FileSystemTask:
        operation = "CopyFile"
        src = None
        dst = None
        overwrite = False

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                if "FileSystemData" in local:
                    operation = child.get("Operation") or "CopyFile"
                    src = child.get("Source")
                    dst = child.get("Destination")
                    ow = child.get("Overwrite") or "False"
                    overwrite = ow.lower() == "true"

        return FileSystemTask(**base, operation=operation, source_path=src,
                              destination_path=dst, overwrite=overwrite)

    def _parse_ftp(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> FTPTask:
        conn_id = None
        operation = "Send"
        local_path = None
        remote_path = None
        overwrite = False

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                if "FtpData" in local:
                    conn_id = _clean_id(child.get("Connection"))
                    operation = child.get("Operation") or "Send"
                    local_path = child.get("LocalPath")
                    remote_path = child.get("RemotePath")
                    ow = child.get("Overwrite") or "False"
                    overwrite = ow.lower() == "true"

        return FTPTask(**base, connection_id=conn_id, operation=operation,
                       local_path=local_path, remote_path=remote_path, overwrite=overwrite)

    def _parse_send_mail(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> SendMailTask:
        conn_id = None
        to_addr = None
        cc_addr = None
        from_addr = None
        subject = None
        message = None

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                if "MailTaskData" in local or "SendMailTask" in local:
                    conn_id = _clean_id(child.get("SMTPConnection") or child.get("Connection") or "")
                    to_addr = child.get("ToLine") or child.get("To")
                    cc_addr = child.get("CCLine") or child.get("CC")
                    from_addr = child.get("FromLine") or child.get("From")
                    subject = child.get("Subject")
                    message = child.get("MessageSourceType") or child.get("MessageSource")

        return SendMailTask(**base, smtp_connection_id=conn_id, to=to_addr, cc=cc_addr,
                            from_address=from_addr, subject=subject, message_source=message)

    def _parse_execute_package(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> ExecutePackageTask:
        pkg_path = None
        conn_id = None
        use_project = False
        project_pkg_name = None

        if object_data is not None:
            for child in object_data:
                local = etree.QName(child.tag).localname
                if "ExecutePackageTask" in local:
                    pkg_path = child.get("PackageName") or child.get("PackagePath")
                    conn_id = _clean_id(child.get("Connection") or "")
                    use_project = (child.get("UseProjectReference") or "False").lower() == "true"
                    project_pkg_name = child.get("PackageName")

        return ExecutePackageTask(**base, package_path=pkg_path,
                                  package_connection_id=conn_id,
                                  use_project_reference=use_project,
                                  project_package_name=project_pkg_name)

    def _parse_execute_process(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> ExecuteProcessTask:
        executable = None
        args = None
        wd = None

        if object_data is not None:
            for child in object_data:
                executable = child.get("Executable") or child.get("ExecutablePath")
                args = child.get("Arguments")
                wd = child.get("WorkingDirectory")

        return ExecuteProcessTask(**base, executable=executable, arguments=args,
                                  working_directory=wd)

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------

    def _parse_sequence(self, elem: etree._Element, base: dict) -> SequenceContainer:
        tasks, constraints = self._parse_executables(elem)
        return SequenceContainer(**base, tasks=tasks, constraints=constraints)

    def _parse_foreach(self, elem: etree._Element, base: dict) -> ForEachLoopContainer:
        tasks, constraints = self._parse_executables(elem)

        enumerator_type = ForEachEnumeratorType.FILE
        config: dict[str, Any] = {}
        var_mappings: list[dict] = []

        fe_elem = elem.find(_dts("ForEachEnumerator"))
        if fe_elem is not None:
            creation = _attr(fe_elem, "CreationName") or ""
            for e_type in ForEachEnumeratorType:
                if e_type.value.lower() in creation.lower():
                    enumerator_type = e_type
                    break

            od = fe_elem.find(_dts("ObjectData"))
            if od is not None:
                for child in od:
                    for attr_name, attr_val in child.attrib.items():
                        config[etree.QName(attr_name).localname] = attr_val

        # Variable mappings
        vm_container = elem.find(_dts("ForEachVariableMappings"))
        if vm_container is not None:
            for vm in vm_container.findall(_dts("ForEachVariableMapping")):
                var_mappings.append({
                    "variable": _attr(vm, "VariableName") or "",
                    "index": _attr(vm, "ValueIndex") or "0",
                })

        return ForEachLoopContainer(
            **base,
            enumerator_type=enumerator_type,
            enumerator_config=config,
            variable_mappings=var_mappings,
            tasks=tasks,
            constraints=constraints,
        )

    def _parse_for_loop(self, elem: etree._Element, base: dict) -> ForLoopContainer:
        tasks, constraints = self._parse_executables(elem)
        init_expr = _attr(elem, "InitExpression")
        eval_expr = _attr(elem, "EvalExpression")
        assign_expr = _attr(elem, "AssignExpression")
        return ForLoopContainer(**base, init_expression=init_expr, eval_expression=eval_expr,
                                assign_expression=assign_expr, tasks=tasks, constraints=constraints)

    # ------------------------------------------------------------------
    # Data Flow
    # ------------------------------------------------------------------

    def _parse_data_flow(
        self, elem: etree._Element, object_data: etree._Element | None, base: dict
    ) -> DataFlowTask:
        components: list[DataFlowComponent] = []
        paths: list[DataFlowPath] = []

        if object_data is not None:
            for pipeline in object_data.iter():
                if _local(pipeline) not in ("pipeline", "Pipeline"):
                    continue
                components_elem = _first_child_local(pipeline, "components")
                if components_elem is not None:
                    for comp in _children_local(components_elem, "component"):
                        dfc = self._parse_df_component(comp, "")
                        if dfc:
                            components.append(dfc)

                paths_elem = _first_child_local(pipeline, "paths")
                if paths_elem is not None:
                    for path_elem in _children_local(paths_elem, "path"):
                        p_id = path_elem.get("id") or path_elem.get("refId") or str(uuid.uuid4())
                        p_name = path_elem.get("name") or p_id
                        start = path_elem.get("startId") or ""
                        end = path_elem.get("endId") or ""
                        paths.append(DataFlowPath(id=p_id, name=p_name, start_id=start, end_id=end))

        # Detect ingestion pattern and cross-DB refs from component SQL properties
        all_cross_db: list[CrossDbReference] = []
        ingestion_pat = IngestionPattern.UNKNOWN
        for comp in components:
            comp_sql = comp.properties.get("SqlCommand") or comp.properties.get("CommandText") or ""
            if comp_sql:
                all_cross_db.extend(_detect_cross_db_references(comp_sql))
                pat, _ = _detect_ingestion_pattern(comp_sql)
                if pat != IngestionPattern.UNKNOWN:
                    ingestion_pat = pat
            # Check for MERGE in destination CommandText
            dest_cmd = comp.properties.get("CommandText") or ""
            if dest_cmd and _MERGE_RE.search(dest_cmd):
                ingestion_pat = IngestionPattern.MERGE

        return DataFlowTask(
            **base,
            components=components,
            paths=paths,
            cross_db_references=all_cross_db,
            ingestion_pattern=ingestion_pat,
        )

    def _parse_df_component(
        self, comp: etree._Element, ns: str
    ) -> DataFlowComponent | None:
        comp_id = comp.get("id") or comp.get("refId") or str(uuid.uuid4())
        comp_name = comp.get("name") or comp_id
        class_id = comp.get("classID") or comp.get("componentClassID") or ""

        # Component-level properties only (not those of inputs/outputs/columns)
        props: dict[str, Any] = {}
        props_elem = _first_child_local(comp, "properties")
        if props_elem is not None:
            for prop in _children_local(props_elem, "property"):
                pname = prop.get("name")
                if pname:
                    props[pname] = prop.text

        comp_type = resolve_component_type(class_id, props)
        if comp_type == "Unknown":
            comp_type = comp.get("componentName") or comp.get("contactInfo") or "Unknown"

        conn_id: str | None = None
        # Connection manager used by this component
        for cm_ref in _iter_local(comp, "connection"):
            raw = (
                cm_ref.get("connectionManagerRefId")
                or cm_ref.get("connectionManagerID")
                or cm_ref.get("componentId")
            )
            if raw:
                conn_id = self._resolve_connection_ref(raw)
                break

        # Columns
        input_cols: list[DataFlowColumn] = []
        output_cols: list[DataFlowColumn] = []

        for input_elem in _iter_local(comp, "input"):
            for col in _iter_local(input_elem, "inputColumn"):
                input_cols.append(self._parse_df_column(col))

        for output_elem in _iter_local(comp, "output"):
            for col in _iter_local(output_elem, "outputColumn"):
                output_cols.append(self._parse_df_column(col))

        # Extract key columns from destination components (e.g., MERGE ON clause)
        key_columns: list[str] = []
        if comp_type in ("OleDbDestination", "ADONetDestination", "SqlServerDestination"):
            command_text = props.get("CommandText") or props.get("OpenRowset") or ""
            key_columns = _extract_key_columns_from_sql(command_text)

        return DataFlowComponent(
            id=comp_id,
            name=comp_name,
            component_class_id=class_id,
            component_type=comp_type,
            input_columns=input_cols,
            output_columns=output_cols,
            properties=props,
            connection_id=conn_id,
            key_columns=key_columns,
        )

    def _parse_df_column(self, col: etree._Element) -> DataFlowColumn:
        dt_str = col.get("dataType") or "wstr"
        try:
            dt = DataType(dt_str)
        except ValueError:
            dt = DataType.WSTRING

        return DataFlowColumn(
            name=col.get("name") or col.get("externalMetadataColumnId") or "column",
            data_type=dt,
            length=int(col.get("length") or "0"),
            precision=int(col.get("precision") or "0"),
            scale=int(col.get("scale") or "0"),
            code_page=int(col.get("codePage") or "0"),
        )

    # ------------------------------------------------------------------
    # Precedence Constraints
    # ------------------------------------------------------------------

    def _resolve_executable_ref(self, raw: str | None) -> str:
        """Map a constraint endpoint (SSIS 2012+ refId path or a DTSID) to a task id."""
        if raw:
            ref_map = getattr(self, "_exec_ref_map", {})
            if raw in ref_map:
                return ref_map[raw]
            cleaned = raw.strip("{}").upper()
            if cleaned in ref_map:
                return ref_map[cleaned]
        return _clean_id(raw)

    def _resolve_connection_ref(self, raw: str | None) -> str | None:
        """Map a component's connectionManagerID / RefId to a connection manager id."""
        if not raw:
            return None
        lookup = getattr(self, "_cm_lookup", {})
        candidates = [raw, raw.split(":")[0], raw.split(":")[0].strip("{}").upper()]
        for c in candidates:
            if c in lookup:
                return lookup[c]
        # Project-level connection (Project.ConnectionManagers[X]) or unknown:
        # keep the most readable identifier available.
        if "[" in raw and raw.endswith("]"):
            return raw[raw.index("[") + 1 : -1]
        return candidates[2]

    def _parse_precedence_constraints(
        self, parent: etree._Element, tasks: list[SSISTask]
    ) -> list[PrecedenceConstraint]:
        constraints: list[PrecedenceConstraint] = []
        pc_container = parent.find(_dts("PrecedenceConstraints"))
        if pc_container is None:
            return constraints

        for pc in pc_container.findall(_dts("PrecedenceConstraint")):
            pc_id = _clean_id(_attr(pc, "DTSID"))
            from_id = self._resolve_executable_ref(_attr(pc, "From"))
            to_id = self._resolve_executable_ref(_attr(pc, "To"))
            eval_op_str = _attr(pc, "EvalOp") or "1"
            eval_op_map = {
                "1": PrecedenceEvalOp.CONSTRAINT,
                "2": PrecedenceEvalOp.EXPRESSION,
                "3": PrecedenceEvalOp.EXPRESSION_AND_CONSTRAINT,
                "5": PrecedenceEvalOp.EXPRESSION_OR_CONSTRAINT,
            }
            eval_op = eval_op_map.get(eval_op_str, PrecedenceEvalOp.CONSTRAINT)
            value_str = _attr(pc, "Value") or "0"
            value_map = {
                "0": PrecedenceValue.SUCCESS,
                "1": PrecedenceValue.FAILURE,
                "2": PrecedenceValue.COMPLETION,
            }
            value = value_map.get(value_str, PrecedenceValue.SUCCESS)
            expression = _attr(pc, "Expression")
            logical_and = (_attr(pc, "LogicalAnd") or "-1") != "0"

            constraints.append(PrecedenceConstraint(
                id=pc_id,
                from_task_id=from_id,
                to_task_id=to_id,
                eval_op=eval_op,
                value=value,
                expression=expression,
                logical_and=logical_and,
            ))
        return constraints

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    def _parse_event_handlers(self, root: etree._Element) -> list[EventHandler]:
        handlers: list[EventHandler] = []
        eh_container = root.find(_dts("EventHandlers"))
        if eh_container is None:
            return handlers

        for eh_elem in eh_container.findall(_dts("EventHandler")):
            event_name = _attr(eh_elem, "EventName") or _attr(eh_elem, "CreationName") or "Unknown"
            tasks, constraints = self._parse_executables(eh_elem)
            variables = self._parse_variables(eh_elem, direct_children_only=True)
            handlers.append(EventHandler(
                event_name=event_name,
                parent_task_id=None,
                parent_task_name=None,
                tasks=tasks,
                constraints=constraints,
                variables=variables,
            ))
        return handlers
