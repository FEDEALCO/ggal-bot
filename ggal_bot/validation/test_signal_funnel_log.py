"""
test_signal_funnel_log.py
============================
Tests para data/signal_funnel_log.py::SignalFunnelLogger (MEJORA
2026-09-29: embudo detallado de candidatas de entrada por ciclo - ver
REPORT.md §12.3/§12.5 punto 5 y docstring de ese modulo).

Correr con:
    python -m ggal_bot.validation.test_signal_funnel_log
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timezone

from ggal_bot.data.signal_funnel_log import SignalFunnelLogger
from ggal_bot.strategy.weekly_asymmetric import CandidateFunnelRecord


def _record(symbol="GFGC5150O", blocked_at=None, **overrides):
    base = dict(
        symbol=symbol, option_type="call", strike=5150.0, expiry=date(2026, 10, 2),
        days_business=7, spot_ref=5200.0, bid=95.0, ask=105.0, bid_size=100.0, ask_size=100.0,
        spread_abs=10.0, spread_relative=0.10, iv=0.45, delta=0.55, gamma=0.002, vega=3.1,
        theta=-0.8, dislocation_vol_points=-4.2, blocked_at=blocked_at,
    )
    base.update(overrides)
    return CandidateFunnelRecord(**base)


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="signal_funnel_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero (no existe todavia)
    return path


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        SignalFunnelLogger(path=path)
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == SignalFunnelLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_not_duplicate_header_on_reuse():
    path = _temp_logger_path()
    try:
        SignalFunnelLogger(path=path)
        SignalFunnelLogger(path=path)  # segunda instancia sobre el mismo archivo (ej. reinicio del bot)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows.count(SignalFunnelLogger._HEADER) == 1
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_one_row_per_record_with_strategy_column():
    path = _temp_logger_path()
    try:
        logger = SignalFunnelLogger(path=path)
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        records = [
            _record("GFGC5150O", blocked_at=None),
            _record("GFGV5150O", blocked_at="moneyness", option_type="put"),
        ]
        logger.log_funnel("weekly_asymmetric", records, now=now)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas
        header = rows[0]
        row0 = dict(zip(header, rows[1]))
        row1 = dict(zip(header, rows[2]))
        assert row0["strategy"] == "weekly_asymmetric" and row0["symbol"] == "GFGC5150O"
        assert row0["blocked_at"] == ""  # califico: nunca se fabrica un motivo
        assert row1["symbol"] == "GFGV5150O" and row1["blocked_at"] == "moneyness"
    finally:
        path.unlink(missing_ok=True)


def test_logger_leaves_missing_market_fields_blank_never_fabricated():
    path = _temp_logger_path()
    try:
        logger = SignalFunnelLogger(path=path)
        r = _record(iv=None, delta=None, gamma=None, vega=None, theta=None, dislocation_vol_points=None)
        logger.log_funnel("weekly_asymmetric", [r])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        row = dict(zip(rows[0], rows[1]))
        assert row["iv"] == "" and row["delta"] == "" and row["gamma"] == ""
        assert row["vega"] == "" and row["theta"] == "" and row["dislocation_vol_points"] == ""
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_nothing_with_empty_records_iterable():
    """
    Caso central de este logger (ver LongFirstConfig/ScalpingConfig.
    enable_signal_funnel_log, default False): con el flag apagado,
    candidate_funnel llega vacio aca - no debe escribirse ninguna fila.
    """
    path = _temp_logger_path()
    try:
        logger = SignalFunnelLogger(path=path)
        logger.log_funnel("weekly_asymmetric", [])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # solo el header, ninguna fila fabricada
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_across_multiple_calls_and_strategies():
    path = _temp_logger_path()
    try:
        logger = SignalFunnelLogger(path=path)
        logger.log_funnel("weekly_asymmetric", [_record("GFGC5150O")])
        logger.log_funnel("scalping", [_record("GFGC5150O")])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas (append-only)
        header = rows[0]
        strategies = [dict(zip(header, row))["strategy"] for row in rows[1:]]
        assert strategies == ["weekly_asymmetric", "scalping"]
    finally:
        path.unlink(missing_ok=True)


def test_logger_write_failure_does_not_raise():
    """Igual que MarketSnapshotLogger/ShadowAuditLogger/PositionEventJournal: un fallo de
    disco al auditar NUNCA debe tumbar una decision de trading real ya tomada."""
    path = _temp_logger_path()
    try:
        logger = SignalFunnelLogger(path=path)
        logger._path = Path("/root/no_existe/no_se_puede_crear/signal_funnel.csv")
        logger.log_funnel("weekly_asymmetric", [_record("GFGC5150O")])  # no debe lanzar excepcion
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_logger_does_not_duplicate_header_on_reuse,
    test_logger_appends_one_row_per_record_with_strategy_column,
    test_logger_leaves_missing_market_fields_blank_never_fabricated,
    test_logger_does_nothing_with_empty_records_iterable,
    test_logger_appends_across_multiple_calls_and_strategies,
    test_logger_write_failure_does_not_raise,
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
