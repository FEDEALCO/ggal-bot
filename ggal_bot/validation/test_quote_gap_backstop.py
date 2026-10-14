"""
test_quote_gap_backstop.py
=============================
Tests para el respaldo de ultimo resorte del filtro de quote operable
(MEJORA 2026-10-13, Fase 2 del mandato de research cuantitativo, a pedido
explicito del usuario - item 1: "si no hay quote válido en N minutos o la
pérdida supera un umbral catastrófico, salir con orden límite a un precio
protegido (mid − margen, nunca a mercado)"). Cubre las tres piezas nuevas:

  1. risk/risk_manager.py::RiskManager.evaluate_quote_gap_backstop (pura,
     sin I/O).
  2. execution/mid_price_exec.py::MidPriceExecutionEngine.
     submit_protected_limit (arma la orden LIMITE protegida en si).
  3. run_bot.py::GgalOptionsBot._apply_quote_gap_backstop (integracion
     completa: tracking + decision + ejecucion + journal).

Correr con:
    python -m ggal_bot.validation.test_quote_gap_backstop
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway - ver
# docstring de ese modulo / test_stale_quote_alert.py.
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.execution.order_gateway import OrderGateway, OrderSide
from ggal_bot.execution.mid_price_exec import MidPriceExecutionEngine
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from run_bot import GgalOptionsBot

# ---------------------------------------------------------------------------
# 1) RiskManager.evaluate_quote_gap_backstop (pura)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def _rm() -> RiskManager:
    return RiskManager(RiskLimits())


def test_backstop_none_when_no_data_and_recent_gap():
    rm = _rm()
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=None, quote_invalid_since=_NOW - timedelta(minutes=2),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason is None


def test_backstop_fires_on_catastrophic_loss_regardless_of_elapsed_time():
    rm = _rm()
    # Gap recien empezo (1 minuto) pero la perdida ya es catastrofica -
    # no espera los N minutos.
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=-0.80, quote_invalid_since=_NOW - timedelta(minutes=1),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason == "catastrophic_loss_backstop"


def test_backstop_does_not_fire_just_under_catastrophic_threshold():
    rm = _rm()
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=-0.749, quote_invalid_since=_NOW - timedelta(minutes=1),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason is None


def test_backstop_ignores_gains_for_catastrophic_check():
    """Una ganancia grande (PnL% positivo) nunca debe disparar el gatillo catastrofico."""
    rm = _rm()
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=0.90, quote_invalid_since=None,
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason is None


def test_backstop_fires_on_time_gap_even_without_pnl_data():
    rm = _rm()
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=None, quote_invalid_since=_NOW - timedelta(minutes=10),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason == "quote_gap_backstop"


def test_backstop_time_gap_threshold_is_inclusive_not_yet_at_boundary():
    rm = _rm()
    reason_before = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=-0.10, quote_invalid_since=_NOW - timedelta(minutes=9, seconds=59),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason_before is None
    reason_at = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=-0.10, quote_invalid_since=_NOW - timedelta(minutes=10),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason_at == "quote_gap_backstop"


def test_backstop_catastrophic_check_takes_priority_when_both_fire():
    rm = _rm()
    reason = rm.evaluate_quote_gap_backstop(
        last_known_pnl_pct=-0.90, quote_invalid_since=_NOW - timedelta(minutes=20),
        now=_NOW, backstop_minutes=10.0, catastrophic_loss_pct=0.75,
    )
    assert reason == "catastrophic_loss_backstop"


# ---------------------------------------------------------------------------
# 2) MidPriceExecutionEngine.submit_protected_limit
# ---------------------------------------------------------------------------

def test_submit_protected_limit_sell_is_below_mid_by_margin():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    book = OrderBookSnapshot("GFGC6600OC", bid=90.0, ask=150.0, bid_size=5, ask_size=5)  # book ancho/invalido
    state = engine.submit_protected_limit(
        symbol="GFGC6600OC", book=book, side=OrderSide.SELL, quantity=8,
        spot_reference=6000.0, margin_pct=0.05,
    )
    expected_price = book.mid * 0.95
    assert abs(state.request.price - expected_price) < 1e-6
    from ggal_bot.execution.order_gateway import OrderTypeEnum
    assert state.request.order_type is OrderTypeEnum.LIMIT  # NUNCA de mercado
    assert engine.open_order_count() == 1


def test_submit_protected_limit_buy_is_above_mid_by_margin():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    book = OrderBookSnapshot("GFGV5000OC", bid=50.0, ask=70.0, bid_size=5, ask_size=5)
    state = engine.submit_protected_limit(
        symbol="GFGV5000OC", book=book, side=OrderSide.BUY, quantity=3,
        spot_reference=6000.0, margin_pct=0.05,
    )
    expected_price = book.mid * 1.05
    assert abs(state.request.price - expected_price) < 1e-6


# ---------------------------------------------------------------------------
# 3) GgalOptionsBot._apply_quote_gap_backstop (integracion)
# ---------------------------------------------------------------------------

def _make_bot() -> GgalOptionsBot:
    """
    NOTA (a diferencia de la version de test_stale_quote_alert.py que esta
    imita): aca NO se restaura SETTINGS.shadow.enabled antes de retornar -
    ese archivo solo testea el tracker/alerta (nunca llega a enviar una
    orden real), pero estos tests SI ejercitan submit_protected_limit() de
    punta a punta, y OrderGateway.send() lee SETTINGS.shadow.enabled en el
    momento del envio (no en el momento de construir el bot) - ver su
    docstring. Cada test que llame a esta funcion es responsable de
    restaurar el flag el mismo (ver _with_shadow_enabled abajo).
    """
    SETTINGS.shadow.enabled = True
    return GgalOptionsBot()


class _with_shadow_enabled:
    """Mantiene SETTINGS.shadow.enabled=True durante TODO el cuerpo del test (no solo la
    construccion del bot) - necesario para que los fills simulados de OrderGateway.send()
    se resuelvan en FILLED en vez de caer al camino real (pyRofex no instalado -> NEW/REJECTED)."""

    def __enter__(self):
        self.orig = SETTINGS.shadow.enabled
        SETTINGS.shadow.enabled = True
        return self

    def __exit__(self, *exc):
        SETTINGS.shadow.enabled = self.orig


def _upsert_quote(bot, symbol, bid, ask, expiry=date(2026, 12, 18)):
    book = OrderBookSnapshot(symbol, bid=bid, ask=ask, bid_size=50, ask_size=50)
    q = OptionQuote(symbol, strike=8000.0, expiry=expiry, option_type=OptionType.CALL,
                     book=book, days_calendar=90, days_business=60)
    bot.option_chain.upsert_quote(q)


def _add_long_position(bot, symbol="GFGC8000OC", quantity=5, entry_price=100.0):
    bot.portfolio.add(Position(
        symbol=symbol, quantity=quantity, multiplier=100.0, entry_price=entry_price,
        entry_time=datetime.now(timezone.utc), expiry=date(2026, 12, 18), strategy_tag="weekly_asymmetric",
    ))


def _with_backstop_settings(enabled=True, minutes=10.0, catastrophic_pct=0.75, margin_pct=0.05):
    class _Ctx:
        def __enter__(self):
            self.orig = (
                SETTINGS.risk.enable_quote_gap_backstop,
                SETTINGS.risk.quote_gap_backstop_minutes,
                SETTINGS.risk.quote_gap_backstop_catastrophic_loss_pct,
                SETTINGS.risk.quote_gap_backstop_limit_margin_pct,
            )
            SETTINGS.risk.enable_quote_gap_backstop = enabled
            SETTINGS.risk.quote_gap_backstop_minutes = minutes
            SETTINGS.risk.quote_gap_backstop_catastrophic_loss_pct = catastrophic_pct
            SETTINGS.risk.quote_gap_backstop_limit_margin_pct = margin_pct
            return self

        def __exit__(self, *exc):
            (
                SETTINGS.risk.enable_quote_gap_backstop,
                SETTINGS.risk.quote_gap_backstop_minutes,
                SETTINGS.risk.quote_gap_backstop_catastrophic_loss_pct,
                SETTINGS.risk.quote_gap_backstop_limit_margin_pct,
            ) = self.orig

    return _Ctx()


def test_disabled_flag_is_a_complete_noop():
    with _with_backstop_settings(enabled=False), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot)
        _upsert_quote(bot, "GFGC8000OC", bid=0.0, ask=0.0)  # book muerto, catastrofico si estuviera activo
        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        assert bot._position_invalid_quote_since == {}
        assert bot.portfolio.positions[0].quantity == 5  # nunca se tocó


def test_tracks_invalid_quote_since_on_first_miss_without_acting_yet():
    with _with_backstop_settings(minutes=10.0), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        _upsert_quote(bot, "GFGC8000OC", bid=98.0, ask=102.0)  # spread 4%, valido - pasa el filtro

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        assert "GFGC8000OC" not in bot._position_invalid_quote_since

        # Ahora el book se pone invalido (spread 80%, 16x el limite de 5%, caso real GFGC6800OC)
        _upsert_quote(bot, "GFGC8000OC", bid=50.0, ask=150.0)
        bot._apply_quote_gap_backstop(spot=6000.0, now=2_000.0)
        assert bot._position_invalid_quote_since.get("GFGC8000OC") == 2_000.0
        assert bot.portfolio.positions[0].quantity == 5  # todavia no se cumplieron los N minutos


def test_catastrophic_loss_forces_close_immediately_even_before_n_minutes():
    with _with_backstop_settings(minutes=10.0, catastrophic_pct=0.75), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        # Book invalido (spread muy ancho) pero con un mid que implica -80% de perdida sobre la prima.
        _upsert_quote(bot, "GFGC8000OC", bid=10.0, ask=30.0)  # mid=20 -> pnl=(20-100)/100=-80%

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)  # recien el primer miss, 0 minutos transcurridos

        assert bot.portfolio.positions[0].quantity == 0  # se forzo el cierre
        assert "GFGC8000OC" not in bot._position_invalid_quote_since  # tracking limpiado tras el cierre


def test_time_gap_forces_close_after_n_minutes_without_catastrophic_loss():
    with _with_backstop_settings(minutes=10.0, catastrophic_pct=0.75), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        # Book invalido (spread ancho) pero con una perdida leve, lejos del umbral catastrofico.
        _upsert_quote(bot, "GFGC8000OC", bid=90.0, ask=130.0)  # mid=110 -> pnl=+10% (ganancia, no deberia importar)

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        assert bot.portfolio.positions[0].quantity == 5  # todavia no pasaron los N minutos

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0 + 600.0)  # 10 minutos exactos
        assert bot.portfolio.positions[0].quantity == 0  # se forzo el cierre por el gap de tiempo


def test_no_book_at_all_logs_but_never_crashes_and_never_fabricates_a_price():
    with _with_backstop_settings(minutes=10.0, catastrophic_pct=0.75), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        # Ninguna cotizacion en absoluto para esta base (ni siquiera en la cadena) - no se puede
        # armar un limite protegido sin un precio real.

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0 + 600.0)

        assert bot.portfolio.positions[0].quantity == 5  # nunca se cierra sin un precio real
        assert "GFGC8000OC" in bot._position_invalid_quote_since  # pero el gap sigue trackeado


def test_short_position_loses_when_price_rises_not_falls():
    """Una posicion CORTA (quantity<0, ej. el wing corto de weekly_asymmetric) pierde cuando el
    precio SUBE - al reves de una larga. Verifica la orientacion por signo documentada en
    _apply_quote_gap_backstop."""
    with _with_backstop_settings(minutes=10.0, catastrophic_pct=0.75), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, symbol="GFGV5000OC", quantity=-8, entry_price=10.0)
        # mid=28 -> para una posicion CORTA vendida a 10, esto es (28-10)/10=+180% crudo,
        # orientado para CORTA: -180% (perdida grande) -> debe disparar el gatillo catastrofico.
        _upsert_quote(bot, "GFGV5000OC", bid=20.0, ask=36.0)

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)

        assert bot.portfolio.positions[0].quantity == 0  # se recompro (buy to close) para cerrar la corta


def test_recovering_valid_quote_resets_the_backstop_tracking():
    with _with_backstop_settings(minutes=10.0), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        _upsert_quote(bot, "GFGC8000OC", bid=50.0, ask=150.0)  # invalido

        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        assert "GFGC8000OC" in bot._position_invalid_quote_since

        _upsert_quote(bot, "GFGC8000OC", bid=98.0, ask=102.0)  # vuelve a ser valido (spread 4%)
        bot._apply_quote_gap_backstop(spot=6000.0, now=1_300.0)

        assert "GFGC8000OC" not in bot._position_invalid_quote_since
        assert bot.portfolio.positions[0].quantity == 5  # nunca se llego a cerrar


def test_closed_position_is_purged_from_tracking():
    with _with_backstop_settings(minutes=10.0), _with_shadow_enabled():
        bot = _make_bot()
        _add_long_position(bot, entry_price=100.0)
        _upsert_quote(bot, "GFGC8000OC", bid=50.0, ask=150.0)
        bot._apply_quote_gap_backstop(spot=6000.0, now=1_000.0)
        assert "GFGC8000OC" in bot._position_invalid_quote_since

        bot.portfolio.positions[0].quantity = 0  # se cerro por otra via (ej. stop normal)
        bot._apply_quote_gap_backstop(spot=6000.0, now=1_300.0)

        assert "GFGC8000OC" not in bot._position_invalid_quote_since


ALL_TESTS = [
    test_backstop_none_when_no_data_and_recent_gap,
    test_backstop_fires_on_catastrophic_loss_regardless_of_elapsed_time,
    test_backstop_does_not_fire_just_under_catastrophic_threshold,
    test_backstop_ignores_gains_for_catastrophic_check,
    test_backstop_fires_on_time_gap_even_without_pnl_data,
    test_backstop_time_gap_threshold_is_inclusive_not_yet_at_boundary,
    test_backstop_catastrophic_check_takes_priority_when_both_fire,
    test_submit_protected_limit_sell_is_below_mid_by_margin,
    test_submit_protected_limit_buy_is_above_mid_by_margin,
    test_disabled_flag_is_a_complete_noop,
    test_tracks_invalid_quote_since_on_first_miss_without_acting_yet,
    test_catastrophic_loss_forces_close_immediately_even_before_n_minutes,
    test_time_gap_forces_close_after_n_minutes_without_catastrophic_loss,
    test_no_book_at_all_logs_but_never_crashes_and_never_fabricates_a_price,
    test_short_position_loses_when_price_rises_not_falls,
    test_recovering_valid_quote_resets_the_backstop_tracking,
    test_closed_position_is_purged_from_tracking,
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
