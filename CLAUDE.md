# CLAUDE.md — ssis_adf_agent (servidor MCP)

Contexto para retomar el trabajo en una sesión nueva (Claude, Copilot o una persona).

## Qué es

Servidor **MCP** que convierte paquetes **SSIS (.dtsx)** en artefactos de **Azure Data Factory**
(pipelines, linked services, datasets, data flows, triggers). Es un fork de
`tyrrestrup/ssis_adf_agent` (`percyqv/ssis_adf_agent`), ampliado para uso remoto:

- Rutas en **Azure Blob Storage** (`az://contenedor/ruta` o `https://…blob.core.windows.net/…`).
- Transporte **Streamable HTTP** (`/mcp`, sin estado, respuestas JSON) además de stdio.
- **Autenticación**: API key (`X-API-Key` o `Authorization: Bearer`) y/o Microsoft Entra ID (JWT).
- Despliegue en **Azure Container Apps** con `scripts/deploy_azure.ps1`.

Lo usan: la web `percyqv/ssis-adf-web` (App Service) y GitHub Copilot en VS Code.

> **Rama:** todo el trabajo está en `feat/azure-blob-http` (todavía no fusionada en `main`).

## Herramientas MCP

`scan_ssis_packages`, `analyze_ssis_package`, `convert_ssis_package`, `validate_adf_artifacts`,
`deploy_to_adf`. En modo remoto (HTTP) `deploy_to_adf` y las rutas locales están **desactivados**
salvo `SSIS_ADF_ALLOW_DEPLOY=true` / `SSIS_ADF_ALLOW_LOCAL_PATHS=true`.
`validate_adf_artifacts` devuelve `valid`, `valid_with_warnings` o `issues_found`.

## Estructura

| Ruta | Qué es |
|---|---|
| `ssis_adf_agent/mcp_server.py` | Definición de herramientas, `main()` (stdio / `--transport http`) |
| `ssis_adf_agent/http_app.py` | App Starlette: `/mcp`, `/healthz`, metadatos RFC 9728 |
| `ssis_adf_agent/auth.py` | API keys + validación JWT de Entra ID |
| `ssis_adf_agent/storage.py` | Lectura/escritura en Blob (`az://`), omite carpetas ADLS |
| `ssis_adf_agent/parsers/ssis_parser.py` | Parser .dtsx (precedencias por `refId`, `PROGID_MAP`, `_CLSID_LOOKUP`) |
| `ssis_adf_agent/converters/` | Conversores por tipo de tarea; `sql_flavor.py` (SqlServer vs AzureSql) |
| `ssis_adf_agent/naming.py` | Nombres de linked services: `LS_<ConnectionManager>` |
| `scripts/deploy_azure.ps1` | Despliegue completo en Azure (ACR + Container Apps + Storage) |
| `docs/DEPLOY_AZURE.md` | Guía de despliegue paso a paso y conexión de clientes |
| `samples/` | `CargaVentas.dtsx`, `CargaClientes.dtsx` de ejemplo |
| `tests/` | `test_conversion.py`, `test_remote.py` (Azurite) |

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q                     # test_remote.py necesita Azurite + AZURE_STORAGE_CONNECTION_STRING
ruff check .                  # line-length 100

# Local por HTTP
MCP_API_KEYS=<clave-larga> ssis-adf-agent --transport http --port 8000   # http://127.0.0.1:8000/mcp
```

Despliegue (PowerShell, `az login` hecho):

```powershell
.\scripts\deploy_azure.ps1                 # reconstruye la imagen y actualiza el Container App
.\scripts\deploy_azure.ps1 -RotateApiKey   # además genera una API key nueva
```

## Recursos en Azure (laboratorio)

| Recurso | Nombre |
|---|---|
| Suscripción | `Inetum-Lab` |
| Grupo de recursos | `Siss2adfLab` (eastus) |
| Container App | `ssis-adf-agent` (entorno `cae-ssis-migracion`, min réplicas 1) |
| Container Registry | `acrsiss2adflab01` |
| Storage Account | `stsiss2adflab01` — contenedores `ssis-packages` (entrada) y `adf-artifacts` (salida) |
| Identidad | Identidad administrada del Container App con *Storage Blob Data Contributor* |

La API key vive **solo** en el secreto `mcp-api-keys` del Container App y en el `mcp.json`
de usuario de VS Code. **Nunca** se imprime ni se sube al repo.

## Decisiones y problemas ya resueltos

- `mcp>=1.30,<2`: el SDK 2.x rompe el servidor original.
- Las precedencias SSIS 2012+ se resuelven por `refId`, no por DTSID.
- Componentes del data flow: se resuelven por ProgID y CLSID (mapa con llaves corregido).
- Linked services con el nombre del connection manager (`LS_<Nombre>`), no GUIDs.
- `az acr build` en Windows: `PYTHONIOENCODING/PYTHONUTF8=utf-8` y `--no-logs` (crash cp1252).
- El entorno de Container Apps tarda ~30 min en aprovisionarse: el script espera su estado.
- PowerShell 5.1 aborta con avisos de `az` en stderr: el script usa `Invoke-Az` con
  `ErrorActionPreference=Continue`. Los secretos se ocultan en los mensajes de error.
- VS Code: el `mcp.json` va a nivel de **usuario** con la clave literal; Copilot no admite
  variables `${input:...}` (sale "Unsupported").

## Convenciones

- Python ≥ 3.11, ruff (100 columnas), tests con pytest.
- Mensajes de commit en inglés; documentación para el usuario en español.
- Laboratorio de bajo coste: preferir SKUs mínimos.
