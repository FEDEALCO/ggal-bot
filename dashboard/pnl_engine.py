"""
pnl_engine.py
==============
Motor de consolidacion de PnL para el dashboard (dashboard/app.py). Lee
logs/shadow_trades.csv (el mismo CSV que escribe
ggal_bot.execution.order_gateway.ShadowAuditLogger en modo Shadow Trading)
y state/bot_state.json (el mismo JSON que escribe ggal_bot.state_writer,
ahora incluyendo un snapshot de la cadena de opciones - ver run_bot.py,
_option_chain_snapshot()), y produce:

    - Trades cerrados: aparea compras y ventas del mismo simbolo con una
      cola FIFO (deque por simbolo) para calcular el PnL realizado de cada
      round-trip.
    - Posiciones abiertas: lo que quedo sin aparear, marcado a mercado con
      la ultima cotizacion vigente del snapshot (o None si esa base ya no
      esta en la cadena vigente - ej. vencio o rodo fuera del universo).
    - Metricas de portafolio: PnL total (realizado + no realizado), win
      rate, profit factor, curva de equity, Sharpe aproximado y max
      drawdown.

IMPORTANTE - alcance y limitaciones (leer antes de confiar en los numeros):

    1. Esto consolida el PnL de las ordenes que EL BOT genero (via
       order_gateway.py), no una conciliacion contra el estado de cuenta
       real de un ALYC. En modo Shadow Trading no existe tal cuenta (ver
       ggal_bot/data/live_shadow_feed.py); en modo real, el PnL "de verdad"
       siempre debe validarse contra get_account_positions() y los
       resumenes de cuenta del broker, no solo contra este CSV.
    2. La clasificacion de estrategia (vol_arbitrage vs delta_hedge) es
       DEDUCIDA por el simbolo, no un campo persistido: en la arquitectura
       actual del bot, la UNICA fuente de ordenes sobre el subyacente/futuro
       es DeltaHedgingEngine (run_bot._maybe_hedge) y la UNICA fuente de
       ordenes sobre opciones es VolatilityArbitrageStrategy
       (run_bot._act_on_signal) - por eso la deduccion es confiable HOY,
       pero dejaria de serlo si en el futuro se agrega una estrategia
       adicional que tambien opere opciones directamente.
    3. Sharpe aproximado: se calcula sobre la serie de retornos por trade
       cerrado (pnl_pct), SIN anualizar (la frecuencia de trades de este
       bot es demasiado irregular para una anualizacion estandar). Sirve
       para comparar configuraciones entre si, no como un Sharpe ratio
       anualizado comparable con benchmarks tradicionales.
    4. PnL % por trade se calcula sobre el "nocional" de esa pata
       (precio_entrada * cantidad * multiplicador), una aproximacion
       practica - las opciones vendidas en descubierto no tienen un
       concepto de "capital invertido" tan limpio como una accion larga.
    5. El multiplicador se resuelve POR SIMBOLO (ver multiplier_for_symbol()):
       1.0 para el subyacente/futuro de GGAL (acciones, sin multiplicador de
       contrato), `SETTINGS.instruments.option_multiplier` (100) para
       cualquier opcion. Antes de esta correccion se aplicaba un unico
       multiplicador global a TODOS los simbolos, lo que inflaba x100 el
       PnL de cada pata de delta-hedge sobre el subyacente - en una sesion
       con rehedgeos frecuentes, eso dominaba el PnL Total mostrado en el
       dashboard (bug real reportado por el usuario: PnL Total de ~$1.670
       millones sobre un CSV que solo sostenia unos pocos millones de PnL
       realizado). Ver test_dashboard_pnl.py.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ggal_bot.config import SETTINGS
from ggal_bot.paths import SHADOW_TRADES_LOG, STATE_FILE

FILLS_COLUMNS = [
    "timestamp_utc", "client_order_id", "symbol", "side", "order_type",
    "quantity", "requested_price", "fill_price", "reference_price", "event",
]


# ---------------------------------------------------------------------------
# Carga de datos crudos
# ---------------------------------------------------------------------------

def load_fills(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Lee logs/shadow_trades.csv y devuelve solo las filas de fill (event ==
    'shadow_fill'), ordenadas cronologicamente. Tolerante a que el archivo
    todavia no exista (el bot nunca corrio) o este vacio.
    """
    path = Path(csv_path) if csv_path is not None else SHADOW_TRADES_LOG
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=FILLS_COLUMNS)

    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=FILLS_COLUMNS)

    if df.empty or "event" not in df.columns:
        return pd.DataFrame(columns=FILLS_COLUMNS)

    df = df[df["event"] == "shadow_fill"].copy()
    if df.empty:
        return df

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    for col in ("quantity", "fill_price", "reference_price", "requested_price"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp_utc", "quantity", "fill_price", "symbol", "side"])
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    return df


def load_bot_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Lee state/bot_state.json (ver ggal_bot/state_writer.py). Devuelve {} si
    el archivo todavia no existe o esta a medio escribir (el bot lo escribe
    de forma atomica - tmp + replace -, asi que esto solo pasa en una
    ventana de carrera muy angosta; el proximo refresh del dashboard lo
    vuelve a leer bien).
    """
    state_path = Path(path) if path is not None else STATE_FILE
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Clasificacion de estrategia (ver nota 2 en el docstring del modulo)
# ---------------------------------------------------------------------------

def _underlying_symbol_aliases() -> frozenset:
    """
    Todos los "alias" de simbolo conocidos para el subyacente/futuro de GGAL
    - no solo el ticker completo y calificado (`contado_ticker`,
    "MERV - XMEV - GGAL - 24hs"), sino tambien el ticker corto
    (`underlying_symbol`, "GGAL") que puede llegar asi desde otra fuente de
    datos o de un fill simulado en tests.

    BUG REAL CORREGIDO (auditoria del 2026-08-27, ver
    docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.6): classify_strategy() y
    multiplier_for_symbol() comparaban el simbolo por IGUALDAD EXACTA contra
    un unico string (`cfg.contado_ticker`). Un fill que llegara con el
    simbolo corto "GGAL" (en vez del ticker completo) no matcheaba nada, y
    caia por default al multiplicador de OPCIONES (100) en vez de 1.0 -
    reintroduciendo, para esa variante de simbolo, exactamente el mismo bug
    x100 que `multiplier_for_symbol()` ya habia corregido para el ticker
    canonico (ver docstring de esa funcion). Fix: comparar contra un
    CONJUNTO de alias conocidos del subyacente, no un unico string.
    """
    cfg = SETTINGS.instruments
    return frozenset(s for s in (cfg.contado_ticker, cfg.futuro_ticker, cfg.underlying_symbol) if s)


def _is_underlying_symbol(symbol: str) -> bool:
    return symbol in _underlying_symbol_aliases()


def classify_strategy(symbol: str) -> str:
    if _is_underlying_symbol(symbol):
        return "delta_hedge"
    return "vol_arbitrage"


def multiplier_for_symbol(symbol: str, option_multiplier: Optional[float] = None) -> float:
    """
    1.0 para el subyacente/futuro (acciones/futuro de GGAL: sin multiplicador
    de contrato de opcion), `option_multiplier` (default: SETTINGS.instruments.
    option_multiplier, 100) para cualquier otro simbolo (una opcion).

    BUG REAL CORREGIDO ACA (reportado por el usuario via el dashboard: PnL
    Total mostraba ~$1.670 millones cuando el CSV de fills solo sostenia
    unos pocos millones de PnL realizado): antes, match_trades_fifo() y
    mark_to_market() aplicaban un UNICO multiplicador global (el de
    opciones, 100) a TODOS los simbolos - incluidas las patas de
    delta-hedge sobre el subyacente (`SETTINGS.instruments.contado_ticker`,
    "MERV - XMEV - GGAL - 24hs"), que son acciones, no contratos de
    opciones de 100 unidades. Eso inflaba x100 el PnL (realizado Y no
    realizado) de CADA pata de delta-hedge, que en un dia de rehedgeos
    frecuentes domina el total. Ver test_dashboard_pnl.py,
    test_multiplier_for_symbol_* y test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs.

    Ver tambien _underlying_symbol_aliases() arriba: la comparacion original
    (un unico string exacto) tenia el mismo bug de clase para variantes de
    simbolo del subyacente (ej. "GGAL" a secas) - ya corregido aca.
    """
    default = option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier
    if _is_underlying_symbol(symbol):
        return 1.0
    return default


def classify_option_type(symbol: str) -> str:
    """Call/Put/Subyacente, por prefijo de simbolo - solo para filtros del dashboard."""
    cfg = SETTINGS.instruments
    if _is_underlying_symbol(symbol):
        return "subyacente"
    bare = symbol.split(" - ")[2].strip() if symbol.count(" - ") >= 2 else symbol
    if bare.startswith(cfg.call_prefix):
        return "call"
    if bare.startswith(cfg.put_prefix):
        return "put"
    return "otro"


# ---------------------------------------------------------------------------
# Apareo FIFO de compras/ventas -> trades cerrados + posiciones abiertas
# ---------------------------------------------------------------------------

@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    direction: str            # "long" (el bot compro primero) o "short" (vendio primero)
    quantity: float            # contratos apareados en este cierre (siempre positivo)
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_order_id: str
    exit_order_id: str
    pnl_ars: float
    pnl_pct: float
    holding_seconds: float


@dataclass
class OpenLot:
    symbol: str
    strategy: str
    quantity: float            # signed: + long, - short
    entry_time: pd.Timestamp
    entry_price: float
    entry_order_id: str


def match_trades_fifo(
    fills: pd.DataFrame, option_multiplier: Optional[float] = None,
) -> Tuple[List[ClosedTrade], List[OpenLot]]:
    """
    Recorre los fills en orden cronologico y aparea, por simbolo, cada
    fill contra los lotes abiertos de signo OPUESTO en una cola FIFO (el
    lote mas viejo se cierra primero). Lo que no se puede aparear (porque
    no hay lotes opuestos, o porque sobra cantidad) queda como un nuevo
    lote abierto.

    El multiplicador se resuelve POR SIMBOLO (ver multiplier_for_symbol()):
    1.0 para el subyacente/futuro (acciones), `option_multiplier` para
    cualquier opcion - nunca un unico valor global para todos los simbolos
    (ver nota de bug real en multiplier_for_symbol()).
    """
    open_lots: Dict[str, deque] = {}
    closed: List[ClosedTrade] = []

    for row in fills.itertuples(index=False):
        symbol = row.symbol
        signed_qty = float(row.quantity) if row.side == "buy" else -float(row.quantity)
        strategy = classify_strategy(symbol)
        multiplier = multiplier_for_symbol(symbol, option_multiplier)
        queue = open_lots.setdefault(symbol, deque())

        remaining = signed_qty
        while remaining != 0 and queue and (queue[0].quantity > 0) != (remaining > 0):
            lot = queue[0]
            match_qty = min(abs(lot.quantity), abs(remaining))
            direction = "long" if lot.quantity > 0 else "short"
            pnl_per_unit = (row.fill_price - lot.entry_price) if direction == "long" else (lot.entry_price - row.fill_price)
            pnl_ars = pnl_per_unit * match_qty * multiplier
            notional = lot.entry_price * match_qty * multiplier
            pnl_pct = (pnl_ars / notional * 100.0) if notional else 0.0

            closed.append(ClosedTrade(
                symbol=symbol, strategy=strategy, direction=direction, quantity=match_qty,
                entry_time=lot.entry_time, exit_time=row.timestamp_utc,
                entry_price=lot.entry_price, exit_price=row.fill_price,
                entry_order_id=lot.entry_order_id, exit_order_id=row.client_order_id,
                pnl_ars=pnl_ars, pnl_pct=pnl_pct,
                holding_seconds=(row.timestamp_utc - lot.entry_time).total_seconds(),
            ))

            if lot.quantity > 0:
                lot.quantity -= match_qty
                remaining += match_qty
            else:
                lot.quantity += match_qty
                remaining -= match_qty
            if lot.quantity == 0:
                queue.popleft()

        if remaining != 0:
            queue.append(OpenLot(
                symbol=symbol, strategy=strategy, quantity=remaining,
                entry_time=row.timestamp_utc, entry_price=row.fill_price,
                entry_order_id=row.client_order_id,
            ))

    open_positions = [lot for q in open_lots.values() for lot in q if lot.quantity != 0]
    return closed, open_positions


def aggregate_open_positions(open_lots: List[OpenLot]) -> pd.DataFrame:
    """Consolida lotes abiertos del mismo simbolo+signo en una sola fila (precio promedio ponderado)."""
    columns = ["symbol", "strategy", "quantity", "avg_entry_price", "entry_time"]
    if not open_lots:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame([lot.__dict__ for lot in open_lots])
    df["direction_sign"] = np.sign(df["quantity"])
    rows = []
    for (symbol, sign), group in df.groupby(["symbol", "direction_sign"]):
        qty = group["quantity"].sum()
        if qty == 0:
            continue
        avg_price = (group["quantity"] * group["entry_price"]).sum() / qty
        rows.append({
            "symbol": symbol,
            "strategy": group["strategy"].iloc[0],
            "quantity": qty,
            "avg_entry_price": avg_price,
            "entry_time": group["entry_time"].min(),
        })
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


# ---------------------------------------------------------------------------
# Marca a mercado de posiciones abiertas
# ---------------------------------------------------------------------------

def get_current_price(symbol: str, bot_state: Dict[str, Any]) -> Optional[float]:
    """Ultimo mid conocido para `symbol` segun state/bot_state.json, o None si no esta disponible."""
    if not bot_state:
        return None
    cfg = SETTINGS.instruments
    if symbol in (cfg.contado_ticker, cfg.futuro_ticker):
        spot = (bot_state.get("extra") or {}).get("spot_mid")
        return float(spot) if spot is not None else None
    for q in bot_state.get("option_chain_snapshot", []) or []:
        if q.get("symbol") == symbol:
            mid = q.get("mid")
            return float(mid) if mid not in (None, 0) else None
    return None


def mark_to_market(
    open_positions_df: pd.DataFrame, bot_state: Dict[str, Any], option_multiplier: Optional[float] = None,
) -> pd.DataFrame:
    """
    Agrega current_price/pnl_ars/pnl_pct/has_current_price a cada fila de
    posiciones abiertas. El multiplicador se resuelve POR SIMBOLO (ver
    multiplier_for_symbol()): 1.0 para el subyacente/futuro, `option_multiplier`
    para opciones - nunca un unico valor global (ver nota de bug real en
    multiplier_for_symbol()).
    """
    columns = list(open_positions_df.columns) + ["current_price", "pnl_ars", "pnl_pct", "has_current_price"]
    if open_positions_df.empty:
        return pd.DataFrame(columns=columns)

    df = open_positions_df.copy()
    # pd.to_numeric convierte los None que devuelve get_current_price() en
    # NaN "de verdad" (float64) en vez de dejar la columna en dtype object
    # con Nones sueltos - object+None revienta mas adelante en cualquier
    # operacion numerica (ej. Series.round() en dashboard/app.py, que no
    # sabe redondear un NoneType).
    df["current_price"] = pd.to_numeric(
        df["symbol"].apply(lambda s: get_current_price(s, bot_state)), errors="coerce",
    )
    df["has_current_price"] = df["current_price"].notna()
    row_multiplier = df["symbol"].apply(lambda s: multiplier_for_symbol(s, option_multiplier))
    df["pnl_ars"] = np.where(
        df["has_current_price"],
        (df["current_price"] - df["avg_entry_price"]) * df["quantity"] * row_multiplier,
        0.0,
    )
    notional = (df["avg_entry_price"] * df["quantity"].abs() * row_multiplier).replace(0, np.nan)
    df["pnl_pct"] = (df["pnl_ars"] / notional * 100.0).fillna(0.0)
    return df


# ---------------------------------------------------------------------------
# Metricas de portafolio
# ---------------------------------------------------------------------------

def compute_equity_curve(closed_trades: List[ClosedTrade]) -> pd.DataFrame:
    """Curva de equity acumulada (solo PnL REALIZADO, ordenado por momento de cierre)."""
    if not closed_trades:
        return pd.DataFrame(columns=["timestamp", "pnl_ars", "cumulative_pnl_ars"])
    df = pd.DataFrame([{"timestamp": t.exit_time, "pnl_ars": t.pnl_ars} for t in closed_trades])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["cumulative_pnl_ars"] = df["pnl_ars"].cumsum()
    return df


def compute_max_drawdown(equity_curve: pd.DataFrame) -> Dict[str, Optional[float]]:
    if equity_curve.empty:
        return {"max_drawdown_ars": 0.0, "max_drawdown_pct": None}
    running_max = equity_curve["cumulative_pnl_ars"].cummax()
    drawdown = equity_curve["cumulative_pnl_ars"] - running_max
    max_dd_ars = float(drawdown.min())
    idx = drawdown.idxmin()
    peak_at_min = float(running_max.loc[idx])
    # El % de drawdown solo tiene sentido cuando el equity acumulado llego a
    # estar en positivo antes de la caida (si el "pico" fue <= 0, dividir
    # por el da un porcentaje sin sentido economico).
    max_dd_pct = (max_dd_ars / peak_at_min * 100.0) if peak_at_min > 0 else None
    return {"max_drawdown_ars": max_dd_ars, "max_drawdown_pct": max_dd_pct}


def compute_summary(
    closed_trades: List[ClosedTrade], open_positions_marked: pd.DataFrame,
) -> Dict[str, Any]:
    n = len(closed_trades)
    total_realized = sum(t.pnl_ars for t in closed_trades)
    total_unrealized = float(open_positions_marked["pnl_ars"].sum()) if not open_positions_marked.empty else 0.0

    wins = [t for t in closed_trades if t.pnl_ars > 0]
    losses = [t for t in closed_trades if t.pnl_ars < 0]
    win_rate = (len(wins) / n * 100.0) if n else None

    gross_profit = sum(t.pnl_ars for t in wins)
    gross_loss = abs(sum(t.pnl_ars for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = None

    returns_pct = [t.pnl_pct for t in closed_trades]
    sharpe_approx = None
    if len(returns_pct) >= 2:
        mean_r = float(np.mean(returns_pct))
        std_r = float(np.std(returns_pct, ddof=1))
        sharpe_approx = (mean_r / std_r) if std_r > 1e-9 else None

    equity_curve = compute_equity_curve(closed_trades)
    drawdown = compute_max_drawdown(equity_curve)

    return {
        "n_closed_trades": n,
        "n_open_positions": int(len(open_positions_marked)) if not open_positions_marked.empty else 0,
        "pnl_realized_ars": total_realized,
        "pnl_unrealized_ars": total_unrealized,
        "pnl_total_ars": total_realized + total_unrealized,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "sharpe_approx": sharpe_approx,
        "max_drawdown_ars": drawdown["max_drawdown_ars"],
        "max_drawdown_pct": drawdown["max_drawdown_pct"],
    }


def closed_trades_to_frame(closed_trades: List[ClosedTrade]) -> pd.DataFrame:
    columns = [
        "symbol", "strategy", "direction", "quantity", "entry_time", "exit_time",
        "entry_price", "exit_price", "entry_order_id", "exit_order_id",
        "pnl_ars", "pnl_pct", "holding_seconds",
    ]
    if not closed_trades:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([t.__dict__ for t in closed_trades], columns=columns)


# ---------------------------------------------------------------------------
# Smile de IV: puntos crudos (snapshot) + curva teorica (ajuste cuadratico)
# ---------------------------------------------------------------------------

def option_chain_snapshot_to_frame(bot_state: Dict[str, Any]) -> pd.DataFrame:
    columns = ["symbol", "strike", "expiry", "option_type", "bid", "ask", "mid", "iv", "spot_ref"]
    rows = bot_state.get("option_chain_snapshot", []) or []
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    return df[[c for c in columns if c in df.columns]]


def fit_smile_curve(quotes_for_expiry: pd.DataFrame, n_points: int = 60) -> pd.DataFrame:
    """
    Replica liviana del ajuste que usa ggal_bot.models.volatility_surface.
    VolatilitySurface (cuadratico en log-moneyness) para poder dibujar una
    curva "teorica" suave, separada de los puntos crudos de IV por strike.
    Se recalcula aca (en vez de leer los coeficientes del bot) para no
    tener que tocar el ciclo de calculo de run_bot.py solo por esta
    visualizacion; el resultado es equivalente porque usa la misma forma
    funcional (cuadratica) sobre los mismos datos de IV cruda.
    """
    valid = quotes_for_expiry.dropna(subset=["iv", "spot_ref", "strike"])
    valid = valid[valid["spot_ref"] > 0]
    if len(valid) < 3:
        return pd.DataFrame(columns=["strike", "log_moneyness", "fitted_iv"])

    x = np.log(valid["strike"].astype(float) / valid["spot_ref"].astype(float))
    y = valid["iv"].astype(float)
    coeffs = np.polyfit(x, y, deg=2)  # [a, b, c] para a*x^2 + b*x + c

    spot_ref = float(valid["spot_ref"].iloc[0])
    x_grid = np.linspace(x.min(), x.max(), n_points)
    fitted = np.polyval(coeffs, x_grid)
    strikes_grid = spot_ref * np.exp(x_grid)
    return pd.DataFrame({"strike": strikes_grid, "log_moneyness": x_grid, "fitted_iv": fitted})
