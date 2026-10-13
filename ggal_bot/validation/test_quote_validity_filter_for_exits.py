"""
test_quote_validity_filter_for_exits.py
==========================================
RiskConfig.enforce_quote_validity_filter_for_exits (Fase 0 del mandato de
research cuantitativo, 2026-10-13, a pedido EXPLICITO del usuario: "Construi
el filtro de quote operable (dos puntas, spread maximo, antiguedad maxima)
y aplicalo a stops y salidas: si el quote no pasa, el stop espera al primer
quote valido").

Hasta esta mejora, `current_prices` (el precio que Stop Loss/Take Profit
usan para medir PnL%, ver _run_weekly_asymmetric_cycle/_run_scalping_cycle
en run_bot.py) solo exigia book de dos puntas (bid>0/ask>0) - un book
anormalmente ANCHO o VIEJO pero con dos puntas pasaba igual, pudiendo
disparar un Stop Loss/Take Profit real contra un precio no cruzable de
verdad (caso real: GFGC6800OC, 2026-10-07T13:30:21 UTC, spread_rel=0.80,
16x el limite de 5% - ver test_exit_execution_quality.py, que SOLO alertaba
via WARNING sin cambiar el precio usado).

Estos tests reproducen el escenario minimo para weekly_asymmetric y, por
separado, para scalping (el filtro se aplica a AMBAS estrategias de forma
idéntica - ver OrderBookSnapshot.is_valid_quote en data/option_chain.py).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from run_bot import GgalOptionsBot


EXPIRY = date(2026, 10, 16)


def _make_quote(symbol: str, bid: float, ask: float, as_of: float) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=bid, ask=ask, bid_size=50.0, ask_size=50.0, as_of=as_of)
    quote = OptionQuote(
        symbol=symbol, strike=7000.0, expiry=EXPIRY,
        option_type=OptionType.CALL, book=book, days_calendar=8, days_business=6,
    )
    mid = (bid + ask) / 2.0
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": mid}
    quote.iv = 0.55
    return quote


class _IsolatedQuoteFilterSettings:
    """
    Guarda/restaura los settings que tocan estos tests y apaga
    weekend_theta_guard (ver docstring equivalente en
    test_fresh_greeks_for_vega_decay_exit.py) - sin esto puede tapar la
    evaluacion de Stop Loss/Take Profit bajo evaluacion.
    """

    def __init__(self, enforce_filter: bool):
        self.enforce_filter = enforce_filter

    def __enter__(self):
        self._shadow = SETTINGS.shadow.enabled
        self._flag = SETTINGS.risk.enforce_quote_validity_filter_for_exits
        self._weekend_guard = SETTINGS.long_first.weekend_theta_guard_enabled
        self._eod_close = SETTINGS.scalping.eod_close_enabled
        SETTINGS.shadow.enabled = True
        SETTINGS.risk.enforce_quote_validity_filter_for_exits = self.enforce_filter
        SETTINGS.long_first.weekend_theta_guard_enabled = False
        SETTINGS.scalping.eod_close_enabled = False
        return self

    def __exit__(self, *exc_info):
        SETTINGS.shadow.enabled = self._shadow
        SETTINGS.risk.enforce_quote_validity_filter_for_exits = self._flag
        SETTINGS.long_first.weekend_theta_guard_enabled = self._weekend_guard
        SETTINGS.scalping.eod_close_enabled = self._eod_close


def _weekly_stop_loss_signals(bot: GgalOptionsBot, spot: float):
    signals = bot._run_weekly_asymmetric_cycle(spot=spot)
    return [s for s in signals if getattr(s, "reason", None) == "stop_loss"]


# ---------------------------------------------------------------------------
# weekly_asymmetric
# ---------------------------------------------------------------------------

def test_wide_spread_quote_does_not_trigger_stop_loss_when_filter_enabled():
    """
    Book con dos puntas y FRESCO, pero spread_rel=0.80 (16x el limite de
    5%, el spread real del incidente GFGC6800OC) - con el filtro activo
    (default), ese precio no debe usarse para medir Stop Loss: la posicion
    simplemente queda sin evaluar SL este ciclo (espera al primer quote
    valido), aunque el precio este muy por debajo del umbral.
    """
    with _IsolatedQuoteFilterSettings(enforce_filter=True):
        bot = GgalOptionsBot()
        # bid=5/ask=20 -> mid=12.5, perdida de -50.98% sobre entry_price=25.5
        # (supera el stop_loss_pct default de 50%) - pero el spread_relative
        # de ESTE book es (20-5)/12.5 = 1.20 (120%), muy por encima del
        # limite de 5% (RiskConfig.max_spread_relative).
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", bid=5.0, ask=20.0, as_of=time.time()))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
            entry_price=25.5, entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            expiry=EXPIRY, strategy_tag="weekly_asymmetric",
        ))
        signals = _weekly_stop_loss_signals(bot, spot=7000.0)
        assert not signals, (
            f"No deberia dispararse Stop Loss contra un book con spread 133% - señales: {signals}"
        )


def test_stale_quote_does_not_trigger_stop_loss_when_filter_enabled():
    """
    Mismo book (tight spread, precio por debajo del Stop Loss) pero con
    `as_of` mas viejo que max_option_quote_staleness_seconds - con el
    filtro activo, tampoco debe disparar SL.
    """
    with _IsolatedQuoteFilterSettings(enforce_filter=True):
        bot = GgalOptionsBot()
        # bid=12.4/ask=12.6 -> mid=12.5, misma perdida de -50.98% que arriba
        # (supera el stop_loss_pct default de 50%), con spread_relative
        # angosto (1.6%, dentro del limite de 5%) - lo unico que invalida
        # este quote es la antiguedad (as_of viejo).
        stale_age = SETTINGS.risk.max_option_quote_staleness_seconds + 120.0
        bot.option_chain.upsert_quote(
            _make_quote("GFGC7000OC", bid=12.4, ask=12.6, as_of=time.time() - stale_age)
        )
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
            entry_price=25.5, entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            expiry=EXPIRY, strategy_tag="weekly_asymmetric",
        ))
        signals = _weekly_stop_loss_signals(bot, spot=7000.0)
        assert not signals, f"No deberia dispararse Stop Loss contra un quote stale - señales: {signals}"


def test_valid_quote_still_triggers_stop_loss_when_filter_enabled():
    """
    Control: el MISMO precio de perdida, pero con book de dos puntas,
    spread normal y fresco - Stop Loss debe seguir disparando igual que
    siempre, confirmando que el fix no rompe el caso real que esta salida
    esta pensada para cubrir.
    """
    with _IsolatedQuoteFilterSettings(enforce_filter=True):
        bot = GgalOptionsBot()
        # bid=12.4/ask=12.6 -> mid=12.5, perdida de -50.98% (supera el
        # stop_loss_pct default de 50%), spread angosto (1.6%) y fresco.
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", bid=12.4, ask=12.6, as_of=time.time()))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
            entry_price=25.5, entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            expiry=EXPIRY, strategy_tag="weekly_asymmetric",
        ))
        signals = _weekly_stop_loss_signals(bot, spot=7000.0)
        assert signals, "Con book valido y perdida real, Stop Loss debe seguir disparando."


def test_wide_spread_quote_triggers_stop_loss_when_flag_disabled():
    """
    Con el flag desactivado explicitamente, se reproduce el comportamiento
    previo a esta mejora (solo exige dos puntas, sin chequear spread/edad).
    """
    with _IsolatedQuoteFilterSettings(enforce_filter=False):
        bot = GgalOptionsBot()
        # Mismo book de spread 120% que el primer test de arriba (perdida
        # de -50.98%, supera el stop_loss_pct default de 50%).
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", bid=5.0, ask=20.0, as_of=time.time()))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
            entry_price=25.5, entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            expiry=EXPIRY, strategy_tag="weekly_asymmetric",
        ))
        signals = _weekly_stop_loss_signals(bot, spot=7000.0)
        assert signals, (
            "Con el flag desactivado explicitamente, deberia reproducirse el comportamiento previo "
            "(dispara igual con un book de spread ancho, mientras tenga dos puntas)."
        )


# ---------------------------------------------------------------------------
# scalping - mismo filtro, aplicado al otro call site en run_bot.py
# ---------------------------------------------------------------------------

def test_scalping_stop_loss_also_respects_the_quote_validity_filter():
    original_scalping_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = True
    try:
        with _IsolatedQuoteFilterSettings(enforce_filter=True):
            bot = GgalOptionsBot()
            assert bot.scalping_strategy is not None
            bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", bid=5.0, ask=25.0, as_of=time.time()))
            bot.portfolio.add(Position(
                symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
                entry_price=25.5, entry_time=datetime.now(timezone.utc),
                expiry=EXPIRY, strategy_tag="scalping",
            ))
            signals = bot._run_scalping_cycle(spot=7000.0)
            stop_loss_signals = [s for s in signals if getattr(s, "reason", None) == "stop_loss"]
            assert not stop_loss_signals, (
                f"Scalping tampoco deberia disparar Stop Loss contra un book de spread 133% - "
                f"señales: {stop_loss_signals}"
            )
    finally:
        SETTINGS.scalping.enabled = original_scalping_enabled


ALL_TESTS = [
    test_wide_spread_quote_does_not_trigger_stop_loss_when_filter_enabled,
    test_stale_quote_does_not_trigger_stop_loss_when_filter_enabled,
    test_valid_quote_still_triggers_stop_loss_when_filter_enabled,
    test_wide_spread_quote_triggers_stop_loss_when_flag_disabled,
    test_scalping_stop_loss_also_respects_the_quote_validity_filter,
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
