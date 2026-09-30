"""
dashboard/data/funnel.py
==========================
Loader puro y testeado de logs/signal_funnel.csv (ver
ggal_bot/data/signal_funnel_log.py::SignalFunnelLogger, MEJORA 2026-09-29).

Tolerante a archivo faltante/vacio, mismo criterio que el resto de este
paquete. Feature opt-in (GGAL_BOT_ENABLE_SIGNAL_FUNNEL_LOG /
GGAL_BOT_SCALPING_ENABLE_SIGNAL_FUNNEL_LOG, ambas apagadas por defecto) -
en la mayoria de los deploys esto va a devolver vacio hasta que el
usuario active los env vars, y el panel de "Embudo de señales" debe
mostrar "SIN DATOS" en ese caso, nunca un embudo vacio sin explicacion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from ggal_bot import paths

# Mismo header que SignalFunnelLogger._HEADER (ggal_bot/data/signal_funnel_log.py)
SIGNAL_FUNNEL_COLUMNS = [
    "timestamp_utc", "strategy", "symbol", "option_type", "strike", "expiry",
    "days_business", "spot_ref", "bid", "ask", "bid_size", "ask_size",
    "spread_abs", "spread_relative", "iv", "delta", "gamma", "vega", "theta",
    "dislocation_vol_points", "blocked_at",
]


def load_signal_funnel(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Lee logs/signal_funnel.csv. Nunca lanza excepcion por archivo faltante/corrupto - devuelve vacio en su lugar."""
    path = Path(csv_path) if csv_path is not None else paths.SIGNAL_FUNNEL_LOG
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=SIGNAL_FUNNEL_COLUMNS)

    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=SIGNAL_FUNNEL_COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=SIGNAL_FUNNEL_COLUMNS)

    if "timestamp_utc" in df.columns:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce", format="ISO8601")
    return df
