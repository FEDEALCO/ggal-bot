"""
run_fase0.py
=============
Script de analisis (NO es parte del bot en vivo, no se importa desde
run_bot.py) que corre la Fase 0 del backtest: baseline de las 3 estrategias
(weekly_asymmetric, scalping, vol_arbitrage) con las 8 mejoras del
2026-09-28 apagadas (que es exactamente como operaron durante la ventana de
los exports disponibles), bajo los escenarios de costos de costs.py, mas el
diagnostico de PnL BRUTO (attribution.py) pedido tras el primer reporte.

Uso:
    python -m ggal_bot.backtest.run_fase0 <path_lifecycle.csv> <path_closed_trades.csv>

Escribe dos CSV de resultados en el directorio actual
(fase0_results.csv, fase0_attribution.csv) e imprime tablas resumen por
consola.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import List

from ggal_bot.backtest.attribution import (
    attribute_by_close_reason,
    attribute_by_dte,
    attribute_by_entry_hour_art,
    attribute_by_holding_time,
    attribute_by_moneyness,
    load_spot_closes_csv,
    unmapped_close_reasons,
    winner_loser_holding_profile,
)
from ggal_bot.backtest.costs import commission_tier_scenarios, default_scenarios
from ggal_bot.backtest.metrics import StrategyReport, build_strategy_report
from ggal_bot.backtest.reconstruct import (
    load_closed_trades_export,
    load_lifecycle_journal_rows,
    reconstruct_lifecycle_trades,
)

_SPREAD_LABELS = {0.0: "mid_sin_spread", 0.03: "spread_3pct", 0.06: "spread_6pct", 0.10: "spread_10pct"}
_SPOT_CSV_PATH = Path(__file__).parent / "data" / "ggal_underlying_daily_2026-08-25_2026-09-29.csv"


def load_all_trades(lifecycle_path: Path, closed_trades_path: Path) -> dict:
    """Devuelve {"weekly_asymmetric": (trades, still_open, incomplete), "scalping": (...), "vol_arbitrage": (trades, 0, 0)}."""
    lifecycle_rows = load_lifecycle_journal_rows(lifecycle_path)
    out = {}
    for strategy in ("weekly_asymmetric", "scalping"):
        out[strategy] = reconstruct_lifecycle_trades(lifecycle_rows, strategies=(strategy,))
    vol_arb_trades = load_closed_trades_export(closed_trades_path)
    out["vol_arbitrage"] = (vol_arb_trades, 0, 0)
    return out


def run(lifecycle_path: Path, closed_trades_path: Path) -> List[StrategyReport]:
    all_trades = load_all_trades(lifecycle_path, closed_trades_path)
    reports: List[StrategyReport] = []

    # --- Eje 1: sensibilidad de spread, a comision Gold (default_scenarios) ---
    for strategy, (trades, still_open, incomplete) in all_trades.items():
        for scenario in default_scenarios():
            label = _SPREAD_LABELS.get(scenario.spread_round_trip_pct, f"spread_{scenario.spread_round_trip_pct}")
            reports.append(build_strategy_report(
                strategy, trades, scenario, label,
                n_still_open_excluded=still_open, n_excluded_incomplete_data=incomplete,
            ))

    # --- Eje 2: sensibilidad de escala de comision (Gold/Platinum/Black), a spread=0 ---
    # Gold ya esta cubierto arriba (mid_sin_spread) - solo se agregan Platinum y Black
    # para no duplicar filas. Ver costs.commission_tier_scenarios: la escala REAL de
    # la cuenta del usuario todavia no fue confirmada (pendiente, ver REPORT.md).
    for strategy, (trades, still_open, incomplete) in all_trades.items():
        for scenario in commission_tier_scenarios(spread_round_trip_pct=0.0):
            if scenario.commission_tier == "gold":
                continue  # ya esta como "mid_sin_spread" en el Eje 1
            label = f"{scenario.commission_tier}_mid_sin_spread"
            reports.append(build_strategy_report(
                strategy, trades, scenario, label,
                n_still_open_excluded=still_open, n_excluded_incomplete_data=incomplete,
            ))

    return reports


def run_attribution(lifecycle_path: Path, closed_trades_path: Path) -> dict:
    """
    Diagnostico de PnL BRUTO por estrategia. Devuelve
    {estrategia: {corte: [AttributionBucket, ...]}}. Los cortes que no se
    pueden calcular para una estrategia dada (DATA INSUFFICIENT, ver
    attribution.py) simplemente no aparecen en su dict (nunca con una lista
    vacia fingiendo que se calculo y dio cero trades).
    """
    all_trades = load_all_trades(lifecycle_path, closed_trades_path)
    spot_by_date = load_spot_closes_csv(_SPOT_CSV_PATH)

    out: dict = {}
    for strategy, (trades, _, _) in all_trades.items():
        cuts = {}
        cuts["close_reason"] = attribute_by_close_reason(trades)
        cuts["moneyness"] = attribute_by_moneyness(trades, spot_by_date)
        cuts["dte"] = attribute_by_dte(trades)
        cuts["holding_time"] = attribute_by_holding_time(trades)
        cuts["entry_hour_art"] = attribute_by_entry_hour_art(trades)
        out[strategy] = {
            "n_total": len(trades), "cuts": cuts,
            "unmapped_close_reasons": unmapped_close_reasons(trades),
            "winner_loser_profile": winner_loser_holding_profile(trades),
        }
    return out


def _fmt(v, spec="{:.2f}"):
    return spec.format(v) if v is not None else "—"


def _fmt_seconds(v: float) -> str:
    if v is None:
        return "—"
    if v < 3600:
        return f"{v/60:.1f}min"
    if v < 86400:
        return f"{v/3600:.1f}h"
    return f"{v/86400:.1f}d"


def print_summary(reports: List[StrategyReport]) -> None:
    header = (
        f"{'estrategia':<18}{'costo':<20}{'n':>4}{'abierto':>8}{'incompl':>8}{'win%':>8}{'net_pnl_ars':>16}"
        f"{'gross_pnl_ars':>16}{'costo%':>8}{'sharpe_tr':>10}{'sortino_tr':>11}{'maxDD_ars':>14}{'recup_d':>14}"
        f"{'edge_bruto/tr':>14}{'costo/tr':>12}{'gap_breakeven':>14}"
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
            f"{r.strategy:<18}{r.cost_scenario_label:<20}{r.n_trades:>4}{r.n_still_open_excluded:>8}"
            f"{r.n_excluded_incomplete_data:>8}"
            f"{_fmt(r.win_rate_pct):>8}{_fmt(r.net_pnl_ars, '{:,.0f}'):>16}{_fmt(r.gross_pnl_ars, '{:,.0f}'):>16}"
            f"{_fmt(r.cost_pct_of_gross_pnl):>8}{_fmt(r.sharpe_per_trade, '{:.4f}'):>10}{_fmt(r.sortino_per_trade, '{:.4f}'):>11}"
            f"{_fmt(r.drawdown.max_drawdown_ars, '{:,.0f}'):>14}"
            f"{recup_label:>14}"
            f"{_fmt(r.avg_gross_pnl_ars_per_trade, '{:,.0f}'):>14}"
            f"{_fmt(r.avg_cost_ars_per_trade, '{:,.0f}'):>12}"
            f"{_fmt(r.breakeven_edge_gap_ars, '{:,.0f}'):>14}"
        )


def print_attribution(attribution: dict) -> None:
    for strategy, data in attribution.items():
        print(f"\n=== Atribucion de PnL bruto - {strategy} (n_total={data['n_total']} trades cerrados) ===")
        for cut_name, buckets in data["cuts"].items():
            n_clasificados = sum(b.n for b in buckets)
            if n_clasificados == 0:
                print(f"  [{cut_name}] DATA INSUFFICIENT - ningun trade pudo clasificarse (ver attribution.py)")
                continue
            faltantes = data["n_total"] - n_clasificados
            faltante_txt = f" ({faltantes} sin clasificar)" if faltantes else ""
            print(f"  [{cut_name}] {n_clasificados}/{data['n_total']} trades clasificados{faltante_txt}")
            for b in sorted(buckets, key=lambda x: x.label):
                print(
                    f"      {b.label:<14} n={b.n:>4}  gross_sum={b.gross_pnl_sum_ars:>14,.0f}  "
                    f"gross_mean={_fmt(b.gross_pnl_mean_ars, '{:,.1f}'):>10}  win%_bruto={_fmt(b.win_rate_gross_pct):>6}"
                )
        if data["unmapped_close_reasons"]:
            print(f"  ADVERTENCIA: motivos de salida sin mapear en attribution.py: {data['unmapped_close_reasons']}")

        p = data["winner_loser_profile"]
        print(f"  [ganadoras vs. perdedoras] n_ganadoras={p.n_winners}  n_perdedoras={p.n_losers}")
        print(
            f"      mediana tenencia ganadoras: {_fmt_seconds(p.median_holding_seconds_winners)}   "
            f"mediana tenencia perdedoras: {_fmt_seconds(p.median_holding_seconds_losers)}"
        )
        print(
            f"      PnL bruto medio ganadoras: {_fmt(p.mean_gross_pnl_winners_ars, '{:,.0f}')}   "
            f"PnL bruto medio perdedoras: {_fmt(p.mean_gross_pnl_losers_ars, '{:,.0f}')}"
        )


def write_csv(reports: List[StrategyReport], out_path: Path) -> None:
    fieldnames = [
        "strategy", "cost_scenario", "n_trades", "n_still_open_excluded", "n_excluded_incomplete_data",
        "win_rate_pct", "win_rate_ci_lo", "win_rate_ci_hi",
        "avg_win_ars", "avg_loss_ars", "payoff_ratio",
        "expectancy_ars_per_trade", "expectancy_ci_lo", "expectancy_ci_hi",
        "gross_pnl_ars", "net_pnl_ars", "total_cost_ars", "cost_pct_of_gross_pnl",
        "avg_gross_pnl_ars_per_trade", "avg_cost_ars_per_trade", "breakeven_edge_gap_ars",
        "sharpe_per_trade", "sharpe_ci_lo", "sharpe_ci_hi",
        "sortino_per_trade", "sortino_ci_lo", "sortino_ci_hi",
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
                "avg_gross_pnl_ars_per_trade": r.avg_gross_pnl_ars_per_trade,
                "avg_cost_ars_per_trade": r.avg_cost_ars_per_trade,
                "breakeven_edge_gap_ars": r.breakeven_edge_gap_ars,
                "sharpe_per_trade": r.sharpe_per_trade,
                "sharpe_ci_lo": r.sharpe_ci[0], "sharpe_ci_hi": r.sharpe_ci[1],
                "sortino_per_trade": r.sortino_per_trade,
                "sortino_ci_lo": r.sortino_ci[0], "sortino_ci_hi": r.sortino_ci[1],
                "observed_days": r.observed_days, "trades_per_year_observed": r.trades_per_year_observed,
                "max_drawdown_ars": r.drawdown.max_drawdown_ars, "max_drawdown_pct": r.drawdown.max_drawdown_pct,
                "recovered": r.drawdown.recovered, "recovery_days": r.drawdown.recovery_days,
                "worst_day_label": r.worst_day[0] if r.worst_day else None,
                "worst_day_pnl_ars": r.worst_day[1] if r.worst_day else None,
                "worst_week_label": r.worst_week[0] if r.worst_week else None,
                "worst_week_pnl_ars": r.worst_week[1] if r.worst_week else None,
            })


def write_attribution_csv(attribution: dict, out_path: Path) -> None:
    fieldnames = ["strategy", "cut", "bucket", "n", "gross_pnl_sum_ars", "gross_pnl_mean_ars", "win_rate_gross_pct"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for strategy, data in attribution.items():
            for cut_name, buckets in data["cuts"].items():
                for b in buckets:
                    writer.writerow({
                        "strategy": strategy, "cut": cut_name, "bucket": b.label, "n": b.n,
                        "gross_pnl_sum_ars": b.gross_pnl_sum_ars, "gross_pnl_mean_ars": b.gross_pnl_mean_ars,
                        "win_rate_gross_pct": b.win_rate_gross_pct,
                    })


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python -m ggal_bot.backtest.run_fase0 <lifecycle.csv> <closed_trades.csv>")
        raise SystemExit(1)
    lifecycle_path, closed_trades_path = Path(sys.argv[1]), Path(sys.argv[2])

    reports = run(lifecycle_path, closed_trades_path)
    print_summary(reports)
    out = Path("fase0_results.csv")
    write_csv(reports, out)
    print(f"\nResultados completos escritos en {out.resolve()}")

    attribution = run_attribution(lifecycle_path, closed_trades_path)
    print_attribution(attribution)
    out_attr = Path("fase0_attribution.csv")
    write_attribution_csv(attribution, out_attr)
    print(f"\nAtribucion completa escrita en {out_attr.resolve()}")
