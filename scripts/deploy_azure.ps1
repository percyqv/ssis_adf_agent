<#
.SYNOPSIS
    Despliega el MCP ssis-adf-agent en Azure Container Apps con un Storage Account.

.DESCRIPTION
    Crea (o reutiliza si ya existen) estos recursos:
      - Grupo de recursos
      - Storage Account con los contenedores de entrada y salida
      - Azure Container Registry, y construye la imagen EN AZURE (no requiere Docker)
      - Container Apps Environment y el Container App con identidad administrada
      - Permiso "Storage Blob Data Contributor" de la app sobre el Storage

    Se puede ejecutar varias veces: si la app ya existe, solo construye una imagen
    nueva y la despliega (no cambia la API key salvo que uses -RotateApiKey).

    Ejecutar desde la carpeta raiz del repositorio, con "az login" hecho.

.EXAMPLE
    .\scripts\deploy_azure.ps1 -StorageAccount stssismigracion01 -Registry acrssismigracion01

.EXAMPLE
    .\scripts\deploy_azure.ps1 -StorageAccount stssismig01 -Registry acrssismig01 `
        -ResourceGroup rg-ssis -Location brazilsouth -Subscription "Mi suscripcion"
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[a-z0-9]{3,24}$')]
    [string] $StorageAccount  = "stsiss2adflab01",   # unico global: 3-24 minusculas/numeros

    [ValidatePattern('^[a-zA-Z0-9]{5,50}$')]
    [string] $Registry        = "acrsiss2adflab01",  # unico global: 5-50 letras/numeros

    [string] $ResourceGroup   = "Siss2adfLab",
    [string] $Location        = "eastus",
    [string] $Environment     = "cae-ssis-migracion",
    [string] $AppName         = "ssis-adf-agent",
    [string] $InputContainer  = "ssis-packages",
    [string] $OutputContainer = "adf-artifacts",
    [string] $Subscription    = "Inetum-Lab",
    [switch] $RotateApiKey
)

$ErrorActionPreference = "Stop"

# La Azure CLI en Windows usa la codificacion de la consola (cp1252) y falla al
# imprimir caracteres Unicode de los logs ("'charmap' codec can't encode").
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Nota: dentro de estas funciones se usa ErrorActionPreference=Continue porque en
# Windows PowerShell 5.1 cualquier texto de az en stderr (avisos) se convierte en un
# error que detendria el script. El resultado real se comprueba con $LASTEXITCODE.

function Format-AzArgs($azArgs) {
    # Texto del comando para mensajes de error, ocultando secretos (API keys).
    $text = ($azArgs -join ' ')
    return ($text -replace '(mcp-api-keys=)\S+', '$1***')
}

function Invoke-Az {
    # Ejecuta az y detiene el script si falla. Devuelve la salida estandar.
    $ErrorActionPreference = "Continue"
    $out = & az @args
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo: az $(Format-AzArgs $args)"
    }
    return $out
}

function Invoke-AzLive {
    # Igual que Invoke-Az pero mostrando la salida en pantalla mientras se ejecuta.
    $ErrorActionPreference = "Continue"
    & az @args | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo: az $(Format-AzArgs $args)"
    }
}

function Test-Az {
    # Devuelve $true si el comando az termina bien (para comprobar si algo existe).
    $ErrorActionPreference = "Continue"
    & az @args *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-AcrTags($registry, $repository) {
    # Tags de un repositorio del registro; vacio si el repositorio aun no existe.
    $ErrorActionPreference = "Continue"
    $tags = & az acr repository show-tags -n $registry --repository $repository -o tsv 2>$null
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($tags)
}

function Get-GitShortSha {
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
    $sha = & git rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0) { return $sha }
    return $null
}

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function New-ApiKey {
    # 32 bytes aleatorios en base64 "url-safe" sin '=' (evita problemas al pasarla a az)
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# ---------------------------------------------------------------------------
Write-Step "Comprobando Azure CLI y sesion"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "No se encuentra 'az'. Instala Azure CLI: winget install -e --id Microsoft.AzureCLI"
}
if (-not (Test-Path ".\Dockerfile")) {
    throw "Ejecuta el script desde la carpeta raiz del repositorio (donde esta el Dockerfile)."
}
if (-not (Test-Az account show)) {
    Write-Host "Iniciando sesion en Azure..."
    Invoke-Az login | Out-Null
}
if ($Subscription) { Invoke-Az account set --subscription $Subscription | Out-Null }
$sub = Invoke-Az account show --query "{name:name, id:id}" -o json | ConvertFrom-Json
Write-Host "Suscripcion: $($sub.name) ($($sub.id))"

# Instala la extension containerapp sin preguntar
Invoke-Az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors | Out-Null

Write-Step "Registrando proveedores de recursos (solo la primera vez tarda)"
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ContainerRegistry", "Microsoft.Storage") {
    Invoke-Az provider register --namespace $ns --wait | Out-Null
}

# ---------------------------------------------------------------------------
Write-Step "Grupo de recursos $ResourceGroup ($Location)"
Invoke-Az group create -n $ResourceGroup -l $Location -o none

# ---------------------------------------------------------------------------
Write-Step "Storage Account $StorageAccount"
if (-not (Test-Az storage account show -n $StorageAccount -g $ResourceGroup)) {
    Invoke-Az storage account create -n $StorageAccount -g $ResourceGroup -l $Location `
        --sku Standard_LRS --kind StorageV2 `
        --allow-blob-public-access false --min-tls-version TLS1_2 -o none
}
$storageId = Invoke-Az storage account show -n $StorageAccount -g $ResourceGroup --query id -o tsv

# Tu usuario necesita un rol de datos para crear contenedores y subir archivos
$me = Invoke-Az ad signed-in-user show --query id -o tsv
if (-not (Invoke-Az role assignment list --assignee $me --scope $storageId --role "Storage Blob Data Contributor" --query "[0].id" -o tsv)) {
    Invoke-Az role assignment create --assignee-object-id $me --assignee-principal-type User `
        --role "Storage Blob Data Contributor" --scope $storageId -o none
    Write-Host "Rol asignado a tu usuario; esperando a que se propague..."
}

foreach ($c in $InputContainer, $OutputContainer) {
    # La asignacion de rol tarda 1-2 minutos en aplicarse: reintentar
    $ok = $false
    for ($i = 1; $i -le 12 -and -not $ok; $i++) {
        if (Test-Az storage container create --account-name $StorageAccount -n $c --auth-mode login -o none) {
            $ok = $true
        } else {
            Start-Sleep -Seconds 15
        }
    }
    if (-not $ok) { throw "No se pudo crear el contenedor '$c'. Revisa tus permisos sobre el Storage Account." }
    Write-Host "Contenedor listo: $c"
}

# ---------------------------------------------------------------------------
Write-Step "Container Registry $Registry"
if (-not (Test-Az acr show -n $Registry -g $ResourceGroup)) {
    Invoke-Az acr create -n $Registry -g $ResourceGroup -l $Location --sku Basic -o none
}
$registryServer = Invoke-Az acr show -n $Registry -g $ResourceGroup --query loginServer -o tsv

$tag = (Get-Date -Format "yyyyMMdd-HHmmss")
$sha = Get-GitShortSha
if ($sha) { $tag = "$tag-$sha" }
$image = "$registryServer/ssis-adf-agent:$tag"

Write-Step "Construyendo la imagen en Azure: $image (5-10 minutos)"
# --no-logs: espera a que termine sin transmitir el log en vivo (evita el fallo de
# codificacion de la CLI en Windows). Se comprueba despues que la imagen exista.
Invoke-AzLive acr build -r $Registry -g $ResourceGroup -t "ssis-adf-agent:$tag" `
    -f Dockerfile --no-logs --query "{run:runId, estado:status}" -o table .
if ((Get-AcrTags $Registry "ssis-adf-agent") -notcontains $tag) {
    throw ("La imagen no se construyo. Revisa las ejecuciones con:`n" +
           "  az acr task list-runs -r $Registry -o table`n" +
           "  az acr task logs -r $Registry --run-id <RUN_ID>")
}
Write-Host "Imagen construida: $image"

# ---------------------------------------------------------------------------
Write-Step "Container Apps Environment $Environment"
if (-not (Test-Az containerapp env show -n $Environment -g $ResourceGroup)) {
    Write-Host "Creando el entorno (la primera vez puede tardar 15-30 minutos)..."
    Invoke-Az containerapp env create -n $Environment -g $ResourceGroup -l $Location -o none
}

# Esperar a que el entorno quede aprovisionado antes de crear la app.
$envState = ""
for ($i = 1; $i -le 90; $i++) {   # hasta ~45 minutos
    $envState = Invoke-Az containerapp env show -n $Environment -g $ResourceGroup `
        --query properties.provisioningState -o tsv
    if ($envState -eq "Succeeded" -or $envState -eq "Failed") { break }
    if ($i -eq 1 -or $i % 4 -eq 0) { Write-Host "Estado del entorno: $envState (esperando...)" }
    Start-Sleep -Seconds 30
}
if ($envState -ne "Succeeded") {
    Write-Host ""
    Write-Host "El entorno '$Environment' quedo en estado '$envState'." -ForegroundColor Red
    Write-Host "Detalles:" -ForegroundColor Yellow
    & az containerapp env show -n $Environment -g $ResourceGroup -o jsonc | Out-Host
    throw ("El entorno no se aprovisiono. Revisa el error en el portal (Activity log del grupo " +
           "$ResourceGroup, evento 'Create or Update Managed Environment'). Para reintentar, " +
           "borralo y vuelve a ejecutar el script:`n" +
           "  az containerapp env delete -n $Environment -g $ResourceGroup --yes")
}
Write-Host "Entorno listo."

$apiKey = $null
if (-not (Test-Az containerapp show -n $AppName -g $ResourceGroup)) {
    Write-Step "Creando Container App $AppName"
    $apiKey = New-ApiKey
    Invoke-Az containerapp create -n $AppName -g $ResourceGroup --environment $Environment `
        --image $image `
        --registry-server $registryServer --registry-identity system `
        --system-assigned `
        --ingress external --target-port 8000 `
        --cpu 0.5 --memory 1.0Gi `
        --min-replicas 1 --max-replicas 3 `
        --secrets "mcp-api-keys=$apiKey" `
        --env-vars "MCP_API_KEYS=secretref:mcp-api-keys" "AZURE_STORAGE_ACCOUNT=$StorageAccount" `
        -o none
} else {
    Write-Step "Actualizando Container App $AppName con la nueva imagen"
    if ($RotateApiKey) {
        $apiKey = New-ApiKey
        Invoke-Az containerapp secret set -n $AppName -g $ResourceGroup --secrets "mcp-api-keys=$apiKey" -o none
    }
    Invoke-Az containerapp update -n $AppName -g $ResourceGroup --image $image -o none
}

# ---------------------------------------------------------------------------
Write-Step "Permiso de la app sobre el Storage Account"
$appPrincipal = Invoke-Az containerapp show -n $AppName -g $ResourceGroup --query identity.principalId -o tsv
if (-not $appPrincipal) { throw "El Container App no tiene identidad administrada (system-assigned)." }
if (-not (Invoke-Az role assignment list --assignee $appPrincipal --scope $storageId --role "Storage Blob Data Contributor" --query "[0].id" -o tsv)) {
    Invoke-Az role assignment create --assignee-object-id $appPrincipal --assignee-principal-type ServicePrincipal `
        --role "Storage Blob Data Contributor" --scope $storageId -o none
}

$fqdn = Invoke-Az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
$publicUrl = "https://$fqdn"
Invoke-Az containerapp update -n $AppName -g $ResourceGroup --set-env-vars "MCP_PUBLIC_URL=$publicUrl" -o none

# ---------------------------------------------------------------------------
Write-Step "Comprobando $publicUrl/healthz"
$healthy = $false
for ($i = 1; $i -le 20 -and -not $healthy; $i++) {
    try {
        $r = Invoke-RestMethod "$publicUrl/healthz" -TimeoutSec 10
        if ($r.status -eq "ok") { $healthy = $true }
    } catch { Start-Sleep -Seconds 15 }
}

Write-Host ""
if ($healthy) {
    Write-Host "Listo. El MCP responde." -ForegroundColor Green
} else {
    Write-Host "La app no respondio todavia. Revisa los logs con:" -ForegroundColor Yellow
    Write-Host "  az containerapp logs show -n $AppName -g $ResourceGroup --follow"
}
Write-Host ""
Write-Host "URL del MCP : $publicUrl/mcp"
Write-Host "Entrada     : az://$InputContainer/<carpeta>/<paquete>.dtsx"
Write-Host "Salida      : az://$OutputContainer/<carpeta>/<paquete>"
if ($apiKey) {
    Write-Host ""
    Write-Host "API key (guardala ahora en un lugar seguro; no se vuelve a mostrar):" -ForegroundColor Yellow
    Write-Host $apiKey
    Write-Host "Los clientes la envian en la cabecera  X-API-Key: <key>  o  Authorization: Bearer <key>"
}
