"""
dashboard/data/journal.py
===========================
Loader puro y testeado del Position Lifecycle Event Journal
(logs/position_events.csv, ver ggal_bot/portfolio/event_journal.py).

Este modulo NO reimplementa la matematica de reconstruccion de trades
cerrados/posiciones abiertas - reutiliza ggal_bot/backtest/reconstruct.py
tal cual (reconstruct_lifecycle_trades, reconstruct_open_positions). Su
unico trabajo nuevo es convertir las filas crudas de
logs/position_events.csv (schema en INGLES, columnas identicas a
PositionEventJournal._HEADER) al formato de dict que esas funciones
esperan - las claves YA coinciden 1:1 con el formato interno que usa
reconstruct.load_lifecycle_journal_rows para el export en español
(_LIFECYCLE_COLUMN_MAP mapea AL MISMO conjunto de claves), asi que esta
conversion es casi identidad, no una traduccion nueva.

GAP QUE CIERRA (ver REPORT.md, pre-analisis Fase 1 2026-09-30): antes de
este modulo, ningun loader leia el CSV crudo de produccion (schema
ingles) directamente - solo existia el loader del export manual en
español (reconstruct.load_lifecycle_journal_rows). Y ninguna funcion
reconstruia posiciones ABIERTAS con strategy_tag real desde el journal
(reconciliation.py::reconstruct_positions_from_shadow_log lo hace desde
shadow_trades.csv y hardcodea strategy_tag=None, documentado ahi mismo).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from dashboard import pnl_engine as pe
from ggal_bot.backtest import reconstruct as bt_reconstruct
from ggal_bot.backtest.reconstruct import OpenPosition, Trade

# Mismas claves que ggal_bot.portfolio.event_journal.PositionEventJournal._HEADER
# (y que reconstruct._LIFECYCLE_COLUMN_MAP mapea desde el export en español)
JOURNAL_ROW_KEYS = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]


def _clean(v):
    """Nunca fabrica un valor: NaN/None -> "" (string vacio), igual que
    reconstruct.load_lifecycle_journal_rows con celdas faltantes del CSV."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return v


def rows_from_position_events_df(df: pd.DataFrame) -> List[Dict]:
    """
    Convierte un DataFrame YA CARGADO de logs/position_events.csv (formato
    de dashboard.pnl_engine.load_position_events) a la lista de dicts que
    reconstruct.reconstruct_lifecycle_trades/reconstruct_open_positions
    esperan, ordenados ASCENDENTE por timestamp_utc (mismo criterio que
    reconstruct.load_lifecycle_journal_rows: no se confia en el orden del
    archivo/DataFrame, se ordena explicitamente aca).

    Separado de load_journal_rows() para que un llamador que YA leyo el
    CSV una vez (ej. dashboard/app.py, que tambien lo necesita para la
    pestaña "Lifecycle") pueda reusar ese mismo DataFrame en vez de volver
    a leer el archivo del disco - una sola lectura, una sola fuente de
    verdad para "que decia el journal en este refresh del dashboard".
    """
    if df.empty:
        return []
    df = df.sort_values("timestamp_utc", kind="stable")

    rows: List[Dict] = []
    for _, r in df.iterrows():
        ts = r.get("timestamp_utc")
        ts_str = ts.isoformat() if pd.notna(ts) else ""
        rows.append({
            "timestamp_utc": ts_str,
            "event_type": _clean(r.get("event_type")) or "",
            "position_id": str(_clean(r.get("position_id")) or ""),
            "contract_key": _clean(r.get("contract_key")) or "",
            "symbol": _clean(r.get("symbol")) or "",
            "strategy_tag": _clean(r.get("strategy_tag")) or "",
            "side": _clean(r.get("side")) or "",
            "quantity_delta": _clean(r.get("quantity_delta")),
            "quantity_after": _clean(r.get("quantity_after")),
            "price": _clean(r.get("price")),
            "order_client_id": _clean(r.get("order_client_id")) or "",
            "reason": _clean(r.get("reason")) or "",
            "data_unavailable_fields": _clean(r.get("data_unavailable_fields")) or "",
        })
    return rows


def load_journal_rows(csv_path: Optional[Path] = None) -> List[Dict]:
    """
    Lee logs/position_events.csv (via dashboard.pnl_engine.load_position_events,
    ya testeado en test_dashboard_pnl.py - tolerante a archivo faltante o
    vacio) y devuelve rows_from_position_events_df() de ese DataFrame.

    Devuelve [] si el archivo no existe o esta vacio (nunca fabrica filas) -
    el llamador debe mostrar "SIN DATOS" en ese caso, no un grafico vacio.
    """
    return rows_from_position_events_df(pe.load_position_events(csv_path))


def get_closed_trades(
    rows: List[Dict], strategies: Optional[Tuple[str, ...]] = None
) -> Tuple[List[Trade], int, int]:
    """Delgado: reutiliza reconstruct.reconstruct_lifecycle_trades tal cual (fuente unica de verdad, sin logica propia)."""
    return bt_reconstruct.reconstruct_lifecycle_trades(rows, strategies=strategies)


def get_open_positions(
    rows: List[Dict], strategies: Optional[Tuple[str, ...]] = None
) -> Tuple[List[OpenPosition], int, int]:
    """Delgado: reutiliza reconstruct.reconstruct_open_positions (gap cerrado 2026-09-30, ver docstring del modulo)."""
    return bt_reconstruct.reconstruct_open_positions(rows, strategies=strategies)
