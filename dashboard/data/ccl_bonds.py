"""
dashboard/data/ccl_bonds.py
=============================
Loader puro y testeado de logs/ccl_bond_quotes.csv (ver
ggal_bot/data/ccl_bond_quote_log.py, MEJORA 2026-09-30) + el UNICO calculo
derivado que hace este proyecto sobre esos datos: CCL implicito =
precio_ARS_del_bono / precio_USD_del_bono_equivalente.

El bot solo registra cotizaciones RAW (bid/ask/ultimo) - nunca calcula ni
publica un CCL ya hecho (ver docstring de ccl_bond_quote_log.py). Este
modulo hace ese calculo del lado del dashboard, con la misma disciplina de
"nunca fabricar" que rige todo el proyecto: un timestamp en el que falta
bid o ask de CUALQUIERA de las dos patas del par se EXCLUYE de la serie,
nunca se interpola ni se completa con el punto anterior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from ggal_bot import paths
from dashboard.data import load_errors

CCL_BOND_QUOTE_COLUMNS = ["timestamp_utc", "symbol", "bid", "ask", "last", "bid_size", "ask_size"]

# (ticker ARS, ticker USD) - mismo par que
# ggal_bot.data.ccl_bond_quote_log.DEFAULT_BOND_TICKERS.
CCL_BOND_PAIRS: Tuple[Tuple[str, str], ...] = (("GD30", "GD30C"), ("AL30", "AL30C"))


def load_ccl_bond_quotes(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Lee logs/ccl_bond_quotes.csv. Tolerante a archivo faltante/vacio - mismo criterio que el resto de dashboard/data/."""
    path = Path(csv_path) if csv_path is not None else paths.CCL_BOND_QUOTES_LOG
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=CCL_BOND_QUOTE_COLUMNS)
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=CCL_BOND_QUOTE_COLUMNS)
    except pd.errors.ParserError as exc:
        # BUG REAL CORREGIDO (2026-10-05, a pedido explicito del usuario -
        # auditoria completa de loaders): antes, silencioso.
        load_errors.register(str(path), f"ParserError: {exc}")
        return pd.DataFrame(columns=CCL_BOND_QUOTE_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=CCL_BOND_QUOTE_COLUMNS)
    if "timestamp_utc" in df.columns:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce", format="ISO8601")
    return df


def compute_ccl_series(df: pd.DataFrame, pair: Tuple[str, str] = ("GD30", "GD30C")) -> pd.DataFrame:
    """
    Serie timestamp_utc/ccl para el par dado, calculada como
    mid(ARS)/mid(USD) EN EL MISMO timestamp_utc (mismo poll - ver
    ccl_bond_quote_log.py::log_records, que loguea las dos patas con el
    mismo timestamp por ciclo). Un timestamp al que le falta bid/ask de
    cualquiera de las dos patas se excluye (inner join + dropna) - nunca
    se fabrica un punto. Devuelve columnas ["timestamp_utc", "ccl"],
    vacio si `df` esta vacio o el par no tiene ningun timestamp en comun.
    """
    ars_ticker, usd_ticker = pair
    empty = pd.DataFrame(columns=["timestamp_utc", "ccl"])
    if df is None or df.empty:
        return empty

    ars = df[df["symbol"] == ars_ticker][["timestamp_utc", "bid", "ask"]].copy()
    usd = df[df["symbol"] == usd_ticker][["timestamp_utc", "bid", "ask"]].copy()
    if ars.empty or usd.empty:
        return empty

    ars["mid_ars"] = (ars["bid"] + ars["ask"]) / 2.0
    usd["mid_usd"] = (usd["bid"] + usd["ask"]) / 2.0
    merged = pd.merge(
        ars[["timestamp_utc", "mid_ars"]], usd[["timestamp_utc", "mid_usd"]],
        on="timestamp_utc", how="inner",
    )
    merged = merged.dropna(subset=["mid_ars", "mid_usd"])
    merged = merged[merged["mid_usd"] != 0]
    if merged.empty:
        return empty
    merged["ccl"] = merged["mid_ars"] / merged["mid_usd"]
    return merged[["timestamp_utc", "ccl"]].sort_values("timestamp_utc").reset_index(drop=True)


def get_latest_ccl(df: pd.DataFrame, pair: Tuple[str, str] = ("GD30", "GD30C")) -> Optional[float]:
    """Ultimo valor de la serie (o None si esta vacia - nunca fabrica un CCL)."""
    series = compute_ccl_series(df, pair)
    if series.empty:
        return None
    return float(series.iloc[-1]["ccl"])
