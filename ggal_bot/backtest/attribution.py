"""
attribution.py
================
Diagnostico del PnL BRUTO por estrategia (Fase 0, correccion pedida por el
usuario tras el reporte inicial: "dos de tres estrategias pierden antes de
costos, asi que ningun filtro nuevo lo arregla" - hay que entender POR QUE
antes de proponer ninguna mejora nueva).

Parte del PnL de CADA trade ya cerrado (ver reconstruct.Trade) y lo agrupa
por:
    1. Motivo de salida (bucket_close_reason): stop / take_profit / timeout / otro.
    2. Moneyness al momento de la ENTRADA (requiere precio de subyacente real,
       ver ggal_bot/backtest/data/ggal_underlying_daily_*.csv).
    3. Dias al vencimiento (DTE) al momento de la entrada (requiere Contract Key
       con fecha de vencimiento real - SOLO disponible en el export de
       lifecycle journal, weekly_asymmetric/scalping).
    4. Tiempo de tenencia (holding time).
    5. Hora del dia de la entrada (en ART, UTC-3, para que sea legible contra
       el horario de rueda de BYMA).

NUNCA fabrica un dato que falte: si una estrategia no tiene el campo
necesario para un corte (ej. vol_arbitrage no tiene Contract Key -> no hay
fecha de vencimiento real -> no se puede cortar por DTE), esa combinacion
se devuelve con una bandera `data_insufficient=True` y una razon explicita,
en vez de inventar o aproximar el dato.

Todo esto es un diagnostico del PnL BRUTO (antes de costos) - la pregunta
que responde es "el bot tiene un edge bruto real, y donde esta / donde no
esta", no "cuanto queda despues de comisiones" (eso ya esta en metrics.py).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ggal_bot.backtest.reconstruct import Trade

_ART_OFFSET = timedelta(hours=-3)  # Argentina, sin horario de verano

_SYMBOL_RE = re.compile(r"^GFG([CV])(\d+)[A-Z]*$")

# --- Mapeo explicito de motivo de salida -> bucket (leer antes de confiar en el numero) ---
# Basado en los motivos REALMENTE vistos en el export de lifecycle journal
# (ver conversacion, Counter sobre 421 CLOSE rows). Cualquier motivo no
# listado aca cae en "otro" (nunca se descarta ni se fuerza a una categoria
# que no le corresponde).
_CLOSE_REASON_BUCKETS: Dict[str, str] = {
    "stop_loss": "stop",
    "scalping_take_profit": "take_profit",
    "scalping_iv_mean_reversion": "take_profit",  # la posicion se cierra al capturar la reversion de IV esperada - funcionalmente un take-profit
    "weekend_theta_guard": "timeout",  # cierre forzado por horizonte temporal (guardia de theta de fin de semana), no por señal de salida
    "scalping_eod_close": "timeout",  # cierre forzado de fin de dia
    "scalping_no_progress": "otro",
    "vega_theta_decay": "otro",
}


def bucket_close_reason(reason: Optional[str]) -> str:
    """
    Clasifica un motivo de salida crudo en {stop, take_profit, timeout, otro}.
    Un motivo no reconocido cae en "otro" (nunca se fuerza a una de las otras
    3 categorias sin evidencia) - se recomienda revisar `unmapped_reasons()`
    sobre la muestra real antes de confiar en la fila "otro" de la tabla.
    """
    if reason is None:
        return "otro"
    return _CLOSE_REASON_BUCKETS.get(reason, "otro")


def unmapped_close_reasons(trades: Sequence[Trade]) -> Dict[str, int]:
    """Motivos de salida vistos en `trades` que NO estan en _CLOSE_REASON_BUCKETS (para auditar el mapeo, nunca ocultarlo)."""
    counts: Dict[str, int] = {}
    for t in trades:
        if t.close_reason and t.close_reason not in _CLOSE_REASON_BUCKETS:
            counts[t.close_reason] = counts.get(t.close_reason, 0) + 1
    return counts


def parse_option_symbol(symbol: str) -> Optional[tuple]:
    """
    Extrae (tipo, strike) de un ticker de opcion GGAL (ej. "GFGC7000OC" ->
    ("call", 7000.0), "GFGV6400I" -> ("put", 6400.0)). Devuelve None si el
    simbolo no matchea el patron conocido (nunca fabrica un strike).
    """
    m = _SYMBOL_RE.match(symbol)
    if not m:
        return None
    tipo = "call" if m.group(1) == "C" else "put"
    strike = float(m.group(2))
    return tipo, strike


def parse_contract_key_expiry(contract_key: Optional[str]) -> Optional[date]:
    """
    Extrae la fecha de vencimiento real de un Contract Key con formato
    "SUBYACENTE|SIMBOLO|YYYY-MM-DD" (tal como lo exporta el lifecycle
    journal). Devuelve None si el campo esta vacio o no tiene ese formato -
    NUNCA fabrica una fecha de vencimiento.
    """
    if not contract_key:
        return None
    parts = contract_key.split("|")
    if len(parts) != 3:
        return None
    try:
        return date.fromisoformat(parts[2])
    except ValueError:
        return None


def load_spot_closes_csv(path: Path) -> Dict[date, float]:
    """
    Carga un CSV de precios diarios reales del subyacente (ver
    ggal_bot/backtest/data/ggal_underlying_daily_*.csv - fuente citada en el
    encabezado del propio archivo). Formato: date,open,high,low,close,volume,
    con lineas de comentario "#" ignoradas.
    """
    out: Dict[date, float] = {}
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
        d = date.fromisoformat(row["date"])
        out[d] = float(row["close"])
    return out


def nearest_close_on_or_before(spot_by_date: Dict[date, float], d: date, max_lookback_days: int = 5) -> Optional[float]:
    """
    Precio de cierre real del dia habil mas cercano EN O ANTES de `d` (nunca
    despues - evita usar informacion posterior a la entrada del trade).
    Busca hasta `max_lookback_days` hacia atras (para saltar fines de semana
    / feriados). Devuelve None si no encuentra nada en ese rango (no se
    fabrica un precio).
    """
    for i in range(max_lookback_days + 1):
        candidate = d - timedelta(days=i)
        if candidate in spot_by_date:
            return spot_by_date[candidate]
    return None


@dataclass
class AttributionBucket:
    label: str
    n: int
    gross_pnl_sum_ars: float
    gross_pnl_mean_ars: Optional[float]
    win_rate_gross_pct: Optional[float]


def _bucketize(trades: Sequence[Trade], key_fn) -> List[AttributionBucket]:
    """
    Agrupa `trades` por `key_fn(trade) -> Optional[str]` (None = no se pudo
    clasificar ese trade, se excluye del resultado y debe reportarse aparte
    por el llamador via el conteo total vs. suma de n en los buckets).
    """
    groups: Dict[str, List[Trade]] = {}
    for t in trades:
        label = key_fn(t)
        if label is None:
            continue
        groups.setdefault(label, []).append(t)

    buckets = []
    for label, ts in groups.items():
        gross = [t.pnl_gross_ars for t in ts]
        n = len(gross)
        wins = sum(1 for g in gross if g > 0)
        buckets.append(AttributionBucket(
            label=label, n=n,
            gross_pnl_sum_ars=sum(gross),
            gross_pnl_mean_ars=(sum(gross) / n) if n else None,
            win_rate_gross_pct=(wins / n * 100.0) if n else None,
        ))
    return buckets


def attribute_by_close_reason(trades: Sequence[Trade]) -> List[AttributionBucket]:
    """DATA INSUFFICIENT para trades sin close_reason (ej. vol_arbitrage: el export de cierres no trae motivo de salida) - esos trades no aparecen en ningun bucket."""
    return _bucketize(trades, lambda t: bucket_close_reason(t.close_reason) if t.close_reason is not None else None)


_MONEYNESS_BUCKET_EDGES = (-0.05, -0.02, 0.02, 0.05)  # limites de moneyness_pct (positivo = ITM)


def _moneyness_bucket_label(moneyness_pct: float) -> str:
    if moneyness_pct < _MONEYNESS_BUCKET_EDGES[0]:
        return "OTM >5%"
    if moneyness_pct < _MONEYNESS_BUCKET_EDGES[1]:
        return "OTM 2-5%"
    if moneyness_pct <= _MONEYNESS_BUCKET_EDGES[2]:
        return "ATM (±2%)"
    if moneyness_pct <= _MONEYNESS_BUCKET_EDGES[3]:
        return "ITM 2-5%"
    return "ITM >5%"


def attribute_by_moneyness(trades: Sequence[Trade], spot_by_date: Dict[date, float]) -> List[AttributionBucket]:
    """
    Moneyness definido de forma uniforme para calls y puts: positivo = ITM,
    negativo = OTM, expresado como % del strike. Usa el precio de cierre
    REAL del dia habil en o antes de la fecha de ENTRADA de cada trade (ver
    nearest_close_on_or_before - nunca informacion posterior a la entrada).

    DATA INSUFFICIENT (trade excluido, no fabricado) si: el simbolo no
    matchea el patron de opcion conocido, no hay fecha de entrada, o no hay
    un precio de cierre real disponible en la ventana de busqueda.
    """
    def key_fn(t: Trade) -> Optional[str]:
        parsed = parse_option_symbol(t.symbol)
        if parsed is None or t.opened_at is None:
            return None
        option_type, strike = parsed
        spot = nearest_close_on_or_before(spot_by_date, t.opened_at.date())
        if spot is None or strike <= 0:
            return None
        raw_moneyness = (spot - strike) / strike
        moneyness_pct = raw_moneyness if option_type == "call" else -raw_moneyness
        return _moneyness_bucket_label(moneyness_pct)

    return _bucketize(trades, key_fn)


_DTE_BUCKET_EDGES = (3, 7, 14)  # dias corridos


def _dte_bucket_label(dte_days: int) -> str:
    if dte_days <= _DTE_BUCKET_EDGES[0]:
        return "0-3d"
    if dte_days <= _DTE_BUCKET_EDGES[1]:
        return "4-7d"
    if dte_days <= _DTE_BUCKET_EDGES[2]:
        return "8-14d"
    return "15+d"


def attribute_by_dte(trades: Sequence[Trade]) -> List[AttributionBucket]:
    """
    Dias al vencimiento (calendario) al momento de la ENTRADA, usando la
    fecha real de vencimiento del Contract Key. DATA INSUFFICIENT (trade
    excluido) para exports que no traen Contract Key (vol_arbitrage) o filas
    con Contract Key vacio/malformado.
    """
    def key_fn(t: Trade) -> Optional[str]:
        expiry = parse_contract_key_expiry(t.contract_key)
        if expiry is None or t.opened_at is None:
            return None
        dte = (expiry - t.opened_at.date()).days
        if dte < 0:
            return None  # dato inconsistente (vencimiento antes de la entrada) - no se fuerza a un bucket
        return _dte_bucket_label(dte)

    return _bucketize(trades, key_fn)


def attribute_by_holding_time(trades: Sequence[Trade]) -> List[AttributionBucket]:
    """Bucket por tiempo de tenencia real (opened_at -> closed_at). DATA INSUFFICIENT (excluido) si falta alguna de las dos fechas."""
    def key_fn(t: Trade) -> Optional[str]:
        secs = t.holding_seconds
        if secs is None:
            return None
        hours = secs / 3600.0
        if hours < 1:
            return "<1h"
        if hours < 6:
            return "1-6h"
        if hours < 24:
            return "6-24h"
        if hours < 72:
            return "1-3d"
        return ">3d"

    return _bucketize(trades, key_fn)


def attribute_by_entry_hour_art(trades: Sequence[Trade]) -> List[AttributionBucket]:
    """
    Bucket por HORA de entrada en horario de Argentina (ART, UTC-3, sin
    horario de verano) - los timestamps del export estan en UTC. DATA
    INSUFFICIENT (excluido) si falta opened_at.
    """
    def key_fn(t: Trade) -> Optional[str]:
        if t.opened_at is None:
            return None
        ts = t.opened_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        art = ts.astimezone(timezone(_ART_OFFSET))
        return f"{art.hour:02d}h ART"

    return _bucketize(trades, key_fn)
