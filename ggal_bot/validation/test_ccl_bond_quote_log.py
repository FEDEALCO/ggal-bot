"""
test_ccl_bond_quote_log.py
=============================
Tests para ggal_bot/data/ccl_bond_quote_log.py (MEJORA 2026-09-30, panel
de CCL implicito - ver REPORT.md). log_records() se testea sin red (con
`records` ya armados), fetch_and_log() solo delega a http_get_json +
log_records(), no se testea contra la red real aca.

Correr con:
    python -m ggal_bot.validation.test_ccl_bond_quote_log
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.data.ccl_bond_quote_log import CclBondQuoteLogger


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="ccl_bond_quotes_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero
    return path


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        CclBondQuoteLogger(path=path)
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == CclBondQuoteLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_log_records_writes_one_row_per_configured_ticker_found():
    path = _temp_logger_path()
    try:
        logger = CclBondQuoteLogger(path=path)
        now = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
        records = [
            {"symbol": "GD30", "px_bid": 87500.0, "px_ask": 87600.0, "c": 87540.0, "q_bid": 10, "q_ask": 10},
            {"symbol": "GD30C", "px_bid": 54.10, "px_ask": 54.20, "c": 54.15, "q_bid": 5, "q_ask": 5},
            {"symbol": "AL30", "px_bid": 83900.0, "px_ask": 84000.0, "c": 83950.0, "q_bid": 8, "q_ask": 8},
            {"symbol": "AL30C", "px_bid": 51.80, "px_ask": 51.95, "c": 51.89, "q_bid": 3, "q_ask": 3},
            {"symbol": "OTRO_BONO_IRRELEVANTE", "px_bid": 1.0, "px_ask": 1.0, "c": 1.0, "q_bid": 1, "q_ask": 1},
        ]
        logger.log_records(records, now=now)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 5  # header + 4 tickers configurados (el 5to record se ignora)
        header = rows[0]
        symbols = [dict(zip(header, r))["symbol"] for r in rows[1:]]
        assert set(symbols) == {"GD30", "GD30C", "AL30", "AL30C"}
    finally:
        path.unlink(missing_ok=True)


def test_log_records_never_fabricates_row_for_missing_ticker():
    path = _temp_logger_path()
    try:
        logger = CclBondQuoteLogger(path=path)
        logger.log_records([{"symbol": "GD30", "px_bid": 1.0, "px_ask": 1.0, "c": 1.0}])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 2  # header + solo GD30, nunca una fila fabricada para GD30C/AL30/AL30C
    finally:
        path.unlink(missing_ok=True)


def test_log_records_does_nothing_with_empty_or_none_records():
    path = _temp_logger_path()
    try:
        logger = CclBondQuoteLogger(path=path)
        logger.log_records([])
        logger.log_records(None)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1  # solo el header
    finally:
        path.unlink(missing_ok=True)


def test_custom_ticker_list_overrides_default():
    path = _temp_logger_path()
    try:
        logger = CclBondQuoteLogger(path=path, tickers=("AL30",))
        logger.log_records([
            {"symbol": "GD30", "px_bid": 1.0, "px_ask": 1.0, "c": 1.0},
            {"symbol": "AL30", "px_bid": 2.0, "px_ask": 2.0, "c": 2.0},
        ])
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header = rows[0]
        symbols = [dict(zip(header, r))["symbol"] for r in rows[1:]]
        assert symbols == ["AL30"]
    finally:
        path.unlink(missing_ok=True)


def test_write_failure_does_not_raise():
    path = _temp_logger_path()
    try:
        logger = CclBondQuoteLogger(path=path)
        logger._path = Path("/root/no_existe/no_se_puede_crear/ccl_bond_quotes.csv")
        logger.log_records([{"symbol": "GD30", "px_bid": 1.0, "px_ask": 1.0, "c": 1.0}])  # no debe lanzar
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_log_records_writes_one_row_per_configured_ticker_found,
    test_log_records_never_fabricates_row_for_missing_ticker,
    test_log_records_does_nothing_with_empty_or_none_records,
    test_custom_ticker_list_overrides_default,
    test_write_failure_does_not_raise,
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
