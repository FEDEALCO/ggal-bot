"""
test_portfolio_greeks_log.py
==============================
Tests para portfolio/portfolio_greeks_log.py::PortfolioGreeksLogger (Tarea
#27/#28 item 5, 2026-10-02: "Logger periodico de griegas de cartera y por
estrategia, default ON" - ver docstring de ese modulo para el por que de
un archivo nuevo, en formato largo, separado del snapshot vivo que ya
publica ggal_bot/state_writer.py).

Correr con:
    python -m ggal_bot.validation.test_portfolio_greeks_log
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.portfolio.portfolio_greeks_log import PortfolioGreeksLogger


def _temp_logger_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="portfolio_greeks_log_test_", suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()  # el logger debe poder crearlo desde cero (no existe todavia)
    return path


def test_logger_writes_header_on_first_use():
    path = _temp_logger_path()
    try:
        PortfolioGreeksLogger(path=path)
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == PortfolioGreeksLogger._HEADER
    finally:
        path.unlink(missing_ok=True)


def test_logger_does_not_duplicate_header_on_reuse():
    path = _temp_logger_path()
    try:
        PortfolioGreeksLogger(path=path)
        PortfolioGreeksLogger(path=path)  # segunda instancia sobre el mismo archivo (ej. reinicio del bot)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows.count(PortfolioGreeksLogger._HEADER) == 1
    finally:
        path.unlink(missing_ok=True)


def test_logger_writes_one_row_for_portfolio_plus_one_per_strategy():
    path = _temp_logger_path()
    try:
        logger = PortfolioGreeksLogger(path=path)
        now = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
        logger.log_greeks(
            portfolio_totals={"delta": 1800.0, "gamma": 5.0, "vega": 300.0, "theta": -50.0},
            per_strategy_totals={
                "weekly_asymmetric": {"delta": 1200.0, "gamma": 3.0, "vega": 200.0, "theta": -30.0},
                "scalping": {"delta": 600.0, "gamma": 2.0, "vega": 100.0, "theta": -20.0},
            },
            now=now,
        )
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 4  # header + portfolio + 2 estrategias
        by_scope = {r[1]: r for r in rows[1:]}
        assert by_scope["portfolio"][2:] == ["1800.0", "5.0", "300.0", "-50.0"]
        assert by_scope["weekly_asymmetric"][2:] == ["1200.0", "3.0", "200.0", "-30.0"]
        assert by_scope["scalping"][2:] == ["600.0", "2.0", "100.0", "-20.0"]
        assert all(r[0] == now.isoformat() for r in rows[1:])
    finally:
        path.unlink(missing_ok=True)


def test_logger_scope_rows_are_alphabetically_sorted_for_determinism():
    path = _temp_logger_path()
    try:
        logger = PortfolioGreeksLogger(path=path)
        logger.log_greeks(
            portfolio_totals={"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0},
            per_strategy_totals={
                "scalping": {"delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0},
                "vol_arbitrage": {"delta": 2.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0},
                "weekly_asymmetric": {"delta": 3.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0},
            },
        )
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        scopes = [r[1] for r in rows[1:]]
        assert scopes == ["portfolio", "scalping", "vol_arbitrage", "weekly_asymmetric"]
    finally:
        path.unlink(missing_ok=True)


def test_logger_leaves_missing_greek_blank_never_fabricated():
    path = _temp_logger_path()
    try:
        logger = PortfolioGreeksLogger(path=path)
        logger.log_greeks(portfolio_totals={"delta": 10.0}, per_strategy_totals={})
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        row = dict(zip(rows[0], rows[1]))
        assert row["delta"] == "10.0"
        assert row["gamma"] == "" and row["vega"] == "" and row["theta"] == ""
    finally:
        path.unlink(missing_ok=True)


def test_logger_appends_across_multiple_calls():
    path = _temp_logger_path()
    try:
        logger = PortfolioGreeksLogger(path=path)
        logger.log_greeks(portfolio_totals={"delta": 1.0}, per_strategy_totals={})
        logger.log_greeks(portfolio_totals={"delta": 2.0}, per_strategy_totals={})
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 filas (append-only, nunca sobreescribe)
    finally:
        path.unlink(missing_ok=True)


def test_logger_write_failure_does_not_raise():
    """Igual que ShadowAuditLogger/PositionEventJournal/MarketSnapshotLogger: un fallo de
    disco al auditar NUNCA debe tumbar una decision de trading real ya tomada."""
    path = _temp_logger_path()
    try:
        logger = PortfolioGreeksLogger(path=path)
        logger._path = Path("/root/no_existe/no_se_puede_crear/portfolio_greeks_log.csv")
        logger.log_greeks(portfolio_totals={"delta": 1.0}, per_strategy_totals={})  # no debe lanzar
    finally:
        path.unlink(missing_ok=True)


def test_recompute_cycle_wires_portfolio_greeks_log_with_portfolio_and_per_strategy_rows():
    """
    Integracion contra el bot real (Tarea #27/#28 item 5): confirma que
    GgalOptionsBot.recompute_cycle() efectivamente llama a
    self.portfolio_greeks_log.log_greeks() con el total de cartera y el
    desglose por cada una de VALID_STRATEGIES - no solo que la clase
    funciona aislada (eso ya lo cubren los tests de arriba).

    Usa tempfile.TemporaryDirectory() (no el fixture tmp_path de pytest) a
    proposito: este archivo corre tanto bajo pytest como standalone (`python
    -m ggal_bot.validation.test_portfolio_greeks_log`, ver ALL_TESTS mas
    abajo), y tmp_path no esta disponible fuera de pytest.
    """
    import tempfile
    from datetime import date

    from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401
    from ggal_bot.config import SETTINGS
    from ggal_bot.data.option_chain import OrderBookSnapshot
    from ggal_bot.portfolio.portfolio import Position
    from run_bot import GgalOptionsBot

    original_shadow = SETTINGS.shadow.enabled
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_market_hours_gate = False  # ver docstring de test_scalping_exclusive_selection_recompute_cycle_never_calls_other_strategies
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "portfolio_greeks_log_test.csv"
            bot = GgalOptionsBot()
            bot.portfolio_greeks_log = PortfolioGreeksLogger(path=log_path)
            bot.market_feed.poll = lambda *_a, **_kw: None  # no pegarle a la red real
            bot._spot_book = OrderBookSnapshot(
                SETTINGS.instruments.contado_ticker, bid=6999.0, ask=7001.0, bid_size=100, ask_size=100,
            )
            bot.portfolio.add(Position(
                symbol="GFGC7000OC", quantity=2, multiplier=100,
                entry_price=100.0, entry_time=datetime.now(timezone.utc) - timedelta(days=1),
                greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0},
                expiry=date(2026, 10, 16), strategy_tag="weekly_asymmetric",
            ))

            bot.recompute_cycle()

            with open(log_path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            scopes = {r[1] for r in rows[1:]}
            assert scopes == {"portfolio", "weekly_asymmetric", "vol_arbitrage", "scalping"}
            portfolio_row = next(r for r in rows[1:] if r[1] == "portfolio")
            weekly_row = next(r for r in rows[1:] if r[1] == "weekly_asymmetric")
            assert portfolio_row[2] == weekly_row[2] == "100.0"  # delta = 2*100*0.5
    finally:
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.risk.enforce_market_hours_gate = original_gate


ALL_TESTS = [
    test_logger_writes_header_on_first_use,
    test_logger_does_not_duplicate_header_on_reuse,
    test_logger_writes_one_row_for_portfolio_plus_one_per_strategy,
    test_logger_scope_rows_are_alphabetically_sorted_for_determinism,
    test_logger_leaves_missing_greek_blank_never_fabricated,
    test_logger_appends_across_multiple_calls,
    test_logger_write_failure_does_not_raise,
    test_recompute_cycle_wires_portfolio_greeks_log_with_portfolio_and_per_strategy_rows,
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
