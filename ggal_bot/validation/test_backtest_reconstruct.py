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
    reconstruct_open_positions,
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


def test_reconstruct_lifecycle_trades_partial_exit_without_close_default_excluded():
    """
    MEJORA 2026-09-30: reproduce EXACTAMENTE la posicion real de produccion
    que motivo esta mejora (position_id=9bf25bc4c8ca, GFGC7400OC,
    weekly_asymmetric, ver REPORT.md): ENTRY de 13 contratos, PARTIAL_EXIT
    de 6 con ganancia real, y NUNCA un CLOSE de los 7 restantes. Con el
    default (include_partial_realized_for_open_positions=False), el
    comportamiento debe ser IDENTICO a antes de la mejora: el PnL ya
    realizado por el PARTIAL_EXIT NO se cuenta, la posicion es 100%
    "todavia abierta".
    """
    path = _write_csv([
        ["2026-09-15T13:50:23.630511+00:00", "ENTRY", "GFGC7400OC", "weekly_asymmetric", "9bf25bc4c8ca", "GGAL|GFGC7400OC|2026-10-16", "buy", "13", "13", "153.0015", "iv_cruda", ""],
        ["2026-09-16T13:30:29.040963+00:00", "PARTIAL_EXIT", "GFGC7400OC", "weekly_asymmetric", "9bf25bc4c8ca", "GGAL|GFGC7400OC|2026-10-16", "sell", "-6", "7", "202.5", "partial_profit_take", ""],
        # sin CLOSE - los 7 contratos restantes siguen abiertos hoy
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(rows)
        assert trades == []
        assert still_open == 1
        assert incomplete == 0
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_include_partial_realized_for_open_positions_counts_realized_pnl():
    """
    Mismo fixture que el test anterior, pero con
    include_partial_realized_for_open_positions=True (MEJORA 2026-09-30,
    ver docstring de la funcion): el PnL YA REALIZADO por el PARTIAL_EXIT
    debe contarse (= exactamente lo que match_trades_fifo() ya contaba
    sobre shadow_trades.csv, verificado contra los datos reales de
    produccion: ARS 29.699,10), marcando el Trade con
    position_still_open=True, y SIN dejar de contar la posicion en
    still_open_count (el remanente de 7 contratos sigue expuesto).
    """
    path = _write_csv([
        ["2026-09-15T13:50:23.630511+00:00", "ENTRY", "GFGC7400OC", "weekly_asymmetric", "9bf25bc4c8ca", "GGAL|GFGC7400OC|2026-10-16", "buy", "13", "13", "153.0015", "iv_cruda", ""],
        ["2026-09-16T13:30:29.040963+00:00", "PARTIAL_EXIT", "GFGC7400OC", "weekly_asymmetric", "9bf25bc4c8ca", "GGAL|GFGC7400OC|2026-10-16", "sell", "-6", "7", "202.5", "partial_profit_take", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(
            rows, include_partial_realized_for_open_positions=True,
        )
        assert len(trades) == 1
        assert still_open == 1  # el remanente sigue abierto - no es un conjunto disjunto en este modo
        assert incomplete == 0
        t = trades[0]
        assert t.position_still_open is True
        assert t.close_reason is None  # nunca se fabrica un motivo de cierre que no existe
        # PnL = (202.5 - 153.0015) * 6 * 100 = 29,699.10 (verificado contra produccion)
        assert abs(t.pnl_gross_ars - 29_699.10) < 1e-2
        assert len(t.exit_legs) == 1 and t.exit_legs[0].quantity == 6.0
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_lifecycle_trades_include_partial_realized_still_excludes_pure_open_positions():
    """Una posicion SIN ningun exit todavia (puro ENTRY) sigue sin generar
    Trade aunque include_partial_realized_for_open_positions=True - no hay
    nada realizado que contar."""
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete = reconstruct_lifecycle_trades(
            rows, include_partial_realized_for_open_positions=True,
        )
        assert trades == []
        assert still_open == 1
        assert incomplete == 0
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


def test_reconstruct_open_positions_returns_open_position_with_real_strategy_tag():
    """
    MEJORA 2026-09-30 (gap identificado en el pre-analisis del dashboard
    Fase 1): a diferencia de reconciliation.py::reconstruct_positions_from_shadow_log
    (que hardcodea strategy_tag=None), esta reconstruccion viene del
    journal y trae el strategy_tag REAL de cada fila.
    """
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos_open", "k", "buy", "10", "10", "100.0", "entrada", ""],
        # sin CLOSE - todavia abierta
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        open_positions, closed_count, incomplete = reconstruct_open_positions(rows)
        assert closed_count == 0
        assert incomplete == 0
        assert len(open_positions) == 1
        p = open_positions[0]
        assert p.position_id == "pos_open"
        assert p.strategy == "weekly_asymmetric"  # nunca None, a diferencia de reconstruct_positions_from_shadow_log
        assert p.symbol == "GFGC5000O"
        assert p.quantity_open == 10.0
        assert p.average_entry_price == 100.0
        assert p.side == "buy"
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_open_positions_uses_quantity_after_for_partial_exit():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "buy", "10", "10", "100.0", "entrada", ""],
        ["2026-09-03T10:00:00+00:00", "PARTIAL_EXIT", "GFGC5000O", "weekly_asymmetric", "pos1", "k", "sell", "-4", "6", "130.0", "partial_profit_take", ""],
        # nunca llega el CLOSE - sigue abierta con 6 de las 10 originales
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        open_positions, closed_count, incomplete = reconstruct_open_positions(rows)
        assert len(open_positions) == 1
        p = open_positions[0]
        assert p.quantity_open == 6.0  # quantity_after del ultimo evento, no la suma de patas de entrada
        assert p.average_entry_price == 100.0  # promedio de entrada no cambia por un PARTIAL_EXIT
        assert p.last_event_type == "PARTIAL_EXIT"
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_open_positions_excludes_closed_positions():
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos_closed", "k", "buy", "10", "10", "100.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos_closed", "k", "sell", "-10", "0", "110.0", "r", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        open_positions, closed_count, incomplete = reconstruct_open_positions(rows)
        assert open_positions == []
        assert closed_count == 1
        assert incomplete == 0
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_open_positions_counts_missing_entry_as_incomplete_data_never_fabricated():
    # Posicion abierta ANTES del inicio de la ventana del journal: nunca
    # aparece un ENTRY/ADD parseable. No debe fabricarse un precio de
    # entrada - se cuenta aparte, explicitamente.
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "PARTIAL_EXIT", "GFGC5000O", "weekly_asymmetric", "pos_legacy", "k", "sell", "-2", "8", "130.0", "partial_profit_take", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        open_positions, closed_count, incomplete = reconstruct_open_positions(rows)
        assert open_positions == []
        assert closed_count == 0
        assert incomplete == 1
    finally:
        path.unlink(missing_ok=True)


def test_reconstruct_open_positions_partition_is_exhaustive_and_matches_closed_trades_invariant():
    """
    Invariante documentado en reconstruct_open_positions: llamando ambas
    funciones sobre el MISMO rows, len(open)+incomplete_open ==
    still_open_count(de reconstruct_lifecycle_trades), y
    closed_count(de reconstruct_open_positions) == len(trades)+incomplete_closed.
    Este es el chequeo cruzado por Position ID que usa el panel de
    reconciliacion del dashboard.
    """
    path = _write_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGC5000O", "weekly_asymmetric", "pos_ok", "k", "buy", "10", "10", "100.0", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "GFGC5000O", "weekly_asymmetric", "pos_ok", "k", "sell", "-10", "0", "110.0", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "GFGV5000O", "weekly_asymmetric", "pos_open", "k2", "buy", "10", "10", "50.0", "e", ""],
        ["2026-09-05T10:00:00+00:00", "CLOSE", "GFGX5000O", "weekly_asymmetric", "pos_legacy", "k3", "sell", "-10", "0", "120.0", "take_profit", ""],
    ], _LIFECYCLE_HEADER)
    try:
        rows = load_lifecycle_journal_rows(path)
        trades, still_open, incomplete_closed = reconstruct_lifecycle_trades(rows)
        open_positions, closed_count, incomplete_open = reconstruct_open_positions(rows)

        assert len(open_positions) + incomplete_open == still_open
        assert closed_count == len(trades) + incomplete_closed
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_load_lifecycle_journal_rows_maps_spanish_headers_and_sorts_chronologically,
    test_reconstruct_lifecycle_trades_simple_entry_and_close,
    test_reconstruct_lifecycle_trades_handles_partial_exit_then_close,
    test_reconstruct_lifecycle_trades_excludes_positions_without_close,
    test_reconstruct_lifecycle_trades_partial_exit_without_close_default_excluded,
    test_reconstruct_lifecycle_trades_include_partial_realized_for_open_positions_counts_realized_pnl,
    test_reconstruct_lifecycle_trades_include_partial_realized_still_excludes_pure_open_positions,
    test_reconstruct_lifecycle_trades_counts_close_without_entry_as_incomplete_data,
    test_reconstruct_lifecycle_trades_position_counts_are_exhaustive_and_disjoint,
    test_reconstruct_lifecycle_trades_filters_by_strategy,
    test_reconstruct_lifecycle_trades_ignores_reject_rows_without_position_id,
    test_load_closed_trades_export_parses_and_validates_pnl,
    test_load_closed_trades_export_excludes_rows_with_inconsistent_pnl,
    test_load_closed_trades_export_populates_direction_from_column,
    test_reconstruct_lifecycle_trades_populates_direction_from_entry_side,
    test_reconstruct_open_positions_returns_open_position_with_real_strategy_tag,
    test_reconstruct_open_positions_uses_quantity_after_for_partial_exit,
    test_reconstruct_open_positions_excludes_closed_positions,
    test_reconstruct_open_positions_counts_missing_entry_as_incomplete_data_never_fabricated,
    test_reconstruct_open_positions_partition_is_exhaustive_and_matches_closed_trades_invariant,
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
