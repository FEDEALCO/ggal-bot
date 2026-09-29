"""
metrics.py
===========
Motor de metricas de la Fase 0 del backtest (ver conversacion): dado un
conjunto de Trade (ver reconstruct.py) y un escenario de costos
(ver costs.py), calcula TODAS las metricas pedidas por el usuario, siempre
juntas (nunca una sola metrica aislada) y con intervalos de confianza por
bootstrap de trades donde tiene sentido estadistico hacerlo.

LIMITACIONES METODOLOGICAS EXPLICITAS (leer antes de interpretar numeros):

    1. Sharpe/Sortino "anualizados": no existe una serie de retornos
       PERIODICOS de cuenta (no se conoce el capital real de la cuenta -
       ver capital_base_ars=None abajo). Se calculan sobre la serie de
       retornos POR TRADE (pnl_net_ars / notional_de_entrada_de_ESE_trade),
       y se anualizan escalando por sqrt(trades_por_año_observado) - una
       extrapolacion de la frecuencia de trades ya vista en la muestra, NO
       una proyeccion de que esa frecuencia se sostendra. Con 2-3 semanas
       de datos esto es, por construccion, una anualizacion de altisima
       incertidumbre (ver bootstrap_ci de cada metrica).
    2. Max drawdown se calcula sobre la curva de equity en PESOS
       (pnl_net_ars acumulado, ordenado por cierre) porque no hay una base
       de capital real conocida contra la cual expresarlo en %. Se reporta
       tambien en % SOLO cuando el pico previo a la caida fue positivo
       (mismo criterio que dashboard/pnl_engine.py::compute_max_drawdown).
    3. Max drawdown y tiempo de recuperacion NO se bootstrapean (bootstrap
       i.i.d. de trades destruye el orden temporal, que es precisamente lo
       que define un drawdown) - se reporta como punto unico, con la
       advertencia de que la incertidumbre real es AL MENOS tan grande como
       la de las demas metricas, probablemente mayor.
    4. "Retorno neto anualizado" se reporta como % sobre el NOCIONAL
       PROMEDIO DE ENTRADA de los trades de la muestra (proxy de "capital
       empleado"), NO sobre el capital total de la cuenta (desconocido -
       DATA INSUFFICIENT, nunca fabricado). Si el usuario provee su capital
       real, se puede recalcular sobre esa base.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ggal_bot.backtest.costs import CostAssumptions, net_pnl_ars, total_cost_ars
from ggal_bot.backtest.reconstruct import Trade

TRADING_DAYS_PER_YEAR = 252.0


@dataclass
class TradeResult:
    """Un Trade ya costeado bajo un escenario de costos especifico."""
    trade: Trade
    pnl_gross_ars: float
    pnl_net_ars: float
    cost_ars: float
    entry_notional_ars: float
    pnl_net_pct: float   # sobre notional de entrada de ESTE trade


def cost_trades(trades: Sequence[Trade], assumptions: CostAssumptions) -> List[TradeResult]:
    results = []
    for t in trades:
        entry_notional = t.entry_notional_ars
        exit_notional = t.exit_notional_ars
        net = net_pnl_ars(t.pnl_gross_ars, entry_notional, exit_notional, assumptions)
        cost = total_cost_ars(entry_notional, exit_notional, assumptions)
        pct = (net / entry_notional * 100.0) if entry_notional > 0 else 0.0
        results.append(TradeResult(
            trade=t, pnl_gross_ars=t.pnl_gross_ars, pnl_net_ars=net,
            cost_ars=cost, entry_notional_ars=entry_notional, pnl_net_pct=pct,
        ))
    return results


# ---------------------------------------------------------------------------
# Bootstrap generico
# ---------------------------------------------------------------------------

def bootstrap_ci(
    values: Sequence[float],
    statistic_fn: Callable[[Sequence[float]], Optional[float]] = statistics.fmean,
    n_resamples: int = 10_000,
    ci: float = 0.95,
    seed: int = 1234,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Bootstrap i.i.d. clasico (remuestreo CON reemplazo de `values`,
    `n_resamples` veces, recalculando `statistic_fn` cada vez). Devuelve
    (estimacion puntual, limite inferior, limite superior) del intervalo
    de confianza `ci` (percentiles). None en los 3 si `values` esta vacio
    o `statistic_fn` no puede calcularse (ej. desvio 0 con 1 solo valor).

    Semilla FIJA (no aleatoria) para que el resultado sea reproducible
    corrida a corrida - no es una fuente de aleatoriedad de mercado, es
    puramente un metodo de remuestreo estadistico.
    """
    n = len(values)
    if n == 0:
        return None, None, None
    point = statistic_fn(values)
    if point is None:
        return None, None, None
    if n < 2:
        return point, None, None  # no hay variabilidad que remuestrear con sentido

    rng = random.Random(seed)
    values_list = list(values)
    resample_stats = []
    for _ in range(n_resamples):
        resample = [values_list[rng.randrange(n)] for _ in range(n)]
        stat = statistic_fn(resample)
        if stat is not None and math.isfinite(stat):
            resample_stats.append(stat)

    if len(resample_stats) < 100:
        return point, None, None  # muy pocas resamples validas para un percentil confiable

    resample_stats.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = max(0, int(alpha * len(resample_stats)))
    hi_idx = min(len(resample_stats) - 1, int((1.0 - alpha) * len(resample_stats)))
    return point, resample_stats[lo_idx], resample_stats[hi_idx]


def _safe_mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def _safe_stdev(values: Sequence[float]) -> Optional[float]:
    return statistics.stdev(values) if len(values) >= 2 else None


def _sharpe(returns_pct: Sequence[float]) -> Optional[float]:
    if len(returns_pct) < 2:
        return None
    mean_r = statistics.fmean(returns_pct)
    std_r = statistics.stdev(returns_pct)
    if std_r <= 1e-9:
        return None
    return mean_r / std_r


def _sortino(returns_pct: Sequence[float]) -> Optional[float]:
    if len(returns_pct) < 2:
        return None
    mean_r = statistics.fmean(returns_pct)
    downside = [min(0.0, r) for r in returns_pct]
    downside_dev = math.sqrt(sum(d * d for d in downside) / len(returns_pct))
    if downside_dev <= 1e-9:
        return None
    return mean_r / downside_dev


# ---------------------------------------------------------------------------
# Equity curve / drawdown / recuperacion
# ---------------------------------------------------------------------------

@dataclass
class DrawdownResult:
    max_drawdown_ars: float
    max_drawdown_pct: Optional[float]
    peak_before_ars: float
    trough_ars: float
    peak_date: Optional[date]
    trough_date: Optional[date]
    recovered: bool
    recovery_days: Optional[int]  # None si no se dispone de fecha, o si recovered=False


def compute_equity_curve(results: Sequence[TradeResult]) -> List[Tuple[date, float, float]]:
    """Lista ordenada de (fecha_de_cierre, pnl_neto_del_dia, equity_acumulada) - un punto por dia calendario con >=1 cierre."""
    by_day: Dict[date, float] = {}
    for r in results:
        if r.trade.closed_at is None:
            continue
        d = r.trade.closed_at.date()
        by_day[d] = by_day.get(d, 0.0) + r.pnl_net_ars
    curve = []
    cumulative = 0.0
    for d in sorted(by_day.keys()):
        cumulative += by_day[d]
        curve.append((d, by_day[d], cumulative))
    return curve


def compute_max_drawdown(curve: Sequence[Tuple[date, float, float]]) -> DrawdownResult:
    if not curve:
        return DrawdownResult(0.0, None, 0.0, 0.0, None, None, True, None)

    running_max = -math.inf
    running_max_date: Optional[date] = None
    max_dd = 0.0
    trough_val = 0.0
    peak_at_trough = 0.0
    peak_date_at_trough: Optional[date] = None
    trough_date: Optional[date] = None

    for d, _, cum in curve:
        if cum > running_max:
            running_max = cum
            running_max_date = d
        dd = cum - running_max
        if dd < max_dd:
            max_dd = dd
            trough_val = cum
            peak_at_trough = running_max
            peak_date_at_trough = running_max_date
            trough_date = d

    max_dd_pct = (max_dd / peak_at_trough * 100.0) if peak_at_trough > 0 else None

    recovered = False
    recovery_days = None
    if trough_date is not None:
        for d, _, cum in curve:
            if d > trough_date and cum >= peak_at_trough:
                recovered = True
                recovery_days = (d - trough_date).days
                break

    return DrawdownResult(
        max_drawdown_ars=max_dd, max_drawdown_pct=max_dd_pct,
        peak_before_ars=peak_at_trough, trough_ars=trough_val,
        peak_date=peak_date_at_trough, trough_date=trough_date,
        recovered=recovered, recovery_days=recovery_days,
    )


def worst_period(curve: Sequence[Tuple[date, float, float]], period: str = "day") -> Optional[Tuple[str, float]]:
    """`period`: "day" o "week" (semana ISO). Devuelve (etiqueta, pnl_neto) del peor periodo, o None si la curva esta vacia."""
    if not curve:
        return None
    buckets: Dict[str, float] = {}
    for d, pnl, _ in curve:
        if period == "week":
            iso = d.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:
            key = d.isoformat()
        buckets[key] = buckets.get(key, 0.0) + pnl
    worst_key = min(buckets, key=lambda k: buckets[k])
    return worst_key, buckets[worst_key]


# ---------------------------------------------------------------------------
# Reporte agregado
# ---------------------------------------------------------------------------

@dataclass
class StrategyReport:
    strategy: str
    cost_scenario_label: str
    n_trades: int
    n_still_open_excluded: int
    n_excluded_incomplete_data: int
    win_rate_pct: Optional[float]
    win_rate_ci: Tuple[Optional[float], Optional[float]]
    avg_win_ars: Optional[float]
    avg_loss_ars: Optional[float]
    payoff_ratio: Optional[float]   # avg_win_ars / |avg_loss_ars|
    expectancy_ars_per_trade: Optional[float]
    expectancy_ci: Tuple[Optional[float], Optional[float]]
    gross_pnl_ars: float
    net_pnl_ars: float
    total_cost_ars: float
    cost_pct_of_gross_pnl: Optional[float]
    sharpe_per_trade: Optional[float]
    sharpe_annualized: Optional[float]
    sharpe_ci: Tuple[Optional[float], Optional[float]]
    sortino_per_trade: Optional[float]
    sortino_annualized: Optional[float]
    sortino_ci: Tuple[Optional[float], Optional[float]]
    observed_days: int
    trades_per_year_observed: float
    drawdown: DrawdownResult
    worst_day: Optional[Tuple[str, float]]
    worst_week: Optional[Tuple[str, float]]
    sample_size_warning: str


def build_strategy_report(
    strategy: str, trades: Sequence[Trade], assumptions: CostAssumptions, cost_scenario_label: str,
    n_still_open_excluded: int = 0, n_excluded_incomplete_data: int = 0,
) -> StrategyReport:
    results = cost_trades(trades, assumptions)
    n = len(results)

    closed_dates = [r.trade.closed_at.date() for r in results if r.trade.closed_at is not None]
    observed_days = (max(closed_dates) - min(closed_dates)).days + 1 if closed_dates else 0
    trades_per_year_observed = (n / observed_days * 365.25) if observed_days > 0 else 0.0

    wins = [r for r in results if r.pnl_net_ars > 0]
    losses = [r for r in results if r.pnl_net_ars < 0]
    win_rate = (len(wins) / n * 100.0) if n else None

    def _win_rate_stat(sample: Sequence[TradeResult]) -> Optional[float]:
        if not sample:
            return None
        return sum(1 for r in sample if r.pnl_net_ars > 0) / len(sample) * 100.0

    _, win_lo, win_hi = bootstrap_ci(results, statistic_fn=_win_rate_stat) if n else (None, None, None)

    avg_win = _safe_mean([r.pnl_net_ars for r in wins]) if wins else None
    avg_loss = _safe_mean([r.pnl_net_ars for r in losses]) if losses else None
    payoff_ratio = (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss not in (None, 0)) else None

    pnl_net_list = [r.pnl_net_ars for r in results]
    expectancy, exp_lo, exp_hi = bootstrap_ci(pnl_net_list) if n else (None, None, None)

    gross_pnl = sum(r.pnl_gross_ars for r in results)
    net_pnl = sum(r.pnl_net_ars for r in results)
    total_cost = sum(r.cost_ars for r in results)
    cost_pct_of_gross = (total_cost / abs(gross_pnl) * 100.0) if gross_pnl != 0 else None

    returns_pct = [r.pnl_net_pct for r in results]
    sharpe_pt, sharpe_lo, sharpe_hi = bootstrap_ci(returns_pct, statistic_fn=_sharpe) if n >= 2 else (None, None, None)
    sortino_pt, sortino_lo, sortino_hi = bootstrap_ci(returns_pct, statistic_fn=_sortino) if n >= 2 else (None, None, None)
    ann_factor = math.sqrt(trades_per_year_observed) if trades_per_year_observed > 0 else None
    sharpe_ann = (sharpe_pt * ann_factor) if (sharpe_pt is not None and ann_factor is not None) else None
    sortino_ann = (sortino_pt * ann_factor) if (sortino_pt is not None and ann_factor is not None) else None

    curve = compute_equity_curve(results)
    dd = compute_max_drawdown(curve)
    w_day = worst_period(curve, "day")
    w_week = worst_period(curve, "week")

    sample_warning = (
        f"MUESTRA: {n} trades cerrados en {observed_days} dias corridos observados "
        f"({closed_dates[0] if closed_dates else '?'} a {closed_dates[-1] if closed_dates else '?'}). "
        "Esto es UN SOLO regimen de mercado de 2-4 semanas: ninguna metrica de esta tabla, "
        "en especial Sharpe/Sortino anualizados y max drawdown, alcanza para activar ninguna "
        "flag en vivo. Se reporta igual, con intervalos de confianza amplios esperables, como "
        "punto de partida - no como evidencia suficiente."
    )
    if n_excluded_incomplete_data:
        sample_warning += (
            f" ADEMAS: {n_excluded_incomplete_data} posiciones se excluyeron de este calculo por "
            "tener CLOSE pero faltarles la pata de ENTRY/ADD dentro de la ventana del export "
            "(posiciones legacy abiertas antes de que empezara el export) - no se les fabrico "
            "un precio de entrada."
        )

    return StrategyReport(
        strategy=strategy, cost_scenario_label=cost_scenario_label,
        n_trades=n, n_still_open_excluded=n_still_open_excluded,
        n_excluded_incomplete_data=n_excluded_incomplete_data,
        win_rate_pct=win_rate, win_rate_ci=(win_lo, win_hi),
        avg_win_ars=avg_win, avg_loss_ars=avg_loss, payoff_ratio=payoff_ratio,
        expectancy_ars_per_trade=expectancy, expectancy_ci=(exp_lo, exp_hi),
        gross_pnl_ars=gross_pnl, net_pnl_ars=net_pnl, total_cost_ars=total_cost,
        cost_pct_of_gross_pnl=cost_pct_of_gross,
        sharpe_per_trade=sharpe_pt, sharpe_annualized=sharpe_ann, sharpe_ci=(sharpe_lo, sharpe_hi),
        sortino_per_trade=sortino_pt, sortino_annualized=sortino_ann, sortino_ci=(sortino_lo, sortino_hi),
        observed_days=observed_days, trades_per_year_observed=trades_per_year_observed,
        drawdown=dd, worst_day=w_day, worst_week=w_week,
        sample_size_warning=sample_warning,
    )
