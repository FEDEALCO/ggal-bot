"""
market_hours_quality.py
==========================
Flags y metricas de calidad de datos para fills/trades ejecutados fuera del
horario asumido de rueda BYMA (MEJORA 2026-10-01, URGENTE a pedido explicito
del usuario).

Contexto verificado contra produccion (ver ggal_bot/market_hours.py y
RiskConfig.enforce_market_hours_gate para el fix del lado del bot): 247 de
1183 fills en shadow_trades.csv tienen timestamp fuera de 11:00-17:00 ART
(incluso fin de semana). Causa raiz: LiveShadowFeed cae automaticamente a
MockReplaySource (failover disenado para una fuente real caida, no para
"no hay rueda ahora mismo") despues de 3 polls vacios consecutivos -
MockReplaySource genera cotizaciones sinteticas 24/7 sin ninguna nocion de
horario de mercado, y el bot las usaba para evaluar y EJECUTAR salidas
(take_profit/stop_loss) tambien fuera de horario.

Caso concreto verificado linea por linea contra shadow_trades.csv y
position_events.csv (sesion 2026-10-01): GFGC6600OC, ENTRY 16 contratos @
119.00 el 2026-09-28 14:05:53 UTC (position_id 8073aff38d40, dentro de
horario), CLOSE 16 contratos @ 382.00 el 2026-09-29 13:28:32 UTC = 10:28:32
ART (position_id afed1e5f148c, FUERA de horario, motivo "take_profit").
(382.00 - 119.00) * 16 * 100 = ARS 420.800 - coincide, dentro del margen de
la estimacion aproximada del usuario, con "~+420k ARS de ganancia ficticia".

Esta historia tiene una SEGUNDA causa, compuesta e independiente, que este
modulo NO corrige (ver docstring de match_trades_fifo en dashboard/pnl_engine.py
y el hallazgo documentado en la sesion 2026-10-01): el CLOSE de esa posicion
quedo logueado en el Event Journal con un position_id DISTINTO al de su
propio ENTRY (orfano de entrada propia, aunque el ENTRY si existe en el
journal) - por eso match_trades_fifo() nunca neteo ambas patas como UN
trade cerrado, y en cambio las dejo como DOS lotes abiertos sin aparear (uno
largo a 119, uno corto a 382) que mark_to_market() sigue marcando a mercado
indefinidamente - de ahi que la "ganancia ficticia" aparezca como PnL NO
REALIZADO persistente en el panel de posiciones abiertas de weekly_asymmetric,
no como un trade cerrado puntual. Se verifico que este patron (CLOSE con
position_id huerfano de su propio ENTRY, ambos posteriores al deploy del
journal 2026-09-07 17:05 UTC) no es unico de este caso: aparecio tambien en
GFGC6400OC (stop_loss, 2026-09-30) y GFGV5600OC (stop_loss, 2026-09-30). Esto
se reporta aca como hallazgo nuevo, pendiente de investigacion y fix
separados - no se "arregla" silenciosamente reasignando position_id's sin
evidencia de cual es el emparejamiento correcto en CADA caso.

Este modulo es SOLO DE LECTURA/DIAGNOSTICO: agrega columnas booleanas
"fuera de horario" y cifras agregadas para que el dashboard se lo muestre
al usuario con transparencia total. NO modifica pnl_ars/current_price ni
ningun otro valor ya calculado por pnl_engine.py - hacerlo sin pedido
explicito seria fabricar un resultado distinto al realmente registrado.
"""
from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from ggal_bot.market_hours import is_within_byma_session


def _is_outside(ts: Any) -> bool:
    """
    True si `ts` (Timestamp/datetime, se asume UTC si es naive - ver
    is_within_byma_session) cae fuera del horario de rueda asumido.
    None/NaT se trata como "no fuera de horario" (no hay evidencia para
    marcarlo, y no es el caso que este modulo necesita señalar).
    """
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or pd.isna(ts):
        return False
    py_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return not is_within_byma_session(py_ts)


def flag_fills_outside_session(fills: pd.DataFrame, ts_col: str = "timestamp_utc") -> pd.Series:
    """
    Serie booleana alineada al indice de `fills`: True si el timestamp del
    fill cae fuera de la rueda asumida (11:00-17:00 ART, lun-vie - SUPUESTO
    explicito NO verificado, ver ggal_bot/market_hours.py).
    """
    if fills.empty or ts_col not in fills.columns:
        return pd.Series([], dtype=bool, index=fills.index)
    return fills[ts_col].apply(_is_outside)


def flag_closed_trades_outside_session(closed_df: pd.DataFrame) -> pd.DataFrame:
    """
    Devuelve una COPIA de `closed_df` (ver pe.closed_trades_to_frame) con 3
    columnas booleanas nuevas:
      - entry_outside_session: la entrada se ejecuto fuera de horario.
      - exit_outside_session: la salida se ejecuto fuera de horario.
      - any_leg_outside_session: OR de las dos anteriores (criterio usado
        para la cuantificacion agregada - "al menos una pata fuera de
        horario" -, verificado contra produccion el 2026-10-01: 127 de 612
        trades cerrados).
    No toca pnl_ars ni ninguna otra columna existente.
    """
    if closed_df.empty:
        out = closed_df.copy()
        for col in ("entry_outside_session", "exit_outside_session", "any_leg_outside_session"):
            out[col] = pd.Series(dtype=bool)
        return out
    out = closed_df.copy()
    out["entry_outside_session"] = out["entry_time"].apply(_is_outside)
    out["exit_outside_session"] = out["exit_time"].apply(_is_outside)
    out["any_leg_outside_session"] = out["entry_outside_session"] | out["exit_outside_session"]
    return out


def flag_open_positions_outside_session(open_positions_df: pd.DataFrame) -> pd.DataFrame:
    """
    Mismo criterio que flag_closed_trades_outside_session, pero para
    posiciones abiertas (solo tienen entry_time - ver
    pe.aggregate_open_positions, no hay exit_time porque no cerraron).
    """
    if open_positions_df.empty:
        out = open_positions_df.copy()
        out["entry_outside_session"] = pd.Series(dtype=bool)
        return out
    out = open_positions_df.copy()
    out["entry_outside_session"] = out["entry_time"].apply(_is_outside)
    return out


def summarize_outside_session_impact(flagged_closed_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Resumen agregado para el banner de alerta del dashboard: cuantos trades
    cerrados tienen al menos una pata fuera de horario y cuanto PnL
    (realizado) suman esos trades.

    IMPORTANTE (honestidad de los numeros, no fabricar una lectura que los
    datos no sostienen): esta cifra es una COTA de EXPOSICION a datos fuera
    de horario, no "PnL fabricado" 1:1 - incluye trades donde la unica
    anomalia es el timestamp de una pata (ej. un cierre forzado por el
    weekend guard a un precio por lo demas razonable), junto con casos como
    el de GFGC6600OC donde el precio mismo esta claramente fabricado. Para
    aislar ESE caso puntual hay que mirar el trade individual (ver
    docstring del modulo).

    Requiere que `flagged_closed_df` ya haya pasado por
    flag_closed_trades_outside_session (si no tiene la columna
    "any_leg_outside_session", devuelve todo en cero en vez de fallar).
    """
    if flagged_closed_df.empty or "any_leg_outside_session" not in flagged_closed_df.columns:
        return {"n_total": 0, "n_outside": 0, "pnl_total_ars": 0.0, "pnl_outside_ars": 0.0}
    mask = flagged_closed_df["any_leg_outside_session"]
    return {
        "n_total": int(len(flagged_closed_df)),
        "n_outside": int(mask.sum()),
        "pnl_total_ars": float(flagged_closed_df["pnl_ars"].sum()),
        "pnl_outside_ars": float(flagged_closed_df.loc[mask, "pnl_ars"].sum()),
    }
