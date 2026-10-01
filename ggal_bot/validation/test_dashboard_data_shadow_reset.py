"""
test_dashboard_data_shadow_reset.py
======================================
Tests para dashboard/data/shadow_reset.py: separacion de PnL antes/despues
del reset administrativo del shadow (Tarea #27 item 5, a pedido explicito
del usuario - ver docstring del modulo).

Correr con:
    python -m pytest ggal_bot/validation/test_dashboard_data_shadow_reset.py
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from dashboard.data import shadow_reset as dsr


def _closed_df(rows):
    """rows: lista de (exit_time_str, pnl_ars). Resto de columnas con valores dummy."""
    return pd.DataFrame({
        "symbol": ["GFGC6600OC"] * len(rows),
        "strategy": ["weekly_asymmetric"] * len(rows),
        "direction": ["long"] * len(rows),
        "quantity": [1.0] * len(rows),
        "entry_time": [pd.Timestamp(t, tz="UTC") for t, _ in rows],
        "exit_time": [pd.Timestamp(t, tz="UTC") for t, _ in rows],
        "entry_price": [100.0] * len(rows),
        "exit_price": [110.0] * len(rows),
        "entry_order_id": ["eid"] * len(rows),
        "exit_order_id": ["xid"] * len(rows),
        "pnl_ars": [pnl for _, pnl in rows],
        "pnl_pct": [1.0] * len(rows),
        "holding_seconds": [60.0] * len(rows),
    })


def _events_df(rows):
    """rows: lista de (timestamp_str, event_type)."""
    return pd.DataFrame({
        "timestamp_utc": [pd.Timestamp(t, tz="UTC") for t, _ in rows],
        "event_type": [et for _, et in rows],
    })


# ---------------------------------------------------------------------------
# most_recent_shadow_reset_timestamp
# ---------------------------------------------------------------------------

def test_most_recent_shadow_reset_timestamp_none_when_no_events():
    assert dsr.most_recent_shadow_reset_timestamp(pd.DataFrame()) is None


def test_most_recent_shadow_reset_timestamp_none_when_no_reset_event():
    events = _events_df([
        ("2026-09-28T10:00:00", "ENTRY"),
        ("2026-09-29T10:00:00", "CLOSE"),
    ])
    assert dsr.most_recent_shadow_reset_timestamp(events) is None


def test_most_recent_shadow_reset_timestamp_picks_the_latest_one():
    events = _events_df([
        ("2026-09-28T10:00:00", "ENTRY"),
        ("2026-10-01T08:00:00", "SHADOW_RESET"),
        ("2026-10-02T09:00:00", "ENTRY"),
        ("2026-10-05T12:00:00", "SHADOW_RESET"),
    ])
    ts = dsr.most_recent_shadow_reset_timestamp(events)
    assert ts == pd.Timestamp("2026-10-05T12:00:00", tz="UTC")


def test_most_recent_shadow_reset_timestamp_missing_columns_returns_none():
    assert dsr.most_recent_shadow_reset_timestamp(pd.DataFrame({"foo": [1]})) is None


# ---------------------------------------------------------------------------
# tag_closed_trades_with_reset_period / split_closed_trades_by_reset
# ---------------------------------------------------------------------------

def test_tag_without_reset_marks_everything_as_single_period():
    closed = _closed_df([("2026-09-28T10:00:00", 100.0), ("2026-10-02T10:00:00", 200.0)])
    tagged = dsr.tag_closed_trades_with_reset_period(closed, None)
    assert list(tagged["reset_period"]) == [dsr.NO_RESET_LABEL, dsr.NO_RESET_LABEL]


def test_tag_with_reset_splits_before_and_after():
    reset_ts = pd.Timestamp("2026-10-01T00:00:00", tz="UTC")
    closed = _closed_df([
        ("2026-09-28T10:00:00", 100.0),   # antes
        ("2026-10-01T00:00:00", 50.0),    # exactamente en el corte -> antes (<=)
        ("2026-10-02T10:00:00", 200.0),   # despues
    ])
    tagged = dsr.tag_closed_trades_with_reset_period(closed, reset_ts)
    assert list(tagged["reset_period"]) == [
        dsr.BEFORE_RESET_LABEL, dsr.BEFORE_RESET_LABEL, dsr.AFTER_RESET_LABEL,
    ]


def test_split_closed_trades_by_reset_without_reset_everything_is_after():
    closed = _closed_df([("2026-09-28T10:00:00", 100.0)])
    split = dsr.split_closed_trades_by_reset(closed, None)
    assert split["before"].empty
    assert len(split["after"]) == 1


def test_split_closed_trades_by_reset_empty_df():
    closed = _closed_df([])
    split = dsr.split_closed_trades_by_reset(closed, pd.Timestamp("2026-10-01", tz="UTC"))
    assert split["before"].empty
    assert split["after"].empty


# ---------------------------------------------------------------------------
# summarize_pnl_before_after_reset
# ---------------------------------------------------------------------------

def test_summarize_without_reset_has_reset_false_and_all_pnl_in_after():
    closed = _closed_df([("2026-09-28T10:00:00", 100.0), ("2026-09-29T10:00:00", 50.0)])
    summary = dsr.summarize_pnl_before_after_reset(closed, pd.DataFrame(), None)
    assert summary["has_reset"] is False
    assert summary["reset_timestamp"] is None
    assert summary["n_trades_before"] == 0
    assert summary["n_trades_after"] == 2
    assert summary["pnl_realized_before_ars"] == 0.0
    assert summary["pnl_realized_after_ars"] == pytest.approx(150.0)
    assert summary["pnl_unrealized_ars"] == 0.0
    assert summary["pnl_after_total_ars"] == pytest.approx(150.0)


def test_summarize_with_reset_splits_realized_pnl_and_adds_open_unrealized():
    reset_ts = pd.Timestamp("2026-10-01T00:00:00", tz="UTC")
    closed = _closed_df([
        ("2026-09-28T10:00:00", 1000.0),   # antes - contaminado, se descarta del "limpio"
        ("2026-10-02T10:00:00", 300.0),    # despues
        ("2026-10-03T10:00:00", -50.0),    # despues
    ])
    open_positions = pd.DataFrame({"symbol": ["GFGC7000OC"], "pnl_ars": [75.0]})

    summary = dsr.summarize_pnl_before_after_reset(closed, open_positions, reset_ts)

    assert summary["has_reset"] is True
    assert summary["reset_timestamp"] == reset_ts
    assert summary["n_trades_before"] == 1
    assert summary["n_trades_after"] == 2
    assert summary["pnl_realized_before_ars"] == pytest.approx(1000.0)
    assert summary["pnl_realized_after_ars"] == pytest.approx(250.0)
    assert summary["pnl_unrealized_ars"] == pytest.approx(75.0)
    assert summary["pnl_after_total_ars"] == pytest.approx(325.0)


def test_summarize_with_empty_closed_df_and_no_reset():
    closed = _closed_df([])
    summary = dsr.summarize_pnl_before_after_reset(closed, pd.DataFrame(), None)
    assert summary["has_reset"] is False
    assert summary["n_trades_before"] == 0
    assert summary["n_trades_after"] == 0
    assert summary["pnl_realized_after_ars"] == 0.0
