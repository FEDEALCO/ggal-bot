"""
test_entry_spread_gate.py
===========================
Gate de spread/liquidez en ENTRADAS nuevas (hallazgo de auditoria,
2026-10-08, a pedido explicito del usuario - ver
RiskConfig.enforce_entry_spread_gate en config.py y run_bot.py::
_act_on_entry_signal).

Caso real que motiva esto: GFGC6800OC, salida (no entrada) ejecutada el
2026-10-07T13:30:21 UTC contra bid=15.0/ask=35.0 - 80% de spread relativo,
16x el limite de 5% (RiskLimits.max_spread_relative) - sin que absolutamente
nada lo bloqueara ni alertara, porque ese chequeo solo existia para el hedge
delta-neutral (ver delta_hedger.py). Esta mejora cierra el gap para
ENTRADAS (bloquea) - para SALIDAS, ver test_exit_execution_quality.py
(decision explicita del usuario: una salida nunca se bloquea, solo se
alerta via WARNING).
"""
from __future__ import annotations

from datetime import date

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.strategy.weekly_asymmetric import EntrySignal
from run_bot import GgalOptionsBot


def _make_quote(symbol: str, bid: float, ask: float, bid_size: float = 50.0, ask_size: float = 50.0) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)
    quote = OptionQuote(
        symbol=symbol, strike=6800.0, expiry=date(2026, 10, 16),
        option_type=OptionType.CALL, book=book, days_calendar=8, days_business=6,
    )
    mid = (bid + ask) / 2.0
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": mid}
    quote.iv = 0.55
    return quote


def _make_signal(symbol: str) -> EntrySignal:
    return EntrySignal(
        symbol=symbol, option_type=OptionType.CALL,
        reason="test_entry_spread_gate", premium_reference=25.0,
        iv_dislocation_vol_points=5.0, convexity_score=0.01,
    )


def test_entry_blocked_when_spread_is_as_wide_as_the_real_gfgc6800oc_incident():
    """
    Reproduce el spread real del incidente (bid=15.0/ask=35.0, spread
    relativo=80%, vs. el limite default de 5%) como si fuera una ENTRADA en
    vez de una salida - con el gate activo (default), debe bloquearse y no
    abrir ninguna posicion.
    """
    original_enabled = SETTINGS.shadow.enabled
    original_gate = SETTINGS.risk.enforce_entry_spread_gate
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_entry_spread_gate = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC6800OC", bid=15.0, ask=35.0))
        signal = _make_signal("GFGC6800OC")

        bot._act_on_entry_signal(signal, spot=6800.0)

        assert bot._position_quantity("GFGC6800OC") == 0.0, (
            "La entrada deberia haber sido bloqueada por spread no operable (80% > 5%)."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_entry_spread_gate = original_gate


def test_entry_still_allowed_when_spread_is_tight():
    """
    Control: una punta con spread normal (bid=570/ask=575, ~0.87% relativo,
    muy por debajo del limite de 5%) no debe verse afectada por este gate -
    la entrada debe proceder igual que antes de esta mejora.
    """
    original_enabled = SETTINGS.shadow.enabled
    original_gate = SETTINGS.risk.enforce_entry_spread_gate
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_entry_spread_gate = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", bid=570.0, ask=575.0))
        signal = _make_signal("GFGC7000OC")

        bot._act_on_entry_signal(signal, spot=7050.0)

        assert bot._position_quantity("GFGC7000OC") > 0.0, (
            "Una punta con spread normal no deberia bloquearse por este gate."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_entry_spread_gate = original_gate


def test_entry_spread_gate_can_be_disabled_via_setting():
    """
    Con RiskConfig.enforce_entry_spread_gate=False (GGAL_BOT_ENFORCE_ENTRY_
    SPREAD_GATE=false), el mismo spread de 80% del test de arriba ya NO debe
    bloquear la entrada - comportamiento previo a esta mejora, disponible
    para desactivar explicitamente (ej. debugging local).
    """
    original_enabled = SETTINGS.shadow.enabled
    original_gate = SETTINGS.risk.enforce_entry_spread_gate
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.enforce_entry_spread_gate = False
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC6800OC", bid=15.0, ask=35.0))
        signal = _make_signal("GFGC6800OC")

        bot._act_on_entry_signal(signal, spot=6800.0)

        assert bot._position_quantity("GFGC6800OC") > 0.0, (
            "Con el gate desactivado explicitamente, la entrada deberia proceder igual que antes de esta mejora."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.enforce_entry_spread_gate = original_gate


ALL_TESTS = [
    test_entry_blocked_when_spread_is_as_wide_as_the_real_gfgc6800oc_incident,
    test_entry_still_allowed_when_spread_is_tight,
    test_entry_spread_gate_can_be_disabled_via_setting,
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
