"""
test_dashboard_data_market_hours_quality.py
==============================================
Tests para dashboard/data/market_hours_quality.py (flag + cuantificacion de
fills/trades fuera de horario de rueda, MEJORA 2026-10-01 a pedido explicito
del usuario - ver docstring del modulo para la evidencia real completa).

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_market_hours_quality
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from dashboard.data import market_hours_quality as mhq

# 2026-09-29 13:28 UTC = 10:28 ART (el caso real, fuera de horario).
_OUTSIDE_TS = pd.Timestamp("2026-09-29 13:28:32", tz="UTC")
# 2026-09-29 14:30 UTC = 11:30 ART (dentro de la rueda asumida).
_INSIDE_TS = pd.Timestamp("2026-09-29 14:30:00", tz="UTC")


def test_flag_fills_outside_session_marks_the_real_case():
    fills = pd.DataFrame({
        "timestamp_utc": [_OUTSIDE_TS, _INSIDE_TS],
        "symbol": ["GFGC6600OC", "GFGC6600OC"],
    })
    flags = mhq.flag_fills_outside_session(fills)
    assert list(flags) == [True, False]


def test_flag_fills_outside_session_empty_df_returns_empty_series():
    fills = pd.DataFrame(columns=["timestamp_utc"])
    flags = mhq.flag_fills_outside_session(fills)
    assert len(flags) == 0


def test_flag_closed_trades_outside_session_the_real_gfgc6600oc_case():
    """
    Reproduce el caso real verificado 2026-09-28/29: entrada DENTRO de
    horario, salida FUERA (take_profit a las 10:28 ART) -> any_leg debe
    quedar True, con el detalle de cual pata especifica fue la anomala.
    """
    closed_df = pd.DataFrame({
        "symbol": ["GFGC6600OC"],
        "entry_time": [pd.Timestamp("2026-09-28 14:05:53", tz="UTC")],  # 11:05 ART, dentro
        "exit_time": [_OUTSIDE_TS],
        "entry_price": [119.0],
        "exit_price": [382.0],
        "pnl_ars": [420800.0],
    })
    flagged = mhq.flag_closed_trades_outside_session(closed_df)
    assert bool(flagged.loc[0, "entry_outside_session"]) is False
    assert bool(flagged.loc[0, "exit_outside_session"]) is True
    assert bool(flagged.loc[0, "any_leg_outside_session"]) is True


def test_flag_closed_trades_outside_session_both_legs_inside_is_false():
    closed_df = pd.DataFrame({
        "symbol": ["GFGC6600OC"],
        "entry_time": [_INSIDE_TS],
        "exit_time": [_INSIDE_TS + pd.Timedelta(minutes=5)],
        "entry_price": [100.0],
        "exit_price": [101.0],
        "pnl_ars": [100.0],
    })
    flagged = mhq.flag_closed_trades_outside_session(closed_df)
    assert bool(flagged.loc[0, "any_leg_outside_session"]) is False


def test_flag_closed_trades_outside_session_empty_df_has_the_three_columns():
    flagged = mhq.flag_closed_trades_outside_session(pd.DataFrame(columns=["entry_time", "exit_time", "pnl_ars"]))
    for col in ("entry_outside_session", "exit_outside_session", "any_leg_outside_session"):
        assert col in flagged.columns
    assert flagged.empty


def test_flag_open_positions_outside_session():
    open_df = pd.DataFrame({
        "symbol": ["GFGC6600OC", "GFGC6600OC"],
        "entry_time": [_OUTSIDE_TS, _INSIDE_TS],
    })
    flagged = mhq.flag_open_positions_outside_session(open_df)
    assert list(flagged["entry_outside_session"]) == [True, False]


def test_summarize_outside_session_impact_matches_manual_aggregation():
    closed_df = pd.DataFrame({
        "symbol": ["A", "B", "C"],
        "entry_time": [_INSIDE_TS, _INSIDE_TS, _OUTSIDE_TS],
        "exit_time": [_INSIDE_TS, _OUTSIDE_TS, _OUTSIDE_TS],
        "pnl_ars": [100.0, -50.0, 420800.0],
    })
    flagged = mhq.flag_closed_trades_outside_session(closed_df)
    summary = mhq.summarize_outside_session_impact(flagged)
    assert summary["n_total"] == 3
    assert summary["n_outside"] == 2  # B (exit fuera) y C (ambas fuera)
    assert summary["pnl_total_ars"] == 100.0 - 50.0 + 420800.0
    assert summary["pnl_outside_ars"] == -50.0 + 420800.0


def test_summarize_outside_session_impact_empty_or_unflagged_returns_zeros():
    assert mhq.summarize_outside_session_impact(pd.DataFrame()) == {
        "n_total": 0, "n_outside": 0, "pnl_total_ars": 0.0, "pnl_outside_ars": 0.0,
    }
    # DataFrame sin pasar por flag_closed_trades_outside_session (falta la columna):
    unflagged = pd.DataFrame({"pnl_ars": [100.0]})
    assert mhq.summarize_outside_session_impact(unflagged) == {
        "n_total": 0, "n_outside": 0, "pnl_total_ars": 0.0, "pnl_outside_ars": 0.0,
    }


ALL_TESTS = [
    test_flag_fills_outside_session_marks_the_real_case,
    test_flag_fills_outside_session_empty_df_returns_empty_series,
    test_flag_closed_trades_outside_session_the_real_gfgc6600oc_case,
    test_flag_closed_trades_outside_session_both_legs_inside_is_false,
    test_flag_closed_trades_outside_session_empty_df_has_the_three_columns,
    test_flag_open_positions_outside_session,
    test_summarize_outside_session_impact_matches_manual_aggregation,
    test_summarize_outside_session_impact_empty_or_unflagged_returns_zeros,
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
