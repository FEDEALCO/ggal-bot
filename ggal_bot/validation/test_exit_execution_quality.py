"""
test_exit_execution_quality.py
==================================
Tests de la instrumentacion de calidad de ejecucion en el path de SALIDA
(TANDA 2 "OPTIMIZACION EJECUTABLE", seccion 9, 2026-09-08,
run_bot.py::_act_on_exit_signal). A diferencia del path de entrada (que ya
excluye quotes stale ANTES de emitir una señal, ver
RiskConfig.max_option_quote_staleness_seconds), el de salida no registraba
la antiguedad/calidad del quote usado - deliberadamente segui sin
BLOQUEAR el exit (ver docstring de KillSwitch: una salida nunca debe
quedar atrapada) pero ahora deja constancia explicita via logs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from run_bot import GgalOptionsBot


@dataclass
class _FakeExitSignal:
    symbol: str
    reason: str
    action: str = "sell_to_close"
    quantity: float = 0.0


def _make_quote(symbol: str, as_of: float) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=570.0, ask=575.0, bid_size=50, ask_size=50, as_of=as_of)
    quote = OptionQuote(
        symbol=symbol, strike=7000.0, expiry=date(2026, 9, 18),
        option_type=OptionType.CALL, book=book, days_calendar=17, days_business=12,
    )
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": 572.5}
    quote.iv = 0.55
    return quote


def test_exit_with_fresh_quote_logs_info_not_warning_and_still_closes(caplog):
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", as_of=time.time()))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=3.0, multiplier=100.0,
            entry_price=500.0, entry_time=None, strategy_tag="weekly_asymmetric",
        ))
        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="take_profit", quantity=3.0)
        with caplog.at_level(logging.INFO, logger="ggal_bot.run_bot"):
            bot._act_on_exit_signal(signal, spot=7050.0)
        assert bot._position_quantity("GFGC7000OC") == 0.0
        info_records = [r for r in caplog.records if r.levelno == logging.INFO and "decision_price" in r.message]
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING and "decision_price" in r.message]
        assert len(info_records) == 1
        assert len(warning_records) == 0
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_exit_with_stale_quote_logs_warning_but_still_closes_the_position():
    """
    Un exit NUNCA debe bloquearse por un quote viejo (ver docstring de
    KillSwitch) - pero debe quedar registrado a nivel WARNING, visible en
    produccion, en vez de proceder en silencio.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        stale_age = SETTINGS.risk.max_option_quote_staleness_seconds + 120.0
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", as_of=time.time() - stale_age))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=3.0, multiplier=100.0,
            entry_price=500.0, entry_time=None, strategy_tag="weekly_asymmetric",
        ))
        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="stop_loss", quantity=3.0)

        import logging as _logging
        caplog_logger = _logging.getLogger("ggal_bot.run_bot")
        records = []
        handler = _logging.Handler()
        handler.emit = lambda record: records.append(record)  # noqa: E731
        caplog_logger.addHandler(handler)
        caplog_logger.setLevel(_logging.INFO)
        try:
            bot._act_on_exit_signal(signal, spot=7050.0)
        finally:
            caplog_logger.removeHandler(handler)

        # La salida SIGUE ejecutandose (nunca se bloquea por un quote viejo).
        assert bot._position_quantity("GFGC7000OC") == 0.0
        warning_records = [r for r in records if r.levelno == _logging.WARNING and "decision_price" in r.getMessage()]
        assert len(warning_records) == 1
        assert "STALE" in warning_records[0].getMessage()
    finally:
        SETTINGS.shadow.enabled = original_enabled


ALL_TESTS = [
    test_exit_with_fresh_quote_logs_info_not_warning_and_still_closes,
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
