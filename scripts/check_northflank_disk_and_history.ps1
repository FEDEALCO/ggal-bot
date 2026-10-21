<#
.SYNOPSIS
    Responde la pregunta de retencion de market_snapshots.csv: lista lo que
    REALMENTE hay en /app/logs del contenedor de Northflank (incluidos los
    archivos rotados de dias anteriores), mide el espacio libre en disco, y
    descarga los .csv.gz rotados que encuentre.

.POR QUE (hallazgo real leyendo el codigo, no una suposicion)
    ggal_bot/data/market_snapshot_log.py::_rotate_if_new_day_locked() NUNCA
    borra un archivo rotado: cuando el dia cambia, comprime
    logs/market_snapshots.csv a logs/market_snapshots-YYYY-MM-DD.csv.gz (en
    el MISMO directorio del volumen persistente) y recien ahi borra el
    archivo "vivo" para empezar limpio - el .gz queda. No hay ningun codigo
    en el repo que borre un .gz viejo. O sea: la retencion YA es
    indefinida por diseño, salvo que algo externo (disco lleno, limpieza
    manual) los borre. Lo que fetch_northflank_logs.ps1 bajaba la semana
    pasada era SOLO el archivo "vivo" de hoy - nunca miro los .gz de dias
    anteriores, que es por lo que parecia que "no habia historia".

    Este script no cambia ningun comportamiento del bot (no hay ninguna
    guardia de riesgo que agregar aca, a diferencia de expiry_handler.py) -
    es puramente de diagnostico/operacion.

.LIMITACION CONOCIDA DE LA CLI (documentada por Northflank, no un bug de
    este script): `northflank exec service --cmd` puede devolver stdout
    VACIO cuando no corre contra una TTY real - que es exactamente lo que
    pasa cuando PowerShell captura la salida en una variable. Si las
    secciones de abajo salen vacias, corre el comando `northflank exec...`
    que se imprime en la consola DIRECTAMENTE (sin este script, en una
    terminal interactiva) y pegame el resultado.

.HALLAZGO ADICIONAL QUE TAMBIEN RESUELVE ESTE SCRIPT (no pedido explicitamente,
    pero descubierto analizando el export anterior): el shadow_trades.csv
    que bajamos la semana pasada termina en 2026-10-02T18:01:45 - no tiene
    NINGUNA fila mas reciente, aunque position_events.csv (el journal) SI
    sigue registrando ENTRY/CLOSE reales hasta hoy. ggal_bot/execution/
    order_gateway.py::ShadowAuditLogger rota a un archivo sibling
    `shadow_trades.schemaN.csv` (N=2,3,...) ante un cambio de header -
    dashboard/pnl_engine.py ya sabe leer y concatenar toda esa familia
    (_discover_shadow_trade_files). Este script tambien busca y baja esos
    siblings si existen en el listado de /app/logs.

.USO
    .\check_northflank_disk_and_history.ps1 -ProjectId "ggal-bot" -ServiceId "supreme-cattle"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [string]$ServiceId,

    [string]$OutDir = ".\ggal_bot_snapshot_history_$(Get-Date -Format 'yyyy-MM-dd_HHmm')"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "== $msg ==" -ForegroundColor Cyan
}

function Invoke-NorthflankExec([string]$RemoteCommand) {
    # Devuelve el stdout como texto. Si viene vacio, es la limitacion de TTY
    # documentada arriba, no necesariamente que el comando no produjo nada.
    & northflank exec service --projectId $ProjectId --service $ServiceId --cmd $RemoteCommand --shell-cmd 'bash -c' 2>&1
}

$nfCmd = Get-Command northflank -ErrorAction SilentlyContinue
if (-not $nfCmd) {
    Write-Error "No se encontro la CLI de Northflank. Corre primero fetch_northflank_logs.ps1 (instala @northflank/cli) o 'npm i -g @northflank/cli'."
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ---------------------------------------------------------------------------
# 1) Espacio libre en disco del volumen persistente (pedido explicito del usuario)
# ---------------------------------------------------------------------------
Write-Step "Espacio en disco del volumen (/app/logs)"
$dfCmd = "df -h /app/logs"
Write-Host "Comando remoto: $dfCmd"
$dfOutput = Invoke-NorthflankExec $dfCmd
if ([string]::IsNullOrWhiteSpace($dfOutput)) {
    Write-Host "SIN SALIDA (posible limitacion de TTY de la CLI - ver cabecera del script)." -ForegroundColor Yellow
    Write-Host "Corre esto directo en tu terminal (no dentro de este script) y pegame el resultado:" -ForegroundColor Yellow
    Write-Host "  northflank exec service --projectId $ProjectId --service $ServiceId --cmd `"$dfCmd`" --shell-cmd 'bash -c'"
} else {
    $dfOutput | Tee-Object -FilePath (Join-Path $OutDir "df_app_logs.txt")
}

# ---------------------------------------------------------------------------
# 2) Listado real de logs/ - para ver si los .gz rotados de dias anteriores
#    todavia estan ahi (deberian, ver docstring de arriba)
# ---------------------------------------------------------------------------
Write-Step "Listado de /app/logs (incluye rotados *.csv.gz)"
$lsCmd = "ls -la /app/logs"
Write-Host "Comando remoto: $lsCmd"
$lsOutput = Invoke-NorthflankExec $lsCmd
if ([string]::IsNullOrWhiteSpace($lsOutput)) {
    Write-Host "SIN SALIDA (posible limitacion de TTY de la CLI - ver cabecera del script)." -ForegroundColor Yellow
    Write-Host "Corre esto directo en tu terminal y pegame el resultado:" -ForegroundColor Yellow
    Write-Host "  northflank exec service --projectId $ProjectId --service $ServiceId --cmd `"$lsCmd`" --shell-cmd 'bash -c'"
    Write-Host ""
    Write-Host "Sin ese listado no se puede saber que archivos .gz rotados existen para descargar - el resto de este script no tiene nada para hacer. Volve a correrlo con -RotatedFiles una vez que tengas los nombres (ver abajo)." -ForegroundColor Yellow
    exit 0
}
$lsOutput | Tee-Object -FilePath (Join-Path $OutDir "ls_app_logs.txt")

# ---------------------------------------------------------------------------
# 3) Descargar cada market_snapshots-YYYY-MM-DD.csv.gz Y cada
#    shadow_trades.schemaN.csv que aparezca en el listado (ver HALLAZGO
#    ADICIONAL en la cabecera del script para el segundo caso)
# ---------------------------------------------------------------------------
Write-Step "Descargando archivos rotados encontrados"
$rotatedNames = $lsOutput | Select-String -Pattern '(market_snapshots-\d{4}-\d{2}-\d{2}\.csv\.gz|shadow_trades\.schema\d+\.csv)' -AllMatches |
    ForEach-Object { $_.Matches.Value } | Select-Object -Unique

if (-not $rotatedNames) {
    Write-Host "Ningun market_snapshots-*.csv.gz ni shadow_trades.schemaN.csv encontrado en el listado de arriba." -ForegroundColor Yellow
} else {
    Write-Host "Encontrados: $($rotatedNames.Count) archivo(s) rotado(s)."
    foreach ($name in $rotatedNames) {
        $localPath = Join-Path $OutDir $name
        Write-Host "Bajando /app/logs/$name -> $localPath ..."
        & northflank download service file --projectId $ProjectId --service $ServiceId `
            --remotePath "/app/logs/$name" --localPath $localPath
    }
}

Write-Host ""
Write-Host "Listo. Resultados en: $OutDir" -ForegroundColor Cyan
