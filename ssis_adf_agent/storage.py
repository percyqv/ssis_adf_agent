"""
Azure Blob Storage support.

Any tool argument that takes a path also accepts a Blob Storage location:

* ``az://<container>/<path>`` — the storage account comes from configuration
  (``AZURE_STORAGE_ACCOUNT_URL``, ``AZURE_STORAGE_ACCOUNT`` or
  ``AZURE_STORAGE_CONNECTION_STRING``).
* ``https://<account>.blob.core.windows.net/<container>/<path>`` — a full URL.

Authentication (first match wins):

1. ``AZURE_STORAGE_CONNECTION_STRING`` (handy for local tests / Azurite).
2. ``DefaultAzureCredential`` — managed identity in Azure Container Apps,
   ``az login`` on a developer machine. The identity needs the
   *Storage Blob Data Reader* role to read packages and
   *Storage Blob Data Contributor* to write artifacts.

Tools work on local files internally: blob inputs are downloaded to a temporary
directory and outputs are written to a temporary directory and then uploaded.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_HTTPS_BLOB_RE = re.compile(
    r"^https://(?P<host>[^/]+)/(?P<container>[^/]+)(?:/(?P<path>.*))?$", re.IGNORECASE
)


class BlobConfigError(RuntimeError):
    """Raised when a blob location is used but storage is not configured."""


@dataclass(frozen=True)
class BlobLocation:
    account_url: str | None  # None → resolved from configuration
    container: str
    path: str  # blob name or prefix, no leading slash ("" = container root)
    scheme: str  # "az" or "https" — used to format child locations the same way

    def child(self, name: str) -> BlobLocation:
        path = f"{self.path.rstrip('/')}/{name}" if self.path else name
        return BlobLocation(self.account_url, self.container, path, self.scheme)

    def format(self, path: str | None = None) -> str:
        p = self.path if path is None else path
        if self.scheme == "https" and self.account_url:
            return f"{self.account_url.rstrip('/')}/{self.container}/{p}".rstrip("/")
        return f"az://{self.container}/{p}".rstrip("/")


def is_blob_uri(value: str | os.PathLike[str] | None) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if s.lower().startswith("az://"):
        return True
    m = _HTTPS_BLOB_RE.match(s)
    return bool(m and ".blob." in m.group("host").lower())


def parse_blob_uri(value: str) -> BlobLocation:
    s = value.strip()
    if s.lower().startswith("az://"):
        rest = s[5:]
        container, _, path = rest.partition("/")
        if not container:
            raise ValueError(f"Blob location has no container: {value!r}")
        return BlobLocation(None, container, path.strip("/"), "az")
    m = _HTTPS_BLOB_RE.match(s)
    if not m or ".blob." not in m.group("host").lower():
        raise ValueError(f"Not a Blob Storage location: {value!r}")
    return BlobLocation(
        account_url=f"https://{m.group('host')}",
        container=m.group("container"),
        path=(m.group("path") or "").strip("/"),
        scheme="https",
    )


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def _service_client(account_url: str | None):  # -> BlobServiceClient
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise BlobConfigError(
            "azure-storage-blob is not installed. Run: pip install azure-storage-blob"
        ) from exc

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str and account_url is None:
        return BlobServiceClient.from_connection_string(conn_str)

    url = account_url or os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not url and os.environ.get("AZURE_STORAGE_ACCOUNT"):
        url = f"https://{os.environ['AZURE_STORAGE_ACCOUNT']}.blob.core.windows.net"
    if not url:
        raise BlobConfigError(
            "Blob Storage is not configured. Set AZURE_STORAGE_ACCOUNT (account name), "
            "AZURE_STORAGE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING, or use a full "
            "https://<account>.blob.core.windows.net/... URL."
        )
    if conn_str:
        return BlobServiceClient.from_connection_string(conn_str)

    from azure.identity import DefaultAzureCredential

    return BlobServiceClient(account_url=url, credential=DefaultAzureCredential())


def _container_client(loc: BlobLocation):
    return _service_client(loc.account_url).get_container_client(loc.container)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _iter_files(container, prefix: str):
    """Yield blobs under *prefix*, skipping directory entries.

    Storage accounts with a hierarchical namespace (ADLS Gen2) list folders as
    zero-length blobs with metadata ``hdi_isfolder=true``; they are not files.
    """
    for blob in container.list_blobs(name_starts_with=prefix or None, include=["metadata"]):
        metadata = getattr(blob, "metadata", None) or {}
        if str(metadata.get("hdi_isfolder", "")).lower() == "true":
            continue
        if blob.name.endswith("/"):
            continue
        yield blob


def list_blobs(uri: str, suffix: str = "", recursive: bool = True) -> list[str]:
    """List blob locations under *uri* (a prefix), optionally filtered by suffix."""
    loc = parse_blob_uri(uri)
    container = _container_client(loc)
    prefix = f"{loc.path}/" if loc.path else ""
    results: list[str] = []
    for blob in _iter_files(container, prefix):
        name: str = blob.name
        if suffix and not name.lower().endswith(suffix.lower()):
            continue
        if not recursive and "/" in name[len(prefix):]:
            continue
        results.append(loc.format(name))
    return sorted(results)


def download_file(uri: str, dest_dir: Path) -> Path:
    """Download one blob into *dest_dir*, keeping its file name."""
    loc = parse_blob_uri(uri)
    if not loc.path:
        raise ValueError(f"Blob location points at a container, not a file: {uri!r}")
    target = dest_dir / PurePosixPath(loc.path).name
    target.parent.mkdir(parents=True, exist_ok=True)
    from azure.core.exceptions import ResourceNotFoundError

    try:
        data = _container_client(loc).download_blob(loc.path).readall()
    except ResourceNotFoundError as exc:
        raise FileNotFoundError(f"Blob not found: {uri}") from exc
    target.write_bytes(data)
    return target


def download_prefix(uri: str, dest_dir: Path) -> Path:
    """Download every blob under the *uri* prefix into *dest_dir* (keeping sub-folders)."""
    loc = parse_blob_uri(uri)
    container = _container_client(loc)
    prefix = f"{loc.path}/" if loc.path else ""
    count = 0
    for blob in _iter_files(container, prefix):
        rel = blob.name[len(prefix):]
        if not rel:
            continue
        target = dest_dir.joinpath(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(container.download_blob(blob.name).readall())
        count += 1
    if count == 0:
        raise FileNotFoundError(f"No blobs found under: {uri}")
    return dest_dir


def upload_dir(local_dir: Path, uri: str) -> list[str]:
    """Upload every file in *local_dir* under the *uri* prefix (overwriting)."""
    from azure.storage.blob import ContentSettings

    loc = parse_blob_uri(uri)
    container = _container_client(loc)
    uploaded: list[str] = []
    for f in sorted(p for p in local_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(local_dir).as_posix()
        name = f"{loc.path}/{rel}" if loc.path else rel
        content_type = "application/json" if f.suffix == ".json" else None
        container.upload_blob(
            name,
            f.read_bytes(),
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type) if content_type else None,
        )
        uploaded.append(loc.format(name))
    return uploaded


# ---------------------------------------------------------------------------
# Context managers used by the tools
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def local_input_file(path: str) -> Iterator[Path]:
    """Yield a local path for *path*, downloading it first if it is a blob."""
    if not is_blob_uri(path):
        yield Path(path)
        return
    tmp = Path(tempfile.mkdtemp(prefix="ssis_adf_in_"))
    try:
        yield download_file(path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def local_input_dir(path: str) -> Iterator[Path]:
    """Yield a local directory for *path*, downloading the blob prefix first if needed."""
    if not is_blob_uri(path):
        yield Path(path)
        return
    tmp = Path(tempfile.mkdtemp(prefix="ssis_adf_in_"))
    try:
        yield download_prefix(path, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class OutputDir:
    """Local working directory for outputs; uploads to Blob Storage on ``finish()``."""

    def __init__(self, path: str) -> None:
        self.target = path
        self.is_blob = is_blob_uri(path)
        self.local = Path(tempfile.mkdtemp(prefix="ssis_adf_out_")) if self.is_blob else Path(path)
        self.uploaded: list[str] = []

    def display(self, local_path: Path | str) -> str:
        """Map a local path inside the working dir to where it ends up."""
        if not self.is_blob:
            return str(local_path)
        rel = Path(local_path).relative_to(self.local).as_posix()
        return parse_blob_uri(self.target).child(rel).format()

    def finish(self) -> None:
        if self.is_blob:
            self.uploaded = upload_dir(self.local, self.target)

    def cleanup(self) -> None:
        if self.is_blob:
            shutil.rmtree(self.local, ignore_errors=True)
