"""
test_risk_invariants.py
=========================
Tests de ggal_bot/risk/invariants.py (Tarea #27 item 4, a pedido explicito
del usuario, sesion 2026-10-01: "agrega limites duros... una estrategia
long-only nunca puede quedar neta corta en un contrato; ninguna pata corta
puede quedar sin su pata larga. Si se viola, bloquear la orden y alertar").

Correr con:
    python -m pytest ggal_bot/validation/test_risk_invariants.py
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timezone

import pytest

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk import invariants
from run_bot import GgalOptionsBot


# ---------------------------------------------------------------------------
# Funciones puras
# ---------------------------------------------------------------------------

def test_confirmed_long_quantity_sums_only_positive_lots_of_the_tag():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(symbol="GFGC7000OC", quantity=3.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(symbol="GFGC7000OC", quantity=-2.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(symbol="GFGC7000OC", quantity=9.0, multiplier=100.0, strategy_tag="scalping"))
    assert invariants.confirmed_long_quantity(portfolio, "GFGC7000OC", "weekly_asymmetric") == pytest.approx(8.0)


def test_long_only_net_short_violation_blocks_overselling():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    reason = invariants.long_only_net_short_violation(portfolio, "GFGC7000OC", "weekly_asymmetric", 8.0)
    assert reason is not None
    assert "GFGC7000OC" in reason


def test_long_only_net_short_violation_none_when_within_holdings():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    assert invariants.long_only_net_short_violation(portfolio, "GFGC7000OC", "weekly_asymmetric", 5.0) is None
    assert invariants.long_only_net_short_violation(portfolio, "GFGC7000OC", "weekly_asymmetric", 3.0) is None


def test_naked_short_wing_violation_blocks_when_wing_not_closable_now():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGV5400OC", quantity=75.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(
        symbol="GFGV5000OC", quantity=-75.0, multiplier=100.0,
        strategy_tag="weekly_asymmetric", financed_by_symbol="GFGV5400OC",
    ))
    reason = invariants.naked_short_wing_violation(
        portfolio, "GFGV5400OC", "weekly_asymmetric", reduce_quantity=75.0,
        wing_is_closable_now=lambda symbol: False,
    )
    assert reason is not None
    assert "GFGV5000OC" in reason


def test_naked_short_wing_violation_none_when_wing_is_closable_now():
    """Si la pata corta SI se puede recomprar este mismo ciclo (Tarea #27
    item 3), no se bloquea nada - build_naked_short_wing_exit_signals la va
    a cubrir en el mismo ciclo."""
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGV5400OC", quantity=75.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(
        symbol="GFGV5000OC", quantity=-75.0, multiplier=100.0,
        strategy_tag="weekly_asymmetric", financed_by_symbol="GFGV5400OC",
    ))
    reason = invariants.naked_short_wing_violation(
        portfolio, "GFGV5400OC", "weekly_asymmetric", reduce_quantity=75.0,
        wing_is_closable_now=lambda symbol: True,
    )
    assert reason is None


def test_naked_short_wing_violation_none_when_long_still_covers_after_reduction():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGV5400OC", quantity=75.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(
        symbol="GFGV5000OC", quantity=-30.0, multiplier=100.0,
        strategy_tag="weekly_asymmetric", financed_by_symbol="GFGV5400OC",
    ))
    # reducir 20 de 75 deja 55, que sigue cubriendo la corta de 30
    reason = invariants.naked_short_wing_violation(
        portfolio, "GFGV5400OC", "weekly_asymmetric", reduce_quantity=20.0,
        wing_is_closable_now=lambda symbol: False,
    )
    assert reason is None


def test_check_portfolio_invariants_detects_existing_net_short_without_financed_by():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGC7000OC", quantity=-3.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    violations = invariants.check_portfolio_invariants(portfolio)
    assert any("GFGC7000OC" in v and "NETA CORTA" in v for v in violations)


def test_check_portfolio_invariants_ignores_deliberate_spread_short_leg():
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGV5400OC", quantity=75.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
    portfolio.add(Position(
        symbol="GFGV5000OC", quantity=-75.0, multiplier=100.0,
        strategy_tag="weekly_asymmetric", financed_by_symbol="GFGV5400OC",
    ))
    violations = invariants.check_portfolio_invariants(portfolio)
    assert violations == []


def test_check_portfolio_invariants_detects_already_naked_wing():
    portfolio = Portfolio()
    # la larga ya no esta (se cerro del todo) - la corta quedo descubierta.
    portfolio.add(Position(
        symbol="GFGV5000OC", quantity=-75.0, multiplier=100.0,
        strategy_tag="weekly_asymmetric", financed_by_symbol="GFGV5400OC",
    ))
    violations = invariants.check_portfolio_invariants(portfolio)
    assert any("GFGV5000OC" in v and "sin cobertura" in v for v in violations)


# ---------------------------------------------------------------------------
# Wiring en run_bot.py: _act_on_exit_signal bloquea una venta que excede lo
# confirmado SOLO para partial_profit_take (un cierre total legitimo con
# una cantidad nominal "de sobra" sigue funcionando via el recorte seguro
# ya existente, ver test_fase53_overclose_fix.py).
# ---------------------------------------------------------------------------

def _make_quote(symbol: str, strike: float = 7000.0) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=570.0, ask=575.0, bid_size=50, ask_size=50)
    quote = OptionQuote(
        symbol=symbol, strike=strike, expiry=date(2026, 9, 18),
        option_type=OptionType.CALL, book=book, days_calendar=17, days_business=12,
    )
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": 572.5}
    return quote


class _FakeExitSignal:
    def __init__(self, symbol, reason, quantity, action="sell_to_close"):
        self.symbol = symbol
        self.reason = reason
        self.quantity = quantity
        self.action = action


def test_act_on_exit_signal_blocks_partial_profit_take_exceeding_confirmed_long():
    original_enabled = SETTINGS.shadow.enabled
    original_invariants = SETTINGS.risk.enforce_position_invariants
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_position_invariants = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=572.5,
            entry_time=datetime(2026, 9, 1, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))

        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="partial_profit_take", quantity=8.0)
        bot._act_on_exit_signal(signal, spot=7050.0)

        assert bot._position_quantity("GFGC7000OC") == 5.0, (
            "el invariante debia bloquear la venta parcial que excedia lo confirmado"
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_position_invariants = original_invariants


def test_act_on_exit_signal_full_close_still_works_when_quantity_exceeds_available():
    """Comportamiento preexistente (test_fase53_overclose_fix.py) preservado:
    un cierre TOTAL con una cantidad nominal mayor a lo disponible sigue
    vaciando de forma segura, el invariante no lo bloquea."""
    original_enabled = SETTINGS.shadow.enabled
    original_invariants = SETTINGS.risk.enforce_position_invariants
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_position_invariants = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=572.5,
            entry_time=datetime(2026, 9, 1, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))

        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="stop_loss", quantity=99.0)
        bot._act_on_exit_signal(signal, spot=7050.0)

        assert bot._position_quantity("GFGC7000OC") == 0.0
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_position_invariants = original_invariants


def test_act_on_exit_signal_disabled_flag_skips_the_guard():
    original_enabled = SETTINGS.shadow.enabled
    original_invariants = SETTINGS.risk.enforce_position_invariants
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_position_invariants = False
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=572.5,
            entry_time=datetime(2026, 9, 1, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))
        # Con la guarda apagada, una venta parcial "de mas" NO se bloquea
        # por el invariante (puede seguir topandose con otras guardas, pero
        # no con esta) - usamos una cantidad que de cualquier forma no
        # exceda lo disponible para aislar especificamente este flag.
        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="partial_profit_take", quantity=2.0)
        bot._act_on_exit_signal(signal, spot=7050.0)
        assert bot._position_quantity("GFGC7000OC") == 3.0
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_position_invariants = original_invariants


def test_warn_position_invariant_violations_logs_but_does_not_raise(caplog):
    original_enabled = SETTINGS.shadow.enabled
    original_invariants = SETTINGS.risk.enforce_position_invariants
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_position_invariants = True
    try:
        bot = GgalOptionsBot()
        bot.portfolio.add(Position(symbol="GFGC9999OC", quantity=-3.0, multiplier=100.0, strategy_tag="weekly_asymmetric"))
        with caplog.at_level("ERROR"):
            bot._warn_position_invariant_violations()
        assert any("GFGC9999OC" in r.message for r in caplog.records)
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_position_invariants = original_invariants
