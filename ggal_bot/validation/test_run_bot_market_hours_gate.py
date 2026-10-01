"""
test_run_bot_market_hours_gate.py
====================================
Tests para GgalOptionsBot._market_data_is_reliable_for_trading() y su
cableado en recompute_cycle() (MEJORA 2026-10-01, URGENTE a pedido
explicito del usuario - ver RiskConfig.enforce_market_hours_gate y
ggal_bot/market_hours.py para la evidencia real completa: 247 de 1183
fills de produccion fuera de 11:00-17:00 ART, con un caso concreto de
~ARS 420.000 de PnL fabricado en 23 minutos sobre GFGC6600OC, causado por
el failover automatico de LiveShadowFeed cayendo a MockReplaySource fuera
de rueda).

Correr con:
    python -m ggal_bot.validation.test_run_bot_market_hours_gate
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway (mismo
# motivo que el resto de la suite, ver test_strategy_selector.py): evita
# contaminar logs/shadow_trades.csv real.
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot import market_hours
from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot
from run_bot import GgalOptionsBot

_WEEKDAY_IN_SESSION = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)   # jueves, 11:00 ART
_WEEKDAY_OVERNIGHT = datetime(2026, 9, 29, 13, 28, tzinfo=timezone.utc)   # martes, 10:28 ART (el caso real)


class _FakeRealSource:
    """Standin minimo para 'la fuente activa NO es Mock' sin pegarle a la red."""


def _make_shadow_bot() -> GgalOptionsBot:
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
    finally:
        SETTINGS.shadow.enabled = original_shadow
    bot._spot_book = OrderBookSnapshot(
        SETTINGS.instruments.contado_ticker, bid=6999.0, ask=7001.0, bid_size=100, ask_size=100,
    )
    return bot


def _with_frozen_clock(now, fn):
    original = market_hours.is_within_byma_session
    market_hours.is_within_byma_session = lambda *_a, **_kw: original(now)
    try:
        return fn()
    finally:
        market_hours.is_within_byma_session = original


def test_gate_returns_true_when_disabled_regardless_of_clock_or_source():
    bot = _make_shadow_bot()
    bot.market_feed._source = _FakeRealSource()
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = False
    try:
        assert _with_frozen_clock(_WEEKDAY_OVERNIGHT, bot._market_data_is_reliable_for_trading) is True
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


def test_gate_false_outside_session_hours_even_with_a_real_source():
    bot = _make_shadow_bot()
    bot.market_feed._source = _FakeRealSource()  # "real", no Mock
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = True
    try:
        # El caso real exacto: 10:28 ART, antes de la apertura asumida.
        assert _with_frozen_clock(_WEEKDAY_OVERNIGHT, bot._market_data_is_reliable_for_trading) is False
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


def test_gate_true_within_session_hours_with_a_real_source():
    bot = _make_shadow_bot()
    bot.market_feed._source = _FakeRealSource()
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = True
    try:
        assert _with_frozen_clock(_WEEKDAY_IN_SESSION, bot._market_data_is_reliable_for_trading) is True
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


def test_gate_false_within_session_hours_if_active_source_is_mock():
    """
    El caso mas importante: AUNQUE el reloj diga que es horario de rueda,
    si la fuente activa es MockReplaySource (el failover automatico cayo
    ahi, ver ShadowConfig.source_failure_threshold), el dato sigue siendo
    100% fabricado - el gate debe bloquear igual.
    """
    from ggal_bot.data.live_shadow_feed import MockReplaySource

    bot = _make_shadow_bot()
    bot.market_feed._source = MockReplaySource()
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = True
    try:
        assert _with_frozen_clock(_WEEKDAY_IN_SESSION, bot._market_data_is_reliable_for_trading) is False
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


def test_gate_ignores_source_name_in_real_broker_mode():
    """En modo NO-shadow (broker real), no hay LiveShadowFeed/MockReplaySource
    que chequear - solo importa el horario."""
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = False
    try:
        bot = GgalOptionsBot()
    finally:
        SETTINGS.shadow.enabled = original_shadow
    bot._spot_book = OrderBookSnapshot(
        SETTINGS.instruments.contado_ticker, bid=6999.0, ask=7001.0, bid_size=100, ask_size=100,
    )
    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = True
    try:
        assert _with_frozen_clock(_WEEKDAY_IN_SESSION, bot._market_data_is_reliable_for_trading) is True
        assert _with_frozen_clock(_WEEKDAY_OVERNIGHT, bot._market_data_is_reliable_for_trading) is False
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


def test_recompute_cycle_skips_strategy_dispatch_and_hedge_when_gate_is_closed():
    """
    Extremo a extremo: con el gate cerrado, recompute_cycle() no debe
    llamar a NINGUNA de las funciones de estrategia ni a _maybe_hedge -
    reproduce la condicion real (fuera de horario + Mock) que produjo fills
    fabricados en produccion.
    """
    bot = _make_shadow_bot()
    bot.market_feed.poll = lambda *_a, **_kw: None  # no pegarle a la red real
    bot.market_feed._source = _FakeRealSource()

    def _boom(*_a, **_kw):
        raise AssertionError("no debia evaluarse ninguna estrategia/hedge con el gate cerrado")

    bot._run_weekly_asymmetric_cycle = _boom
    bot._run_vol_arbitrage_cycle = _boom
    bot._run_scalping_cycle = _boom
    bot._maybe_hedge = _boom

    original_gate = SETTINGS.risk.enforce_market_hours_gate
    SETTINGS.risk.enforce_market_hours_gate = True
    try:
        _with_frozen_clock(_WEEKDAY_OVERNIGHT, bot.recompute_cycle)  # no debe levantar AssertionError desde _boom
    finally:
        SETTINGS.risk.enforce_market_hours_gate = original_gate


ALL_TESTS = [
    test_gate_returns_true_when_disabled_regardless_of_clock_or_source,
    test_gate_false_outside_session_hours_even_with_a_real_source,
    test_gate_true_within_session_hours_with_a_real_source,
    test_gate_false_within_session_hours_if_active_source_is_mock,
    test_gate_ignores_source_name_in_real_broker_mode,
    test_recompute_cycle_skips_strategy_dispatch_and_hedge_when_gate_is_closed,
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
