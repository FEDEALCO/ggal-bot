"""
test_dashboard_data_journal.py
=================================
Tests para dashboard/data/journal.py (capa de datos del dashboard, Fase 1
2026-09-30 - ver REPORT.md). No dibuja nada, no importa streamlit.

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_journal
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard import pnl_engine as pe
from dashboard.data import journal as dj

_RAW_HEADER = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]


def _write_raw_csv(rows) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_RAW_HEADER)
        w.writerows(rows)
    return path


def test_load_journal_rows_returns_empty_list_when_file_missing():
    # FIX DE AISLAMIENTO (2026-10-01, hallado al verificar en Windows):
    # tempfile.mkstemp() devuelve (fd, path) con el fd YA ABIERTO - hay que
    # cerrarlo (ver _write_raw_csv de arriba, que si lo hace) antes de poder
    # borrar el archivo. En POSIX, unlink() sobre un archivo con un handle
    # abierto funciona igual (el inodo se libera cuando se cierra el ultimo
    # handle) - en Windows, PermissionError: "El proceso no tiene acceso al
    # archivo porque esta siendo utilizado por otro proceso".
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    missing = Path(name)
    missing.unlink()  # no existe
    assert dj.load_journal_rows(missing) == []


def test_load_journal_rows_maps_english_schema_and_sorts_ascending():
    path = _write_raw_csv([
        ["2026-09-05T10:00:00+00:00", "ENTRY", "pos1", "GGAL|GFGC5000O|2026-10-16", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "entrada", ""],
        ["2026-09-01T10:00:00+00:00", "REJECT", "", "", "GFGC5000O", "weekly_asymmetric", "buy", "", "", "", "oc0", "greeks_limit_exceeded", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        assert len(rows) == 2
        # Ordenado ascendente por timestamp_utc, sin importar el orden del archivo.
        assert rows[0]["event_type"] == "REJECT"
        assert rows[1]["event_type"] == "ENTRY"
        assert rows[1]["position_id"] == "pos1"
        assert rows[1]["strategy_tag"] == "weekly_asymmetric"
        assert set(dj.JOURNAL_ROW_KEYS) <= set(rows[1].keys())
    finally:
        path.unlink(missing_ok=True)


def test_load_journal_rows_never_fabricates_missing_numeric_fields():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "REJECT", "", "", "GFGC5000O", "weekly_asymmetric", "buy", "", "", "", "oc0", "sizing_not_operable", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        assert rows[0]["quantity_delta"] == ""
        assert rows[0]["quantity_after"] == ""
        assert rows[0]["price"] == ""
    finally:
        path.unlink(missing_ok=True)


def test_get_closed_trades_reuses_backtest_reconstruct_without_duplicating_math():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "entrada", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "120.0", "oc1", "take_profit", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        trades, still_open, incomplete = dj.get_closed_trades(rows)
        assert still_open == 0 and incomplete == 0
        assert len(trades) == 1
        assert abs(trades[0].pnl_gross_ars - 20_000.0) < 1e-6  # (120-100)*10*100
    finally:
        path.unlink(missing_ok=True)


def test_get_open_positions_reuses_backtest_reconstruct_with_real_strategy_tag():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos_open", "k", "GFGC5000O", "scalping", "buy", "5", "5", "80.0", "oc2", "entrada", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        open_positions, closed_count, incomplete = dj.get_open_positions(rows)
        assert closed_count == 0 and incomplete == 0
        assert len(open_positions) == 1
        p = open_positions[0]
        assert p.strategy == "scalping"
        assert p.quantity_open == 5.0
    finally:
        path.unlink(missing_ok=True)


def test_rows_from_position_events_df_allows_reusing_an_already_loaded_dataframe():
    """
    MEJORA 2026-09-30 (panel de reconciliacion): dashboard/app.py ya carga
    logs/position_events.csv una vez (pe.load_position_events(), tambien
    usado para la pestaña Lifecycle) - esta funcion le permite reusar ESE
    DataFrame en vez de que load_journal_rows() vuelva a leer el archivo.
    """
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
    ])
    try:
        df = pe.load_position_events(path)
        rows = dj.rows_from_position_events_df(df)
        assert len(rows) == 1
        assert rows[0]["position_id"] == "pos1"
    finally:
        path.unlink(missing_ok=True)


def test_rows_from_position_events_df_empty_for_empty_dataframe():
    import pandas as pd
    assert dj.rows_from_position_events_df(pd.DataFrame()) == []


def test_get_closed_trades_and_get_open_positions_filter_by_strategy_consistently():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "110.0", "oc1", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos2", "k2", "GFGV5000O", "scalping", "buy", "5", "5", "50.0", "oc2", "e", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        trades, _, _ = dj.get_closed_trades(rows, strategies=("weekly_asymmetric",))
        open_positions, _, _ = dj.get_open_positions(rows, strategies=("scalping",))
        assert len(trades) == 1 and trades[0].strategy == "weekly_asymmetric"
        assert len(open_positions) == 1 and open_positions[0].strategy == "scalping"
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_load_journal_rows_returns_empty_list_when_file_missing,
    test_load_journal_rows_maps_english_schema_and_sorts_ascending,
    test_load_journal_rows_never_fabricates_missing_numeric_fields,
    test_get_closed_trades_reuses_backtest_reconstruct_without_duplicating_math,
    test_get_open_positions_reuses_backtest_reconstruct_with_real_strategy_tag,
    test_rows_from_position_events_df_allows_reusing_an_already_loaded_dataframe,
    test_rows_from_position_events_df_empty_for_empty_dataframe,
    test_get_closed_trades_and_get_open_positions_filter_by_strategy_consistently,
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
