"""
test_stale_quote_alert.py
==========================
Tests para la alerta ACTIVA por posicion sin cotizacion vigente (MEJORA
2026-09-17, a pedido explicito del usuario: "analiza a fondo como mejorar
la estrategia del bot"). Ver config.RiskConfig.stale_quote_warning_seconds
y run_bot.py:GgalOptionsBot._warn_positions_without_valid_quote para la
motivacion completa.

Hasta esta mejora, que una base con una posicion abierta se quedara sin
cotizacion vigente en self.option_chain (vencio del universo, cadena caida,
etc.) solo se hacia visible de forma PASIVA en el dashboard (ver
dashboard/app.py, caption "Sin cotizacion actual"), y ese dashboard depende
de reconstruir shadow_trades.csv, no del estado vivo del bot. El riesgo
real (VERIFICADO por lectura de risk/risk_manager.py:
evaluate_position_exit): con current_price=None, Stop Loss/Take Profit/
toma de ganancia parcial/compresion de vega simplemente se OMITEN para esa
posicion - solo el horizonte de dias habiles y la guardia de fin de semana
le siguen aplicando.

Correr con:
    python -m ggal_bot.validation.test_stale_quote_alert
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timezone

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway - ver
# docstring de ese modulo / test_vol_arbitrage_exit_management.py.
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from run_bot import GgalOptionsBot


def _make_bot() -> GgalOptionsBot:
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        return GgalOptionsBot()
    finally:
        SETTINGS.shadow.enabled = original_enabled


def _upsert_valid_quote(bot, symbol, mid=100.0, expiry=date(2026, 12, 18)):
    book = OrderBookSnapshot(symbol, bid=mid - 2.0, ask=mid + 2.0, bid_size=50, ask_size=50)
    q = OptionQuote(symbol, strike=8000.0, expiry=expiry, option_type=OptionType.CALL,
                     book=book, days_calendar=90, days_business=60)
    bot.option_chain.upsert_quote(q)


def _upsert_invalid_quote(bot, symbol, expiry=date(2026, 12, 18)):
    """Simula una base con punta caida (bid=ask=0) - PRESENTE en la cadena pero no operable."""
    book = OrderBookSnapshot(symbol, bid=0.0, ask=0.0, bid_size=0, ask_size=0)
    q = OptionQuote(symbol, strike=8000.0, expiry=expiry, option_type=OptionType.CALL,
                     book=book, days_calendar=90, days_business=60)
    bot.option_chain.upsert_quote(q)


def _add_open_position(bot, symbol="GFGC8000OC", quantity=5):
    bot.portfolio.add(Position(
        symbol=symbol, quantity=quantity, multiplier=100.0,
        entry_price=100.0, entry_time=datetime.now(timezone.utc), expiry=date(2026, 12, 18),
    ))


def test_records_missing_since_on_first_miss_without_warning_yet():
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot)
        # Sin cotizacion en absoluto para esta base (ni siquiera en la cadena).

        bot._warn_positions_without_valid_quote(now=1_000.0)

        assert bot._position_missing_quote_since.get("GFGC8000OC") == 1_000.0
        assert "GFGC8000OC" not in bot._position_missing_quote_warned
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_warns_once_elapsed_time_exceeds_threshold():
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot)

        bot._warn_positions_without_valid_quote(now=1_000.0)   # primer miss: solo se registra
        bot._warn_positions_without_valid_quote(now=1_000.0 + 299.0)  # todavia no llega al umbral
        assert "GFGC8000OC" not in bot._position_missing_quote_warned

        bot._warn_positions_without_valid_quote(now=1_000.0 + 300.0)  # umbral inclusive
        assert "GFGC8000OC" in bot._position_missing_quote_warned
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_invalid_book_bid_ask_zero_counts_as_missing():
    """Una base PRESENTE en la cadena pero con bid=ask=0 (punta caida) debe tratarse igual que ausente."""
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot)
        _upsert_invalid_quote(bot, "GFGC8000OC")

        bot._warn_positions_without_valid_quote(now=1_000.0)
        bot._warn_positions_without_valid_quote(now=1_000.0 + 300.0)

        assert "GFGC8000OC" in bot._position_missing_quote_warned
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_recovering_valid_quote_resets_the_alert_state():
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot)

        bot._warn_positions_without_valid_quote(now=1_000.0)
        bot._warn_positions_without_valid_quote(now=1_000.0 + 300.0)
        assert "GFGC8000OC" in bot._position_missing_quote_warned

        _upsert_valid_quote(bot, "GFGC8000OC")
        bot._warn_positions_without_valid_quote(now=1_000.0 + 301.0)

        assert "GFGC8000OC" not in bot._position_missing_quote_since
        assert "GFGC8000OC" not in bot._position_missing_quote_warned
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_closed_position_is_never_tracked():
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot, quantity=0)  # posicion ya cerrada (quantity=0)

        bot._warn_positions_without_valid_quote(now=1_000.0)

        assert bot._position_missing_quote_since == {}
        assert bot._position_missing_quote_warned == set()
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_closing_position_purges_stale_tracking_state():
    """Si la posicion se cierra DESPUES de haber sido registrada como faltante, el estado debe purgarse."""
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = 300.0
        bot = _make_bot()
        _add_open_position(bot, quantity=5)
        bot._warn_positions_without_valid_quote(now=1_000.0)
        assert "GFGC8000OC" in bot._position_missing_quote_since

        bot.portfolio.positions[0].quantity = 0  # se cerro la posicion
        bot._warn_positions_without_valid_quote(now=1_000.0 + 300.0)

        assert "GFGC8000OC" not in bot._position_missing_quote_since
        assert "GFGC8000OC" not in bot._position_missing_quote_warned
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


def test_disabled_when_threshold_is_none():
    """stale_quote_warning_seconds=None debe ser un no-op completo (ninguna alerta nueva)."""
    original_threshold = SETTINGS.risk.stale_quote_warning_seconds
    try:
        SETTINGS.risk.stale_quote_warning_seconds = None
        bot = _make_bot()
        _add_open_position(bot)

        bot._warn_positions_without_valid_quote(now=1_000.0)
        bot._warn_positions_without_valid_quote(now=1_000.0 + 10_000.0)

        assert bot._position_missing_quote_since == {}
        assert bot._position_missing_quote_warned == set()
    finally:
        SETTINGS.risk.stale_quote_warning_seconds = original_threshold


ALL_TESTS = [
    test_records_missing_since_on_first_miss_without_warning_yet,
    test_warns_once_elapsed_time_exceeds_threshold,
    test_invalid_book_bid_ask_zero_counts_as_missing,
    test_recovering_valid_quote_resets_the_alert_state,
    test_closed_position_is_never_tracked,
    test_closing_position_purges_stale_tracking_state,
    test_disabled_when_threshold_is_none,
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
