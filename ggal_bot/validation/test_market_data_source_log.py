"""
test_market_data_source_log.py
================================
Tests para data/market_data_source_log.py::MarketDataSourceLogger (Tarea
#27/#28 item 3(b), 2026-10-02: "que la fuente activa quede registrada en
cada fill y evento del journal" - ver docstring de ese modulo para el por
que de un archivo nuevo en vez de una columna en shadow_trades.csv/
position_events.csv).

Correr con:
    python -m ggal_bot.validation.test_market_data_source_log
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="market_data_source_log_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero (no existe todavia)
    return path


from ggal_bot.data.market_data_source_log import MarketDataSourceLogger


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        MarketDataSourceLogger(path=path)
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == MarketDataSourceLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_not_duplicate_header_on_reuse():
    path = _temp_logger_path()
    try:
        MarketDataSourceLogger(path=path)
        MarketDataSourceLogger(path=path)  # segunda instancia sobre el mismo archivo (ej. reinicio del bot)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows.count(MarketDataSourceLogger._HEADER) == 1
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_one_row_per_call_with_correlation_id():
    path = _temp_logger_path()
    try:
        logger = MarketDataSourceLogger(path=path)
        logger.log_source("shadow_fill", "abc123", "MockReplaySource")
        logger.log_source("ENTRY", "pos-1", "Data912RestSource")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas
        assert rows[1][1:] == ["shadow_fill", "abc123", "MockReplaySource"]
        assert rows[2][1:] == ["ENTRY", "pos-1", "Data912RestSource"]
    finally:
        path.unlink(missing_ok=True)


def test_logger_write_failure_does_not_raise():
    """Igual que ShadowAuditLogger/PositionEventJournal/MarketSnapshotLogger: un fallo de
    disco al auditar NUNCA debe tumbar una decision de trading real ya tomada."""
    path = _temp_logger_path()
    try:
        logger = MarketDataSourceLogger(path=path)
        logger._path = Path("/root/no_existe/no_se_puede_crear/market_data_source_log.csv")
        logger.log_source("shadow_fill", "abc123", "MockReplaySource")  # no debe lanzar
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_logger_does_not_duplicate_header_on_reuse,
    test_logger_appends_one_row_per_call_with_correlation_id,
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
