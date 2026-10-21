<#
.SYNOPSIS
    Descarga los logs/CSV reales del bot GGAL_BOT desde el contenedor
    corriendo en Northflank, usando la CLI OFICIAL de Northflank
    (@northflank/cli) - no un endpoint de API inventado.

.POR QUE ASI (y no "pegame tu token en el chat")
    - fetch_bot_logs.py (ya existe en el repo) solo trae el STDOUT del
      contenedor via la API de Logs de Northflank - NO trae los archivos
      reales que estan en el volumen persistente (/app/logs, /app/state).
    - Para traer esos archivos hace falta la Execute-Command API o la
      Copy-Files API de Northflank. Verificado en la documentacion oficial
      (northflank.com/docs/v1/api/execute-command y /copy-files, oct 2026):
      el endpoint HTTP crudo y su esquema de auth/body NO estan publicados
      en esas paginas (solo CLI y cliente JS). La CLI SI esta documentada
      end-to-end, asi que este script usa la CLI (`northflank download
      service file`), que internamente habla con la API oficial.
    - Tu token de Northflank NUNCA se escribe en este archivo ni se me
      pasa a mi (Claude) en el chat. Lo tipeas vos, en tu maquina, cuando
      el script te lo pide (o hacés login interactivo por navegador).

.REQUISITOS (una sola vez)
    - Node.js 12+ instalado (https://nodejs.org).
    - Conocer tu ProjectId y ServiceId de Northflank. Los ves en la URL
      del dashboard cuando entras al servicio del bot:
      https://app.northflank.com/t/_/project/<ProjectId>/service/<ServiceId>
      (o corriendo, despues de loguearte: northflank get projects / northflank get services --projectId <ProjectId>)

.USO
    .\fetch_northflank_logs.ps1 -ProjectId "tu-project-id" -ServiceId "tu-service-id"

    Parametros opcionales:
    -OutDir "C:\ruta\donde\guardar"   (default: .\ggal_bot_export_<fecha>)
    -SkipLogin                        (si ya corriste `northflank login` antes en esta maquina)
    -IncludeOptional                  (tambien intenta traer los logs opt-in:
                                        signal_funnel.csv, ccl_bond_quotes.csv,
                                        market_data_source_log.csv,
                                        portfolio_greeks_log.csv - pueden no
                                        existir si esos flags no estan activados,
                                        el script lo maneja sin frenar)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [string]$ServiceId,

    [string]$OutDir = ".\ggal_bot_export_$(Get-Date -Format 'yyyy-MM-dd_HHmm')",

    [switch]$SkipLogin,

    [switch]$IncludeOptional
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "== $msg ==" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 1) Verificar/instalar la CLI de Northflank (@northflank/cli, via npm)
# ---------------------------------------------------------------------------
Write-Step "Verificando Northflank CLI"

$northflankCmd = Get-Command northflank -ErrorAction SilentlyContinue
if (-not $northflankCmd) {
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        Write-Error "No se encontro 'npm'. Instala Node.js (https://nodejs.org) y volve a correr este script."
        exit 1
    }
    Write-Host "Northflank CLI no encontrada. Instalando @northflank/cli globalmente (npm i -g @northflank/cli)..."
    npm i -g @northflank/cli
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Fallo la instalacion de @northflank/cli. Revisa el error de npm arriba."
        exit 1
    }
}
northflank --version

# ---------------------------------------------------------------------------
# 2) Login - interactivo, NUNCA le paso el token a Claude ni lo guardo en disco
# ---------------------------------------------------------------------------
if (-not $SkipLogin) {
    Write-Step "Login en Northflank"
    Write-Host "Elegi una opcion:"
    Write-Host "  [1] Login interactivo por navegador (recomendado - no tipeas ningun token)"
    Write-Host "  [2] Login con un API token que vos generes en app.northflank.com -> Account -> API tokens"
    $choice = Read-Host "Opcion (1/2)"

    if ($choice -eq "2") {
        # SecureString: el token queda en memoria, no se imprime en pantalla,
        # no se loguea, no se escribe a ningun archivo por este script.
        $secureToken = Read-Host "Pega tu token de Northflank (no se va a mostrar)" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $plainToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            northflank login -t $plainToken -n ggal-bot
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            Remove-Variable plainToken -ErrorAction SilentlyContinue
        }
    } else {
        northflank login
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Error "El login de Northflank fallo. Volve a correr el script (o usa -SkipLogin si ya estabas logueado)."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 3) Descargar los archivos reales del volumen persistente del bot
# ---------------------------------------------------------------------------
Write-Step "Descargando archivos a $OutDir"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# Rutas confirmadas en ggal_bot/paths.py (PROJECT_ROOT = /app, WORKDIR del
# Dockerfile) - LOGS_DIR=/app/logs, STATE_DIR=/app/state.
$coreFiles = @(
    @{ Remote = "/app/logs/position_events.csv";  Local = "position_events.csv" },
    @{ Remote = "/app/logs/shadow_trades.csv";    Local = "shadow_trades.csv" },
    @{ Remote = "/app/logs/market_snapshots.csv"; Local = "market_snapshots.csv" },
    @{ Remote = "/app/logs/ggal_bot.log";         Local = "ggal_bot.log" },
    @{ Remote = "/app/state/bot_state.json";      Local = "bot_state.json" }
)

$optionalFiles = @(
    @{ Remote = "/app/logs/signal_funnel.csv";           Local = "signal_funnel.csv" },
    @{ Remote = "/app/logs/ccl_bond_quotes.csv";          Local = "ccl_bond_quotes.csv" },
    @{ Remote = "/app/logs/market_data_source_log.csv";   Local = "market_data_source_log.csv" },
    @{ Remote = "/app/logs/portfolio_greeks_log.csv";     Local = "portfolio_greeks_log.csv" }
)

$filesToFetch = $coreFiles
if ($IncludeOptional) { $filesToFetch += $optionalFiles }

$results = @()
foreach ($f in $filesToFetch) {
    $localPath = Join-Path $OutDir $f.Local
    Write-Host "Bajando $($f.Remote) -> $localPath ..."
    & northflank download service file `
        --projectId $ProjectId `
        --service $ServiceId `
        --remotePath $f.Remote `
        --localPath $localPath

    if ($LASTEXITCODE -eq 0 -and (Test-Path $localPath)) {
        Write-Host "  OK" -ForegroundColor Green
        $results += [pscustomobject]@{ Archivo = $f.Remote; Estado = "OK" }
    } else {
        # Esperable para los logs opt-in si ese flag nunca se activo, o si
        # el archivo todavia no existe porque el bot no corrio nada aun.
        Write-Host "  No se pudo bajar (puede no existir todavia)." -ForegroundColor Yellow
        $results += [pscustomobject]@{ Archivo = $f.Remote; Estado = "FALLO o no existe" }
    }
}

# ---------------------------------------------------------------------------
# 4) Resumen + zip para mandarme los archivos facil
# ---------------------------------------------------------------------------
Write-Step "Resumen"
$results | Format-Table -AutoSize

$zipPath = "$OutDir.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$OutDir\*" -DestinationPath $zipPath
Write-Host ""
Write-Host "Listo. Carpeta: $OutDir" -ForegroundColor Cyan
Write-Host "Zip para compartir: $zipPath" -ForegroundColor Cyan
