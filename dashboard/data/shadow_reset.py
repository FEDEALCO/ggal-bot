"""
dashboard/data/shadow_reset.py
=================================
Separa el PnL del dashboard en "antes" / "despues" del reset administrativo
del shadow (Tarea #27 item 5, 2026-10-01, a pedido explicito del usuario:
"El dashboard y los reportes tienen que separar el PnL antes/despues del
reset"). Contexto completo en ggal_bot/portfolio/event_journal.py
(VALID_EVENT_TYPES, evento "SHADOW_RESET") y run_bot.py::
GgalOptionsBot._perform_shadow_reset: items 1 y 2 del mismo pedido
establecieron que las posiciones shadow abiertas al momento del reset
estaban contaminadas por fills sinteticos y position_id fragmentados por
restarts - el cierre administrativo que el reset ejecuta mezcla, en un solo
fill de cierre a mid, ese arrastre contaminado con lo que vino antes. Sin
este modulo, el PnL Total del dashboard sumaria ambos periodos sin
distincion, ocultando justamente la discontinuidad que motivo el reset.

REGLA (nunca se fabrica nada):
    - Si el journal no tiene ningun evento SHADOW_RESET todavia (el caso
      normal, antes de que el usuario dispare el flag una vez), no hay
      corte: `most_recent_shadow_reset_timestamp` devuelve None y toda la
      funcion de este modulo trata el historial completo como "despues"
      (no hay un "antes" que reportar aparte) - NUNCA se asume un reset
      que no esta en el journal.
    - Si hay mas de un evento SHADOW_RESET (el usuario puede disparar el
      flag en mas de un deploy), se usa el MAS RECIENTE como corte: lo
      anterior a ese es "antes", lo posterior es "despues". Reportar contra
      el reset mas reciente es lo que importa para evaluar la estrategia
      "de aca en adelante" - si se necesita el detalle de resets
      intermedios, estan todos en logs/position_events.csv (el boton de
      descarga de la sidebar del dashboard expone el CSV crudo tal cual).
    - El corte es por `exit_time` (el momento en que CADA trade cerro, no
      cuando se abrio) - un trade que abrio antes del reset y cerro
      despues (no deberia poder pasar: el reset cierra TODO lo abierto al
      momento, ver _perform_shadow_reset) igual se contaria como
      "despues", que es lo correcto: su PnL realizado se concreto despues
      del corte.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

BEFORE_RESET_LABEL = "antes_reset"
AFTER_RESET_LABEL = "despues_reset"
NO_RESET_LABEL = "unico_periodo"  # todavia no hubo ningun SHADOW_RESET en el journal


def most_recent_shadow_reset_timestamp(position_events_df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """
    Timestamp (UTC, tz-aware) del evento SHADOW_RESET mas reciente del
    journal (dashboard.pnl_engine.load_position_events), o None si todavia
    no hubo ninguno. None es la respuesta esperada en la gran mayoria de
    los refrescos del dashboard - nunca se fabrica un timestamp cuando no
    hay evento real.
    """
    if position_events_df is None or position_events_df.empty:
        return None
    if "event_type" not in position_events_df.columns or "timestamp_utc" not in position_events_df.columns:
        return None
    resets = position_events_df[position_events_df["event_type"] == "SHADOW_RESET"]
    if resets.empty:
        return None
    ts = resets["timestamp_utc"].dropna()
    if ts.empty:
        return None
    return ts.max()


def tag_closed_trades_with_reset_period(
    closed_df: pd.DataFrame, reset_timestamp: Optional[pd.Timestamp],
) -> pd.DataFrame:
    """
    Devuelve una copia de closed_df (dashboard.pnl_engine.closed_trades_to_frame)
    con una columna nueva "reset_period": BEFORE_RESET_LABEL/AFTER_RESET_LABEL
    segun `exit_time` relativo a `reset_timestamp`, o NO_RESET_LABEL para
    todas las filas si `reset_timestamp` es None (todavia no hubo reset).
    No modifica closed_df in-place.
    """
    if closed_df is None or closed_df.empty:
        tagged = closed_df.copy() if closed_df is not None else closed_df
        if tagged is not None:
            tagged["reset_period"] = pd.Series(dtype=object)
        return tagged

    tagged = closed_df.copy()
    if reset_timestamp is None:
        tagged["reset_period"] = NO_RESET_LABEL
    else:
        tagged["reset_period"] = tagged["exit_time"].apply(
            lambda t: BEFORE_RESET_LABEL if pd.notna(t) and t <= reset_timestamp else AFTER_RESET_LABEL
        )
    return tagged


def split_closed_trades_by_reset(
    closed_df: pd.DataFrame, reset_timestamp: Optional[pd.Timestamp],
) -> Dict[str, pd.DataFrame]:
    """
    Parte closed_df en {"before": ..., "after": ...} segun el corte del
    reset mas reciente. Si reset_timestamp es None (sin reset todavia),
    "before" queda vacio (mismas columnas que closed_df) y "after" es
    closed_df entero - no hay un "antes" que reportar cuando no hubo
    ningun reset real.
    """
    if closed_df is None or closed_df.empty:
        empty = closed_df.iloc[0:0] if closed_df is not None else closed_df
        return {"before": empty, "after": closed_df}

    tagged = tag_closed_trades_with_reset_period(closed_df, reset_timestamp)
    before = tagged[tagged["reset_period"] == BEFORE_RESET_LABEL].drop(columns=["reset_period"])
    after = tagged[tagged["reset_period"] != BEFORE_RESET_LABEL].drop(columns=["reset_period"])
    return {"before": before, "after": after}


def summarize_pnl_before_after_reset(
    closed_df: pd.DataFrame,
    open_positions_marked: pd.DataFrame,
    reset_timestamp: Optional[pd.Timestamp],
) -> Dict[str, Any]:
    """
    Resumen listo para mostrar en el dashboard (KPIs). El PnL NO realizado
    siempre se reporta como "despues" del reset: un SHADOW_RESET cierra
    TODAS las posiciones shadow abiertas en ese momento al mid vigente (ver
    _perform_shadow_reset) - cualquier posicion abierta HOY, por
    definicion, se abrio despues del ultimo reset (o nunca hubo reset).
    """
    split = split_closed_trades_by_reset(closed_df, reset_timestamp)
    before, after = split["before"], split["after"]

    pnl_before = float(before["pnl_ars"].sum()) if before is not None and not before.empty else 0.0
    pnl_after_realized = float(after["pnl_ars"].sum()) if after is not None and not after.empty else 0.0
    pnl_unrealized = (
        float(open_positions_marked["pnl_ars"].sum())
        if open_positions_marked is not None and not open_positions_marked.empty
        else 0.0
    )

    return {
        "has_reset": reset_timestamp is not None,
        "reset_timestamp": reset_timestamp,
        "n_trades_before": int(len(before)) if before is not None else 0,
        "n_trades_after": int(len(after)) if after is not None else 0,
        "pnl_realized_before_ars": pnl_before,
        "pnl_realized_after_ars": pnl_after_realized,
        "pnl_unrealized_ars": pnl_unrealized,
        "pnl_after_total_ars": pnl_after_realized + pnl_unrealized,
    }
