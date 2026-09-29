"""
paths.py
========
Resolucion centralizada de rutas del proyecto (mismo patron que Quantbot),
para que ningun modulo tenga que hardcodear rutas relativas propias.
"""

import sys
from pathlib import Path

# Raiz del proyecto = carpeta que contiene el paquete ggal_bot/. Si el bot
# corre empaquetado como .exe (ver build_exe.bat / PyInstaller), __file__
# apunta adentro del directorio temporal de extraccion (sys._MEIPASS en
# modo --onefile), que se borra al cerrar el proceso - usar esa ruta
# dejaria logs/state/data_cache sin persistir entre corridas. En ese caso
# se usa la carpeta donde esta el .exe real (sys.executable) en su lugar.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOGS_DIR = PROJECT_ROOT / "logs"
DOCS_DIR = PROJECT_ROOT / "docs"
STATE_DIR = PROJECT_ROOT / "state"
DATA_DIR = PROJECT_ROOT / "data_cache"

STATE_FILE = STATE_DIR / "bot_state.json"
LOG_FILE = LOGS_DIR / "ggal_bot.log"
SHADOW_TRADES_LOG = LOGS_DIR / "shadow_trades.csv"  # auditoria de fills simulados (ver order_gateway.py)

# Fase 5.3 - Position Lifecycle Engine (ver AUDITORIA_FASE5.3_*.md):
# ARCHIVO NUEVO, independiente de SHADOW_TRADES_LOG - deliberadamente NO se
# agregan columnas a shadow_trades.csv (ver ggal_bot/portfolio/event_journal.py
# para la justificacion completa: el archivo de produccion ya existe con un
# header fijo de 10 columnas: agregar columnas ahi rompe pd.read_csv()
# contra las filas viejas, que no las tienen). Registra el evento de
# lifecycle (ENTRY/ADD/REDUCE/PARTIAL_EXIT/CLOSE/REJECT) con position_id/
# contract_key/strategy_tag - datos que shadow_trades.csv nunca tuvo.
POSITION_EVENTS_LOG = LOGS_DIR / "position_events.csv"

# Fase 5.3 - Kill switch (ver ggal_bot/risk/kill_switch.py): estado
# persistido en disco (no solo en memoria) para que un halt de riesgo
# sobreviva a un restart del proceso - la misma clase de problema que la
# reconciliacion de portfolio (ver reconciliation.py): sin persistencia,
# un kill switch activado por una corrida se "olvida" en el siguiente
# arranque, exactamente el tipo de gap que esta fase busca cerrar.
KILL_SWITCH_STATE_FILE = STATE_DIR / "kill_switch.json"

# MEJORA 2026-09-28 (a pedido explicito del usuario, "mejor trader quant...
# exprime tu capacidad al maximo" - ver ggal_bot/data/market_snapshot_log.py):
# snapshot append-only de TODA la cadena de opciones (spot/IV/griegas/book)
# en cada ciclo. Antes de esta mejora no existia NINGUN historial de
# mercado persistido (solo fills y eventos de lifecycle, ambos a nivel de
# TRADE) - sin esto, cualquier cambio de umbral/modelo solo se podia
# validar desplegando a shadow y esperando dias por un export nuevo. Este
# archivo es lo que permite, de ahora en mas, backtestear offline.
MARKET_SNAPSHOT_LOG = LOGS_DIR / "market_snapshots.csv"

# MEJORA 2026-09-29 (a pedido explicito del usuario, prioridad de despliegue
# junto con MARKET_SNAPSHOT_LOG de arriba - ver REPORT.md §12.3/§12.5 punto 5
# y ggal_bot/data/signal_funnel_log.py): embudo detallado de candidatas de
# entrada por ciclo (universo completo, con spread/profundidad/griegas y que
# filtro la descarto) - opt-in via LongFirstConfig/ScalpingConfig.
# enable_signal_funnel_log, apagado por defecto. Archivo NUEVO, mismo
# criterio que los de arriba (nunca se agregan columnas a un CSV de
# produccion ya existente).
SIGNAL_FUNNEL_LOG = LOGS_DIR / "signal_funnel.csv"

for _dir in (LOGS_DIR, STATE_DIR, DATA_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
