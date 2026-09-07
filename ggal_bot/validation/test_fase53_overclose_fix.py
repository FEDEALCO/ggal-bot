"""
test_fase53_overclose_fix.py
=============================
Test de regresion (Fase 5.3, COMMIT 1) para el bug de "over-close"
documentado en AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md SS1 y corregido en
run_bot.py::_act_on_exit_signal().

Bug original: cuando existen VARIOS objetos Position fragmentados para el
mismo symbol+strategy_tag (ver weekly_asymmetric.py:build_exit_signals, que
emite un ExitSignal POR CADA Position en vez de uno agregado por simbolo),
el codigo de _act_on_exit_signal() aplicaba la MISMA operacion (vaciar a 0,
o descontar signal.quantity) a TODOS los lotes que matcheaban symbol+tag,
en vez de limitar la reduccion a los contratos realmente vendidos en ESE
fill puntual. Con 3 lotes de 3 contratos cada uno (9 en total), una sola
señal de salida por 3 contratos terminaba vaciando los 3 lotes completos
(9 contratos) en la rama de cierre total, o sobre-reduciendo cada lote en
la rama parcial.

Este test reproduce exactamente ese escenario contra el CODIGO REAL
(GgalOptionsBot._act_on_exit_signal, sin reimplementar nada) y verifica que
la version corregida:
  (a) en un cierre TOTAL (reason != "partial_profit_take"): consume
      exactamente signal.quantity contratos en orden FIFO (lote mas viejo
      primero), dejando el resto de los lotes intactos.
  (b) en un cierre PARCIAL (reason == "partial_profit_take"): igual,
      consume exactamente signal.quantity en orden FIFO y marca
      partial_profit_taken=True SOLO en los lotes efectivamente tocados.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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


def _make_three_fragmented_lots(bot: GgalOptionsBot) -> None:
    """3 lotes de 3 contratos cada uno para GFGC7000OC, mismo strategy_tag,
    entry_time escalonado (para verificar el orden FIFO explícitamente)."""
    base = datetime(2026, 9, 1, 20, 14, tzinfo=timezone.utc)
    for i in range(3):
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=3.0, multiplier=100.0,
            entry_price=572.5, entry_time=base + timedelta(days=i),
            strategy_tag="weekly_asymmetric",
        ))


def test_full_close_only_consumes_signal_quantity_fifo():
    """
    3 lotes fragmentados de 3 contratos (9 total). Una señal de cierre
    TOTAL (reason distinto de partial_profit_take) por 3 contratos NO debe
    vaciar los 9 contratos - debe dejar 6 (2 lotes intactos) y consumir
    exactamente el lote mas viejo primero (FIFO).
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_gfgc7000oc_quote())
        _make_three_fragmented_lots(bot)

        total_before = bot._position_quantity("GFGC7000OC")
        assert total_before == 9.0, f"Fixture invalido: esperaba 9 antes del cierre, hay {total_before}."

        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="stop_loss", quantity=3.0)
        bot._act_on_exit_signal(signal, spot=7040.0)

        total_after = bot._position_quantity("GFGC7000OC")
        qtys = sorted(
            p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC7000OC"
        )
        print(f"RESULT full-close: total_after={total_after} per_lot={qtys}")

        assert total_after == 6.0, (
            f"BUG DE OVER-CLOSE REPRODUCIDO: una señal de cierre por 3 contratos "
            f"dejo {total_after} contratos totales (esperado 6.0, es decir, solo "
            f"el lote mas viejo de 3 debia vaciarse)."
        )
        assert qtys == [0.0, 3.0, 3.0], (
            f"El lote consumido no fue exactamente el mas viejo (orden FIFO roto): {qtys}"
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_partial_close_marks_only_touched_lots():
    """
    Misma fragmentacion (3 lotes de 3). Una señal PARCIAL
    (reason="partial_profit_take") por 3 contratos debe reducir solo el
    lote mas viejo a 0 (o al remanente correspondiente) y marcar
    partial_profit_taken=True UNICAMENTE en el/los lotes efectivamente
    tocados, no en los 3.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_gfgc7000oc_quote())
        _make_three_fragmented_lots(bot)

        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="partial_profit_take", quantity=3.0)
        bot._act_on_exit_signal(signal, spot=7040.0)

        positions = [p for p in bot.portfolio.positions if p.symbol == "GFGC7000OC"]
        positions.sort(key=lambda p: p.entry_time)
        qtys = [p.quantity for p in positions]
        taken_flags = [p.partial_profit_taken for p in positions]
        print(f"RESULT partial-close: per_lot={qtys} partial_profit_taken={taken_flags}")

        assert qtys == [0.0, 3.0, 3.0], (
            f"La reduccion parcial no respeto FIFO/cantidad exacta: {qtys}"
        )
        assert taken_flags == [True, False, False], (
            f"BUG REPRODUCIDO: partial_profit_taken se marco en lotes que la señal "
            f"no toco: {taken_flags} (esperado solo el lote mas viejo)."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_full_close_requesting_more_than_available_does_not_crash():
    """
    Caso limite: 3 lotes de 3 (9 total), señal de cierre TOTAL pide
    "cerrar" mas de lo que la reduccion FIFO por signal.quantity alcanzaria
    a cubrir en un solo lote (12 > 9 disponibles en total, o incluso
    simplemente > el lote mas viejo). No debe crashear ni fabricar
    cantidad negativa; en la rama de cierre total, el remanente_a_reducir
    puede exceder lo disponible solo si signal.quantity > suma total.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_gfgc7000oc_quote())
        _make_three_fragmented_lots(bot)

        signal = _FakeExitSignal(symbol="GFGC7000OC", reason="stop_loss", quantity=99.0)
        bot._act_on_exit_signal(signal, spot=7040.0)

        total_after = bot._position_quantity("GFGC7000OC")
        assert total_after == 0.0, f"Esperaba que los 9 contratos disponibles se vaciaran, quedo {total_after}."
        for p in bot.portfolio.positions:
            assert p.quantity >= 0.0, f"Cantidad negativa detectada: {p.quantity}"
    finally:
        SETTINGS.shadow.enabled = original_enabled


if __name__ == "__main__":
    test_full_close_only_consumes_signal_quantity_fifo()
    test_partial_close_marks_only_touched_lots()
    test_full_close_requesting_more_than_available_does_not_crash()
    print("PASS: fix de over-close verificado (FIFO, cierre total, parcial, y caso limite).")
