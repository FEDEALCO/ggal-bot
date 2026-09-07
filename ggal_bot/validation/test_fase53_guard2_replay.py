"""
test_fase53_guard2_replay.py
=============================
Replay forense y aislado (Fase 5.3, resolucion de la contradiccion de
Guarda 2 encontrada en Fase 5.2/5.2B): reproduce, contra el CODIGO REAL
de run_bot.py (no una reimplementacion), la secuencia exacta observada en
logs/shadow_trades.csv de produccion para GFGC7000OC:

    01/09 20:14  BUY 3
    03/09 15:20  BUY 3
    03/09 15:22  BUY 3

sin ninguna venta entre medio, para determinar si `_act_on_entry_signal()`
bloquea la 2da y 3ra entrada (como el codigo leido en Fase 5.2 sugiere que
deberia) o si el bug es reproducible en el codigo tal cual esta HOY.

Resultado esperado de este test, segun Fase 5.2B: la Guarda 2
(`_position_quantity(symbol) != 0`) deberia BLOQUEAR las llamadas 2 y 3 -
si el test efectivamente lo confirma, el bug observado en produccion NO es
reproducible con el codigo actual en un proceso continuo, lo que fortalece
la hipotesis de un reinicio de proceso sin persistencia de estado (Fase
5.2B SS13) como explicacion. Si el test FALLA (permite las 3 entradas),
hemos encontrado y reproducido un bug real en el codigo actual.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.strategy.weekly_asymmetric import EntrySignal
from run_bot import GgalOptionsBot


def _make_gfgc7000oc_quote() -> OptionQuote:
    book = OrderBookSnapshot(
        "GFGC7000OC", bid=570.0, ask=575.0, bid_size=50, ask_size=50,
    )
    quote = OptionQuote(
        symbol="GFGC7000OC", strike=7000.0, expiry=date(2026, 9, 18),
        option_type=OptionType.CALL, book=book, days_calendar=17, days_business=12,
    )
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": 572.5}
    quote.iv = 0.55
    return quote


def test_replay_gfgc7000oc_three_buys_no_sell_between():
    """
    Reproduce, llamada por llamada, EXACTAMENTE la secuencia de fills real
    de produccion (Fase 5.1/5.2): 3 BUY consecutivos sobre GFGC7000OC, sin
    ningun SELL entre medio, contra el codigo actual de
    run_bot.py::_act_on_entry_signal().
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_gfgc7000oc_quote())

        signal = EntrySignal(
            symbol="GFGC7000OC", option_type=OptionType.CALL,
            reason="test_replay_fase53", premium_reference=572.5,
            iv_dislocation_vol_points=5.0, convexity_score=0.01,
        )

        # --- Evento 1: 01/09 20:14 BUY 3 -----------------------------------
        qty_before_1 = bot._position_quantity("GFGC7000OC")
        bot._act_on_entry_signal(signal, spot=7050.0)
        qty_after_1 = bot._position_quantity("GFGC7000OC")
        print(f"EVENT 1: before={qty_before_1} after={qty_after_1} n_positions={len(bot.portfolio.positions)}")

        # --- Evento 2: 03/09 15:20 BUY 3 (sin venta entre medio) -----------
        qty_before_2 = bot._position_quantity("GFGC7000OC")
        bot._act_on_entry_signal(signal, spot=7040.0)
        qty_after_2 = bot._position_quantity("GFGC7000OC")
        print(f"EVENT 2: before={qty_before_2} after={qty_after_2} n_positions={len(bot.portfolio.positions)}")

        # --- Evento 3: 03/09 15:22 BUY 3 (sin venta entre medio) -----------
        qty_before_3 = bot._position_quantity("GFGC7000OC")
        bot._act_on_entry_signal(signal, spot=7040.0)
        qty_after_3 = bot._position_quantity("GFGC7000OC")
        print(f"EVENT 3: before={qty_before_3} after={qty_after_3} n_positions={len(bot.portfolio.positions)}")

        print(f"RESULT: quantities={[p.quantity for p in bot.portfolio.positions if p.symbol=='GFGC7000OC']}")

        assert qty_after_1 > 0, "La primera entrada deberia abrir posicion (sizing/liquidez ok en el fixture)."
        assert qty_after_2 == qty_after_1, (
            f"BUG REPRODUCIDO: Guarda 2 NO bloqueo la 2da entrada "
            f"(qty subio de {qty_after_1} a {qty_after_2})."
        )
        assert qty_after_3 == qty_after_1, (
            f"BUG REPRODUCIDO: Guarda 2 NO bloqueo la 3ra entrada "
            f"(qty subio de {qty_after_2} a {qty_after_3})."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled


if __name__ == "__main__":
    test_replay_gfgc7000oc_three_buys_no_sell_between()
    print("PASS: Guarda 2 bloqueo correctamente las entradas repetidas en un proceso continuo.")
