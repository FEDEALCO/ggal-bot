"""
run_fase0.py
=============
Script de analisis (NO es parte del bot en vivo, no se importa desde
run_bot.py) que corre la Fase 0 del backtest: baseline de las 3 estrategias
(weekly_asymmetric, scalping, vol_arbitrage) con las 8 mejoras del
2026-09-28 apagadas (que es exactamente como operaron durante la ventana de
los exports disponibles), bajo los 4 escenarios de costos de costs.py.

Uso:
    python -m ggal_bot.backtest.run_fase0 <path_lifecycle.csv> <path_closed_trades.csv>

Escribe un CSV de resultados (fase0_results.csv, en el directorio actual) y
imprime una tabla resumen por consola.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import List

from ggal_bot.backtest.costs import default_scenarios
from ggal_bot.backtest.metrics import StrategyReport, build_strategy_report
from ggal_bot.backtest.reconstruct import (
    load_closed_trades_export,
    load_lifecycle_journal_rows,
    reconstruct_lifecycle_trades,
)

_SPREAD_LABELS = {0.0: "mid_sin_spread", 0.03: "spread_3pct", 0.06: "spread_6pct", 0.10: "spread_10pct"}


def run(lifecycle_path: Path, closed_trades_path: Path) -> List[StrategyReport]:
    lifecycle_rows = load_lifecycle_journal_rows(lifecycle_path)

    reports: List[StrategyReport] = []

    for strategy in ("weekly_asymmetric", "scalping"):
        trades, still_open, incomplete = reconstruct_lifecycle_trades(lifecycle_rows, strategies=(strategy,))
        for scenario in default_scenarios():
            label = _SPREAD_LABELS.get(scenario.spread_round_trip_pct, f"spread_{scenario.spread_round_trip_pct}")
            reports.append(build_strategy_report(
                strategy, trades, scenario, label,
                n_still_open_excluded=still_open, n_excluded_incomplete_data=incomplete,
            ))

    vol_arb_trades = load_closed_trades_export(closed_trades_path)
    for scenario in default_scenarios():
        label = _SPREAD_LABELS.get(scenario.spread_round_trip_pct, f"spread_{scenario.spread_round_trip_pct}")
        reports.append(build_strategy_report("vol_arbitrage", vol_arb_trades, scenario, label))

    return reports


def _fmt(v, spec="{:.2f}"):
    return spec.format(v) if v is not None else "—"


def print_summary(reports: List[StrategyReport]) -> None:
    header = (
        f"{'estrategia':<18}{'costo':<16}{'n':>4}{'abierto':>8}{'incompl':>8}{'win%':>8}{'net_pnl_ars':>16}"
        f"{'gross_pnl_ars':>16}{'costo%':>8}{'sharpe_an':>10}{'sortino_an':>11}{'maxDD_ars':>14}{'recup_d':>14}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        if r.drawdown.recovery_days is not None:
            recup_label = str(r.drawdown.recovery_days)
        elif r.drawdown.recovered:
            recup_label = "N/A"
        else:
            recup_label = "sin_recuperar"
        print(
            f"{r.strategy:<18}{r.cost_scenario_label:<16}{r.n_trades:>4}{r.n_still_open_excluded:>8}"
            f"{r.n_excluded_incomplete_data:>8}"
            f"{_fmt(r.win_rate_pct):>8}{_fmt(r.net_pnl_ars, '{:,.0f}'):>16}{_fmt(r.gross_pnl_ars, '{:,.0f}'):>16}"
            f"{_fmt(r.cost_pct_of_gross_pnl):>8}{_fmt(r.sharpe_annualized):>10}{_fmt(r.sortino_annualized):>11}"
            f"{_fmt(r.drawdown.max_drawdown_ars, '{:,.0f}'):>14}"
            f"{recup_label:>14}"
        )


def write_csv(reports: List[StrategyReport], out_path: Path) -> None:
    fieldnames = [
        "strategy", "cost_scenario", "n_trades", "n_still_open_excluded", "n_excluded_incomplete_data",
        "win_rate_pct", "win_rate_ci_lo", "win_rate_ci_hi",
        "avg_win_ars", "avg_loss_ars", "payoff_ratio",
        "expectancy_ars_per_trade", "expectancy_ci_lo", "expectancy_ci_hi",
        "gross_pnl_ars", "net_pnl_ars", "total_cost_ars", "cost_pct_of_gross_pnl",
        "sharpe_per_trade", "sharpe_annualized", "sharpe_ci_lo", "sharpe_ci_hi",
        "sortino_per_trade", "sortino_annualized", "sortino_ci_lo", "sortino_ci_hi",
        "observed_days", "trades_per_year_observed",
        "max_drawdown_ars", "max_drawdown_pct", "recovered", "recovery_days",
        "worst_day_label", "worst_day_pnl_ars", "worst_week_label", "worst_week_pnl_ars",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in reports:
            writer.writerow({
                "strategy": r.strategy, "cost_scenario": r.cost_scenario_label,
                "n_trades": r.n_trades, "n_still_open_excluded": r.n_still_open_excluded,
                "n_excluded_incomplete_data": r.n_excluded_incomplete_data,
                "win_rate_pct": r.win_rate_pct, "win_rate_ci_lo": r.win_rate_ci[0], "win_rate_ci_hi": r.win_rate_ci[1],
                "avg_win_ars": r.avg_win_ars, "avg_loss_ars": r.avg_loss_ars, "payoff_ratio": r.payoff_ratio,
                "expectancy_ars_per_trade": r.expectancy_ars_per_trade,
                "expectancy_ci_lo": r.expectancy_ci[0], "expectancy_ci_hi": r.expectancy_ci[1],
                "gross_pnl_ars": r.gross_pnl_ars, "net_pnl_ars": r.net_pnl_ars,
                "total_cost_ars": r.total_cost_ars, "cost_pct_of_gross_pnl": r.cost_pct_of_gross_pnl,
                "sharpe_per_trade": r.sharpe_per_trade, "sharpe_annualized": r.sharpe_annualized,
                "sharpe_ci_lo": r.sharpe_ci[0], "sharpe_ci_hi": r.sharpe_ci[1],
                "sortino_per_trade": r.sortino_per_trade, "sortino_annualized": r.sortino_annualized,
                "sortino_ci_lo": r.sortino_ci[0], "sortino_ci_hi": r.sortino_ci[1],
                "observed_days": r.observed_days, "trades_per_year_observed": r.trades_per_year_observed,
                "max_drawdown_ars": r.drawdown.max_drawdown_ars, "max_drawdown_pct": r.drawdown.max_drawdown_pct,
                "recovered": r.drawdown.recovered, "recovery_days": r.drawdown.recovery_days,
                "worst_day_label": r.worst_day[0] if r.worst_day else None,
                "worst_day_pnl_ars": r.worst_day[1] if r.worst_day else None,
                "worst_week_label": r.worst_week[0] if r.worst_week else None,
                "worst_week_pnl_ars": r.worst_week[1] if r.worst_week else None,
            })


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python -m ggal_bot.backtest.run_fase0 <lifecycle.csv> <closed_trades.csv>")
        raise SystemExit(1)
    reports = run(Path(sys.argv[1]), Path(sys.argv[2]))
    print_summary(reports)
    out = Path("fase0_results.csv")
    write_csv(reports, out)
    print(f"\nResultados completos escritos en {out.resolve()}")
