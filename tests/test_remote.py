"""Tests for the remote deployment features: Blob Storage paths, HTTP transport and auth.

Blob tests run against Azurite (the Azure Storage emulator) and are skipped unless
``SSIS_ADF_TEST_BLOB_CONNECTION_STRING`` points at it, e.g.::

    npx azurite-blob --silent --location .azurite --blobPort 10000 &
    export SSIS_ADF_TEST_BLOB_CONNECTION_STRING="DefaultEndpointsProtocol=http;\\
AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsu\\
Fq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from ssis_adf_agent import mcp_server
from ssis_adf_agent.auth import AuthConfig, MultiTokenVerifier
from ssis_adf_agent.http_app import create_app
from ssis_adf_agent.storage import is_blob_uri, parse_blob_uri

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
API_KEY = "unit-test-key-0123456789abcdef"
TENANT = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://ssis-adf-agent"


# ---------------------------------------------------------------------------
# Blob locations
# ---------------------------------------------------------------------------

def test_parse_blob_uris() -> None:
    loc = parse_blob_uri("az://ssis-packages/Finanzas/CargaVentas.dtsx")
    assert (loc.container, loc.path, loc.account_url) == (
        "ssis-packages", "Finanzas/CargaVentas.dtsx", None
    )
    loc = parse_blob_uri("https://stmig.blob.core.windows.net/adf-artifacts/Fin/Carga/")
    assert loc.account_url == "https://stmig.blob.core.windows.net"
    assert (loc.container, loc.path) == ("adf-artifacts", "Fin/Carga")
    assert loc.child("pipeline/PL.json").format() == (
        "https://stmig.blob.core.windows.net/adf-artifacts/Fin/Carga/pipeline/PL.json"
    )
    assert is_blob_uri("az://c/x")
    assert not is_blob_uri("samples/CargaVentas.dtsx")
    assert not is_blob_uri(r"C:\\Users\\me\\pkg.dtsx")
    assert not is_blob_uri("https://example.com/pkg.dtsx")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class _FakeJwks:
    def __init__(self, public_key) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return type("K", (), {"key": self._key})()


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _entra_token(key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUDIENCE,
        "exp": now + 600,
        "iat": now,
        "tid": TENANT,
        "oid": "user-object-id",
        "azp": "client-app-id",
        "scp": "Convert.Run",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def _verifier(rsa_key, **cfg) -> MultiTokenVerifier:
    config = AuthConfig(
        api_keys=[API_KEY], entra_tenant_id=TENANT, entra_audiences=[AUDIENCE], **cfg
    )
    v = MultiTokenVerifier(config)
    assert v._entra is not None
    v._entra._jwks = _FakeJwks(rsa_key.public_key())  # no network in tests
    return v


def test_verifier_accepts_api_key_and_valid_entra_token(rsa_key) -> None:
    v = _verifier(rsa_key)
    assert asyncio.run(v.verify_token(API_KEY)).client_id == "api-key-1"
    tok = asyncio.run(v.verify_token(_entra_token(rsa_key)))
    assert tok is not None and tok.scopes == ["Convert.Run"] and tok.subject == "user-object-id"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://someone-else"},
        {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"},
        {"exp": int(time.time()) - 3600},
    ],
)
def test_verifier_rejects_bad_entra_tokens(rsa_key, overrides) -> None:
    assert asyncio.run(_verifier(rsa_key).verify_token(_entra_token(rsa_key, **overrides))) is None


def test_verifier_rejects_token_signed_with_other_key(rsa_key) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert asyncio.run(_verifier(rsa_key).verify_token(_entra_token(other))) is None
    assert asyncio.run(_verifier(rsa_key).verify_token("not-the-key-0123456789abcdef")) is None


def test_short_api_keys_are_refused() -> None:
    with pytest.raises(ValueError):
        AuthConfig(api_keys=["short"]).validate()


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}
_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _client(rsa_key, **cfg) -> TestClient:
    config = AuthConfig(
        api_keys=[API_KEY], entra_tenant_id=TENANT, entra_audiences=[AUDIENCE], **cfg
    )
    app = create_app(
        mcp_server.server, config, bind_host="0.0.0.0", verifier=_verifier(rsa_key, **cfg)
    )
    return TestClient(app, base_url="https://mcp.example.test")


def test_http_requires_auth(rsa_key) -> None:
    with _client(rsa_key) as c:
        assert c.get("/healthz").json()["status"] == "ok"
        assert c.post("/mcp", json=_INIT, headers=_HEADERS).status_code == 401
        bad = {**_HEADERS, "authorization": "Bearer nope-nope-nope-nope-nope-nope"}
        assert c.post("/mcp", json=_INIT, headers=bad).status_code == 401


def test_http_accepts_x_api_key_header(rsa_key) -> None:
    with _client(rsa_key) as c:
        r = c.post("/mcp", json=_INIT, headers={**_HEADERS, "x-api-key": API_KEY})
        assert r.status_code == 200
        wrong = {**_HEADERS, "x-api-key": "wrong-wrong-wrong-wrong-wrong"}
        r = c.post("/mcp", json=_INIT, headers=wrong)
        assert r.status_code == 401


@pytest.mark.parametrize("use_entra", [False, True])
def test_http_initialize_with_credentials(rsa_key, use_entra: bool) -> None:
    token = _entra_token(rsa_key) if use_entra else API_KEY
    with _client(rsa_key) as c:
        r = c.post("/mcp", json=_INIT, headers={**_HEADERS, "authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["result"]["serverInfo"]["name"] == "ssis-adf-agent"


def test_http_required_scope_enforced(rsa_key) -> None:
    with _client(rsa_key, required_scopes=["Convert.Run"]) as c:
        h = {**_HEADERS, "authorization": f"Bearer {_entra_token(rsa_key, scp='Other')}"}
        assert c.post("/mcp", json=_INIT, headers=h).status_code == 403
        h = {**_HEADERS, "authorization": f"Bearer {_entra_token(rsa_key)}"}
        assert c.post("/mcp", json=_INIT, headers=h).status_code == 200


def test_protected_resource_metadata(rsa_key) -> None:
    with _client(rsa_key, public_url="https://mcp.example.test") as c:
        meta = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == "https://mcp.example.test/mcp"
        assert meta["authorization_servers"] == [
            f"https://login.microsoftonline.com/{TENANT}/v2.0"
        ]
        r = c.post("/mcp", json=_INIT, headers=_HEADERS)
        assert "oauth-protected-resource" in r.headers["www-authenticate"]


# ---------------------------------------------------------------------------
# Remote policy (applies to every transport once configured)
# ---------------------------------------------------------------------------

@pytest.fixture
def remote_policy():
    mcp_server.configure_policy(allow_local_paths=False, allow_deploy=False)
    yield
    mcp_server.configure_policy(allow_local_paths=True, allow_deploy=True)


def _call(tool: str, args: dict) -> str:
    return asyncio.run(mcp_server.call_tool(tool, args))[0].text


def test_remote_policy_blocks_local_paths_and_deploy(remote_policy) -> None:
    assert "local filesystem paths are disabled" in _call(
        "analyze_ssis_package", {"package_path": str(SAMPLES / "CargaVentas.dtsx")}
    )
    assert "disabled" in _call("scan_ssis_packages", {"source_type": "local",
                                                       "path_or_connection": str(SAMPLES)})
    assert "deploy_to_adf is disabled" in _call("deploy_to_adf", {
        "artifacts_dir": "az://adf-artifacts/x", "subscription_id": "s",
        "resource_group": "g", "factory_name": "f",
    })


# ---------------------------------------------------------------------------
# Blob Storage end-to-end (Azurite)
# ---------------------------------------------------------------------------

BLOB_CONN = os.environ.get("SSIS_ADF_TEST_BLOB_CONNECTION_STRING")


@pytest.fixture
def blob_env(monkeypatch):
    if not BLOB_CONN:
        pytest.skip("SSIS_ADF_TEST_BLOB_CONNECTION_STRING not set (Azurite not available)")
    from azure.storage.blob import BlobServiceClient

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", BLOB_CONN)
    svc = BlobServiceClient.from_connection_string(BLOB_CONN)
    suffix = uuid.uuid4().hex[:8]
    src, dst = f"pkgs-{suffix}", f"artifacts-{suffix}"
    svc.create_container(src)
    svc.create_container(dst)
    for name in ("CargaVentas", "CargaClientes"):
        svc.get_container_client(src).upload_blob(
            f"Finanzas/{name}.dtsx", (SAMPLES / f"{name}.dtsx").read_bytes()
        )
    yield svc, src, dst
    svc.delete_container(src)
    svc.delete_container(dst)


def test_blob_scan_convert_validate(blob_env, remote_policy) -> None:
    svc, src, dst = blob_env

    scan = json.loads(_call("scan_ssis_packages", {
        "source_type": "blob", "path_or_connection": f"az://{src}/Finanzas",
    }))
    assert [p["name"] for p in scan["packages"]] == ["CargaClientes", "CargaVentas"]

    out = f"az://{dst}/Finanzas/CargaVentas"
    summary = json.loads(_call("convert_ssis_package", {
        "package_path": f"az://{src}/Finanzas/CargaVentas.dtsx", "output_dir": out,
    }))
    assert summary["output_directory"] == out
    assert summary["files"]["pipeline"] == f"{out}/pipeline/PL_CargaVentas.json"

    names = {b.name for b in svc.get_container_client(dst).list_blobs()}
    assert "Finanzas/CargaVentas/pipeline/PL_CargaVentas.json" in names
    pipeline = json.loads(
        svc.get_container_client(dst)
        .download_blob("Finanzas/CargaVentas/pipeline/PL_CargaVentas.json").readall()
    )
    assert f"az://{src}/Finanzas/CargaVentas.dtsx" in pipeline["properties"]["description"]

    val = json.loads(_call("validate_adf_artifacts", {"artifacts_dir": out}))
    assert val["status"] == "valid"


def test_blob_missing_package_reports_error(blob_env, remote_policy) -> None:
    _, src, dst = blob_env
    text = _call("convert_ssis_package", {
        "package_path": f"az://{src}/Finanzas/NoExiste.dtsx", "output_dir": f"az://{dst}/x",
    })
    assert "Blob not found" in text
