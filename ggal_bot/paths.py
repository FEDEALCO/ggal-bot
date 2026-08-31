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

for _dir in (LOGS_DIR, STATE_DIR, DATA_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
