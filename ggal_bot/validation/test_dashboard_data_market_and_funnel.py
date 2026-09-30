"""
test_dashboard_data_market_and_funnel.py
===========================================
Tests para dashboard/data/market_data.py y dashboard/data/funnel.py -
loaders tolerantes de logs/market_snapshots.csv y logs/signal_funnel.csv.
Ambos comparten el mismo criterio (tolerante a archivo faltante/vacio,
nunca fabrica una fila), por eso un solo archivo de test para los dos.

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_market_and_funnel
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.data import funnel as fn
from dashboard.data import market_data as md


def _missing_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def _write_csv(header, rows) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def test_load_market_snapshots_empty_when_file_missing():
    df = md.load_market_snapshots(_missing_path())
    assert df.empty
    assert list(df.columns) == md.MARKET_SNAPSHOT_COLUMNS


def test_load_market_snapshots_empty_when_only_header():
    path = _write_csv(md.MARKET_SNAPSHOT_COLUMNS, [])
    try:
        df = md.load_market_snapshots(path)
        assert df.empty
    finally:
        path.unlink(missing_ok=True)


def test_load_market_snapshots_parses_real_row():
    path = _write_csv(md.MARKET_SNAPSHOT_COLUMNS, [
        ["2026-09-28T15:00:00+00:00", "GFGC5000O", "call", "5000", "2026-10-16", "18", "12", "5100.0", "95.0", "105.0", "10", "10", "0.45", "0.55", "0.002", "3.1", "-0.8"],
    ])
    try:
        df = md.load_market_snapshots(path)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "GFGC5000O"
    finally:
        path.unlink(missing_ok=True)


def test_load_signal_funnel_empty_when_file_missing():
    df = fn.load_signal_funnel(_missing_path())
    assert df.empty
    assert list(df.columns) == fn.SIGNAL_FUNNEL_COLUMNS


def test_load_signal_funnel_parses_real_row_with_blocked_at():
    path = _write_csv(fn.SIGNAL_FUNNEL_COLUMNS, [
        ["2026-09-29T15:00:00+00:00", "weekly_asymmetric", "GFGC5150O", "call", "5150", "2026-10-02", "7", "5200.0",
         "95.0", "105.0", "100", "100", "10.0", "0.10", "0.45", "0.55", "0.002", "3.1", "-0.8", "-4.2", "moneyness"],
    ])
    try:
        df = fn.load_signal_funnel(path)
        assert len(df) == 1
        assert df.iloc[0]["blocked_at"] == "moneyness"
        assert df.iloc[0]["strategy"] == "weekly_asymmetric"
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_load_market_snapshots_empty_when_file_missing,
    test_load_market_snapshots_empty_when_only_header,
    test_load_market_snapshots_parses_real_row,
    test_load_signal_funnel_empty_when_file_missing,
    test_load_signal_funnel_parses_real_row_with_blocked_at,
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
