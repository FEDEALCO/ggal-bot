"""
test_backtest_reconstruct.py
===============================
Tests para ggal_bot/backtest/reconstruct.py: parseo de los dos exports
reales (lifecycle journal en español, y reconstruccion de cierres de
vol_arbitrage) hacia el esquema uniforme `Trade`. Usa fixtures SINTETICOS
minimos escritos a disco en un directorio temporal (nunca datos reales del
usuario) - solo para ejercitar la logica de parseo/agrupacion/matematica de
PnL, no para hacer ninguna afirmacion sobre el desempeño real del bot.

Correr con:
    python -m ggal_bot.validation.test_backtest_reconstruct
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.backtest.reconstruct import (
    load_closed_trades_export,
    load_lifecycle_journal_rows,
    reconstruct_lifecycle_trades,
)

_LIFECYCLE_HEADER = [
    "Cuando (UTC)", "Evento", "Ticker", "Estrategia", "Position ID", "Contract Key",
    "Lado", "Δ Cantidad", "Cantidad restante", "Precio", "Motivo", "Campos no disponibles",
]

_CLOSED_HEADER = [
    "Ticker", "Estrategia", "Direccion", "Cantidad", "Entrada", "Salida",
    "Precio Entrada", "Precio Salida", "PnL ($)", "PnL (%)", "Duracion (s)",
]


def _write_csv(rows, header) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_load_lifecycle_journal_rows_maps_spanish_headers_and_sorts_chronologically():
    path = _write_csv([
        ["2026-09-05T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "GGAL|GFGC5000O|2026-10-16", "buy", "10", "10", "100.0", "motivo entrada", ""],
        ["2026-09-01T10:00:00+00:00", "REJECT", "GFGC5000O", "weekly_asymmetric", "", "", "buy", "", "", "", "greeks_limit_exceeded", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        assert len(rows) == 2
        # Ordenado cronologicamente ascendente, sin importar el orden del archivo.
        assert rows[0]["event_type"] == "REJECT"
        assert rows[1]["event_type"] == "ENTRY"
        assert rows[1]["position_id"] == "pos1"
        assert rows[1]["quantity_delta"] == "10"
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_simple_entry_and_close():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-10", "0", "120.0", "take_profit", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert still_open == 0
        assert incomplete == 0
        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == "GFGC5000O"
        assert t.multiplier == 100.0  # opcion -> multiplicador de contrato
        # PnL = (120 - 100) * 10 * 100 = 20,000
        assert abs(t.pnl_gross_ars - 20_000.0) < 1e-6
        assert t.close_reason == "take_profit"
        assert len(t.entry_legs) == 1 and len(t.exit_legs) == 1
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_handles_partial_exit_then_close():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
        ["2026-09-03T10:00:00+00:00", "PARTIAL_EXIT", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-5", "5", "130.0", "partial_profit_take", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-5", "0", "110.0", "weekly_horizon_expired", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert still_open == 0
        assert incomplete == 0
        assert len(trades) == 1
        t = trades[0]
        # PnL = (130-100)*5*100 + (110-100)*5*100 = 15,000 + 5,000 = 20,000
        assert abs(t.pnl_gross_ars - 20_000.0) < 1e-6
        assert len(t.exit_legs) == 2  # dos patas de salida por separado, cada una costeable
        assert t.close_reason == "weekly_horizon_expired"  # el motivo del ultimo CLOSE, no del parcial
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_excludes_positions_without_close():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
        # sin CLOSE - todavia abierta al final de la ventana del export
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert trades == []
        assert still_open == 1
        assert incomplete == 0
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_counts_close_without_entry_as_incomplete_data():
    # Posicion "legacy": tiene CLOSE dentro de la ventana del export, pero
    # nunca aparece un ENTRY/ADD (se abrio antes de que empezara el export).
    # No debe fabricarse un trade ni contarse como "todavia abierta" (ya
    # cerro) - debe contarse aparte, explicitamente, como dato incompleto.
    path = _write_csv([
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos_legacy", "k", "sell", "-10", "0", "120.0", "take_profit", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert trades == []
        assert still_open == 0
        assert incomplete == 1
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_position_counts_are_exhaustive_and_disjoint():
    # 1 trade completo + 1 todavia abierta + 1 legacy (CLOSE sin ENTRY) =
    # 3 position_id unicos -> trades + still_open + incomplete deben sumar 3,
    # sin overlap y sin perder ninguna posicion en silencio.
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos_ok", "k", "buy", "10", "10", "100.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos_ok", "k", "sell", "-10", "0", "110.0", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGV5000O", "weekly_asymmetric", "pos_open", "k2", "buy", "10", "10", "50.0", "e", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGX5000O", "weekly_asymmetric", "pos_legacy", "k3", "sell", "-10", "0", "120.0", "take_profit", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert len(trades) == 1
        assert still_open == 1
        assert incomplete == 1
        assert len(trades) + still_open + incomplete == 3
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_filters_by_strategy():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-10", "0", "110.0", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGV5000O", "scalping", "pos2", "k2", "buy", "10", "10", "50.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGV5000O", "scalping", "pos2", "k2", "sell", "-10", "0", "60.0", "r", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, _, _ = reconstruct_lifecycle_trades(rows, strategies=("weekly_asymmetric",))
        assert len(trades) == 1
        assert trades[0].strategy == "weekly_asymmetric"
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_ignores_reject_rows_without_position_id():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "REJECT", "GFGC5000O", "weekly_asymmetric", "", "", "buy", "", "", "", "greeks_limit_exceeded", ""],
        ["2026-09-01T10:05:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-10", "0", "110.0", "r", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert len(trades) == 1
        assert still_open == 0
        assert incomplete == 0
    finally:
        path.unlink(missing_ok=True)


def test_load_closed_trades_export_parses_and_validates_pnl():
    path = _write_csv([
        ["GFGC5000O", "vol_arbitrage", "long", "10", "2026-09-01 10:00:00", "2026-09-02 10:00:00", "100.0", "120.0", "20000", "20.0", "86400"],
    ], _CLOSED_HEADER)
    try:
        trades = load_closed_trades_export(path)
        assert len(trades) == 1
        t = trades[0]
        assert t.strategy == "vol_arbitrage"
        assert abs(t.pnl_gross_ars - 20_000.0) < 1e-6
        assert t.multiplier == 100.0
    finally:
        path.unlink(missing_ok=True)


def test_load_closed_trades_export_excludes_rows_with_inconsistent_pnl():
    path = _write_csv([
        # PnL declarado (999999) no coincide con (120-100)*10*100=20000 -> se excluye.
        ["GFGC5000O", "vol_arbitrage", "long", "10", "2026-09-01 10:00:00", "2026-09-02 10:00:00", "100.0", "120.0", "999999", "999.0", "86400"],
    ], _CLOSED_HEADER)
    try:
        trades = load_closed_trades_export(path)
        assert trades == []
    finally:
        path.unlink(missing_ok=True)


def test_load_closed_trades_export_populates_direction_from_column():
    """FIX 2026-09-29 (chequeo direccional §4.2): la columna 'Direccion' del export de cierres ahora se conserva en Trade.direction."""
    path = _write_csv([
        ["GFGC5000O", "vol_arbitrage", "long", "10", "2026-09-01 10:00:00", "2026-09-02 10:00:00", "100.0", "120.0", "20000", "20.0", "86400"],
        ["GFGC5000O", "vol_arbitrage", "short", "10", "2026-09-01 10:00:00", "2026-09-02 10:00:00", "120.0", "100.0", "20000", "20.0", "86400"],
    ], _CLOSED_HEADER)
    try:
        trades = load_closed_trades_export(path)
        assert len(trades) == 2
        assert trades[0].direction == "long"
        assert trades[1].direction == "short"
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_populates_direction_from_entry_side():
    """FIX 2026-09-29 (chequeo direccional §4.2): 'Lado' de la primera pata de ENTRY se normaliza a long/short en Trade.direction."""
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-10", "0", "120.0", "take_profit", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, _, _ = reconstruct_lifecycle_trades(rows)
        assert trades[0].direction == "long"
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_load_lifecycle_journal_rows_maps_spanish_headers_and_sorts_chronologically,
    test_reconstruct_lifecycle_trades_simple_entry_and_close,
    test_reconstruct_lifecycle_trades_handles_partial_exit_then_close,
    test_reconstruct_lifecycle_trades_excludes_positions_without_close,
    test_reconstruct_lifecycle_trades_counts_close_without_entry_as_incomplete_data,
    test_reconstruct_lifecycle_trades_position_counts_are_exhaustive_and_disjoint,
    test_reconstruct_lifecycle_trades_filters_by_strategy,
    test_reconstruct_lifecycle_trades_ignores_reject_rows_without_position_id,
    test_load_closed_trades_export_parses_and_validates_pnl,
    test_load_closed_trades_export_excludes_rows_with_inconsistent_pnl,
    test_load_closed_trades_export_populates_direction_from_column,
    test_reconstruct_lifecycle_trades_populates_direction_from_entry_side,
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
