"""
test_backtest_metrics.py
===========================
Tests para ggal_bot/backtest/metrics.py: bootstrap generico, equity curve,
max drawdown + recuperacion, peor dia/semana, y el reporte agregado por
estrategia (Fase 0 del backtest). Usa Trade sinteticos construidos a mano
(nunca datos reales del usuario) para poder verificar cada formula contra
un calculo manual exacto.

Correr con:
    python -m ggal_bot.validation.test_backtest_metrics
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.backtest.costs import CostAssumptions
from ggal_bot.backtest.metrics import (
    bootstrap_ci,
    build_strategy_report,
    compute_equity_curve,
    compute_max_drawdown,
    cost_trades,
    worst_period,
)
from ggal_bot.backtest.reconstruct import Leg, Trade


def _trade(symbol, entry_price, exit_price, qty, opened, closed, multiplier=100.0) -> Trade:
    opened_dt = datetime.fromisoformat(opened)
    closed_dt = datetime.fromisoformat(closed)
    pnl = (exit_price - entry_price) * qty * multiplier
    return Trade(
        strategy="weekly_asymmetric", symbol=symbol, trade_id=f"{symbol}_{opened}",
        opened_at=opened_dt, closed_at=closed_dt, multiplier=multiplier,
        entry_legs=[Leg(quantity=qty, price=entry_price, timestamp=opened_dt)],
        exit_legs=[Leg(quantity=qty, price=exit_price, timestamp=closed_dt)],
        pnl_gross_ars=pnl,
    )


_NO_COST = CostAssumptions(commission_pct_override=0.0, market_rights_pct=0.0, spread_round_trip_pct=0.0)


def test_bootstrap_ci_mean_matches_point_estimate_and_brackets_true_mean():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 100.0]  # una cola larga a proposito
    point, lo, hi = bootstrap_ci(values, statistic_fn=statistics.fmean, n_resamples=2000)
    assert abs(point - statistics.fmean(values)) < 1e-9
    assert lo is not None and hi is not None
    assert lo <= point <= hi


def test_bootstrap_ci_empty_returns_all_none():
    assert bootstrap_ci([]) == (None, None, None)


def test_bootstrap_ci_single_value_returns_point_without_interval():
    point, lo, hi = bootstrap_ci([42.0])
    assert point == 42.0
    assert lo is None and hi is None


def test_bootstrap_ci_is_reproducible_with_fixed_seed():
    values = [1.0, -2.0, 3.5, -0.5, 10.0]
    r1 = bootstrap_ci(values, n_resamples=500, seed=99)
    r2 = bootstrap_ci(values, n_resamples=500, seed=99)
    assert r1 == r2


def test_compute_equity_curve_aggregates_by_calendar_day_and_accumulates():
    trades = [
        _trade("A", 100, 110, 1, "2026-09-01T10:00:00+00:00", "2026-09-01T12:00:00+00:00"),  # +1000
        _trade("B", 100, 90, 1, "2026-09-01T10:00:00+00:00", "2026-09-01T15:00:00+00:00"),   # -1000, mismo dia
        _trade("C", 100, 130, 1, "2026-09-02T10:00:00+00:00", "2026-09-02T12:00:00+00:00"),  # +3000
    ]
    results = cost_trades(trades, _NO_COST)
    curve = compute_equity_curve(results)
    assert len(curve) == 2
    assert curve[0][1] == 0.0    # +1000 - 1000 = 0 neto ese dia
    assert curve[0][2] == 0.0
    assert curve[1][1] == 3000.0
    assert curve[1][2] == 3000.0


def test_compute_max_drawdown_identifies_trough_and_recovery():
    trades = [
        _trade("A", 100, 150, 1, "2026-09-01T10:00:00+00:00", "2026-09-01T12:00:00+00:00"),  # +5000 (pico)
        _trade("B", 100, 60, 1, "2026-09-02T10:00:00+00:00", "2026-09-02T12:00:00+00:00"),   # -4000 (caida a +1000)
        _trade("C", 100, 200, 1, "2026-09-03T10:00:00+00:00", "2026-09-03T12:00:00+00:00"),  # +10000 (recupera y supera el pico)
    ]
    results = cost_trades(trades, _NO_COST)
    curve = compute_equity_curve(results)
    dd = compute_max_drawdown(curve)
    assert abs(dd.max_drawdown_ars - (-4000.0)) < 1e-6
    assert dd.recovered is True
    assert dd.recovery_days == 1  # de 09-02 (trough) a 09-03 (recupera)


def test_compute_max_drawdown_no_recovery_within_sample():
    trades = [
        _trade("A", 100, 150, 1, "2026-09-01T10:00:00+00:00", "2026-09-01T12:00:00+00:00"),  # +5000
        _trade("B", 100, 60, 1, "2026-09-02T10:00:00+00:00", "2026-09-02T12:00:00+00:00"),   # -4000, nunca recupera
    ]
    results = cost_trades(trades, _NO_COST)
    curve = compute_equity_curve(results)
    dd = compute_max_drawdown(curve)
    assert dd.recovered is False
    assert dd.recovery_days is None  # nunca se fabrica una fecha de recuperacion que no ocurrio


def test_compute_max_drawdown_empty_curve():
    dd = compute_max_drawdown([])
    assert dd.max_drawdown_ars == 0.0
    assert dd.recovered is True


def test_worst_period_day_and_week():
    trades = [
        _trade("A", 100, 150, 1, "2026-09-01T10:00:00+00:00", "2026-09-01T12:00:00+00:00"),  # +5000, martes W36
        _trade("B", 100, 60, 1, "2026-09-08T10:00:00+00:00", "2026-09-08T12:00:00+00:00"),   # -4000, martes W37 (peor dia y semana)
    ]
    results = cost_trades(trades, _NO_COST)
    curve = compute_equity_curve(results)
    worst_day = worst_period(curve, "day")
    worst_week = worst_period(curve, "week")
    assert worst_day[1] == -4000.0
    assert worst_week[1] == -4000.0


def test_worst_period_empty_curve_returns_none():
    assert worst_period([], "day") is None


def test_build_strategy_report_basic_counts_and_pnl_no_cost():
    trades = [
        _trade("A", 100, 120, 10, "2026-09-01T10:00:00+00:00", "2026-09-02T10:00:00+00:00"),  # +20,000
        _trade("B", 100, 90, 10, "2026-09-03T10:00:00+00:00", "2026-09-04T10:00:00+00:00"),   # -10,000
    ]
    report = build_strategy_report(
        "weekly_asymmetric", trades, _NO_COST, "sin_costos",
        n_still_open_excluded=3, n_excluded_incomplete_data=2,
    )
    assert report.n_trades == 2
    assert report.n_still_open_excluded == 3
    assert report.n_excluded_incomplete_data == 2
    assert "2 posiciones" in report.sample_size_warning
    assert report.win_rate_pct == 50.0
    assert abs(report.gross_pnl_ars - 10_000.0) < 1e-6
    assert abs(report.net_pnl_ars - 10_000.0) < 1e-6  # sin costos: neto == bruto
    assert report.total_cost_ars == 0.0
    assert "3 semanas" in report.sample_size_warning or "muestra" in report.sample_size_warning.lower()


def test_build_strategy_report_costs_reduce_net_pnl_vs_gross():
    trades = [
        _trade("A", 100, 120, 10, "2026-09-01T10:00:00+00:00", "2026-09-02T10:00:00+00:00"),
        _trade("B", 100, 90, 10, "2026-09-03T10:00:00+00:00", "2026-09-04T10:00:00+00:00"),
    ]
    assumptions_with_cost = CostAssumptions(commission_tier="gold", spread_round_trip_pct=0.06)
    report = build_strategy_report("weekly_asymmetric", trades, assumptions_with_cost, "con_costos")
    assert report.net_pnl_ars < report.gross_pnl_ars
    assert report.total_cost_ars > 0.0


def test_build_strategy_report_empty_trades_does_not_crash():
    report = build_strategy_report("weekly_asymmetric", [], _NO_COST, "vacio")
    assert report.n_trades == 0
    assert report.win_rate_pct is None
    assert report.gross_pnl_ars == 0.0
    assert report.drawdown.max_drawdown_ars == 0.0


ALL_TESTS = [
    test_bootstrap_ci_mean_matches_point_estimate_and_brackets_true_mean,
    test_bootstrap_ci_empty_returns_all_none,
    test_bootstrap_ci_single_value_returns_point_without_interval,
    test_bootstrap_ci_is_reproducible_with_fixed_seed,
    test_compute_equity_curve_aggregates_by_calendar_day_and_accumulates,
    test_compute_max_drawdown_identifies_trough_and_recovery,
    test_compute_max_drawdown_no_recovery_within_sample,
    test_compute_max_drawdown_empty_curve,
    test_worst_period_day_and_week,
    test_worst_period_empty_curve_returns_none,
    test_build_strategy_report_basic_counts_and_pnl_no_cost,
    test_build_strategy_report_costs_reduce_net_pnl_vs_gross,
    test_build_strategy_report_empty_trades_does_not_crash,
]


if __name__ == "__main__":
    failures = 0
    for test_fn in ALL_TESTS:
        try:
            test_fn()
            print(f"OK   - {test_fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL - {test_fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR - {test_fn.__name__}: {exc!r}")

    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} tests OK")
    if failures:
        raise SystemExit(1)
