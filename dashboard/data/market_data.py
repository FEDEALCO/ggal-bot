"""
dashboard/data/market_data.py
===============================
Loader puro y testeado de logs/market_snapshots.csv (ver
ggal_bot/data/market_snapshot_log.py::MarketSnapshotLogger, deployado
2026-09-28, commit d38eea7).

Tolerante a archivo faltante/vacio - MISMO criterio que
dashboard/pnl_engine.py::load_fills/load_position_events. NUNCA fabrica
una fila: si el archivo esta vacio o no existe, devuelve un DataFrame
vacio con las columnas correctas, para que el panel lo muestre como
"SIN DATOS" explicito (con el motivo) en vez de un grafico vacio sin
explicacion.

ESTADO REAL (2026-09-30): en este sandbox el archivo local es solo header
(0 filas) - artefacto de prueba, no dato sincronizado de Northflank. El
estado real en produccion esta sin confirmar (el usuario dijo que lo iba
a chequear).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from ggal_bot import paths

# Mismo header que MarketSnapshotLogger._HEADER (ggal_bot/data/market_snapshot_log.py)
MARKET_SNAPSHOT_COLUMNS = [
    "timestamp_utc", "symbol", "option_type", "strike", "expiry",
    "days_calendar", "days_business", "spot_ref", "bid", "ask",
    "bid_size", "ask_size", "iv", "delta", "gamma", "vega", "theta",
]


def load_market_snapshots(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Lee logs/market_snapshots.csv. Nunca lanza excepcion por archivo faltante/corrupto - devuelve vacio en su lugar."""
    path = Path(csv_path) if csv_path is not None else paths.MARKET_SNAPSHOT_LOG
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=MARKET_SNAPSHOT_COLUMNS)

    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=MARKET_SNAPSHOT_COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=MARKET_SNAPSHOT_COLUMNS)

    if "timestamp_utc" in df.columns:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce", format="ISO8601")
    return df
