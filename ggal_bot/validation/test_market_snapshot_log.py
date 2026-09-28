"""
test_market_snapshot_log.py
==============================
Tests para data/market_snapshot_log.py::MarketSnapshotLogger (MEJORA
2026-09-28: persistencia de la cadena de opciones completa por ciclo, base
para backtesting offline futuro - ver docstring de ese modulo).

Correr con:
    python -m ggal_bot.validation.test_market_snapshot_log
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

from ggal_bot.data.market_snapshot_log import MarketSnapshotLogger
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType


def _quote(symbol, strike=5150.0, option_type=OptionType.CALL, iv=0.45, spot_ref=5200.0,
           greeks=None, expiry=date(2026, 10, 2), days_calendar=10, days_business=7):
    book = OrderBookSnapshot(symbol, bid=95.0, ask=105.0, bid_size=100.0, ask_size=100.0, last_volume=1000.0)
    q = OptionQuote(symbol, strike=strike, expiry=expiry, option_type=option_type,
                     book=book, days_calendar=days_calendar, days_business=days_business)
    q.iv = iv
    q.spot_ref = spot_ref
    q.greeks = greeks
    return q


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="market_snapshot_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero (no existe todavia)
    return path


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        MarketSnapshotLogger(path=path)
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == MarketSnapshotLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_not_duplicate_header_on_reuse():
    path = _temp_logger_path()
    try:
        MarketSnapshotLogger(path=path)
        MarketSnapshotLogger(path=path)  # segunda instancia sobre el mismo archivo (ej. reinicio del bot)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows.count(MarketSnapshotLogger._HEADER) == 1
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_one_row_per_quote():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        quotes = [
            _quote("GFGC5150O", greeks={"delta": 0.55, "gamma": 0.002, "vega": 3.1, "theta": -0.8}),
            _quote("GFGV5150O", option_type=OptionType.PUT, greeks=None),
        ]
        logger.log_quotes(quotes, now=now)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas
        assert rows[1][1] == "GFGC5150O"
        assert rows[2][1] == "GFGV5150O"
    finally:
        path.unlink(missing_ok=True)


def test_logger_leaves_iv_and_greeks_blank_when_missing_never_fabricated():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        q = _quote("GFGC5150O", iv=None, greeks=None)
        logger.log_quotes([q])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header = rows[0]
        row = dict(zip(header, rows[1]))
        assert row["iv"] == ""
        assert row["delta"] == "" and row["gamma"] == "" and row["vega"] == "" and row["theta"] == ""
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_across_multiple_calls():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([_quote("GFGC5150O")])
        logger.log_quotes([_quote("GFGC5150O")])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas (append-only, nunca sobreescribe)
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_nothing_with_empty_quotes_iterable():
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        logger.log_quotes([])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # solo el header, ninguna fila fabricada
    finally:
        path.unlink(missing_ok=True)


def test_logger_write_failure_does_not_raise():
    """Igual que ShadowAuditLogger/PositionEventJournal: un fallo de disco al auditar
    NUNCA debe tumbar una decision de trading real ya tomada."""
    path = _temp_logger_path()
    try:
        logger = MarketSnapshotLogger(path=path)
        # Se fuerza un path invalido tras la creacion, para simular una falla de escritura
        # (ej. disco lleno / permiso revocado a mitad de sesion) sin tocar el sistema real.
        logger._path = Path("/root/no_existe/no_se_puede_crear/market_snapshots.csv")
        logger.log_quotes([_quote("GFGC5150O")])  # no debe lanzar excepcion
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_logger_does_not_duplicate_header_on_reuse,
    test_logger_appends_one_row_per_quote,
    test_logger_leaves_iv_and_greeks_blank_when_missing_never_fabricated,
    test_logger_appends_across_multiple_calls,
    test_logger_does_nothing_with_empty_quotes_iterable,
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
