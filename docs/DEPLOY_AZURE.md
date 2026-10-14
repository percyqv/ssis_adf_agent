# Desplegar el MCP en Azure (Storage Account + Container Apps)

Esta guía deja el servidor MCP `ssis-adf-agent` funcionando en **Azure Container Apps**,
leyendo los paquetes SSIS desde un **Storage Account** y escribiendo ahí los artefactos de
ADF, listo para conectarlo a **Copilot Studio** (agente en Teams), **Claude** o **VS Code**.

```
Teams ─▶ Agente Copilot Studio ─┐
Claude (conector)  ─────────────┼─▶  https://<app>.<region>.azurecontainerapps.io/mcp
VS Code            ─────────────┘          │  (Streamable HTTP + API key / Entra ID)
                                           ▼
                              Storage Account (identidad administrada)
                              ├── ssis-packages/   ← subes aquí los .dtsx
                              └── adf-artifacts/   ← el MCP deja aquí los JSON de ADF
```

Los comandos son para **PowerShell** con la [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
instalada (`az login` hecho). También sirven en Azure Cloud Shell (PowerShell).

## Opción rápida: script

`scripts/deploy_azure.ps1` hace las secciones 2 a 4 de una vez (y se puede volver a
ejecutar para desplegar una versión nueva). Desde la carpeta raíz del repositorio:

```powershell
.\scripts\deploy_azure.ps1 -StorageAccount stssismigracion01 -Registry acrssismigracion01 `
    -ResourceGroup rg-ssis-migracion -Location eastus2
```

Al terminar muestra la URL del MCP y, la primera vez, la API key. Si PowerShell bloquea el
script, ejecuta antes `Set-ExecutionPolicy -Scope Process Bypass`.

Las secciones siguientes explican lo mismo paso a paso.

---

## 1. Variables

```powershell
$RG   = "rg-ssis-migracion"
$LOC  = "eastus2"
$ST   = "stssismigracion01"      # nombre único global, solo minúsculas y números
$ACR  = "acrssismigracion01"     # nombre único global
$ENV  = "cae-ssis-migracion"
$APP  = "ssis-adf-agent"
```

## 2. Storage Account y contenedores

```powershell
az group create -n $RG -l $LOC

az storage account create -n $ST -g $RG -l $LOC `
  --sku Standard_LRS --kind StorageV2 `
  --allow-blob-public-access false --min-tls-version TLS1_2

# Para crear contenedores y subir archivos con tu usuario necesitas un rol de datos
$ST_ID = az storage account show -n $ST -g $RG --query id -o tsv
$ME    = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $ME --role "Storage Blob Data Contributor" --scope $ST_ID

az storage container create --account-name $ST -n ssis-packages --auth-mode login
az storage container create --account-name $ST -n adf-artifacts --auth-mode login
```

> La asignación de rol puede tardar 1–2 minutos en aplicarse.

**Subir un proyecto SSIS** (toda la carpeta, conservando subcarpetas):

```powershell
az storage blob upload-batch --account-name $ST --auth-mode login `
  -d ssis-packages --destination-path Finanzas `
  -s "C:\ruta\a\MiProyectoSSIS" --pattern "*.dtsx"
```

También puedes usar Azure Storage Explorer o el portal.

## 3. Construir la imagen (sin Docker local)

`az acr build` construye la imagen en Azure a partir del `Dockerfile` del repo. Ejecútalo
**desde la carpeta del repositorio**:

```powershell
az acr create -n $ACR -g $RG --sku Basic
az acr build -r $ACR -t ssis-adf-agent:1 .
```

## 4. Crear el Container App

Genera una API key larga y aleatoria (mínimo 24 caracteres):

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$API_KEY = [Convert]::ToBase64String($bytes)
$API_KEY   # guárdala en un gestor de secretos; la necesitarán los clientes
```

```powershell
az containerapp env create -n $ENV -g $RG -l $LOC

az containerapp create -n $APP -g $RG --environment $ENV `
  --image "$ACR.azurecr.io/ssis-adf-agent:1" `
  --registry-server "$ACR.azurecr.io" --registry-identity system `
  --system-assigned `
  --ingress external --target-port 8000 `
  --min-replicas 1 --max-replicas 3 `
  --secrets "mcp-api-keys=$API_KEY" `
  --env-vars "MCP_API_KEYS=secretref:mcp-api-keys" "AZURE_STORAGE_ACCOUNT=$ST"
```

Da permiso a la identidad del Container App sobre el Storage Account:

```powershell
$APP_PRINCIPAL = az containerapp show -n $APP -g $RG --query identity.principalId -o tsv
az role assignment create --assignee-object-id $APP_PRINCIPAL `
  --assignee-principal-type ServicePrincipal `
  --role "Storage Blob Data Contributor" --scope $ST_ID

$FQDN = az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv
az containerapp update -n $APP -g $RG --set-env-vars "MCP_PUBLIC_URL=https://$FQDN"
"URL del MCP: https://$FQDN/mcp"
```

**Comprobar:**

```powershell
Invoke-RestMethod "https://$FQDN/healthz"          # → status ok, auth True
```

## 5. Configuración (variables de entorno)

| Variable | Para qué | Valor por defecto |
|---|---|---|
| `MCP_API_KEYS` | API keys aceptadas, separadas por comas (≥ 24 caracteres) | — |
| `ENTRA_TENANT_ID`, `ENTRA_AUDIENCE` | Activan OAuth con Microsoft Entra ID (ver §6) | — |
| `ENTRA_REQUIRED_SCOPES` | Scopes/roles obligatorios en el token, p. ej. `Convert.Run` | — |
| `MCP_PUBLIC_URL` | URL pública; publica los metadatos OAuth (RFC 9728) | — |
| `AZURE_STORAGE_ACCOUNT` | Cuenta para las rutas `az://contenedor/ruta` | — |
| `SSIS_ADF_ALLOW_DEPLOY` | Permite la herramienta `deploy_to_adf` | `false` en HTTP |
| `SSIS_ADF_ALLOW_LOCAL_PATHS` | Permite rutas del disco del contenedor | `false` en HTTP |
| `MCP_ALLOWED_HOSTS` | Lista de Host permitidos (protección DNS-rebinding) | sin restricción |
| `LOG_LEVEL` | Nivel de logs | `INFO` |

Sin `MCP_API_KEYS` ni Entra ID, el servidor **se niega a arrancar** en modo HTTP.

> **Despliegue a ADF:** si activas `SSIS_ADF_ALLOW_DEPLOY=true`, la identidad del Container
> App necesita el rol *Data Factory Contributor* sobre la factoría (usa una factoría de
> **desarrollo**). Cualquiera con credenciales del MCP podrá desplegar.

## 6. (Opcional) OAuth con Microsoft Entra ID

Necesario para clientes que solo admiten OAuth, y recomendable para saber **quién** hace
cada conversión.

1. **Portal → Microsoft Entra ID → App registrations → New registration**:
   nombre `ssis-adf-agent-api`, cuentas de *este directorio*. Anota el
   **Application (client) ID** y el **Directory (tenant) ID**.
2. **Expose an API** → *Application ID URI* → `api://<client-id>` → **Add a scope**
   `Convert.Run` (quién puede consentir: administradores y usuarios).
3. Configura el Container App:

```powershell
$TENANT = "<directory-tenant-id>"
$CLIENT = "<application-client-id>"
az containerapp update -n $APP -g $RG --set-env-vars `
  "ENTRA_TENANT_ID=$TENANT" `
  "ENTRA_AUDIENCE=api://$CLIENT $CLIENT" `
  "ENTRA_REQUIRED_SCOPES=Convert.Run"
```

Con `MCP_PUBLIC_URL` configurado, los clientes OAuth descubren Entra ID en
`https://<fqdn>/.well-known/oauth-protected-resource/mcp`.

Las API keys siguen funcionando junto con Entra ID; si quieres solo OAuth, elimina
`MCP_API_KEYS`.

## 7. Conectar los clientes

Las rutas siempre van al Storage Account, por ejemplo:

> *"Escanea az://ssis-packages/Finanzas y convierte CargaVentas a az://adf-artifacts/Finanzas/CargaVentas"*

### Copilot Studio (agente en Teams)

1. Crea un agente → **Tools** → **Add a tool** → **Model Context Protocol** (los nombres de
   menú pueden variar según la versión).
2. URL del servidor: `https://<fqdn>/mcp`.
3. Autenticación:
   - **API key**: tipo *Header*, nombre `X-API-Key`, valor = tu API key.
   - **OAuth 2.0**: usa un *client app registration* con permiso al scope
     `api://<client-id>/Convert.Run`, endpoints
     `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize` y `/token`.
4. Publica el agente en el canal **Microsoft Teams**.

### VS Code

Copilot (modo agente) **no admite** variables `${input:...}` en servidores MCP: el servidor sale
como *Unsupported*. Por eso el servidor remoto se configura en el `mcp.json` **de usuario**
(no en el del proyecto), con la clave escrita tal cual, para que no acabe en Git.

Ábrelo con *Ctrl+Shift+P → MCP: Open User Configuration*
(`%APPDATA%\Code\User\mcp.json` en Windows):

```json
{
  "servers": {
    "ssis-adf-agent-azure": {
      "type": "http",
      "url": "https://<fqdn>/mcp",
      "headers": { "X-API-Key": "<tu-api-key>" }
    }
  }
}
```

El `.vscode/mcp.json` del repo queda para el servidor **local** (`ssis-adf-agent`, stdio).
Si aparece *Sign In* o un 401, la clave del archivo no coincide con el secreto
`mcp-api-keys` del Container App.

### Claude

En Claude: **Settings → Connectors → Add custom connector** con la URL
`https://<fqdn>/mcp`. Los conectores de Claude se autentican con **OAuth** (no con API
key), así que hace falta la §6. Entra ID no admite registro dinámico de clientes: registra
una app cliente en Entra ID e indica su Client ID/Secret en las opciones avanzadas del
conector. Revisa la documentación actual de conectores de Claude, porque estas opciones
cambian con frecuencia.

## 8. Actualizar

```powershell
az acr build -r $ACR -t ssis-adf-agent:2 .
az containerapp update -n $APP -g $RG --image "$ACR.azurecr.io/ssis-adf-agent:2"
```

## 9. Probar en local antes de desplegar

```powershell
$env:MCP_API_KEYS = "una-clave-local-de-al-menos-24-caracteres"
$env:AZURE_STORAGE_ACCOUNT = $ST          # usa tu `az login`
.venv\Scripts\ssis-adf-agent --transport http --port 8000
# MCP en http://127.0.0.1:8000/mcp
```
